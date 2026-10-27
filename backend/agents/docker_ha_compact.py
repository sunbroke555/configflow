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


def _annotate_compose(text, compose):
    """Attach fixed maintenance notes at YAML node boundaries, never inside values."""
    notes = {('name',): '保持默认：项目名，重建时沿用'}
    for role, service in compose['services'].items():
        prefix = ('services', role)
        helper = role == 'ha-companion'
        fields = {
            'image': '保持默认：复用上方镜像' if helper else '可改：镜像版本，需支持精简安装；升级只改此处',
            'container_name': '保持默认：容器名称',
            'restart': '保持默认：异常重启，手动停止后保持停止',
            'network_mode': '保持默认：使用宿主机网络',
            'cap_add': '保持默认：管理 VIP、发送高可用报文',
            'stop_grace_period': '保持默认：正常退出最多等待 30 秒',
            'volumes': ('保持默认：与上方使用同一组目录，前两项只读' if helper else
                        '核对：左侧是宿主机目录；安装后保留数据，右侧路径不改'),
        }
        for key, note in fields.items():
            if key in service:
                notes[prefix + (key,)] = note
        environments = {
            'SERVICE_TYPE': '保持默认：仅运行独立 VIP 服务' if helper else '保持默认：OxiDNS 与高可用运行入口',
            'SERVER_URL': '核对：容器可访问的 ConfigFlow 地址',
            'AGENT_NAME': '核对：设备登记名称',
            'AGENT_IP': '核对：宿主机固定 IPv4' if service.get('network_mode') == 'host' else '核对：容器独立 IPv4',
            'AGENT_PORT': '核对：Agent 管理端口，不是 DNS 端口',
            'INSTALL_KEY': '保持生成值：安装凭据，请勿分享；登记成功后可删',
            'INSTALL_SCOPE': '保持默认：核对安装归属，不是密码',
        }
        for key in service['environment']:
            notes[prefix + ('environment', key)] = environments[key]
        notes[prefix + ('logging', 'driver')] = '保持默认：容器日志格式'
        notes[prefix + ('logging', 'options')] = ('可改：每份 5 MB，最多 2 份' if helper else '可改：每份 10 MB，最多 3 份')
        for index, mount in enumerate(service['volumes']):
            path = prefix + ('volumes', index)
            if isinstance(mount, str):
                notes[path] = ('Agent 身份', '配置与规则', '高可用状态')[index]
            else:
                if index < 3:
                    notes[path + ('source',)] = '核对：宿主机持久目录'
                else:
                    notes[path + ('source',)] = ('保持默认：识别宿主，防止同机配对' if mount['source'].endswith('boot_id')
                                                else '保持默认：核对 Host 网络；不是文本文件')
                notes[path + ('type',)] = '保持默认：宿主路径直接挂载'
                notes[path + ('target',)] = '保持默认：容器内路径，宿主机无需创建'
                notes[path + ('read_only',)] = '保持默认：' + ('只读访问' if mount.get('read_only') else '允许保存数据')
                notes[path + ('bind',)] = ('保持默认：源路径缺失即报错' if index >= 3 else '保持默认：自动创建数据目录')
        if 'networks' in service:
            notes[prefix + ('networks', 'dns-ha', 'ipv4_address')] = '核对：与 AGENT_IP 一致；需调整请重新生成'
    if 'networks' in compose:
        network = ('networks', 'dns-ha')
        notes.update({
            network + ('name',): '保持默认：专用网络名称',
            network + ('driver',): '保持默认：容器使用独立局域网地址',
            network + ('driver_opts', 'parent'): '核对：宿主机实际网卡',
            network + ('ipam', 'config', 0, 'subnet'): '核对：局域网网段',
            network + ('ipam', 'config', 0, 'gateway'): '核对：局域网网关',
        })
    annotations = {}

    def visit(node, path=(), key=None):
        if path in notes:
            line = (key or node).start_mark.line
            # A wrapped quoted/plain scalar (or flow collection) must not receive
            # a comment in the middle. Aliases retain their anchor's marks, so
            # mapping keys identify their own output line instead.
            wrapped = node.start_mark.line == line and node.end_mark.line > line and (
                isinstance(node, yaml.ScalarNode) or node.flow_style)
            annotations[line] = (notes[path], wrapped)
        if isinstance(node, yaml.MappingNode):
            for child_key, value in node.value:
                visit(value, path + (child_key.value,), child_key)
        elif isinstance(node, yaml.SequenceNode):
            for index, value in enumerate(node.value):
                visit(value, path + (index,))

    visit(yaml.compose(text))
    lines = ['# 已按页面填写生成；地址、名称、端口或网络要调整，请回页面重新生成。',
             '# 已安装请保留原数据目录和身份；变更设备信息请使用迁移入口。']
    for index, line in enumerate(text.splitlines()):
        note, wrapped = annotations.get(index, ('', False))
        if note and wrapped:
            lines.append(line[:len(line) - len(line.lstrip())] + '# ' + note)
        lines.append(line + ('  # ' + note if note and not wrapped else ''))
    return '\n'.join(lines) + '\n'


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
    return _annotate_compose(text, compose), {'filename': 'compose.yaml', 'project_name': project, 'command': 'docker compose up -d',
                  'identity_mode': 'installation', 'service_count': len(services)}
