"""Optional loopback-only native proof that flow YAML needs delivery conversion."""
import json
import os
import shutil
import socket
import subprocess
import time
from urllib.request import urlopen

import pytest
import yaml

from backend.native_test_helpers import wait_for_mihomo_routing

from backend.utils.ruleset_content import normalize_mihomo_ruleset


@pytest.mark.parametrize('layout', ['flow', 'json'])
@pytest.mark.parametrize('behavior,value,host', [
    ('domain', '+.tmdb.org', 'api.tmdb.org'),
    ('ipcidr', '127.0.0.0/8', '127.0.0.1'),
    ('classical', 'DOMAIN-SUFFIX,tmdb.org', 'api.tmdb.org'),
])
def test_native_flow_and_json_payloads_load_after_delivery_conversion(tmp_path, layout, behavior, value, host):
    binary = os.environ.get('MIHOMO_BINARY') or shutil.which('mihomo')
    if not binary:
        pytest.skip('set MIHOMO_BINARY to enable native Mihomo validation')
    source = ('payload: [' + json.dumps(value) + ']\n' if layout == 'flow'
              else json.dumps({'metadata': 'keep', 'payload': [value]}) + '\n')
    converted, warnings = normalize_mihomo_ruleset(source, behavior, 'yaml')
    assert warnings == []
    assert yaml.safe_load(converted) == yaml.safe_load(source)
    (tmp_path / 'original.yaml').write_text(source)
    (tmp_path / 'converted.yaml').write_text(converted)

    def free_port():
        with socket.socket() as reserve:
            reserve.bind(('127.0.0.1', 0))
            return reserve.getsockname()[1]

    proxy_port, controller_port = free_port(), free_port()
    config = tmp_path / 'config.yaml'
    config.write_text(yaml.safe_dump({
        'mixed-port': proxy_port, 'bind-address': '127.0.0.1', 'mode': 'rule',
        'log-level': 'info', 'external-controller': f'127.0.0.1:{controller_port}',
        'dns': {'enable': False},
        'proxy-groups': [{'name': name, 'type': 'select', 'proxies': ['REJECT']}
                         for name in ('MATCHED', 'FALLBACK')],
        'rule-providers': {name: {'type': 'file', 'behavior': behavior,
                                 'format': 'yaml', 'path': f'./{name}.yaml'}
                           for name in ('original', 'converted')},
        'rules': ['AND,((DST-PORT,31000),(RULE-SET,original,no-resolve)),MATCHED',
                  'AND,((DST-PORT,31001),(RULE-SET,converted,no-resolve)),MATCHED',
                  'MATCH,FALLBACK'],
    }))
    log_path = tmp_path / 'mihomo.log'
    with log_path.open('w') as log:
        process = subprocess.Popen([binary, '-d', str(tmp_path), '-f', str(config)],
                                   stdout=log, stderr=subprocess.STDOUT)
        try:
            for _ in range(100):
                try:
                    with urlopen(f'http://127.0.0.1:{controller_port}/providers/rules', timeout=.2) as response:
                        providers = json.load(response).get('providers') or {}
                    if providers.get('converted', {}).get('ruleCount') == 1:
                        break
                except OSError:
                    pass
                assert process.poll() is None, log_path.read_text()
                time.sleep(.05)
            else:
                pytest.fail('Converted YAML provider did not load: ' + log_path.read_text())
            assert providers['original']['ruleCount'] == 0
            wait_for_mihomo_routing(process, proxy_port, log_path)
            for port in (31000, 31001):
                with socket.create_connection(('127.0.0.1', proxy_port), timeout=2) as connection:
                    connection.settimeout(2)
                    target = f'{host}:{port}'
                    connection.sendall(f'GET http://{target}/ HTTP/1.1\r\nHost: {target}\r\n'
                                       'Connection: close\r\n\r\n'.encode())
                    try:
                        connection.recv(1024)
                    except (ConnectionResetError, socket.timeout):
                        pass
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    logs = log_path.read_text().splitlines()
    assert any(f' --> {host}:31000 ' in line and 'using FALLBACK' in line for line in logs), logs
    assert any(f' --> {host}:31001 ' in line and 'using MATCHED' in line for line in logs), logs
