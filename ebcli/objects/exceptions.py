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

import sys


class EBCLIException(Exception):
    """ Base exception for all EB-CLI exceptions """

    ''' Python 3 removes the .message property
        This causes some weird unicode issues.
        A message for a python 2 exception could be in unicode.
        Using str(e) and comparing results is problematic with unicode in
        python 2. However using the .message works just fine. So we add the
        .message back into 3 to solve compatibility problems'''
    if sys.version_info[0] >= 3:
        @property
        def message(self):
            return str(self)


class NotFoundError(EBCLIException):
    pass


class CommandError(EBCLIException):
    """ Error occurred executing some non eb cli command """
    pass


class ServiceError(EBCLIException):
    """  Error occurred calling the api    """
    def __init__(self, message=None, code=None):
        self.code = code
        super(ServiceError, self).__init__(message)


class CredentialsError(EBCLIException):
    """  Error occurred with credentials   """
    pass


class NotAuthorizedError(EBCLIException):
    pass


class NotInitializedError(EBCLIException):
    """  The eb directory can not be found.  """
    pass


class NoSourceControlError(EBCLIException):
    """  Error occured because a source control system
    can not be found in the current directory
     """
    pass


class NoEnvironmentForBranchError(EBCLIException):
    """ No provided environment for the given Branch   """
    pass


class NoRegionError(EBCLIException):
    """  No region provided or found   """
    pass


class ConnectionError(EBCLIException):
    """ Region provided cannot be resolved    """
    pass


class TimeoutError(EBCLIException):
    """ Operation timed out   """
    pass


class InvalidStateError(EBCLIException):
    """ Environment is in an updating state    """
    pass


class AlreadyExistsError(EBCLIException):
    """ The object already exists and can not be created  """
    pass


class InvalidSyntaxError(EBCLIException):
    """ The file syntax is invalid  """
    pass


class InvalidOptionsError(EBCLIException):
    """ Options given through command line are invalid  """
    pass


class NoKeypairError(EBCLIException):
    """ Instance/environment has no associated keypair    """
    pass


class NotSupportedError(EBCLIException):
    """  Operation not supported   """
    pass

class InvalidProfileError(EBCLIException):
    """ Given profile is invalid  """
    pass

class ValidationError(EBCLIException):
    pass


