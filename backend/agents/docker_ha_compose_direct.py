"""Single-file Compose for new installs; all preparation runs inside the image."""
import hashlib
import json
import re

import yaml

from .install_script import generate_docker_agent_compose

RUNTIME = '/usr/local/lib/configflow/docker-compose-install.py'
IDENTITY_VOLUME = 'configflow-ha-host-identity'
LABEL = 'io.configflow.dns-ha.installation'


def generate_direct(params, request, enrollment=None):
    from .docker_ha_guide import _volume
    data = params['data_dir']
    system_roots = ('/etc', '/usr', '/bin', '/sbin', '/proc', '/sys', '/dev', '/run', '/boot')
    if (any(data == root or data.startswith(root + '/') for root in system_roots)
            or data in ('/var', '/var/lib', '/opt', '/srv', '/home', '/root', '/tmp')
            or re.fullmatch(r'/volume[0-9]+(?:/docker)?', data)):
        raise ValueError('请为应用选择专用持久子目录，不能读写挂载系统目录或共享根目录')
    identity_mode = 'docker-volume' if request.get('create_host_identity') else 'file'
    if identity_mode == 'file':
        identity_path = params['ha_host_id_path']
        if identity_path == data or identity_path.startswith(data + '/'):
            raise ValueError('单文件 Compose 的现有宿主身份文件须位于应用数据目录之外')
    image = request.get('image', 'thsrite/config-flow-agent:latest')
    normalized = {**params, 'ha_host_id_path': params['ha_host_id_path'] if identity_mode == 'file' else ''}
    plan = {**normalized, 'image': image, 'enrollment_url': (enrollment or {}).get('url', ''),
            'install_method': 'compose_direct', 'identity_mode': identity_mode}
    owner = hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    project = 'cf-oxidns-' + owner[:16]
    # Base generator validates topology without making the volume fallback look like a host file.
    base = {**params, 'ha_host_id_path': params['ha_host_id_path'] if identity_mode == 'file' else '/etc/machine-id'}
    compose = yaml.safe_load(generate_docker_agent_compose(**base))
    compose.pop('version', None)
    compose['name'] = project
    settings = {'protocol': 1, 'owner': owner, 'url': (enrollment or {}).get('url', ''), 'identity_mode': identity_mode}

    def bind(source, target, readonly=False, create=False):
        result = _volume({'type': 'bind', 'source': source}, target, readonly)
        result['bind']['create_host_path'] = create
        return result

    def identity_mount(readonly=True):
        if identity_mode == 'file':
            return bind(params['ha_host_id_path'], '/run/configflow-host-machine-id', True)
        return {'type': 'volume', 'source': 'host-identity', 'target': '/run/configflow-host-identity',
                'read_only': readonly, 'volume': {'nocopy': True}}

    for name, service in compose['services'].items():
        service['image'] = image
        service['labels'] = {LABEL: owner}
        helper = name == 'ha-companion'
        # Compose 2.20 creates all containers before starting init. These exact
        # directories may be pre-created by Docker; init validates their contents.
        service['volumes'] = [bind(data + '/agent', '/opt/configflow-agent', helper, True),
                              bind(data + '/oxidns', '/etc/oxidns', helper, True),
                              bind(data + '/ha', '/var/lib/configflow-ha', False, True), identity_mount()]
        env = dict(entry.split('=', 1) for entry in service['environment'])
        if identity_mode == 'docker-volume':
            env['CONFIGFLOW_DNS_HA_IDENTITY'] = 'volume'
        if name == 'agent':
            service['volumes'].append(bind('/proc/sys/kernel/random/boot_id', '/run/configflow-host-boot-id', True))
            if params['network_mode'] == 'host':
                service['volumes'].append(bind('/proc/1/ns/net', '/run/configflow-host-netns', True))
            service['entrypoint'] = ['python3', RUNTIME, 'run']
            service['command'] = []
            env['CONFIGFLOW_COMPOSE_INSTALL'] = json.dumps({**settings, 'token': (enrollment or {}).get('token', '')}, separators=(',', ':'))
            dependencies = {'install-init': {'condition': 'service_completed_successfully'}}
            if params['network_mode'] == 'host':
                dependencies['ha-companion'] = {'condition': 'service_started'}
            service['depends_on'] = dependencies
        else:
            service['depends_on'] = {'install-init': {'condition': 'service_completed_successfully'}}
        service['environment'] = env
    compose['services']['install-init'] = {
        'image': image, 'entrypoint': ['python3', RUNTIME, 'prepare'], 'command': [],
        'restart': 'no', 'network_mode': 'none', 'read_only': True, 'user': '0:0',
        'cap_drop': ['ALL'], 'cap_add': ['DAC_OVERRIDE', 'FOWNER'],
        'security_opt': ['no-new-privileges:true'],
        'environment': {'CONFIGFLOW_COMPOSE_INSTALL': json.dumps(settings, separators=(',', ':')),
                        'PYTHONDONTWRITEBYTECODE': '1'},
        'volumes': [bind(data, '/configflow-data', False, True), identity_mount(False)],
        'labels': {LABEL: owner},
        'logging': {'driver': 'json-file', 'options': {'max-size': '5m', 'max-file': '2'}},
    }
    if identity_mode == 'docker-volume':
        compose['volumes'] = {'host-identity': {'name': IDENTITY_VOLUME, 'driver': 'local'}}
    for network in compose.get('networks', {}).values():
        network['name'] = project + '-dns-ha'
        network['labels'] = {LABEL: owner}
    text = yaml.safe_dump(compose, sort_keys=False, allow_unicode=True).replace('$', '$$')
    return text, {'filename': 'compose.yaml', 'project_name': project, 'command': 'docker compose up -d',
                  'identity_mode': identity_mode}
