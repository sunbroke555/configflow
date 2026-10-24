"""Mihomo Domain label wildcards retain their scope in OxiDNS/MosDNS rules."""
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import time

import pytest
import yaml
from flask import Flask

from backend.agents.deployment_bundle import convert_mosdns_rules
from backend.converters.mosdns import convert_rule_content_for_mosdns
from backend.routes import mosdns
from backend.utils.rule_matcher import RuleConfigMatcher


CASES = [
    ('*.baidu.com', r'regexp:^[^.]+\.baidu\.com$',
     ['a.baidu.com', 'A.BAIDU.COM'], ['baidu.com', 'a.b.baidu.com', 'notbaidu.com']),
    ('*', r'regexp:^[^.]+$',
     ['localhost', 'HOST'], ['baidu.com', 'a.baidu.com']),
    ('+.baidu.com', 'domain:baidu.com',
     ['baidu.com', 'a.baidu.com', 'a.b.baidu.com'], ['notbaidu.com', 'baidu.com.test']),
    ('.baidu.com', r'regexp:.+\.baidu\.com$',
     ['a.baidu.com', 'a.b.baidu.com'], ['baidu.com', 'notbaidu.com']),
    ('*.*.microsoft.com', r'regexp:^[^.]+\.[^.]+\.microsoft\.com$',
     ['a.b.microsoft.com', 'XBOX.CN.MICROSOFT.COM'],
     ['microsoft.com', 'a.microsoft.com', 'a.b.c.microsoft.com']),
    ('books.itunes.apple.com', 'full:books.itunes.apple.com',
     ['books.itunes.apple.com', 'BOOKS.ITUNES.APPLE.COM'],
     ['itunes.apple.com', 'a.books.itunes.apple.com']),
    ('xbox.*.microsoft.com', r'regexp:^xbox\.[^.]+\.microsoft\.com$',
     ['xbox.cn.microsoft.com', 'XBOX.US.MICROSOFT.COM'],
     ['xbox.microsoft.com', 'xbox.a.b.microsoft.com', 'other.cn.microsoft.com']),
    ('localhost', 'full:localhost', ['localhost', 'LOCALHOST'], ['a.localhost', 'localhost.test']),
    ('+.*.example.org', r'regexp:^(?:[^.]+\.)*[^.]+\.example\.org$',
     ['a.example.org', 'a.b.example.org'], ['example.org', 'notexample.org']),
    ('.xbox.*.microsoft.com', r'regexp:^(?:[^.]+\.)+xbox\.[^.]+\.microsoft\.com$',
     ['a.xbox.cn.microsoft.com', 'a.b.xbox.cn.microsoft.com'],
     ['xbox.cn.microsoft.com', 'a.xbox.microsoft.com', 'a.xbox.cn.us.microsoft.com']),
    ('+.API.*.ORG', r'regexp:^(?:[^.]+\.)*api\.[^.]+\.org$',
     ['api.test.org', 'a.api.test.org', 'A.B.API.TEST.ORG'], ['api.org', 'api.a.b.org']),
]


def source_for(pattern, layout):
    if layout == 'text':
        return '# Domain patterns\n// ignored comment\n' + pattern + '\n'
    if layout == 'block':
        return yaml.safe_dump({'payload': [pattern]})
    if layout == 'flow':
        return 'payload: [' + json.dumps(pattern) + ']\n'
    return json.dumps({'payload': [pattern]})


def expression_matches(expression, query):
    """The documented full/suffix/regexp forms; OxiDNS regex is case-insensitive."""
    kind, value = expression.split(':', 1)
    query = query.lower().rstrip('.')
    if kind == 'full':
        return query == value.lower()
    if kind == 'domain':
        return query == value.lower() or query.endswith('.' + value.lower())
    assert kind == 'regexp'
    return bool(re.search(value, query, re.IGNORECASE))


@pytest.mark.parametrize('pattern,expected,hits,misses', CASES)
@pytest.mark.parametrize('layout', ['text', 'block', 'flow', 'json'])
def test_domain_wildcards_have_equal_proxy_bundle_and_ui_scope(monkeypatch, pattern, expected, hits, misses, layout):
    source = source_for(pattern, layout)
    assert convert_rule_content_for_mosdns(source, 'domain') == expected
    assert convert_mosdns_rules(source.encode()).decode() == expected
    monkeypatch.setattr(mosdns, 'get_config', lambda: {})
    monkeypatch.setattr(mosdns, '_require_rule_proxy_auth', lambda: True)
    monkeypatch.setattr(mosdns, '_local_content_rule', lambda *_: source)
    with Flask(__name__).test_request_context('/api/mosdns/rule-proxy?url=https://rules.test/list&part=domain'):
        actual, status, _ = mosdns.mosdns_rule_proxy()
    assert status == 200 and actual == expected
    matcher = RuleConfigMatcher([{'id': 'domain', 'itemType': 'ruleset', 'behavior': 'domain',
                                  'format': 'text' if layout == 'text' else 'yaml'}], [], lambda *_: source)
    for query in hits + misses:
        assert expression_matches(actual, query) == (query in hits), (pattern, query, actual)
        assert bool(matcher.match(query)) == (query in hits), (pattern, query)


def test_native_and_classical_regex_payloads_are_not_lowercased_or_reinterpreted():
    pattern = r'^\D+\.EXAMPLE\.org$'
    for line in ['DOMAIN-REGEX,' + pattern, 'regexp:' + pattern]:
        assert convert_rule_content_for_mosdns(line, 'domain') == 'regexp:' + pattern
        assert convert_mosdns_rules(line.encode()).decode() == 'regexp:' + pattern
    assert convert_rule_content_for_mosdns('DOMAIN-WILDCARD,*.example.org', 'domain') == '# no domain rules'
    assert convert_mosdns_rules(b'DOMAIN-WILDCARD,*.example.org') == b''


def test_native_oxidns_preserves_root_and_label_boundaries(tmp_path):
    binary = os.environ.get('OXIDNS_BINARY') or shutil.which('oxidns')
    if not binary:
        pytest.skip('set OXIDNS_BINARY to enable real OxiDNS Domain wildcard validation')

    def free_port():
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reserve:
            reserve.bind(('127.0.0.1', 0))
            return reserve.getsockname()[1]

    plugins, queries = [], []
    for index, (pattern, expected, hits, misses) in enumerate(CASES):
        tag, entry, port = f'provider_{index}', f'entry_{index}', free_port()
        content = convert_mosdns_rules(source_for(pattern, 'flow').encode()).decode()
        assert content == expected
        path = tmp_path / f'{tag}.txt'
        path.write_text(content)
        plugins.extend([
            {'tag': tag, 'type': 'domain_set', 'args': {'files': [str(path)]}},
            {'tag': entry, 'type': 'sequence', 'args': [
                {'matches': f'qname ${tag}', 'exec': 'reject 5'}, {'exec': 'reject 3'}]},
            {'tag': f'udp_{index}', 'type': 'udp_server',
             'args': {'entry': entry, 'listen': f'127.0.0.1:{port}'}},
        ])
        queries.extend((port, query, 5 if query in hits else 3, pattern) for query in hits + misses)
    config = tmp_path / 'config.yaml'
    config.write_text(yaml.safe_dump({'log': {'level': 'error'}, 'plugins': plugins}))

    def query_rcode(port, query):
        labels = query.rstrip('.').split('.')
        wire_name = b''.join(bytes([len(label)]) + label.encode('ascii') for label in labels) + b'\0'
        packet = struct.pack('!6H', 1234, 0x100, 1, 0, 0, 0) + wire_name + struct.pack('!2H', 1, 1)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            client.settimeout(.2)
            client.sendto(packet, ('127.0.0.1', port))
            response = client.recv(4096)
        return struct.unpack('!H', response[2:4])[0] & 0xF

    log_path = tmp_path / 'oxidns.log'
    with log_path.open('w') as log:
        process = subprocess.Popen([binary, 'start', '-c', str(config)], stdout=log, stderr=subprocess.STDOUT)
        try:
            for _ in range(100):
                try:
                    query_rcode(queries[0][0], queries[0][1])
                    break
                except OSError:
                    assert process.poll() is None, log_path.read_text()
                    time.sleep(.05)
            else:
                pytest.fail('OxiDNS did not start: ' + log_path.read_text())
            for port, query, expected, pattern in queries:
                assert query_rcode(port, query) == expected, (pattern, query, log_path.read_text())
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
