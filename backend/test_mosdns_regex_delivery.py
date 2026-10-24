"""MosDNS downloads and rule-proxy preserve commas inside rule-set regexes."""
import io
import re
import zipfile
from itertools import product
from urllib.parse import urlsplit

import pytest
import requests
import yaml
from flask import Flask

from backend.common import config as config_module
from backend.common.config_repository import ProfileRepository
from backend.converters.mosdns import (
    generate_mosdns_config,
    get_mosdns_custom_files,
    get_mosdns_ruleset_downloads,
)
from backend.routes import mosdns, register_blueprints
from backend.utils.mosdns_archive import build_mosdns_zip, MosdnsArchiveError


SOURCE_URL = 'https://rules.test/regex.list'
BASE_URL = 'https://config.test'
REGEX = r'^node[0-9]{1,3}\.example\.test$'
DOMAIN_LINES = [
    'full:exact.example.test',
    'domain:suffix.example.test',
    'keyword:keyword',
    'regexp:' + REGEX,
]
IP_LINES = ['192.0.2.0/24', '2001:db8::/32']
CASES = list(product(('text', 'yaml'), ('content', 'url'), ('domain', 'classical')))


@pytest.fixture(params=CASES, ids=lambda case: '-'.join(case))
def delivery(request, tmp_path, monkeypatch):
    source_format, source_type, behavior = request.param
    lines = [
        'DOMAIN,exact.example.test,DIRECT',
        'DOMAIN-SUFFIX,suffix.example.test,PROXY',
        'DOMAIN-KEYWORD,keyword,Custom Policy',
        'DOMAIN-REGEX,' + REGEX,
        'IP-CIDR,192.0.2.0/24,DIRECT,no-resolve',
        'IP-CIDR6,2001:db8::/32,no-resolve',
        'PROCESS-NAME,ignored-process',
    ]
    source = yaml.safe_dump({'payload': lines}) if source_format == 'yaml' else '\n'.join(lines)
    repository = ProfileRepository(tmp_path)
    monkeypatch.setattr(config_module, '_repository', repository)
    config_module.reset_config_context()
    repository.create_profile({'id': 'office', 'name': 'Office'})
    repository.update_system_transaction(
        lambda system: system['system_config'].update({'server_domain': BASE_URL})
    )
    repository.save_shared({'rule_library': [{
        'id': 'regex', 'name': 'Regex', 'source_type': source_type,
        'content': source if source_type == 'content' else '',
        'url': SOURCE_URL if source_type == 'url' else '',
        'format': source_format, 'behavior': behavior, 'enabled': True,
    }]})
    repository.save_profile('office', {
        'rule_configs': [{'id': 'selected', 'itemType': 'ruleset', 'library_rule_id': 'regex',
                          'policy': 'DIRECT', 'enabled': True}],
        'mosdns': {'direct_rulesets': ['selected']},
    })
    calls = []

    def fetch(url, **kwargs):
        # Mock only remote I/O: source selection, decoding, conversion,
        # archive creation and endpoint authentication all run normally.
        assert source_type == 'url', 'Inline rules must not download their own callback'
        assert url == SOURCE_URL
        calls.append(url)
        response = requests.Response()
        response.status_code = 200
        response._content = source.encode('utf-8')
        response._content_consumed = True
        return response

    monkeypatch.setattr(mosdns, 'request_rule', fetch)
    monkeypatch.setattr('backend.common.auth.is_auth_enabled', lambda: False)
    app = Flask(__name__)
    register_blueprints(app)
    yield {
        'config': repository.get_compat_config('office'),
        'client': app.test_client(), 'calls': calls,
        'remote': source_type == 'url', 'mixed': behavior == 'classical',
        'invalid_remote': source_type == 'url' and behavior == 'domain',
    }
    config_module.reset_config_context()


def archive_files(buffer):
    with zipfile.ZipFile(buffer) as archive:
        return {name: archive.read(name).decode('utf-8') for name in archive.namelist()}


def assert_domain_rules(content):
    assert content.splitlines() == DOMAIN_LINES
    pattern = re.compile(next(line.removeprefix('regexp:') for line in content.splitlines()
                              if line.startswith('regexp:')))
    assert pattern.fullmatch('node1.example.test')
    assert pattern.fullmatch('node123.example.test')
    assert not pattern.fullmatch('node1234.example.test')
    assert not pattern.fullmatch('node.example.test')
    assert not pattern.fullmatch('node1.exampleXtest')


def assert_archive(files, mixed):
    assert_domain_rules(files['rules/Regex.txt'])
    expected_paths = {'config.yaml', 'rules/Regex.txt'}
    if mixed:
        expected_paths.add('rules/Regex_ip.txt')
        assert files['rules/Regex_ip.txt'].splitlines() == IP_LINES
    assert set(files) == expected_paths
    references = {}
    for plugin in yaml.safe_load(files['config.yaml'])['plugins']:
        args = plugin.get('args')
        if isinstance(args, dict):
            for path in args.get('files', []):
                normalized = path.removeprefix('./')
                assert normalized in files
                references[normalized] = plugin['type']
    assert references['rules/Regex.txt'] == 'domain_set'
    if mixed:
        assert references['rules/Regex_ip.txt'] == 'ip_set'


def test_generated_zip_and_web_download_keep_regex_and_split_ip(delivery):
    config = delivery['config']
    yaml_content = generate_mosdns_config(config, base_url=BASE_URL)
    downloads = get_mosdns_ruleset_downloads(config, base_url=BASE_URL)
    if delivery['invalid_remote']:
        with pytest.raises(MosdnsArchiveError, match='Domain 不接受 Classical'):
            build_mosdns_zip(config, yaml_content, downloads, get_mosdns_custom_files(config), base_url=BASE_URL)
        for path, headers in [('/api/generate/mosdns', {'X-ConfigFlow-Profile': 'office'}),
                              ('/api/profiles/office/generate/mosdns', {})]:
            response = delivery['client'].post(path, json={}, headers=headers)
            assert response.status_code == 500
            assert 'Domain 不接受 Classical' in response.json['message']
            assert '第 ' in response.json['message']
        return
    files = archive_files(build_mosdns_zip(
        config, yaml_content, downloads, get_mosdns_custom_files(config), base_url=BASE_URL,
    ))
    assert_archive(files, delivery['mixed'])
    # A classical source feeds two files but must only be fetched once per ZIP.
    assert delivery['calls'] == ([SOURCE_URL] if delivery['remote'] else [])

    for path, headers in [
        ('/api/generate/mosdns', {'X-ConfigFlow-Profile': 'office'}),
        ('/api/profiles/office/generate/mosdns', {}),
    ]:
        delivery['calls'].clear()
        response = delivery['client'].post(path, json={}, headers=headers)
        assert response.status_code == 200, response.get_data(as_text=True)
        assert response.mimetype == 'application/zip'
        assert 'mosdns-config.zip' in response.headers['Content-Disposition']
        downloaded = archive_files(io.BytesIO(response.data))
        assert_archive(downloaded, delivery['mixed'])
        assert downloaded == files
        assert delivery['calls'] == ([SOURCE_URL] if delivery['remote'] else [])


def test_rule_proxy_keeps_regex_and_matches_generated_file_parts(delivery):
    downloads = get_mosdns_ruleset_downloads(delivery['config'], base_url=BASE_URL)
    assert len(downloads) == (2 if delivery['mixed'] else 1)
    for download in downloads:
        callback = urlsplit(download['url'])
        assert callback.path == '/api/profiles/office/mosdns/rule-proxy'
        for path, headers in [
            ('/api/mosdns/rule-proxy', {'X-ConfigFlow-Profile': 'office'}),
            (callback.path, {}),
        ]:
            delivery['calls'].clear()
            response = delivery['client'].get(path + '?' + callback.query, headers=headers)
            if delivery['invalid_remote']:
                assert response.status_code == 422
                assert response.json['code'] == 'RULE_CONTENT_INVALID'
                assert 'Domain 不接受 Classical' in response.json['message']
                assert '第 ' in response.json['message']
                continue
            assert response.status_code == 200, response.get_data(as_text=True)
            assert response.mimetype == 'text/plain'
            if download['local_path'].endswith('_ip.txt'):
                assert response.get_data(as_text=True).splitlines() == IP_LINES
            else:
                assert_domain_rules(response.get_data(as_text=True))
            assert delivery['calls'] == ([SOURCE_URL] if delivery['remote'] else [])
