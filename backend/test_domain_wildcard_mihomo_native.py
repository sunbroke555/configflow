"""Optional loopback-only agreement checks against real Mihomo Domain providers."""
import json
import os
import re
import shutil
import socket
import subprocess
import time
from urllib.request import urlopen

import pytest
import yaml

from backend.test_domain_wildcard_delivery import CASES, source_for
from backend.test_domain_wildcard_oxidns_edges import EDGE_CASES
from backend.utils.rule_matcher import RuleConfigMatcher
from backend.utils.ruleset_content import normalize_mihomo_ruleset


NATIVE_CASES = CASES + EDGE_CASES + [
    ('*.example.com', None, ['_sip.example.com'],
     ['example.com', '_service._tcp.example.com']),
    ('+.example.com', None, ['_sip.example.com', '_service._tcp.example.com'],
     ['notexample.com']),
]


def _free_port():
    with socket.socket() as reserve:
        reserve.bind(('127.0.0.1', 0))
        return reserve.getsockname()[1]


def _domain_matcher(source, rule_format):
    return RuleConfigMatcher([{'id': 'domain', 'itemType': 'ruleset', 'behavior': 'domain',
                              'format': rule_format}], [], lambda *_: source)


@pytest.mark.parametrize('layout', ['text', 'text-bom', 'text-bom-comment', 'text-cr',
                                    'block', 'block-cr', 'flow', 'json'])
def test_native_mihomo_domain_wildcards_agree_with_page_matcher(tmp_path, layout):
    binary = os.environ.get('MIHOMO_BINARY') or shutil.which('mihomo')
    if not binary:
        pytest.skip('set MIHOMO_BINARY to enable real Mihomo Domain wildcard validation')

    providers, rules, requests = {}, [], []
    for case_index, (pattern, _, hits, misses) in enumerate(NATIVE_CASES):
        tag = f'{layout}_{case_index}'
        rule_format = 'text' if layout.startswith('text') else 'yaml'
        source = ('\ufeff' + pattern + '\n' if layout == 'text-bom'
                  else '\ufeff# Domain patterns\n' + pattern + '\n' if layout == 'text-bom-comment'
                  else source_for(pattern, layout.removesuffix('-cr')))
        if layout.endswith('-cr'):
            source = source.replace('\n', '\r')
        content, warnings = normalize_mihomo_ruleset(source, 'domain', rule_format)
        assert warnings == []
        path = tmp_path / f'{tag}.{rule_format}'
        path.write_text(content)
        providers[tag] = {'type': 'file', 'behavior': 'domain', 'format': rule_format,
                          'path': str(path)}
        matcher = _domain_matcher(source, rule_format)
        queries = {variant: expected for expected, values in ((True, hits), (False, misses))
                   for query in values for variant in (query, query.upper(), query.rstrip('.') + '.')}
        for host, expected in queries.items():
            destination_port = 31000 + len(requests)
            rules.append(f'AND,((DST-PORT,{destination_port}),(RULE-SET,{tag})),MATCHED')
            requests.append((host, destination_port, expected, bool(matcher.match(host)), pattern, layout))
    rules.append('MATCH,FALLBACK')
    proxy_port, controller_port = _free_port(), _free_port()
    config = tmp_path / 'config.yaml'
    config.write_text(yaml.safe_dump({
        'mixed-port': proxy_port, 'bind-address': '127.0.0.1', 'mode': 'rule',
        'log-level': 'info', 'external-controller': f'127.0.0.1:{controller_port}',
        'dns': {'enable': False},
        'proxy-groups': [{'name': name, 'type': 'select', 'proxies': ['REJECT']}
                         for name in ('MATCHED', 'FALLBACK')],
        'rule-providers': providers, 'rules': rules,
    }))
    log_path = tmp_path / 'mihomo.log'
    with log_path.open('w') as log:
        process = subprocess.Popen([binary, '-d', str(tmp_path), '-f', str(config)],
                                   stdout=log, stderr=subprocess.STDOUT)
        try:
            for _ in range(100):
                try:
                    with urlopen(f'http://127.0.0.1:{controller_port}/providers/rules', timeout=.2) as response:
                        actual = json.load(response)['providers']
                    if all(actual.get(tag, {}).get('ruleCount', 0) >= 1 for tag in providers):
                        break
                except OSError:
                    pass
                assert process.poll() is None, log_path.read_text()
                time.sleep(.05)
            else:
                pytest.fail('Domain providers did not initialize: ' + log_path.read_text())
            assert all(actual[tag]['ruleCount'] == 1 for tag in providers), actual
            for host, port, _, _, _, _ in requests:
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
    logs = log_path.read_text()
    outcomes = {int(port): policy == 'MATCHED' for port, policy in
                re.findall(r' --> [^\s]+:(\d+) .*using (MATCHED|FALLBACK)', logs)}
    differences = []
    for host, port, expected, page_match, pattern, layout in requests:
        assert port in outcomes, (host, port, logs)
        if outcomes[port] != expected or page_match != expected:
            differences.append({'pattern': pattern, 'layout': layout, 'host': host,
                                'expected': expected, 'page': page_match, 'mihomo': outcomes[port]})
    assert 'skip invalid domain' not in logs, logs
    assert not differences, json.dumps(differences, ensure_ascii=False, indent=2)
