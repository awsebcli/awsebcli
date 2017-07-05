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

from ..lib import elasticbeanstalk, aws
from ..core import io
from . import commonops


def deploy(app_name, env_name, version, label, message, staged=False,
           timeout=5):
    region_name = aws.get_default_region()

    io.log_info('Deploying code to ' + env_name + " in region " + region_name)

    if version:
        app_version_label = version
    else:
        # Create app version
        app_version_label = commonops.create_app_version(
            app_name, label=label, message=message, staged=staged)

    # swap env to new app version
    request_id = elasticbeanstalk.update_env_application_version(
        env_name, app_version_label)

    commonops.wait_for_success_events(request_id,
                                      timeout_in_minutes=timeout,
                                      can_abort=True)