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

from cement.utils.misc import minimal_logger

from ..lib import aws
from ..objects.exceptions import ServiceError, NotFoundError
from ..resources.strings import responses

LOG = minimal_logger(__name__)


def _make_api_call(operation_name, **operation_options):
    return aws.make_api_call('elb', operation_name, **operation_options)


def get_health_of_instances(load_balancer_name, region=None):
    try:
        result = _make_api_call('describe-instance-health',
                            load_balancer_name=load_balancer_name,
                            region=region)
    except ServiceError as e:
        if e.message.startswith(responses['loadbalancer.notfound']):
            raise NotFoundError(e)
    return result['InstanceStates']