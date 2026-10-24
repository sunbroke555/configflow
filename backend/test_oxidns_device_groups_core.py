"""Real-core checks of unmodified device-group generator output.

Uses existing local IPv4/IPv6 addresses as group members and loopback as the
default client. No model validation is bypassed and no YAML rules are injected
after generation: deployment rendering only adds its real watchdog.
"""
from copy import deepcopy
import os
import ipaddress
import re
import shutil
import socket
import subprocess
import time

import pytest
import yaml

from backend.agents.deployment_bundle import render_deployment_config
from backend.common.oxidns_device_groups import normalize_transition
from backend.common.oxidns_editor import prepare_editor
from backend.converters.oxidns import generate_oxidns_config, get_oxidns_custom_files
from backend.test_oxidns_device_groups import config
from scripts.verify_oxidns_native import MockDNS, free_port

BINARY = os.environ.get('OXIDNS_TEST_BINARY')


@pytest.mark.skipif(not BINARY, reason='Set OXIDNS_TEST_BINARY for real-core acceptance')
@pytest.mark.parametrize(('direct_block', 'proxy_block', 'full', 'native'), [
    (False, True, False, False), (True, False, False, False),
    (False, True, True, False), (False, False, False, True),
])
def test_real_generator_paths_blocking_and_cache_isolation(tmp_path, direct_block, proxy_block, full, native):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
        route.connect(('192.0.2.1', 9))
        lan = route.getsockname()[0]
    if lan in ('127.0.0.1', '0.0.0.0'):
        pytest.skip('A non-loopback local IPv4 is needed for the default-group client')
    client6 = os.environ.get('OXIDNS_TEST_CLIENT_IPV6')
    if not client6 and shutil.which('ifconfig'):
        addresses = subprocess.run(['ifconfig'], capture_output=True, text=True, check=True).stdout
        client6 = next((value for value in re.findall(r'\binet6 ([0-9a-f:]+)(?:\s|%)', addresses)
                        if not ipaddress.ip_address(value).is_loopback and
                        not ipaddress.ip_address(value).is_link_local), None)
    if not client6:
        pytest.skip('Set OXIDNS_TEST_CLIENT_IPV6 to an existing local non-loopback IPv6 address')
    local, remote = MockDNS('203.0.113.51'), MockDNS('198.18.0.50')
    process = None
    try:
        port4, port6 = free_port(), free_port()
        source = config(blocking=False)
        direct, proxy, default = source['device_groups']['groups']
        direct.update(members=[lan], keep_blocking=direct_block, dns='direct_full' if full else 'follow')
        if full:
            source['oxidns']['custom_matches'] = []
        proxy.update(members=[client6], keep_blocking=proxy_block)
        default['members'] = []
        for name in ('cached.test', 'reverse.test'):
            identity = name.split('.')[0]
            source['rule_configs'].insert(-2, {'id': identity, 'itemType': 'rule', 'enabled': True,
                                             'rule_type': 'DOMAIN', 'value': name, 'policy': 'DIRECT'})
            source['oxidns']['direct_rules'].append(identity)
        source['oxidns'].update(
            listen=f'0.0.0.0:{port4}', tcp_enabled=False, prefer_ipv4=False,
            local_dns=f'udp://127.0.0.1:{local.server_address[1]}',
            remote_dns=f'udp://127.0.0.1:{remote.server_address[1]}', remote_ttl=0,
            cache_enabled=True, probe_cache_enabled=True, cache_dump_enabled=False,
            api={'enabled': False}, query_recorder={'enabled': True}, log={'level': 'warn'},
            custom_hosts='full:hosts.test 192.0.2.9',
            adguard={'enabled': True, 'content': '||global-block.test^'},
            custom_config=yaml.safe_dump({'plugins': [{'tag': 'server_v6', 'type': 'udp_server',
                'args': {'listen': f'[{client6}]:{port6}', 'entry': 'sequence_main'}}]}))
        if native:
            source['oxidns'] = prepare_editor(source['oxidns'], source)
            normalize_transition({}, source)
        document = yaml.safe_load(generate_oxidns_config(source))
        path = tmp_path / 'config.yaml'
        path.write_bytes(render_deployment_config(document, 'oxidns'))
        for item in get_oxidns_custom_files(source):
            target = tmp_path / item['path']
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(item['content'])
        checked = subprocess.run([BINARY, 'check', '-c', str(path), '-d', str(tmp_path)],
                                 capture_output=True, text=True, timeout=10)
        (tmp_path / 'check.log').write_text(checked.stdout + checked.stderr)
        assert checked.returncode == 0, checked.stdout + checked.stderr
        clients = {'direct': (lan, lan, port4),
                   'proxy': (client6, client6, port6), 'default': ('127.0.0.1', '127.0.0.1', port4)}
        transcripts = []

        def query(who, name):
            address, server, port = clients[who]
            command = ['dig', '-b', address, '@' + server, '-p', str(port), name, 'A',
                       '+tries=1', '+time=1', '+noall', '+comments', '+answer']
            reply = subprocess.run(command, capture_output=True, text=True, timeout=4)
            output = reply.stdout + reply.stderr
            transcripts.append(' '.join(command) + '\n' + output)
            status = re.search(r'status: (\w+)', output)
            assert reply.returncode == 0 and status, output
            return status[1], re.findall(r'\sIN\s+A\s+(\S+)', output)

        def expect(who, name, status, answers):
            assert query(who, name) == (status, answers), (who, name)

        with (tmp_path / 'core.log').open('w') as log:
            process = subprocess.Popen([BINARY, 'start', '-c', str(path), '-d', str(tmp_path)], stdout=log, stderr=log)
            deadline = time.monotonic() + 5
            while True:
                try:
                    query('direct', 'ready.test')
                    break
                except AssertionError:
                    assert time.monotonic() < deadline, (tmp_path / 'core.log').read_text()
                    time.sleep(.05)
            for who, expected in [('direct', '203.0.113.51'), ('proxy', '198.18.0.50'), ('default', '198.18.0.50')]:
                expect(who, 'path.test', 'NOERROR', [expected])
                expect(who, 'hosts.test', 'NOERROR', ['192.0.2.9'])
                expect(who, 'global-block.test', 'NXDOMAIN', [])
                expect(who, 'configflow-watchdog.invalid', 'NXDOMAIN', [])
            for who, keep, expected in [('direct', direct_block, '203.0.113.51'), ('proxy', proxy_block, '198.18.0.50')]:
                for name, status, answers in [('overlap.test', 'REFUSED', []), ('nx.test', 'NXDOMAIN', []),
                                              ('nodata.test', 'NOERROR', []), ('bh.test', 'NOERROR', ['0.0.0.0'])]:
                    expect(who, name, status if keep else 'NOERROR', answers if keep else [expected])
                expect(who, 'after.test', 'NOERROR', [expected])
            expect('default', 'overlap.test', 'NOERROR', ['203.0.113.51'])
            for name, order in [('cached.test', ['default', 'proxy', 'direct', 'proxy', 'default']),
                                ('reverse.test', ['proxy', 'direct', 'default', 'direct', 'proxy'])]:
                before = local.count(name)
                for who in order:
                    expect(who, name, 'NOERROR', ['198.18.0.50' if who == 'proxy' else '203.0.113.51'])
                assert local.count(name) - before == (1 if full else 1 + order.count('direct'))
        (tmp_path / 'dig.log').write_text('\n'.join(transcripts))
    finally:
        if process:
            process.terminate()
            process.wait(timeout=5)
        local.close()
        remote.close()
