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
import re
import time
from datetime import datetime, timedelta
import platform

from cement.utils.misc import minimal_logger
from cement.utils.shell import exec_cmd

from ..core import fileoperations, io
from ..core.fileoperations import _marker
from ..containers import dockerrun
from ..lib import aws, ec2, elasticbeanstalk, heuristics, s3, utils
from ..lib.aws import InvalidParameterValueError
from ..objects.exceptions import *
from ..objects.solutionstack import SolutionStack
from ..objects.sourcecontrol import SourceControl
from ..resources.strings import strings, responses, prompts

LOG = minimal_logger(__name__)


def wait_for_success_events(request_id, timeout_in_minutes=None,
                            sleep_time=5, stream_events=True, can_abort=False):
    if timeout_in_minutes == 0:
        return
    if timeout_in_minutes is None:
        timeout_in_minutes = 10

    start = datetime.now()
    timediff = timedelta(seconds=timeout_in_minutes * 60)

    last_time = None

    streamer = io.get_event_streamer()
    if can_abort:
        streamer.prompt += strings['events.abortmessage']

    #Get first events
    events = []
    try:
        while not events:
            events = elasticbeanstalk.get_new_events(
                None, None, request_id, last_event_time=None
            )

            if len(events) > 0:
                event = events[-1]
                app_name = event.app_name
                env_name = event.environment_name

                if stream_events:
                    streamer.stream_event(get_event_string(event))
                # Test event message for success string
                if _is_success_string(event.message):
                    return
                last_time = event.event_date
            else:
                time.sleep(sleep_time)

        while (datetime.now() - start) < timediff:
            time.sleep(sleep_time)

            events = elasticbeanstalk.get_new_events(
                app_name, env_name, None, last_event_time=last_time
            )

            for event in reversed(events):
                if stream_events:
                    streamer.stream_event(get_event_string(event))
                    # We dont need to update last_time if we are not printing.
                    # This can solve timing issues
                    last_time = event.event_date

                # Test event message for success string
                if _is_success_string(event.message):
                    return
    finally:
        streamer.end_stream()
    # We have timed out
    raise TimeoutError('Timed out while waiting for command to Complete')


def _is_success_string(message):
    if message == responses['event.greenmessage']:
        return True
    if message.startswith(responses['event.launchsuccess']):
        return True
    if message == responses['event.redmessage']:
        raise ServiceError(message)
    if message.startswith(responses['event.launchbad']):
        raise ServiceError(message)
    if message.startswith(responses['event.updatebad']):
        raise ServiceError(message)
    if message == responses['event.failedlaunch']:
        raise ServiceError(message)
    if message == responses['event.faileddeploy']:
        raise ServiceError(message)
    if message == responses['logs.pulled']:
        return True
    if message.startswith(responses['logs.fail']):
        raise ServiceError(message)
    if message == responses['env.terminated']:
        return True
    if message == responses['env.updatesuccess']:
        return True
    if message == responses['env.configsuccess']:
        return True
    if message == responses['app.deletesuccess']:
        return True
    if responses['logs.successtail'] in message:
        return True
    if responses['logs.successbundle'] in message:
        return True
    if message.startswith(responses['swap.success']):
        return True
    if message.startswith(responses['create.ecsdockerrun1']):
        raise NotSupportedError(prompts['create.dockerrunupgrade'])

    return False


def get_event_string(event, long_format=False):
    message = event.message
    severity = event.severity
    date = event.event_date
    if long_format:
        return '{0} {1} {2}'.format(
            date.strftime("%Y-%m-%d %H:%M:%S").ljust(22),
            severity.ljust(7),
            message)
    else:
        return '{0}: {1}'.format(severity, message)


def get_all_env_names():
    envs = elasticbeanstalk.get_all_environments()
    return [e.name for e in envs]


def get_env_names(app_name):
    envs = elasticbeanstalk.get_app_environments(app_name)
    return [e.name for e in envs]


def get_app_version_labels(app_name):
    app_versions = elasticbeanstalk.get_application_versions(app_name)
    return [v['VersionLabel'] for v in app_versions]


def prompt_for_solution_stack():

    solution_stacks = elasticbeanstalk.get_available_solution_stacks()

    # get list of platforms
    platforms = []
    for stack in solution_stacks:
        if stack.platform not in platforms:
            platforms.append(stack.platform)

    cwd = os.getcwd()
    # First check to see if we know what language the project is in
    try:
        fileoperations._traverse_to_project_root()
        platform = heuristics.find_language_type()

        if platform == 'Docker':
            # Check to see if dockerrun is version one or two
            dockerrun_file = dockerrun.get_dockerrun(
                os.path.join(os.getcwd(), 'Dockerrun.aws.json'))
            if dockerrun_file:
                if dockerrun_file.get('AWSEBDockerrunVersion') in (1, '1'):
                    platform = 'Docker'
                else:
                    platform = 'Multi-container Docker'
    finally:
        os.chdir(cwd)

    if platform is not None:
        io.echo()
        io.echo(prompts['platform.validate'].replace('{platform}', platform))
        correct = io.get_boolean_response()

    if not platform or not correct:
        # ask for platform
        io.echo()
        io.echo(prompts['platform.prompt'])
        platform = utils.prompt_for_item_in_list(platforms)

    # filter
    solution_stacks = [x for x in solution_stacks if x.platform == platform]

    #get Versions
    versions = []
    for stack in solution_stacks:
        if stack.version not in versions:
            versions.append(stack.version)

    #now choose a version (if applicable)
    if len(versions) > 1:
        io.echo()
        io.echo(prompts['sstack.version'])
        version = utils.prompt_for_item_in_list(versions)
    else:
        version = versions[0]

    return get_latest_solution_stack(version, stack_list=solution_stacks)


def get_latest_solution_stack(platform_version, stack_list=None):
    if stack_list:
        solution_stacks = stack_list
    else:
        solution_stacks = elasticbeanstalk.\
            get_available_solution_stacks()

    #filter
    solution_stacks = [x for x in solution_stacks
                       if x.version == platform_version]

    #Lastly choose a server type
    servers = []
    for stack in solution_stacks:
        if stack.server not in servers:
            servers.append(stack.server)

    # Default to latest version of server
    # We are assuming latest is always first in list.
    if len(servers) < 1:
        raise NotFoundError(strings['sstacks.notaversion'].
                            replace('{version}', platform_version))
    server = servers[0]

    #filter
    solution_stacks = [x for x in solution_stacks if x.server == server]

    #should have 1 and only have 1 result
    assert len(solution_stacks) == 1, 'Filtered Solution Stack list ' \
                                      'contains multiple results'
    return solution_stacks[0]


def create_app(app_name, default_env=None):
    # Attempt to create app
    try:
        io.log_info('Creating application: ' + app_name)
        elasticbeanstalk.create_application(
            app_name,
            strings['app.description']
        )

        set_environment_for_current_branch(None)
        io.echo('Application', app_name,
                'has been created.')
        return None, None

    except AlreadyExistsError:
        io.log_info('Application already exists.')
        return pull_down_app_info(app_name, default_env=default_env)


def pull_down_app_info(app_name, default_env=None):
    # App exists, set up default environment
    envs = elasticbeanstalk.get_app_environments(app_name)
    if len(envs) == 0:
        # no envs, set None as default to override
        set_environment_for_current_branch(None)
        return None, None
    elif len(envs) == 1:
        # Set only env as default
        env = envs[0]
        io.log_info('Setting only environment "' +
                    env.name + '" as default')
    elif len(envs) > 1:
        if default_env:
            if default_env == '/ni':
                env = envs[0]
            else:
                env = next((env for env in envs if env.name == default_env),
                           None)
        if not default_env or env is None:
            # Prompt for default
            io.echo(prompts['init.selectdefaultenv'])
            env = utils.prompt_for_item_in_list(envs)

    set_environment_for_current_branch(env.name)

    io.log_info('Pulling down defaults from environment ' + env.name)
    # Get keyname
    keyname = elasticbeanstalk.get_specific_configuration_for_env(
        app_name, env.name, 'aws:autoscaling:launchconfiguration',
        'EC2KeyName'
    )
    if keyname is None:
        keyname = -1

    return env.platform.name, keyname


def open_webpage_in_browser(url, ssl=False):
    io.log_info('Opening webpage with default browser.')
    if not url.startswith('http'):
        if ssl:
            url = 'https://' + url
        else:
            url = 'http://' + url
    LOG.debug('url={}'.format(url))
    if utils.is_ssh() or platform.system().startswith('Win'):
        # Prefered way for ssh or windows
        # Windows cant do a fork so we have to do inline
        LOG.debug('Running webbrowser inline.')
        import webbrowser
        webbrowser.open_new_tab(url)
    else:
        # this is the prefered way to open a web browser on *nix.
        # It squashes all output which can be typical on *nix.
        LOG.debug('Running webbrowser as subprocess.')
        from subprocess import Popen, PIPE

        p = Popen(['{python} -m webbrowser \'{url}\''
                  .format(python=sys.executable, url=url)],
                  stderr=PIPE, stdout=PIPE, shell=True)
        '''
         We need to fork the process for various reasons
            1. Calling p.communicate waits for the thread. Some browsers
                (if opening a new window) dont return to the thread until
                 the browser closes. We dont want the terminal to hang in
                 this case
            2. If we dont call p.communicate, there is a race condition. If
                the main process terminates before the browser call is made,
                the call never gets made and the browser doesn't open.
            Therefor the solution is to fork, then wait for the child
            in the backround.
         '''
        pid = os.fork()
        if pid == 0:  # Is child
            p.communicate()
        # Else exit


def get_application_names():
    app_list = elasticbeanstalk.get_all_applications()

    return [n.name for n in app_list]


def print_env_details(env, health=True):
    region = aws.get_default_region()

    io.echo('Environment details for:', env.name)
    io.echo('  Application name:', env.app_name)
    io.echo('  Region:', region)
    io.echo('  Deployed Version:', env.version_label)
    io.echo('  Environment ID:', env.id)
    io.echo('  Platform:', env.platform)
    io.echo('  Tier:', env.tier)
    io.echo('  CNAME:', env.cname)
    io.echo('  Updated:', env.date_updated)

    if health:
        io.echo('  Status:', env.status)
        io.echo('  Health:', env.health)


def create_envvars_list(var_list):
    namespace = 'aws:elasticbeanstalk:application:environment'

    options = []
    options_to_remove = []
    for pair in var_list:
        ## validate
        if not re.match('^[\w\\_.:/+@-][^=]*=([\w\\_.:/+@-][^=]*)?$', pair):
            raise InvalidOptionsError(strings['setenv.invalidformat'])
        else:
            option_name, value = pair.split('=')
            d = {'Namespace': namespace,
                 'OptionName': option_name}

            if not value:
                options_to_remove.append(d)
            else:
                d['Value'] = value
                options.append(d)
    return options, options_to_remove


def create_dummy_app_version(app_name):
    version_label = 'Sample Application'
    return _create_application_version(app_name, version_label, None,
                                       None, None, warning=False)


def create_app_version(app_name, label=None, message=None, staged=False):
    cwd = os.getcwd()
    fileoperations._traverse_to_project_root()
    try:
        if heuristics.directory_is_empty():
            io.log_warning(strings['appversion.none'])
            return None
    finally:
        os.chdir(cwd)

    source_control = SourceControl.get_source_control()
    if source_control.untracked_changes_exist():
        io.log_warning(strings['sc.unstagedchanges'])

    #get version_label
    if label:
        version_label = label
    else:
        version_label = source_control.get_version_label()
        if staged:
            # Make a unique version label
            timestamp = datetime.now().strftime("%y%m%d_%H%M%S")
            version_label = version_label + '-stage-' + timestamp


    # get description
    if message:
        description = message
    else:
        description = source_control.get_message()

    if len(description) > 200:
        description = description[:195] + '...'


    # Check for zip or artifact deploy
    artifact = fileoperations.get_config_setting('deploy', 'artifact')
    if artifact:
        file_name, file_extension = os.path.splitext(artifact)
        file_name = version_label + file_extension
        file_path = artifact
    else:
        # Create zip file
        file_name, file_path = _zip_up_project(
            app_name, version_label, source_control, staged=staged)

    # Get s3 location
    bucket = elasticbeanstalk.get_storage_location()
    # upload to s3
    key = app_name + '/' + file_name

    try:
        s3.get_object_info(bucket, key)
        io.log_info('S3 Object already exists. Skipping upload.')
    except NotFoundError:
        io.log_info('Uploading archive to s3 location: ' + key)
        s3.upload_application_version(bucket, key, file_path)

    fileoperations.delete_app_versions()
    io.log_info('Creating AppVersion ' + version_label)
    return _create_application_version(app_name, version_label, description,
                                       bucket, key)


def _create_application_version(app_name, version_label, description,
                                bucket, key, warning=True):
    """
    A wrapper around elasticbeanstalk.create_application_version that
    handles certain error cases:
     * application doesnt exist
     * version already exists
    """
    while True:
        try:
            elasticbeanstalk.create_application_version(
                app_name, version_label, description, bucket, key
            )
            return version_label
        except InvalidParameterValueError as e:
            if e.message.startswith('Application Version ') and \
                        e.message.endswith(' already exists.'):
                # we must be deploying with an existing app version
                if warning:
                    io.log_warning('Deploying a previously deployed commit.')
                return version_label
            elif e.message == responses['app.notexists'].replace(
                        '{app-name}', '\'' + app_name + '\''):
                # App doesnt exist, must be a new region.
                ## Lets create the app in the region
                create_app(app_name)
            else:
                raise


def _zip_up_project(app_name, version_label, source_control, staged=False):
    # Create zip file
    file_name = version_label + '.zip'
    file_path = fileoperations.get_zip_location(file_name)
    # Check to see if file already exists from previous attempt
    if not fileoperations.file_exists(file_path) and \
                            version_label not in \
                            get_app_version_labels(app_name):
        # If it doesn't already exist, create it
        io.echo(strings['appversion.create'].replace('{version}',
                                                     version_label))
        ignore_files = fileoperations.get_ebignore_list()
        if ignore_files is None:
            source_control.do_zip(file_path, staged)
        else:
            io.log_info('Found .ebignore, using system zip.')
            fileoperations.zip_up_project(file_path, ignore_list=ignore_files)
    return file_name, file_path


def update_environment(env_name, changes, nohang, remove=None,
                       template=None, timeout=None, template_body=None,
                       solution_stack_name=None):
    try:
        request_id = elasticbeanstalk.update_environment(
            env_name, changes, remove=remove, template=template,
            template_body=template_body,
            solution_stack_name=solution_stack_name)
    except InvalidStateError:
        io.log_error(prompts['update.invalidstate'])
        return
    except InvalidSyntaxError as e:
        io.log_error(prompts['update.invalidsyntax'] +
                     '\nError = ' + e.message)
        return

    if nohang:
        return

    io.echo('Printing Status:')
    try:
        wait_for_success_events(request_id, timeout_in_minutes=timeout,
                                can_abort=True)
    except TimeoutError:
        io.log_error(strings['timeout.error'])


def write_setting_to_current_branch(keyname, value):
    source_control = SourceControl.get_source_control()

    branch_name = source_control.get_current_branch()

    fileoperations.write_config_setting(
        'branch-defaults',
        branch_name,
        {keyname: value}
    )


def set_environment_for_current_branch(value):
    write_setting_to_current_branch('environment', value)


def get_current_branch_environment():
    return get_setting_from_current_branch('environment')


def get_default_keyname():
    return get_config_setting_from_branch_or_default('default_ec2_keyname')


def get_default_profile():
    try:
        return get_config_setting_from_branch_or_default('profile')
    except NotInitializedError:
        return None


def get_default_region():
    try:
        return get_config_setting_from_branch_or_default('default_region')
    except NotInitializedError:
        return None


def get_default_solution_stack():
    return get_config_setting_from_branch_or_default('default_platform')


def get_setting_from_current_branch(keyname):
    source_control = SourceControl.get_source_control()

    branch_name = source_control.get_current_branch()

    branch_dict = fileoperations.get_config_setting('branch-defaults', branch_name)

    if branch_dict is None:
        return None
    else:
        try:
            return branch_dict[keyname]
        except KeyError:
            return None


def get_config_setting_from_branch_or_default(key_name, default=_marker):
    setting = get_setting_from_current_branch(key_name)

    if setting is not None:
        return setting
    else:
        return fileoperations.get_config_setting('global', key_name, default=default)


def get_solution_stack(solution_string):
    #If string is explicit, do not check
    if re.match(r'^\d\dbit [\w\s]+[0-9.]* v[0-9.]+ running .*$',
                solution_string):
        return SolutionStack(solution_string)

    solution_string = solution_string.lower()
    solution_stacks = elasticbeanstalk.get_available_solution_stacks()

    # check for exact string
    stacks = [x for x in solution_stacks if x.name.lower() == solution_string]

    if len(stacks) == 1:
        return stacks[0]

    #should only have 1 result
    if len(stacks) > 1:
        LOG.error('Platform list contains '
                  'multiple results')
        return None

    # No exact match, check for versions
    string = solution_string.replace('-', ' ')
    # put dash back in preconfigured types
    string = re.sub('preconfigured\\s+docker', 'preconfigured - docker', string)
    # put dash back in multi-container types
    string = re.sub('multi\\s+container', 'multi-container', string)
    string = re.sub(r'([a-z])([0-9])', '\\1 \\2', string)
    stacks = [x for x in solution_stacks if x.version.lower() == string]

    if len(stacks) > 0:
        # Give the latest version. Latest is always first in list
        return stacks[0]

    # No exact match, check for platforms
    stacks = [x for x in solution_stacks if x.platform.lower() == string]

    if len(stacks) > 0:
        # Give the latest version. Latest is always first in list
        return stacks[0]

    raise NotFoundError(prompts['sstack.invalidkey'].replace('{string}',
                                                             solution_string))


def is_cname_available(cname):
    return elasticbeanstalk.is_cname_available(cname)


def get_instance_ids(app_name, env_name):
    env = elasticbeanstalk.get_environment_resources(env_name)
    instances = [i['Id'] for i in env['EnvironmentResources']['Instances']]
    return instances


def upload_keypair_if_needed(keyname):
    keys = [k['KeyName'] for k in ec2.get_key_pairs()]
    if keyname in keys:
        return

    key_material = _get_public_ssh_key(keyname)

    try:
        ec2.import_key_pair(keyname, key_material)
    except AlreadyExistsError:
        return
    region = aws.get_default_region()
    io.log_warning(strings['ssh.uploaded'].replace('{keyname}', keyname)
                   .replace('{region}', region))


def _get_public_ssh_key(keypair_name):
    key_file = fileoperations.get_ssh_folder() + keypair_name
    if os.path.exists(key_file):
        file_name = key_file
    elif os.path.exists(key_file + '.pem'):
        file_name = key_file + '.pem'
    else:
        raise NotSupportedError(strings['ssh.filenotfound'].replace(
            '{key-name}', keypair_name))

    try:
        stdout, stderr, returncode = exec_cmd(['ssh-keygen', '-y', '-f',
                                           file_name])
        if returncode != 0:
            raise CommandError('An error occurred while trying '
                               'to get ssh public key')
        key_material = stdout
        return key_material
    except OSError:
        CommandError(strings['ssh.notpresent'])
