"""Mihomo deployments repair classical text without changing shared sources."""
from copy import deepcopy
import io
import tarfile

import pytest
import requests
import yaml

from backend.agents import deployment_bundle as bundles


SOURCE = '+.tmdb.org\nexact.test\n192.0.2.1\n2001:db8::1\n'
EXPECTED = ('DOMAIN-SUFFIX,tmdb.org\nDOMAIN,exact.test\n'
            'IP-CIDR,192.0.2.1/32,no-resolve\nIP-CIDR6,2001:db8::1/128,no-resolve\n')
BASE = 'https://config.test'


def snapshot(content=SOURCE, source_type='content'):
    return {'profile_id': 'default', 'system_config': {'server_domain': BASE},
            'rule_library': [{'id': 'shared', 'name': 'Shared', 'behavior': 'classical',
                              'source_type': source_type, 'content': content,
                              'url': 'https://rules.test/shared'}]}


def provider(behavior='classical', rule_format='text'):
    return {'type': 'http', 'behavior': behavior, 'format': rule_format,
            'url': BASE + '/api/profiles/default/rules/local/Shared?target=mihomo',
            'path': './rules/shared.list'}


def contents(bundle):
    with tarfile.open(fileobj=io.BytesIO(bundle.archive), mode='r:gz') as archive:
        return {item['path']: archive.extractfile('files/' + item['path']).read()
                for item in bundle.manifest['files']}


def build(config, providers, service='mihomo', **kwargs):
    return bundles.build_deployment_bundle(
        {'id': 'fixture', 'service_type': service}, config,
        yaml.safe_dump({'rule-providers': providers}), base_url=BASE, **kwargs)


@pytest.mark.parametrize('source_type', ['content', 'url'])
@pytest.mark.parametrize('rule_format', ['text', 'yaml'])
def test_snapshot_classical_providers_normalize_without_mutating_source(monkeypatch, source_type, rule_format):
    source = SOURCE if rule_format == 'text' else yaml.safe_dump({'payload': SOURCE.splitlines()})
    config = snapshot(source, source_type)
    original = deepcopy(config)
    calls = []

    def fetch(url, **_kwargs):
        calls.append(url)
        result = requests.Response()
        result.status_code = 200
        result._content = source.encode()
        result._content_consumed = True
        return result

    monkeypatch.setattr(bundles, 'request_rule', fetch)
    files = contents(build(config, {'shared': provider(rule_format=rule_format)}))
    actual = files['rules/shared.list'].decode()
    if rule_format == 'yaml':
        assert yaml.safe_load(actual)['payload'] == EXPECTED.splitlines()
    else:
        assert actual == EXPECTED
    assert calls == (['https://rules.test/shared'] if source_type == 'url' else [])
    assert config == original


@pytest.mark.parametrize('behavior,rule_format,source', [
    ('domain', 'text', SOURCE.encode()),
    ('ipcidr', 'text', b'192.0.2.1/32\n'),
    ('classical', 'mrs', b'\x00MRS\xff\xfe\x80'),
    ('classical', 'text', b'\xff\xfe+\x00.\x00'),
])
def test_non_classical_or_non_text_payload_is_byte_preserved(monkeypatch, behavior, rule_format, source):
    monkeypatch.setattr(bundles, '_fetch_bytes', lambda *_args: source)
    files = contents(build(snapshot(source_type='url'), {'shared': provider(behavior, rule_format)}))
    assert files['rules/shared.list'] == source


def test_shared_raw_download_does_not_rewrite_domain_consumer(monkeypatch):
    calls = []

    def fetch(url, *_args):
        calls.append(url)
        return SOURCE.encode()

    monkeypatch.setattr(bundles, '_fetch_bytes', fetch)
    domain = {**provider('domain'), 'path': './rules/domain.list'}
    files = contents(build(snapshot(source_type='url'), {'classical': provider(), 'domain': domain}))
    assert files['rules/shared.list'] == EXPECTED.encode()
    assert files['rules/domain.list'] == SOURCE.encode()
    assert calls == ['https://rules.test/shared']


@pytest.mark.parametrize('url,library_behavior', [
    ('https://rules.test/shared', 'classical'),
    ('https://rules.test/shared?target=mihomo', 'classical'),
    (BASE + '/api/profiles/default/rules/local/Shared', 'classical'),
    (BASE + '/api/profiles/default/rules/local/Shared?target=mihomo', 'domain'),
])
def test_only_marked_classical_library_callbacks_are_rewritten(monkeypatch, url, library_behavior):
    monkeypatch.setattr(bundles, '_fetch_bytes', lambda *_args: SOURCE.encode())
    config = snapshot()
    config['rule_library'][0]['behavior'] = library_behavior
    files = contents(build(config, {'shared': {**provider(), 'url': url}}))
    assert files['rules/shared.list'] == SOURCE.encode()


@pytest.mark.parametrize('service', ['mosdns', 'oxidns'])
def test_dns_consumers_keep_existing_conversion(service, monkeypatch):
    monkeypatch.setattr(bundles, 'request_rule', lambda *_a, **_k: pytest.fail('self HTTP'))
    main = {'plugins': [{'tag': 'domains', 'type': 'domain_set',
                         'args': {'files': ['./rules/shared.list']}}]}
    bundle = bundles.build_deployment_bundle(
        {'id': 'fixture', 'service_type': service}, snapshot(), yaml.safe_dump(main),
        ruleset_downloads=[{'name': 'Shared', 'behavior': 'classical',
                           'url': BASE + '/api/profiles/default/rules/local/Shared',
                           'local_path': './rules/shared.list'}],
        base_url=BASE, local_dns_watchdog=False)
    assert contents(bundle)['rules/shared.list'] == b'domain:tmdb.org\nfull:exact.test'
