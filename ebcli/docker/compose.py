import os

from botocore.compat import six

from . import log


AWSEB_LOGS = 'awseb-logs-'
COMPOSE_CMD_KEY = 'command'
COMPOSE_ENV_KEY = 'environment'
COMPOSE_IMG_KEY = 'image'
COMPOSE_LINKS_KEY = 'links'
COMPOSE_PORTS_KEY = 'ports'
COMPOSE_VOLUMES_KEY = 'volumes'
CONTAINER_DEF_CMD_KEY = 'command'
CONTAINER_DEF_CONTAINERPORT_KEY = 'containerPort'
CONTAINER_DEF_HOSTPORT_KEY = 'hostPort'
CONTAINER_DEF_IMG_KEY = 'image'
CONTAINER_DEF_KEY = 'containerDefinitions'
CONTAINER_DEF_LINKS_KEY = 'links'
CONTAINER_DEF_NAME_KEY = 'name'
CONTAINER_DEF_PORT_MAPPINGS_KEY = 'portMappings'
CONTAINER_PATH_KEY = 'containerPath'
MOUNT_POINTS = 'mountPoints'
READ_ONLY_KEY = 'readOnly'
READ_ONLY_VOLUME = ':ro'
SOURCE_VOLUME_KEY = 'sourceVolume'
VAR_APP_CURRENT = '/var/app/current/'
VOLUMES_KEY = 'volumes'
VOLUMES_NAME_KEY = 'name'
VOLUMES_HOST_KEY = 'host'
VOLUMES_SOURCE_PATH_KEY = 'sourcePath'


iter_services = six.iterkeys


def compose_dict(dockerrun, docker_proj_path, host_log, envvars_map):
    """
    Return a docker-compose.yml representation as dict translated from the info
    provided in Dockerrun.aws.json.
    :param dockerrun: dict: dictionary representation of Dockerrun.aws.json
    :param docker_proj_path: str: path of the project directory
    :param host_log: str: path to the root host logs
    :param envvars_map: dict: key val map of environment variables
    :return dict
    """

    # Service is to docker-compose.yml as container definition
    # is to Dockerrun.aws.json. We want to turn definition -> service
    services = {}
    definitions = _get_container_definitions(dockerrun)

    # Maps 'volume name' to local path
    # E.x.: proxy-static -> /workspace/eb-project/proxy/html
    volume_map = _get_volume_map(dockerrun.get(VOLUMES_KEY, []), docker_proj_path)

    for definition in definitions:
        _add_service(services, definition, volume_map, host_log, envvars_map)

    return services


def _add_service(services, definition, volume_map, host_log, envvars_map):
    realname = definition[CONTAINER_DEF_NAME_KEY]
    img = definition[CONTAINER_DEF_IMG_KEY]
    links = definition.get(CONTAINER_DEF_LINKS_KEY, [])
    command = definition.get(CONTAINER_DEF_CMD_KEY, [])
    dockerrun_port_mappings = definition.get(CONTAINER_DEF_PORT_MAPPINGS_KEY, [])
    ports = _get_port_maps(dockerrun_port_mappings)
    remote_mountpoints = definition.get(MOUNT_POINTS, [])

    service = {COMPOSE_IMG_KEY: img}

    if command:
        service[COMPOSE_CMD_KEY] = command

    if ports:
        service[COMPOSE_PORTS_KEY] = ports

    if links:
        service[COMPOSE_LINKS_KEY] = ['{}:{}'.format(_fakename(n), n)
                                      for n in links]
    if envvars_map:
        service[COMPOSE_ENV_KEY] = envvars_map

    volumes = []
    for mp in remote_mountpoints:
        src_vol = mp[SOURCE_VOLUME_KEY]
        container_path = mp[CONTAINER_PATH_KEY]
        read_only = mp.get(READ_ONLY_KEY)

        if src_vol in volume_map:
            src_path = volume_map[src_vol]

        elif src_vol.startswith(AWSEB_LOGS):
            dirname = src_vol[len(AWSEB_LOGS):]
            src_path = os.path.join(host_log, dirname)

            os.makedirs(src_path)
        else:
            continue

        volume = '{}:{}'.format(src_path, container_path)
        if read_only:
            volume += READ_ONLY_VOLUME
        volumes.append(volume)

    if volumes:
        service[COMPOSE_VOLUMES_KEY] = volumes

    # alias the container names because '-' character is not allowed for service
    # names in docker-compose.yml
    services[_fakename(realname)] = service


def _get_port_maps(dockerrun_port_mappings):
    port_maps = []

    for m in dockerrun_port_mappings:
        hostport = m[CONTAINER_DEF_HOSTPORT_KEY]
        containerport = m[CONTAINER_DEF_CONTAINERPORT_KEY]
        port_map = '{}:{}'.format(hostport, containerport)
        port_maps.append(port_map)

    return port_maps


def _get_container_definitions(dockerrun):
    return dockerrun.get(CONTAINER_DEF_KEY, [])


def _get_volume_map(volumes, docker_proj_path):
    vmap = {}

    for volume in volumes:
        name = volume[VOLUMES_NAME_KEY]
        source_path = volume[VOLUMES_HOST_KEY][VOLUMES_SOURCE_PATH_KEY]

        if source_path.startswith(VAR_APP_CURRENT):
            local_relative_path = source_path[len(VAR_APP_CURRENT):]
            local_source_path = os.path.join(docker_proj_path, local_relative_path)

            vmap[name] = local_source_path
    return vmap

def _fakename(realname):
    return realname.replace('-', '')
