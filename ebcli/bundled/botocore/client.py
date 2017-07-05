# Copyright 2014 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You
# may not use this file except in compliance with the License. A copy of
# the License is located at
#
# http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific
# language governing permissions and limitations under the License.
import copy
import functools
import logging

import botocore.serialize
import botocore.validate
from botocore import credentials, waiter, xform_name
from botocore.endpoint import EndpointCreator
from botocore.exceptions import ClientError, DataNotFoundError
from botocore.exceptions import OperationNotPageableError
from botocore.model import ServiceModel
from botocore.paginate import Paginator
from botocore.signers import RequestSigner
from botocore.utils import CachedProperty


logger = logging.getLogger(__name__)


class ClientCreator(object):
    """Creates client objects for a service."""
    def __init__(self, loader, endpoint_resolver, user_agent, event_emitter,
                 retry_handler_factory, retry_config_translator,
                 response_parser_factory=None):
        self._loader = loader
        self._endpoint_resolver = endpoint_resolver
        self._user_agent = user_agent
        self._event_emitter = event_emitter
        self._retry_handler_factory = retry_handler_factory
        self._retry_config_translator = retry_config_translator
        self._response_parser_factory = response_parser_factory

    def create_client(self, service_name, region_name, is_secure=True,
                      endpoint_url=None, verify=None,
                      credentials=None, scoped_config=None,
                      client_config=None):
        service_model = self._load_service_model(service_name)
        cls = self.create_client_class(service_name)
        client_args = self._get_client_args(
            service_model, region_name, is_secure, endpoint_url,
            verify, credentials, scoped_config, client_config)
        return cls(**client_args)

    def create_client_class(self, service_name):
        service_model = self._load_service_model(service_name)
        methods = self._create_methods(service_model)
        py_name_to_operation_name = self._create_name_mapping(service_model)
        self._add_pagination_methods(service_model, methods,
                                     py_name_to_operation_name)
        self._add_waiter_methods(service_model, methods,
                                 py_name_to_operation_name)
        cls = type(service_name, (BaseClient,), methods)
        return cls

    def _add_pagination_methods(self, service_model, methods, name_mapping):
        loader = self._loader

        def get_paginator(self, operation_name):
            """Create a paginator for an operation.

            :type operation_name: string
            :param operation_name: The operation name.  This is the same name
                as the method name on the client.  For example, if the
                method name is ``create_foo``, and you'd normally invoke the
                operation as ``client.create_foo(**kwargs)``, if the
                ``create_foo`` operation can be paginated, you can use the
                call ``client.get_paginator("create_foo")``.

            :raise OperationNotPageableError: Raised if the operation is not
                pageable.  You can use the ``client.can_paginate`` method to
                check if an operation is pageable.

            :rtype: L{botocore.paginate.Paginator}
            :return: A paginator object.

            """
            # Note that the 'self' in this method refers to the self on
            # BaseClient, not on ClientCreator.
            if not self.can_paginate(operation_name):
                raise OperationNotPageableError(operation_name=operation_name)
            else:
                actual_operation_name = name_mapping[operation_name]
                paginator = Paginator(
                    getattr(self, operation_name),
                    self._cache['page_config'][actual_operation_name])
                return paginator

        def can_paginate(self, operation_name):
            """Check if an operation can be paginated.

            :type operation_name: string
            :param operation_name: The operation name.  This is the same name
                as the method name on the client.  For example, if the
                method name is ``create_foo``, and you'd normally invoke the
                operation as ``client.create_foo(**kwargs)``, if the
                ``create_foo`` operation can be paginated, you can use the
                call ``client.get_paginator("create_foo")``.

            :return: ``True`` if the operation can be paginated,
                ``False`` otherwise.

            """
            if 'page_config' not in self._cache:
                try:
                    page_config = loader.load_data('aws/%s/%s.paginators' % (
                        service_model.service_name,
                        service_model.api_version))['pagination']
                    self._cache['page_config'] = page_config
                except DataNotFoundError:
                    self._cache['page_config'] = {}
            actual_operation_name = name_mapping[operation_name]
            return actual_operation_name in self._cache['page_config']

        methods['get_paginator'] = get_paginator
        methods['can_paginate'] = can_paginate

    def _add_waiter_methods(self, service_model, methods_dict,
                            method_name_map):

        loader = self._loader

        def _get_waiter_config(self):
            if 'waiter_config' not in self._cache:
                try:
                    waiter_config = loader.load_data('aws/%s/%s.waiters' % (
                        service_model.service_name,
                        service_model.api_version))
                    self._cache['waiter_config'] = waiter_config
                except DataNotFoundError:
                    self._cache['waiter_config'] = {}
            return self._cache['waiter_config']

        def get_waiter(self, waiter_name):
            config = self._get_waiter_config()
            if not config:
                raise ValueError("Waiter does not exist: %s" % waiter_name)
            model = waiter.WaiterModel(config)
            mapping = {}
            for name in model.waiter_names:
                mapping[xform_name(name)] = name
            if waiter_name not in mapping:
                raise ValueError("Waiter does not exist: %s" % waiter_name)

            return waiter.create_waiter_with_client(
                mapping[waiter_name], model, self)

        @CachedProperty
        def waiter_names(self):
            """Returns a list of all available waiters."""
            config = self._get_waiter_config()
            if not config:
                return[]
            model = waiter.WaiterModel(config)
            # Waiter configs is a dict, we just want the waiter names
            # which are the keys in the dict.
            return [xform_name(name) for name in model.waiter_names]

        methods_dict['_get_waiter_config'] = _get_waiter_config
        methods_dict['get_waiter'] = get_waiter
        methods_dict['waiter_names'] = waiter_names

    def _load_service_model(self, service_name):
        json_model = self._loader.load_service_model('aws/%s' % service_name)
        service_model = ServiceModel(json_model, service_name=service_name)
        self._register_retries(service_model)
        return service_model

    def _register_retries(self, service_model):
        endpoint_prefix = service_model.endpoint_prefix

        # First, we load the entire retry config for all services,
        # then pull out just the information we need.
        original_config = self._loader.load_data('aws/_retry')
        if not original_config:
            return

        retry_config = self._retry_config_translator.build_retry_config(
            endpoint_prefix, original_config.get('retry', {}),
            original_config.get('definitions', {}))

        logger.debug("Registering retry handlers for service: %s",
                     service_model.service_name)
        handler = self._retry_handler_factory.create_retry_handler(
            retry_config, endpoint_prefix)
        unique_id = 'retry-config-%s' % endpoint_prefix
        self._event_emitter.register('needs-retry.%s' % endpoint_prefix,
                                     handler, unique_id=unique_id)


    def _get_signature_version_and_region(self, service_model, region_name,
                                          is_secure, scoped_config):
        # Get endpoint heuristic overrides before creating the
        # request signer.
        resolver = self._endpoint_resolver
        scheme = 'https' if is_secure else 'http'
        endpoint_config = resolver.construct_endpoint(
                service_model.endpoint_prefix,
                region_name, scheme=scheme)

        # Signature version override from endpoint
        signature_version = service_model.signature_version
        if 'signatureVersion' in endpoint_config.get('properties', {}):
            signature_version = endpoint_config['properties']\
                                               ['signatureVersion']

        # Signature overrides from a configuration file
        if scoped_config is not None:
            service_config = scoped_config.get(service_model.endpoint_prefix)
            if service_config is not None and isinstance(service_config, dict):
                override = service_config.get('signature_version')
                if override:
                    logger.debug(
                        "Switching signature version for service %s "
                         "to version %s based on config file override.",
                         service_model.endpoint_prefix, override)
                    signature_version = override

        return signature_version, region_name

    def _get_client_args(self, service_model, region_name, is_secure,
                         endpoint_url, verify, credentials,
                         scoped_config, client_config):
        # A client needs:
        #
        # * serializer
        # * endpoint
        # * response parser
        # * request signer
        protocol = service_model.metadata['protocol']
        serializer = botocore.serialize.create_serializer(
            protocol, include_validation=True)
        event_emitter = copy.copy(self._event_emitter)
        endpoint_creator = EndpointCreator(self._endpoint_resolver, region_name,
                                           event_emitter, self._user_agent)
        endpoint = endpoint_creator.create_endpoint(
            service_model, region_name, is_secure=is_secure,
            endpoint_url=endpoint_url, verify=verify,
            response_parser_factory=self._response_parser_factory)
        response_parser = botocore.parsers.create_parser(protocol)

        # This is only temporary in the sense that we should remove any
        # region_name logic from endpoints and put it into clients.
        # But that can only happen once operation objects are deprecated.
        region_name = endpoint.region_name
        signature_version, region_name = \
            self._get_signature_version_and_region(
                service_model, region_name, is_secure, scoped_config)

        if client_config and client_config.signature_version is not None:
            signature_version = client_config.signature_version

        signer = RequestSigner(service_model.service_name, region_name,
                               service_model.signing_name,
                               signature_version, credentials,
                               event_emitter)
        return {
            'serializer': serializer,
            'endpoint': endpoint,
            'response_parser': response_parser,
            'event_emitter': event_emitter,
            'request_signer': signer,
        }

    def _create_methods(self, service_model):
        op_dict = {}
        for operation_name in service_model.operation_names:
            py_operation_name = xform_name(operation_name)
            op_dict[py_operation_name] = self._create_api_method(
                py_operation_name, operation_name, service_model)
        return op_dict

    def _create_name_mapping(self, service_model):
        # py_name -> OperationName, for every operation available
        # for a service.
        mapping = {}
        for operation_name in service_model.operation_names:
            py_operation_name = xform_name(operation_name)
            mapping[py_operation_name] = operation_name
        return mapping

    def _create_api_method(self, py_operation_name, operation_name,
                           service_model):
        def _api_call(self, **kwargs):
            operation_model = service_model.operation_model(operation_name)
            event_name = (
                'before-parameter-build.{endpoint_prefix}.{operation_name}')
            self.meta.events.emit(
                event_name.format(
                    endpoint_prefix=service_model.endpoint_prefix,
                    operation_name=operation_name),
                params=kwargs, model=operation_model)

            request_dict = self._serializer.serialize_to_request(
                kwargs, operation_model)

            self.meta.events.emit(
                'before-call.{endpoint_prefix}.{operation_name}'.format(
                    endpoint_prefix=service_model.endpoint_prefix,
                    operation_name=operation_name),
                model=operation_model, params=request_dict,
                request_signer=self._request_signer
            )

            http, parsed_response = self._endpoint.make_request(
                operation_model, request_dict)

            self.meta.events.emit(
                'after-call.{endpoint_prefix}.{operation_name}'.format(
                    endpoint_prefix=service_model.endpoint_prefix,
                    operation_name=operation_name),
                http_response=http, parsed=parsed_response,
                model=operation_model
            )

            if http.status_code >= 300:
                raise ClientError(parsed_response, operation_name)
            else:
                return parsed_response

        _api_call.__name__ = str(py_operation_name)
        # TODO: docstrings.
        return _api_call


class BaseClient(object):

    def __init__(self, serializer, endpoint, response_parser,
                 event_emitter, request_signer):
        self._serializer = serializer
        self._endpoint = endpoint
        self._response_parser = response_parser
        self._request_signer = request_signer
        self._cache = {}
        self.meta = ClientMeta(event_emitter)

        # Register request signing, but only if we have an event
        # emitter. When a client is cloned this is ignored, because
        # the client's ``meta`` will be copied anyway.
        if self.meta.events:
            self.meta.events.register('request-created', self._sign_request)

    def _sign_request(self, operation_name=None, request=None, **kwargs):
        # Sign the request. This fires its own events and will
        # mutate the request as needed.
        self._request_signer.sign(operation_name, request)

    def clone_client(self, serializer=None, endpoint=None,
                     response_parser=None, request_signer=None):
        """Create a copy of the client object.

        This method will create a clone of an existing client.  By default, the
        same internal attributes are used when creating a clone of the client,
        with the exception of the event emitter. A copy of the event handlers
        are created when a clone of the client is created.

        You can also provide any of the above arguments as an override.  This
        allows you to create a client that has the same values except for the
        args you pass in as overrides.

        :return: A new copy of the botocore client.

        """
        kwargs = {
            'serializer': serializer,
            'endpoint': endpoint,
            'response_parser': response_parser,
            'request_signer': request_signer,
        }
        for key, value in kwargs.items():
            if value is None:
                kwargs[key] = getattr(self, '_%s' % key)
        # This will be swapped out in the ClientMeta class.
        kwargs['event_emitter'] = None
        new_object = self.__class__(**kwargs)
        new_object.meta = copy.copy(self.meta)
        return new_object


class ClientMeta(object):
    """Holds additional client methods.

    This class holds additional information for clients.  It exists for
    two reasons:

        * To give advanced functionality to clients
        * To namespace additional client attributes from the operation
          names which are mapped to methods at runtime.  This avoids
          ever running into collisions with operation names.

    """

    def __init__(self, events):
        self.events = events

    def __copy__(self):
        copied_events = copy.copy(self.events)
        return ClientMeta(copied_events)


class Config(object):
    """Advanced configuration for Botocore clients.

    This class allows you to configure:

        * Signature version

    """
    def __init__(self, signature_version=None):
        self.signature_version = signature_version
