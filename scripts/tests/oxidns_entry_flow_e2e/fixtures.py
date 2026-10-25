"""Disposable native configurations; all network destinations are loopback."""
from copy import deepcopy
import socket

import yaml


_USED_PORTS = set()


def reserve_port():
    # Check both transports before releasing an ephemeral port for OxiDNS.
    for _ in range(100):
        with socket.socket() as tcp, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            tcp.bind(('127.0.0.1', 0))
            port = tcp.getsockname()[1]
            if port in _USED_PORTS:
                continue
            try:
                udp.bind(('127.0.0.1', port))
            except OSError:
                continue
            _USED_PORTS.add(port)
            return port
    raise RuntimeError('No unused loopback UDP/TCP port')


def fixtures(local_port, remote_port):
    local = {'tag': 'local_dns', 'type': 'forward', 'args': {'upstreams': [{'addr': f'udp://127.0.0.1:{local_port}'}]}}
    remote = {'tag': 'remote_dns', 'type': 'forward', 'args': {'upstreams': [{'addr': f'udp://127.0.0.1:{remote_port}'}]}}
    local_flow = {'tag': 'local_flow', 'type': 'sequence', 'args': [{'exec': '$local_dns'}, {'exec': 'accept'}]}
    remote_flow = {'tag': 'remote_flow', 'type': 'sequence', 'args': [{'exec': '$remote_dns'}, {'exec': 'accept'}]}
    extension = {'keep': [False, 0, None, {'nested': 'preserved'}]}
    result = []

    def fixture(identity, title, plugins, listeners, *, files=None, include=None, edit_tag, edit_source='config.yaml', case='route'):
        document = {'log': {'level': 'warn'}, 'plugins': deepcopy(plugins)}
        if include:
            document['include'] = include
        entry_specs = []
        for tag, typ, entry in listeners:
            port = reserve_port()
            document['plugins'].append({'tag': tag, 'type': typ, 'args': {'listen': f'127.0.0.1:{port}', 'entry': entry}})
            entry_specs.append({'tag': tag, 'type': typ, 'entry': entry, 'port': port})
        result.append({'id': identity, 'name': title, 'settings': {'config_mode': 'native', 'native_config': yaml.safe_dump(document, sort_keys=False), 'additional_files': files or []}, 'listeners': entry_specs, 'edit_tag': edit_tag, 'edit_source': edit_source, 'case': case})

    base = [local, remote, local_flow, remote_flow]
    flow_a = {'tag': 'entry_a', 'type': 'sequence', 'args': [{'matches': 'qname full:order.test', 'exec': 'goto local_flow'}, {'exec': '$local_dns'}, {'exec': 'accept'}], 'e2e_extension': extension}
    flow_b = {'tag': 'entry_b', 'type': 'sequence', 'args': [{'exec': '$local_dns'}, {'exec': 'accept'}]}
    fixture('e2e-independent', 'E2E · 独立入口', base + [flow_a, flow_b], [('listen_a', 'udp_server', 'entry_a'), ('listen_b', 'udp_server', 'entry_b')], edit_tag='entry_a')
    shared = deepcopy(flow_a)
    shared['tag'] = 'shared_entry'
    fixture('e2e-shared', 'E2E · 共享流程', base + [shared], [('shared_udp', 'udp_server', 'shared_entry'), ('shared_tcp', 'tcp_server', 'shared_entry')], edit_tag='shared_entry')
    prep = {'tag': 'manual_preparation', 'type': 'sequence', 'args': [{'exec': 'return'}]}
    handwritten = {'tag': 'handwritten_entry', 'type': 'sequence', 'e2e_extension': extension, 'args': [
        {'exec': 'jump manual_preparation'},
        {'matches': 'qname full:order.test', 'exec': 'goto local_flow', 'e2e_extra': {'keep': [False, 0, None]}},
        {'matches': ['qname full:order.test'], 'exec': 'reject 3'},
        {'exec': '$local_dns'}, {'exec': 'accept'}]}
    fixture('e2e-handwritten', 'E2E · 手写次序', base + [prep, handwritten], [('handwritten_udp', 'udp_server', 'handwritten_entry')], edit_tag='handwritten_entry')
    included = {'tag': 'included_entry', 'type': 'sequence', 'e2e_extension': extension, 'args': [{'matches': ['qname full:order.test'], 'exec': 'goto local_flow'}, {'exec': '$local_dns'}, {'exec': 'accept'}]}
    fixture('e2e-include', 'E2E · 附加文件入口', base, [('included_udp', 'udp_server', 'included_entry')], edit_tag='included_entry', edit_source='flows/entries.yaml', include=['flows/entries.yaml'], files=[{'path': 'flows/entries.yaml', 'encoding': 'text', 'content': yaml.safe_dump({'plugins': [included]}, sort_keys=False)}, {'path': 'notes/untouched.txt', 'encoding': 'text', 'content': 'untouched fixture attachment\n'}])
    fixture('e2e-forward', 'E2E · 插件直接入口', [local, remote], [('forward_udp', 'udp_server', 'local_dns')], edit_tag='local_dns', case='forward')
    http_port = reserve_port()
    http_document = {'log': {'level': 'warn'}, 'plugins': deepcopy(base + [flow_a, flow_b]) + [
        {'tag': 'http_listener', 'type': 'http_server', 'args': {'listen': f'127.0.0.1:{http_port}',
         'entries': [{'path': '/dns-a', 'exec': 'entry_a', 'json_api': True}, {'path': '/dns-b', 'exec': 'entry_b', 'json_api': True}]}}]}
    result.append({'id': 'e2e-http', 'name': 'E2E · HTTP 多路径入口', 'settings': {'config_mode': 'native', 'native_config': yaml.safe_dump(http_document, sort_keys=False), 'additional_files': []},
        'listeners': [{'tag': f'http_listener:{path}', 'type': 'http_server', 'entry': entry, 'port': http_port, 'path': path} for path, entry in [('/dns-a', 'entry_a'), ('/dns-b', 'entry_b')]],
        'edit_tag': 'entry_a', 'edit_source': 'config.yaml', 'case': 'http'})
    return result
