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

import os
import shutil
import zipfile
import sys
import glob
import stat
import codecs
import json

from yaml import load, dump, safe_dump
from yaml.parser import ParserError
from yaml.scanner import ScannerError
try:
    import configparser
except ImportError:
    import ConfigParser as configparser

from botocore.compat import six
from six import StringIO
from cement.utils.misc import minimal_logger

from ..core import io
from ..objects.exceptions import NotInitializedError, InvalidSyntaxError, \
    NotFoundError

LOG = minimal_logger(__name__)


def get_aws_home():
    sep = os.path.sep
    p = '~' + sep + '.aws' + sep
    return os.path.expanduser(p)


def get_ssh_folder():
    sep = os.path.sep
    p = '~' + sep + '.ssh' + sep
    p = os.path.expanduser(p)
    if not os.path.exists(p):
        os.makedirs(p)
    return p


beanstalk_directory = '.elasticbeanstalk' + os.path.sep
global_config_file = beanstalk_directory + 'config.global.yml'
local_config_file = beanstalk_directory + 'config.yml'
aws_config_folder = get_aws_home()
aws_config_location = aws_config_folder + 'config'
aws_access_key = 'aws_access_key_id'
aws_secret_key = 'aws_secret_access_key'
region_key = 'region'
default_section = 'default'
ebcli_section = 'profile eb-cli'
app_version_folder = beanstalk_directory + 'app_versions'
logs_folder = beanstalk_directory + 'logs' + os.path.sep


def _get_option(config, section, key, default):
    try:
        return config.get(section, key)
    except (configparser.NoSectionError, configparser.NoOptionError):
        return default


def is_git_directory_present():
    return os.path.isdir('.git')


def clean_up():
    # remove dir
    cwd = os.getcwd()
    try:
        _traverse_to_project_root()
        if os.path.isdir(beanstalk_directory):
            shutil.rmtree(beanstalk_directory)
    finally:
        os.chdir(cwd)


def _set_not_none(config, section, option, value):
    if value:
        config.set(section, option, value)


def get_war_file_location():
    cwd = os.getcwd()
    try:
        _traverse_to_project_root()
        lst = glob.glob('build/libs/*.war')
        try:
            return os.path.join(os.getcwd(), lst[0])
        except IndexError:
            raise NotFoundError('Can not find .war artifact in build' +
                                os.path.sep + 'libs' + os.path.sep)
    finally:
        os.chdir(cwd)


def old_eb_config_present():
    return os.path.isfile(beanstalk_directory + 'config')


def config_file_present():
    return os.path.isfile(local_config_file)


def get_values_from_old_eb():
    old_config_file = beanstalk_directory + 'config'
    config = configparser.ConfigParser()
    config.read(old_config_file)

    app_name = _get_option(config, 'global', 'ApplicationName', None)
    cred_file = _get_option(config, 'global', 'AwsCredentialFile', None)
    default_env = _get_option(config, 'global', 'EnvironmentName', None)
    solution_stack_name = _get_option(config, 'global', 'SolutionStack', None)
    region = _get_option(config, 'global', 'Region', None)

    access_id, secret_key = read_old_credentials(cred_file)
    return {'app_name': app_name,
            'access_id': access_id,
            'secret_key': secret_key,
            'default_env': default_env,
            'platform': solution_stack_name,
            'region': region,
            }


def read_old_credentials(file_location):
    if file_location is None:
        return None, None
    config_str = '[default]\n' + open(file_location, 'r').read()
    config_fp = StringIO(config_str)

    config = configparser.ConfigParser()
    config.readfp(config_fp)

    access_id = _get_option(config, 'default', 'AWSAccessKeyId', None)
    secret_key = _get_option(config, 'default', 'AWSSecretKey', None)
    return access_id, secret_key


def save_to_aws_config(access_key, secret_key):
    config = configparser.ConfigParser()
    if not os.path.isdir(aws_config_folder):
        os.makedirs(aws_config_folder)

    config.read(aws_config_location)

    if ebcli_section not in config.sections():
        config.add_section(ebcli_section)

    _set_not_none(config, ebcli_section, aws_access_key, access_key)
    _set_not_none(config, ebcli_section, aws_secret_key, secret_key)

    with open(aws_config_location, 'w') as f:
        config.write(f)

    set_user_only_permissions(aws_config_location)


_marker = object()


def set_user_only_permissions(location):
    """
    Sets permissions so that only a user can read/write (chmod 400).
    Can be a folder or a file.
    :param location: Full location of either a folder or a location
    """
    if os.path.isdir(location):

        for root, dirs, files in os.walk(location):
            for d in dirs:
                pass
                _set_user_only_permissions_file(os.path.join(root, d), ex=True)
            for f in files:
                _set_user_only_permissions_file(os.path.join(root, f))

    else:
        _set_user_only_permissions_file(location)


def _set_user_only_permissions_file(location, ex=False):
    """
    :param ex: Boolean: add executable permission
    """
    permission = stat.S_IRUSR | stat.S_IWUSR
    if ex:
        permission |= stat.S_IXUSR
    os.chmod(location, permission)


def get_current_directory_name():
    dirname, filename = os.path.split(os.getcwd())
    if sys.version_info[0] < 3:
        filename = filename.decode('utf8')

    return filename


def get_application_name(default=_marker):
    result = get_config_setting('global', 'application_name')
    if result is not None:
        return result

    # get_config_setting should throw error if directory is not set up
    LOG.debug('Directory found, but no config or app name exists')
    if default is _marker:
        raise NotInitializedError
    return default


def get_default_region():
    return get_config_setting('global', 'default_region')


def get_default_solution_stack():
    return get_config_setting('global', 'default_platform')


def get_default_keyname():
    return get_config_setting('global', 'default_ec2_keyname')


def get_default_profile():
    try:
        return get_config_setting('global', 'profile')
    except NotInitializedError:
        return None


def touch_config_folder():
    if not os.path.isdir(beanstalk_directory):
        os.makedirs(beanstalk_directory)


def create_config_file(app_name, region, solution_stack):
    """
        We want to make sure we do not override the file if it already exists,
         but we do want to fill in all missing pieces
    :param app_name: name of the application
    :return: VOID: no return value
    """
    LOG.debug('Creating config file at ' + os.getcwd())

    if not os.path.isdir(beanstalk_directory):
        os.makedirs(beanstalk_directory)

    # add to global without writing over any settings if they exist
    write_config_setting('global', 'application_name', app_name)
    write_config_setting('global', 'default_region', region)
    write_config_setting('global', 'default_platform', solution_stack)


def _traverse_to_project_root():
    cwd = os.getcwd()
    if not os.path.isdir(beanstalk_directory):
        LOG.debug('beanstalk directory not found in ' + cwd +
                  '  -Going up a level')
        os.chdir(os.path.pardir)  # Go up one directory

        if cwd == os.getcwd():  # We can't move any further
            LOG.debug('Still at the same directory ' + cwd)
            raise NotInitializedError('EB is not yet initialized')

        _traverse_to_project_root()

    else:
        LOG.debug('Project root found at: ' + cwd)


def get_zip_location(file_name):
    cwd = os.getcwd()
    try:
        _traverse_to_project_root()
        if not os.path.isdir(app_version_folder):
            # create it
            os.makedirs(app_version_folder)

        return os.path.abspath(app_version_folder) + os.path.sep + file_name

    finally:
        os.chdir(cwd)


def get_logs_location(folder_name):
    cwd = os.getcwd()
    try:
        _traverse_to_project_root()
        if not os.path.isdir(logs_folder):
            # create it
            os.makedirs(logs_folder)

        return os.path.abspath(os.path.join(logs_folder, folder_name))

    finally:
        os.chdir(cwd)


def program_is_installed(program):
    return False if os_which(program) is None else True


def os_which(program):
    path = os.getenv('PATH')
    for p in path.split(os.path.pathsep):
        p = os.path.join(p, program)
        if sys.platform.startswith('win'):
            # Add .exe for windows
            p += '.exe'
        if os.path.exists(p) and os.access(p, os.X_OK):
            return p


def delete_file(location):
    if os.path.exists(location):
        os.remove(location)


def delete_directory(location):
    if os.path.isdir(location):
        shutil.rmtree(location)


def delete_app_versions():
    cwd = os.getcwd()
    try:
        _traverse_to_project_root()
        delete_directory(app_version_folder)
    finally:
        os.chdir(cwd)


def zip_up_folder(directory, location):
    cwd = os.getcwd()
    try:
        os.chdir(directory)
        io.log_info('Zipping up folder at location: ' + str(os.getcwd()))
        zipf = zipfile.ZipFile(location, 'w', zipfile.ZIP_DEFLATED)
        _zipdir('./', zipf)
        zipf.close()
        LOG.debug('File size: ' + str(os.path.getsize(location)))
    finally:
        os.chdir(cwd)


def zip_up_project(location):
    cwd = os.getcwd()

    try:
        _traverse_to_project_root()

        zip_up_folder('./', location)

    finally:
        os.chdir(cwd)


def _zipdir(path, zipf):
    zipped_roots = []
    for root, dirs, files in os.walk(path):
        if '.elasticbeanstalk' in root:
            io.log_info('  -skipping: ' + str(root))
            continue
        for f in files:
            # Windows requires us to index the folders.
            if root not in zipped_roots:
                zipf.write(root)
                zipped_roots.append(root)
            cur_file = os.path.join(root, f)
            if cur_file.endswith('~'):
                # Ignore editor backup files (like file.txt~)
                io.log_info('  -skipping: ' + str(cur_file))
            else:
                io.log_info('  +adding: ' + str(cur_file))
                zipf.write(cur_file)


def unzip_folder(file_location, directory):
    if not os.path.isdir(directory):
        os.makedirs(directory)

    zip = zipfile.ZipFile(file_location, 'r')
    for cur_file in zip.namelist():
        if not cur_file.endswith('/'):
            root, name = os.path.split(cur_file)
            path = os.path.normpath(os.path.join(directory, root))
            if not os.path.isdir(path):
                os.makedirs(path)
            open(os.path.join(path, name), 'wb').write(zip.read(cur_file))


def save_to_file(data, location, filename):
    if not os.path.isdir(location):
        os.makedirs(location)

    file_location = os.path.join(location, filename)
    data_file = open(file_location, 'wb')
    data_file.write(data)

    data_file.close()
    return file_location


def delete_env_file(env_name):
    cwd = os.getcwd()
    file_name = beanstalk_directory + env_name

    try:
        _traverse_to_project_root()
        for file_ext in ['.ebe.yml', '.env.yml']:
            path = file_name + file_ext
            delete_file(path)
    finally:
        os.chdir(cwd)


def get_editor():
    editor = get_config_setting('global', 'editor')
    if not editor:
        editor = os.getenv('EDITOR')
    if not editor:
        platform = sys.platform
        windows = platform.startswith('win')
        if windows:
            editor = None
        else:
            editor = 'nano'

    return editor


def save_env_file(env):
    cwd = os.getcwd()
    env_name = env['EnvironmentName']
    # ..yml extension helps editors enable syntax highlighting
    file_name = env_name + '.env.yml'

    file_name = beanstalk_directory + file_name
    try:
        _traverse_to_project_root()

        file_name = os.path.abspath(file_name)

        with codecs.open(file_name, 'w', encoding='utf8') as f:
            f.write(safe_dump(env, default_flow_style=False,
                              line_break=os.linesep))

    finally:
        os.chdir(cwd)

    return file_name


def get_environment_from_file(env_name):
    cwd = os.getcwd()
    file_name = beanstalk_directory + env_name

    try:
        _traverse_to_project_root()
        file_ext = '.env.yml'
        path = file_name + file_ext
        if os.path.exists(path):
            with codecs.open(path, 'r', encoding='utf8') as f:
                env = load(f)
    except (ScannerError, ParserError):
        raise InvalidSyntaxError('The environment file contains '
                                 'invalid syntax.')

    finally:
        os.chdir(cwd)

    return env


def write_config_setting(section, key_name, value):
    cwd = os.getcwd()  # save working directory
    try:
        _traverse_to_project_root()

        config = _get_yaml_dict(local_config_file)
        if not config:
            config = {}
        config.setdefault(section, {})[key_name] = value

        with codecs.open(local_config_file, 'w', encoding='utf8') as f:
            f.write(safe_dump(config, default_flow_style=False,
                              line_break=os.linesep))

    finally:
        os.chdir(cwd)  # go back to working directory


def get_config_setting(section, key_name, default=_marker):
    # get setting from global if it exists
    cwd = os.getcwd()  # save working directory
    try:
        _traverse_to_project_root()

        config_global = _get_yaml_dict(global_config_file)
        config_local = _get_yaml_dict(local_config_file)

        # Grab value, local gets priority
        try:
            value = config_global[section][key_name]
        except KeyError:
            value = None

        try:
            if config_local:
                value = config_local[section][key_name]
        except KeyError:
            pass  # Revert to global value

        if value is None and default != _marker:
            return default
    except NotInitializedError:
        if default == _marker:
            raise
        else:
            return default
    finally:
        os.chdir(cwd)  # move back to working directory
    return value


def get_json_dict(fullpath):
    """
    Read json file at fullpath and deserialize as dict.
    :param fullpath: str: path to the json file
    :return: dict
    """

    return json.loads(read_from_text_file(fullpath))


def _get_yaml_dict(filename):
    try:
        with codecs.open(filename, 'r', encoding='utf8') as f:
            return load(f)
    except IOError:
        return {}


def file_exists(full_path):
    return os.path.isfile(full_path)


def eb_file_exists(location):
    cwd = os.getcwd()
    try:
        _traverse_to_project_root()
        path = beanstalk_directory + location
        return os.path.isfile(path)
    finally:
        os.chdir(cwd)


def make_eb_dir(location):
    cwd = os.getcwd()
    try:
        _traverse_to_project_root()
        path = beanstalk_directory + location
        if not os.path.isdir(path):
            os.makedirs(path)
    finally:
        os.chdir(cwd)


def write_to_eb_data_file(location, data):
    cwd = os.getcwd()
    try:
        _traverse_to_project_root()
        path = beanstalk_directory + location
        write_to_data_file(path, data)
    finally:
        os.chdir(cwd)


def read_from_eb_data_file(location):
    cwd = os.getcwd()
    try:
        _traverse_to_project_root()
        path = beanstalk_directory + location
        read_from_data_file(path)
    finally:
        os.chdir(cwd)


def write_to_data_file(location, data):
    with codecs.open(location, 'wb', encoding=None) as f:
        f.write(data)


def read_from_data_file(location):
    with codecs.open(location, 'rb', encoding=None) as f:
        return f.read()


def read_from_text_file(location):
    with codecs.open(location, 'rt', encoding=None) as f:
        return f.read()


def get_eb_file_full_location(location):
    cwd = os.getcwd()
    try:
        _traverse_to_project_root()
        path = beanstalk_directory + location
        full_path = os.path.abspath(path)
        return full_path
    finally:
        os.chdir(cwd)
