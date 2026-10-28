"""Readable Compose; initialization and enrollment live in the image entrypoint."""
import hashlib
import json
import re

import yaml

from .install_script import generate_docker_agent_compose


class FlowMap(dict):
    pass


class FlowList(list):
    pass


class ImageName(str):
    pass


class CompactDumper(yaml.SafeDumper):
    def ignore_aliases(self, data):
        return False if isinstance(data, ImageName) else super().ignore_aliases(data)

    def generate_anchor(self, node):
        return 'agent_image' if isinstance(node, yaml.ScalarNode) else super().generate_anchor(node)


CompactDumper.add_representer(FlowMap, lambda dumper, value: dumper.represent_mapping('tag:yaml.org,2002:map', value, flow_style=True))
CompactDumper.add_representer(FlowList, lambda dumper, value: dumper.represent_sequence('tag:yaml.org,2002:seq', value, flow_style=True))
CompactDumper.add_representer(ImageName, lambda dumper, value: dumper.represent_str(str(value)))


def generate_compact(params, request, enrollment=None):
    data = params['data_dir']
    system_roots = ('/etc', '/usr', '/bin', '/sbin', '/proc', '/sys', '/dev', '/run', '/boot')
    if (any(data == root or data.startswith(root + '/') for root in system_roots)
            or data in ('/var', '/var/lib', '/opt', '/srv', '/home', '/root', '/tmp')
            or re.fullmatch(r'/volume[0-9]+(?:/docker)?', data)):
        raise ValueError('请为应用选择专用持久子目录，不能读写挂载系统目录或共享根目录')
    # Reuse existing address, port, topology and path validation; no host identity
    # file is needed by this entrypoint or exposed in the resulting Compose.
    base = yaml.safe_load(generate_docker_agent_compose(**{**params, 'ha_host_id_path': '/etc/machine-id'}))
    image = ImageName(request.get('image', 'thsrite/config-flow-agent:latest'))
    name = params['agent_name'] + '-oxidns'
    key, session = '', ''
    if enrollment:
        session = enrollment['url'].rsplit('/', 1)[-1]
        if not re.fullmatch(r'[0-9a-f]{32}', session) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', enrollment['token']):
            raise ValueError('安装凭据格式无效')
        key = session + '.' + enrollment['token']
    identity = {key: value for key, value in params.items() if key != 'ha_host_id_path'}
    identity['session'] = session
    suffix = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:8]
    project = 'cf-oxidns-' + params['agent_name'].lower()[:32] + '-' + suffix
    host = params['network_mode'] == 'host'

    def kernel_mount(source, target):
        return {'type': 'bind', 'source': source, 'target': target, 'read_only': True,
                'bind': FlowMap(create_host_path=False)}

    def directories(readonly=False):
        if ':' in data:
            return [{'type': 'bind', 'source': data + '/' + name, 'target': target,
                     'read_only': readonly and name != 'ha', 'bind': FlowMap(create_host_path=True)}
                    for name, target in (('agent', '/opt/configflow-agent'), ('oxidns', '/etc/oxidns'), ('ha', '/var/lib/configflow-ha'))]
        return [data + '/agent:/opt/configflow-agent' + (':ro' if readonly else ''),
                data + '/oxidns:/etc/oxidns' + (':ro' if readonly else ''),
                data + '/ha:/var/lib/configflow-ha']

    main = {'image': image, 'container_name': name, 'restart': 'unless-stopped',
            **({'network_mode': 'host'} if host else {'networks': base['services']['agent']['networks']}),
            'environment': {'SERVICE_TYPE': 'oxidns-ha-' + params['network_mode'],
                            'SERVER_URL': params['server_url'], 'AGENT_NAME': name,
                            'AGENT_IP': params['agent_ip'], 'AGENT_PORT': str(params['oxidns_port']),
                            'INSTALL_KEY': key},
            'volumes': directories() + [kernel_mount('/proc/sys/kernel/random/boot_id', '/run/configflow-host-boot-id')],
            'stop_grace_period': '30s',
            'logging': {'driver': 'json-file', 'options': FlowMap({'max-size': '10m', 'max-file': '3'})}}
    services = {'agent': main}
    if host:
        main['volumes'].append(kernel_mount('/proc/1/ns/net', '/run/configflow-host-netns'))
        services['ha-companion'] = {'image': image, 'container_name': name + '-ha', 'restart': 'unless-stopped',
            'network_mode': 'host', 'environment': {'SERVICE_TYPE': 'ha-companion-compact', 'INSTALL_SCOPE': session},
            'cap_add': FlowList(['NET_ADMIN', 'NET_RAW']), 'volumes': directories(True),
            'stop_grace_period': '30s',
            'logging': {'driver': 'json-file', 'options': FlowMap({'max-size': '5m', 'max-file': '2'})}}
    else:
        main['cap_add'] = FlowList(['NET_ADMIN', 'NET_RAW'])
    compose = {'name': project, 'services': services}
    if not host:
        compose['networks'] = base['networks']
        for network in compose['networks'].values():
            network['name'] = project + '-dns-ha'
    text = yaml.dump(compose, Dumper=CompactDumper, sort_keys=False, allow_unicode=True, width=1000).replace('$', '$$')
    return text, {'filename': 'compose.yaml', 'project_name': project, 'command': 'docker compose up -d',
                  'identity_mode': 'installation', 'service_count': len(services)}
