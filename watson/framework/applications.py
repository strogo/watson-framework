# -*- coding: utf-8 -*-
import abc
from types import ModuleType
from watson.console import Runner
from watson.console.command import find_commands_in_module
from watson.common.datastructures import dict_deep_update, module_to_dict
from watson.common.imports import get_qualified_name
from watson.di import ContainerAware
from watson.di.container import IocContainer
from watson.events.dispatcher import EventDispatcherAware
from watson.events.types import Event
from watson.http.messages import create_request_from_environ, Response
from watson.framework.exceptions import ApplicationError
from watson.framework import config as DefaultConfig, events
from watson.framework.support.console import commands as DefaultConsoleCommands


class Base(ContainerAware, EventDispatcherAware, metaclass=abc.ABCMeta):

    """The core application structure for a Watson application.

    It makes heavy use of the IocContainer and EventDispatcher classes to handle
    the wiring and executing of methods.
    The default configuration for Watson applications can be seen at watson.framework.config.

    Attributes:
        _config (dict): The configuration for the application.
        global_app (Base): A reference to the currently running application.
    """
    _config = None
    global_app = None

    @property
    def config(self):
        """Returns the configuration of the application.
        """
        return self._config

    @config.setter
    def config(self, config):
        """Sets the configuration for the application.

        Example:

        .. code-block:: python

            app = Base()
            app.config = {'some': 'settings'}

        Args:
            config (mixed): The configuration to use.
        """
        if isinstance(config, ModuleType):
            conf = module_to_dict(config, '__')
        else:
            conf = config or {}
        self._config = dict_deep_update(
            module_to_dict(DefaultConfig, '__'), conf)
        self.container.add('application.config', self.config)

    @property
    def container(self):
        """Returns the applications IocContainer.

        If no container has been created, a new container will be created
        based on the dependencies within the application configuration.
        """
        if not self._container:
            self.container = IocContainer(self.config['dependencies'])
        return self._container

    @container.setter
    def container(self, container):
        """Sets the application IocContainer.

        Adds the application to the container, which can then be accessed via
        the 'application' key.
        """
        container.add('application', self)
        self._container = container

    def __init__(self, config=None):
        """Initializes the application.

        Registers any events that are within the application configuration.

        Example:

        .. code-block:: python

            app = Base()

        Events:
            Dispatches the INIT.

        Args:
            config (mixed): See the Base.config properties.
        """
        Base.global_app = self
        self.config = config or {}
        self.register_events()
        self.trigger_init_event()
        super(Base, self).__init__()

    def trigger_init_event(self):
        """Execute any event listeners for the INIT event.
        """
        self.dispatcher.trigger(Event(events.INIT, target=self))

    def register_events(self):
        """Collect all the events from the app config and register
        them against the event dispatcher.
        """
        self.dispatcher = self.container.get('shared_event_dispatcher')
        for event, listeners in self.config['events'].items():
            for callback_priority_pair in listeners:
                try:
                    priority = callback_priority_pair.priority
                except:
                    priority = 1
                try:
                    once_only = callback_priority_pair.once_only
                except:
                    once_only = False
                self.dispatcher.add(
                    event,
                    self.container.get(callback_priority_pair[0]),
                    priority,
                    once_only)

    def __call__(self, *args, **kwargs):
        return self.run(*args, **kwargs)

    @abc.abstractmethod
    def run(self):
        raise NotImplementedError('You must implement __call__')  # pragma: no cover


class Http(Base):

    """An application structure suitable for use with the WSGI protocol.

    For more information regarding creating an application consult the documentation.

    Example:

    .. code-block:: python

        application = applications.Http({..})
        application(environ, start_response)
    """

    def run(self, environ, start_response):
        request = create_request_from_environ(environ,
                                              self.config['session']['class'],
                                              self.config['session'].get('options'))
        self.container.add('request', request)
        try:
            route_result = self.dispatcher.trigger(Event(events.ROUTE_MATCH, target=self, params={
                'request': request,
                'router': self.container.get('router')
            }))
            route_match = route_result.first()
        except ApplicationError as exc:
            route_match = None
            response, view_model = self.handle_exception(exception=exc, request=request)
        if route_match:
            try:
                dispatch_event = Event(events.DISPATCH_EXECUTE,
                                       target=self,
                                       params={'route_match': route_match,
                                               'request': request,
                                               'container': self.container
                                               }
                                       )
                dispatch_result = self.dispatcher.trigger(dispatch_event)
                response = dispatch_event.params['controller_class'].response
                view_model = dispatch_result.first()
            except ApplicationError as exc:
                response, view_model = self.handle_exception(exception=exc, request=request, route_match=route_match)
        if not isinstance(view_model, Response):
            try:
                self.render(request=request, response=response, view_model=view_model)
            except Exception as exc:
                response, view_model = self.handle_exception(exception=exc, request=request, route_match=route_match)
        self.dispatcher.trigger(Event(events.COMPLETE,
                                      target=self,
                                      params={'container': self.container}))
        return response(start_response)

    def __call__(self, *args, **kwargs):
        return self.run(*args, **kwargs)

    def handle_exception(self, last_exception=None, **kwargs):
        exception_event = Event(events.EXCEPTION, target=self, params=kwargs)
        exception_result = self.dispatcher.trigger(exception_event)
        response = Response(kwargs['exception'].status_code)
        view_model = exception_result.first()
        kwargs.update({
            'response': response,
            'view_model': view_model
        })
        if last_exception:
            self.render(with_dispatcher=False, **kwargs)
        try:
            self.render(**kwargs)
        except Exception as exc:
            kwargs['exception'] = exc
            self.handle_exception(last_exception=exc, **kwargs)
        return response, view_model

    def render(self, with_dispatcher=True, **kwargs):
        kwargs['container'] = self.container
        render_event = Event(events.RENDER_VIEW, target=self, params=kwargs)
        if with_dispatcher:
            self.container.add('render_event_params', kwargs)
            self.dispatcher.trigger(render_event)
        else:
            listener = self.container.get('app_render_listener')
            listener(render_event)


class Console(Base):

    """An application structure suitable for the command line.

    For more information regarding creating an application consult the documentation.

    Example:

    .. code-block:: python

        application = applications.Console({...})
        application()
    """
    runner = None

    def __init__(self, config=None, argv=None):
        super(Console, self).__init__(config)
        self.config = dict_deep_update({
            'commands': find_commands_in_module(DefaultConsoleCommands)
        }, self.config)
        self.runner = Runner(argv, commands=self.config.get('commands'))
        self.runner.get_command = self.get_command

    def run(self):
        return self.runner()

    def get_command(self, command_name):
        # overrides the runners get_command method
        if command_name not in self.runner.commands:
            return None
        command = self.runner.commands[command_name]
        if not isinstance(command, str):
            self.container.add(command_name, get_qualified_name(command))
        return self.container.get(command_name)
