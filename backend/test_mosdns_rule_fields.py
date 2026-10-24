"""Classical provider payloads have no target: regex commas are all payload."""
import pytest
import yaml
from flask import Flask

from backend.agents.deployment_bundle import convert_mosdns_rules
from backend.converters.mosdns import convert_rule_content_for_mosdns
from backend.routes import mosdns


@pytest.mark.parametrize('expression', [
    r'^node[0-9]{1,3}\.example\.test$',
    r'^node[a,b]\.example\.test$',
    r'^(node,host)\.example\.test$',
    r'^node\,host\.example\.test$',
    r'^node[(,]{1,3}\.example\.test$',
    r'^node\[x\]{1,3}\.example\.test$',
    r'^node,host$',
    r'^node{foo$',
    r'^node{1$',
    r'^node[]a,b]$',
    r'^node[^]a,b]$',
])
@pytest.mark.parametrize('yaml_payload', [False, True])
@pytest.mark.parametrize('suffix', ['', ',DIRECT', ',no-resolve', ',Proxy Group,no-resolve'])
@pytest.mark.parametrize('consumer', ['converter', 'route', 'bundle'])
def test_regex_commas_are_preserved(monkeypatch, expression, yaml_payload, consumer, suffix):
    line = 'DOMAIN-REGEX,' + expression + suffix
    source = yaml.safe_dump({'payload': [line]}) if yaml_payload else line
    # classicalStrategy calls ParseRulePayload(..., false): no field is a target.
    expected = 'regexp:' + expression + suffix
    if consumer == 'converter':
        actual = convert_rule_content_for_mosdns(source, 'domain')
    elif consumer == 'bundle':
        actual = convert_mosdns_rules(source.encode()).decode()
    else:
        monkeypatch.setattr(mosdns, 'get_config', lambda: {})
        monkeypatch.setattr(mosdns, '_require_rule_proxy_auth', lambda: True)
        monkeypatch.setattr(mosdns, '_local_content_rule', lambda *_: source)
        with Flask(__name__).test_request_context('/api/mosdns/rule-proxy?url=https://rules.test/list&part=domain'):
            actual, status, _headers = mosdns.mosdns_rule_proxy()
        assert status == 200
    assert actual == expected


@pytest.mark.parametrize('yaml_payload', [False, True])
@pytest.mark.parametrize('part,plugin,expected', [
    ('domain', 'domain_set', 'full:exact.example.test\ndomain:suffix.example.test\nkeyword:word'),
    ('ip', 'ip_set', '192.0.2.0/24\n2001:db8::/32'),
])
def test_regular_rule_parameter_fields_agree_across_paths(monkeypatch, yaml_payload, part, plugin, expected):
    lines = [
        'DOMAIN,exact.example.test,DIRECT',
        'DOMAIN-SUFFIX,suffix.example.test,Proxy Group,no-resolve',
        'DOMAIN-KEYWORD,word,no-resolve',
        'IP-CIDR,192.0.2.0/24,DIRECT,no-resolve',
        'IP-CIDR6,2001:db8::/32,no-resolve',
        'GEOIP,CN,DIRECT',
        'PROCESS-NAME,example,DIRECT',
    ]
    source = yaml.safe_dump({'payload': lines}) if yaml_payload else '\n'.join(lines)
    assert convert_rule_content_for_mosdns(source, part) == expected
    assert convert_mosdns_rules(source.encode(), plugin).decode() == expected
    monkeypatch.setattr(mosdns, 'get_config', lambda: {})
    monkeypatch.setattr(mosdns, '_require_rule_proxy_auth', lambda: True)
    monkeypatch.setattr(mosdns, '_local_content_rule', lambda *_: source)
    with Flask(__name__).test_request_context('/api/mosdns/rule-proxy?url=https://rules.test/list&part=' + part):
        actual, status, _headers = mosdns.mosdns_rule_proxy()
    assert status == 200
    assert actual == expected


@pytest.mark.parametrize('payload,valid', [
    (r'^node,host$', True),
    (r'^node[0-9]{1,3}$', True),
    (r'^node{foo$,DIRECT', True),
    (r'^node{1$', True),
    (r'^node[]a,b]$', True),
    (r'^node[^]a,b]$', True),
    (r'^node$,no-resolve', True),
    (r'^node$,DIRECT,(', False),
])
def test_native_classical_and_converted_regex_validation_agree(tmp_path, payload, valid):
    """Compare real classical parsing with converted regex native validation.

    Mihomo -t warns (but exits zero) for an invalid classical entry. A full
    rule with an explicit final target instead exits nonzero for that regex.
    The invalid tail is a negative control: dropping it falsely makes it valid.
    """
    import os
    import shutil
    import subprocess

    binary = os.environ.get('MIHOMO_BINARY') or shutil.which('mihomo')
    if not binary:
        pytest.skip('native differential requires MIHOMO_BINARY or installed mihomo')
    source = 'DOMAIN-REGEX,' + payload
    provider_config = {
        'mixed-port': 0,
        'rules': ['RULE-SET,probe,DIRECT', 'MATCH,DIRECT'],
        'rule-providers': {'probe': {
            'type': 'inline', 'behavior': 'classical', 'payload': [source],
        }},
    }

    def validate(name, config):
        path = tmp_path / (name + '.yaml')
        path.write_text(yaml.safe_dump(config))
        return subprocess.run(
            [binary, '-t', '-d', str(tmp_path), '-f', str(path)],
            capture_output=True, text=True, timeout=15,
        )

    native_provider = validate('provider', provider_config)
    assert native_provider.returncode == 0, native_provider.stdout + native_provider.stderr
    provider_valid = 'parse classical rule [' not in native_provider.stdout + native_provider.stderr
    assert provider_valid == valid
    converted = convert_rule_content_for_mosdns(source, 'domain').removeprefix('regexp:')
    assert converted == payload
    assert convert_mosdns_rules(source.encode()).decode() == 'regexp:' + converted
    # The explicit target exists only in this native validation harness;
    # application inputs above remain classical provider payloads.
    native_converted = validate('converted', {
        'mixed-port': 0, 'rules': ['DOMAIN-REGEX,' + converted + ',DIRECT', 'MATCH,DIRECT'],
    })
    assert (native_converted.returncode == 0) == provider_valid, (
        native_converted.stdout + native_converted.stderr
    )


@pytest.mark.parametrize('source', [
    '\ufeff+.tmdb.org\n192.0.2.0/24\nDOMAIN,exact.test',
    '"payload": ["+.tmdb.org", "192.0.2.0/24", "DOMAIN,exact.test"]',
    "'payload':\n  - '+.tmdb.org'\n  - 192.0.2.0/24\n  - DOMAIN,exact.test\n",
    '{"payload": ["+.tmdb.org", "192.0.2.0/24", "DOMAIN,exact.test"]}',
    'domain:tmdb.org\nip:192.0.2.0/24\nDOMAIN,exact.test',
])
@pytest.mark.parametrize('part,plugin,expected', [
    ('domain', 'domain_set', 'domain:tmdb.org\nfull:exact.test'),
    ('ip', 'ip_set', '192.0.2.0/24'),
])
def test_dns_refresh_and_bundle_agree_for_mixed_text_bom_and_quoted_yaml(source, part, plugin, expected):
    assert convert_rule_content_for_mosdns(source, part) == expected
    assert convert_mosdns_rules(source.encode(), plugin, mixed=True).decode() == expected


def test_native_dns_regex_commas_and_ip_prefix_are_classified_per_line():
    source = r'regexp:^node[0-9]{1,3}\.test$' + '\nip:2001:db8::/32\n+.tmdb.org'
    assert convert_rule_content_for_mosdns(source, 'domain') == r'regexp:^node[0-9]{1,3}\.test$' + '\ndomain:tmdb.org'
    assert convert_rule_content_for_mosdns(source, 'ip') == '2001:db8::/32'
