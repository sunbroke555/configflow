"""Optional real Mihomo parsing and loopback-only routing regression."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import shutil
import socket
import subprocess
from threading import Thread
import time
from urllib.request import urlopen

import pytest
import yaml

from backend.utils.ruleset_content import normalize_classical_lines


@pytest.mark.parametrize('rule_format', ['text', 'yaml'])
def test_native_classical_bare_lines_parse_and_match(tmp_path, rule_format):
    binary = os.environ.get('MIHOMO_BINARY') or shutil.which('mihomo')
    if not binary:
        pytest.skip('set MIHOMO_BINARY to enable native Mihomo validation')
    lines = ['+.tmdb.org', 'exact.test', '192.0.2.1', '192.0.2.0/24',
             '2001:db8::1', '2001:db8::/32']
    source = '\n'.join(lines) if rule_format == 'text' else yaml.safe_dump({'payload': lines})
    normalized, warnings = normalize_classical_lines(source)
    assert warnings == []

    # Native negative control: a successful -t exit alone does not prove that
    # classical lines loaded. Older kernels use a different warning message.
    parse_warnings = ('parse classical rule [', 'parse rule error:')
    baseline = tmp_path / 'baseline.yaml'
    baseline.write_text(yaml.safe_dump({
        'mixed-port': 0, 'rules': ['RULE-SET,bare,DIRECT', 'MATCH,DIRECT'],
        'rule-providers': {'bare': {'type': 'inline', 'behavior': 'classical', 'payload': lines}},
    }))
    checked = subprocess.run([binary, '-t', '-d', str(tmp_path), '-f', str(baseline)],
                             capture_output=True, text=True, timeout=15)
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert any(message in checked.stdout + checked.stderr for message in parse_warnings)

    class Origin(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Length', '2')
            self.end_headers()
            self.wfile.write(b'OK')

        def log_message(self, *_args):
            pass

    def free_port():
        with socket.socket() as reserve:
            reserve.bind(('127.0.0.1', 0))
            return reserve.getsockname()[1]

    origin = ThreadingHTTPServer(('127.0.0.1', 0), Origin)
    server = Thread(target=origin.serve_forever, daemon=True)
    server.start()
    proxy_port, controller_port = free_port(), free_port()
    (tmp_path / 'bare.rules').write_text(normalized)
    config = tmp_path / 'config.yaml'
    config.write_text(yaml.safe_dump({
        'mixed-port': proxy_port, 'bind-address': '127.0.0.1', 'mode': 'rule',
        'log-level': 'info', 'external-controller': f'127.0.0.1:{controller_port}',
        'dns': {'enable': False},
        'hosts': {host: '127.0.0.1' for host in ('tmdb.org', 'api.tmdb.org', 'exact.test', 'child.exact.test')},
        'proxy-groups': [{'name': 'MATCHED', 'type': 'select', 'proxies': ['DIRECT']}],
        'rule-providers': {'bare': {'type': 'file', 'behavior': 'classical',
                                    'format': rule_format, 'path': './bare.rules'}},
        'rules': ['RULE-SET,bare,MATCHED', 'MATCH,REJECT'],
    }))
    log_path = tmp_path / 'mihomo.log'
    process = None
    try:
        with log_path.open('w') as log:
            process = subprocess.Popen([binary, '-d', str(tmp_path), '-f', str(config)],
                                       stdout=log, stderr=subprocess.STDOUT)
            for _ in range(100):
                try:
                    with urlopen(f'http://127.0.0.1:{controller_port}/providers/rules', timeout=.2) as response:
                        providers = json.load(response)
                    # The controller can respond before asynchronous providers
                    # finish loading, especially in parallel backend runs.
                    if providers['providers']['bare']['ruleCount'] == len(lines):
                        break
                except OSError:
                    pass
                assert process.poll() is None, log_path.read_text()
                time.sleep(.05)
            else:
                pytest.fail('Mihomo classical provider did not load: ' + log_path.read_text())
            assert providers['providers']['bare']['ruleCount'] == len(lines)
            for host, matched in [('tmdb.org', True), ('api.tmdb.org', True),
                                  ('exact.test', True), ('child.exact.test', False)]:
                with socket.create_connection(('127.0.0.1', proxy_port), timeout=3) as connection:
                    connection.settimeout(3)
                    target = f'{host}:{origin.server_port}'
                    connection.sendall(f'GET http://{target}/ HTTP/1.1\r\nHost: {target}\r\n'
                                       'Connection: close\r\n\r\n'.encode())
                    try:
                        response = connection.recv(4096)
                    except ConnectionResetError:
                        response = b''
                    assert (b'200 OK' in response) == matched, (host, response, log_path.read_text())
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        origin.shutdown()
        origin.server_close()
        server.join(timeout=2)
    logs = log_path.read_text()
    assert not any(message in logs for message in parse_warnings), logs
    assert 'match RuleSet(bare)' in logs, logs
