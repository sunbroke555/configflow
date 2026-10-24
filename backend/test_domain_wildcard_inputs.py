"""Page input and actual delivery paths agree without rewriting stored rules."""
from copy import deepcopy
from urllib.parse import urlsplit

import pytest
import requests
import yaml

from backend import test_classical_deployment as bundle_helpers
from backend.agents.deployment_bundle import convert_mosdns_rules
from backend.converters.mihomo import generate_mihomo_config, get_mihomo_ruleset_downloads
from backend.converters.mosdns import convert_rule_content_for_mosdns
from backend.test_mihomo_classical_delivery import delivery  # noqa: F401
from backend.utils.rule_matcher import RuleConfigMatcher, is_valid_domain, match_query
from backend.utils.ruleset_content import inspect_domain_content, normalize_mihomo_ruleset


@pytest.mark.parametrize('pattern,hits,misses', [
    ('*.example.com', ['A.EXAMPLE.COM.', '_sip.example.com', '-host.example.com', 'host-.example.com'],
     ['example.com.', '_service._tcp.example.com']),
    ('+.example.com', ['EXAMPLE.COM.', '_service._tcp.example.com.'], ['notexample.com.']),
    ('*', ['LOCALHOST.', '_service'], ['example.com.']),
    ('.*', ['example.com.', '_service._tcp'], ['LOCALHOST.']),
])
def test_page_match_endpoint_accepts_dns_query_spellings(delivery, pattern, hits, misses):
    client, repository, rule = delivery
    rule.update(behavior='domain', content=pattern)
    repository.save_shared({'rule_library': [rule]})
    for query in hits + misses:
        response = client.post('/api/rules/match-test', json={'query': query})
        assert response.status_code == 200, response.json
        assert response.json['matched'] == (query in hits), (pattern, query, response.json)


@pytest.mark.parametrize('query', [
    '.', '..', 'example.com..', '.example.com', 'a..example.com', 'bad host',
    'example.com\nother.com', 'https://example.com', '*.example.com', '+.example.com',
    '127.0.0.1.', '300.1.1.1.', '::1.', 'a' * 64 + '.com', ('a.' * 127) + 'a',
])
def test_query_normalization_does_not_accept_invalid_names(delivery, query):
    client, repository, rule = delivery
    rule.update(behavior='domain', content='+.*')
    repository.save_shared({'rule_library': [rule]})
    assert client.post('/api/rules/match-test', json={'query': query}).status_code == 400
    matcher = RuleConfigMatcher([{'id': 'set', 'itemType': 'ruleset', 'behavior': 'domain',
                                  'format': 'text'}], [], lambda *_: '+.*')
    assert matcher.match(query) is None


def test_dns_label_support_is_scoped_to_rule_matching():
    for name in ('_service._tcp.example.com', '-host.example', 'host-.example'):
        assert not is_valid_domain(name)
        assert is_valid_domain(name, allow_dns_labels=True)
        assert match_query(name + '.', 'DOMAIN', name)
    assert match_query('127.0.0.1', 'IP-CIDR', '127.0.0.0/8')
    assert not match_query('127.0.0.1.', 'IP-CIDR', '127.0.0.0/8')


@pytest.mark.parametrize('source,rule_format,expected', [
    ('\ufeff*.example.com\r+.other.com\r', 'text', '*.example.com\n+.other.com\n'),
    ('\ufeff# keep\r\n*.example.com\r\n+.other.com\r\n', 'text',
     '# keep\r\n*.example.com\r\n+.other.com\r\n'),
    ("\ufeffpayload:\r  - '*.example.com'\r  - '+.other.com'\r", 'yaml',
     "payload:\n  - '*.example.com'\n  - '+.other.com'\n"),
])
@pytest.mark.parametrize('source_type', ['content', 'url'])
def test_generated_http_and_agent_delivery_normalize_framing_only(
        delivery, monkeypatch, source, rule_format, expected, source_type):
    client, repository, rule = delivery
    rule.update(behavior='domain', format=rule_format, source_type=source_type,
                content=source, url='https://rules.test/domain.list')
    repository.save_shared({'rule_library': [rule]})

    def fetch(*_args, **_kwargs):
        response = requests.Response()
        response.status_code, response.encoding = 200, 'utf-8'
        response._content = source.encode()
        response._content_consumed = True
        return response

    monkeypatch.setattr('backend.routes.rules.request_rule', fetch)
    monkeypatch.setattr('backend.agents.deployment_bundle.request_rule', fetch)
    assert inspect_domain_content(source, 'domain', rule_format)['errors'] == []
    before = deepcopy(repository.export_all())
    config = repository.get_compat_config('default')
    config['system_config']['server_domain'] = bundle_helpers.BASE
    config['mihomo'] = {'custom_config': 'mixed-port: 0\ndns: {enable: false}\n'}
    main = yaml.safe_load(generate_mihomo_config(config, preflight_providers=False))
    provider = main['rule-providers']['Mixed']
    assert provider['url'] == get_mihomo_ruleset_downloads(config)[0]['url']
    url = urlsplit(provider['url'])
    assert url.query == 'target=mihomo'
    actual = client.get(url.path + '?' + url.query)
    assert actual.status_code == 200
    assert actual.data == expected.encode()
    raw = client.get(url.path)
    assert raw.data == source.encode()
    files = bundle_helpers.contents(bundle_helpers.build(config, {'Mixed': provider}))
    assert files[provider['path'].removeprefix('./')] == expected.encode()
    assert normalize_mihomo_ruleset(expected, 'domain', rule_format) == (expected, [])
    dns = 'regexp:^[^.]+\\.example\\.com$\ndomain:other.com'
    assert convert_rule_content_for_mosdns(source, 'domain') == dns
    assert convert_mosdns_rules(source.encode()).decode() == dns
    assert repository.export_all() == before


@pytest.mark.parametrize('separator', ['\n', '\r\n', '\r', '\v', '\f', '\x1c', '\x1d', '\x1e', '\x85', '\u2028', '\u2029'])
def test_all_accepted_text_line_boundaries_deliver_as_separate_rules(separator):
    source = separator.join(['*.example.com', '+.other.com', ''])
    assert not inspect_domain_content(source, 'domain', 'text')['errors']
    converted, warnings = normalize_mihomo_ruleset(source, 'domain', 'text')
    assert not warnings
    assert converted == ('\r\n' if separator == '\r\n' else '\n').join(
        ['*.example.com', '+.other.com', ''])
    assert normalize_mihomo_ruleset(converted, 'domain', 'text') == (converted, [])
    expected = 'regexp:^[^.]+\\.example\\.com$\ndomain:other.com'
    assert convert_rule_content_for_mosdns(source, 'domain') == expected
    assert convert_mosdns_rules(source.encode()).decode() == expected
    for value in (source, converted):
        matcher = RuleConfigMatcher([{'id': 'set', 'itemType': 'ruleset', 'behavior': 'domain',
                                      'format': 'text'}], [], lambda *_: value)
        assert matcher.match('a.example.com')
        assert matcher.match('other.com')
        assert matcher.match('a.b.example.com') is None
