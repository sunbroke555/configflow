import hashlib
import io
import tarfile

import pytest
import requests
import yaml

from backend.agents.deployment_bundle import build_deployment_bundle, DeploymentPreparationError
from backend.agents.mihomo_resources import prepare_mihomo_resources, required_geo_resources


def bundle(document):
    return build_deployment_bundle({'id': 'test', 'service_type': 'mihomo', 'profile_id': 'default'},
                                   {'profile_id': 'default'}, yaml.safe_dump(document))


def test_only_required_custom_geo_resource_is_bundled(monkeypatch):
    seen = []
    def fetch(url, config):
        seen.append((url, config))
        return b'geo database bytes'
    monkeypatch.setattr('backend.agents.mihomo_resources._fetch_bytes', fetch)
    original = bundle({'rules': ['GEOSITE,cn,DIRECT'], 'geox-url': {
        'geosite': 'https://data.test/custom-name.bin', 'geoip': 'https://unused.test/other.dat'}})
    settings = {'system_config': {'github_proxy_domain': 'https://gh-proxy.com'}}
    prepared = prepare_mihomo_resources(original, {'mihomo_resources': [], 'config_directory': '/opt/mihomo'}, settings)
    assert seen == [('https://data.test/custom-name.bin', settings)]
    assert {item['path'] for item in prepared.manifest['files']} == {'config.yaml', 'GeoSite.dat'}
    resource = next(item for item in prepared.manifest['files'] if item['role'] == 'resource')
    with tarfile.open(fileobj=io.BytesIO(prepared.archive), mode='r:gz') as archive:
        value = archive.extractfile('files/GeoSite.dat').read()
    assert value == b'geo database bytes'
    assert resource['sha256'] == hashlib.sha256(value).hexdigest()
    assert original.config_version == prepared.config_version
    assert len(original.manifest['files']) == 1


@pytest.mark.parametrize('files', [['GeoSite.dat'], ['geosite.dat']])
def test_manually_installed_geo_resource_needs_no_download(monkeypatch, files):
    monkeypatch.setattr('backend.agents.mihomo_resources._fetch_bytes', lambda *_a: pytest.fail('must reuse local file'))
    original = bundle({'rules': ['GEOSITE,cn,DIRECT']})
    assert prepare_mihomo_resources(original, {'mihomo_resources': files}, {}) is original


def test_old_agent_retains_compatibility_and_no_unneeded_download(monkeypatch):
    monkeypatch.setattr('backend.agents.mihomo_resources._fetch_bytes', lambda *_a: pytest.fail('unexpected download'))
    old = bundle({'rules': ['GEOSITE,cn,DIRECT']})
    assert prepare_mihomo_resources(old, {}, {}) is old
    no_geo = bundle({'rules': ['MATCH,DIRECT'], 'geox-url': {'geosite': 'https://unused.test/a'}})
    assert prepare_mihomo_resources(no_geo, {'mihomo_resources': []}, {}) is no_geo


def test_geo_progress_names_actual_file_and_checks_without_exposing_source_url(monkeypatch):
    messages = []

    def fetch(_url, _config):
        assert messages[-1] == '正在下载 GeoSite 资源：GeoSite.dat'
        return b'geo fixture'

    monkeypatch.setattr('backend.agents.mihomo_resources._fetch_bytes', fetch)
    original = bundle({'rules': ['GEOSITE,cn,DIRECT'], 'geox-url': {'geosite': 'https://source.test/private?token=secret'}})
    prepare_mihomo_resources(original, {'mihomo_resources': []}, {}, progress=messages.append)
    assert messages == ['正在下载 GeoSite 资源：GeoSite.dat', '正在检查 GeoSite.dat 下载内容与文件大小',
                        'GeoSite.dat 已准备（11 字节），已计算发布用 SHA256']
    assert 'source.test' not in str(messages) and 'secret' not in str(messages)
    messages.clear()
    prepare_mihomo_resources(original, {'mihomo_resources': ['GeoSite.dat']}, {}, progress=messages.append)
    assert messages == ['复用已有 GeoSite 资源（GeoSite.dat），不重复下载']


@pytest.mark.parametrize('document, expected', [
    # Mihomo v1.19.32 NewGEOIP skips the database only for "lan"; "private" is a database category.
    ({'rules': ['GEOIP,LAN,DIRECT', 'SRC-GEOIP,lan,DIRECT']}, []),
    ({'rules': ['GEOIP,private,DIRECT']}, ['mmdb']),
    ({'rules': ['GEOIP,CN,DIRECT']}, ['mmdb']),
    ({'rules': ['GEOIP,CN,DIRECT'], 'geodata-mode': True}, ['geoip']),
    ({'sub-rules': {'nested': ['AND,((GEOSITE,cn),(IP-ASN,123)),DIRECT']}}, ['geosite', 'asn']),
    # parseDNS loads its matchers whether or not DNS is enabled.
    ({'dns': {'enable': False, 'fallback': ['1.1.1.1'], 'fallback-filter': {'geosite': ['cn']}}}, ['geosite', 'mmdb']),
    ({'dns': {'nameserver-policy': {'geosite:cn,private': '223.5.5.5'}}}, ['geosite']),
    ({'dns': {'enable': True, 'fallback': ['1.1.1.1']}}, ['mmdb']),
    ({'dns': {'enable': True, 'fallback': ['1.1.1.1'], 'fallback-filter': {'geoip-code': 'LAN'}}}, []),
    ({'dns': {'enable': True, 'fallback': ['1.1.1.1'], 'fallback-filter': {'geoip-code': 'private'}}}, ['mmdb']),
    ({'dns': {'enable': True, 'nameserver-policy': {'geosite:cn': '223.5.5.5'}}}, ['geosite']),
    ({'dns': {'enable': True, 'fallback': ['1.1.1.1'], 'fallback-filter': {'geosite': ['cn'], 'geoip': False}}}, ['geosite']),
    ({'dns': {'enable': True, 'enhanced-mode': 'fake-ip', 'fake-ip-filter-mode': 'rule', 'fake-ip-filter': ['GEOSITE,cn,real-ip']}}, ['geosite']),
    ({'dns': {'enhanced-mode': 'redir-host', 'fake-ip-filter': ['geosite:cn']}}, []),
    # parseSniffer also runs while sniffing is disabled.
    ({'sniffer': {'enable': False, 'skip-domain': ['geosite:apple']}}, ['geosite']),
    ({'sniffer': {'force-domain': ['GeoSite:google'], 'skip-dst-address': ['geoip:telegram']}}, ['geosite', 'mmdb']),
    ({'sniffer': {'skip-src-address': ['geoip:lan,LAN', '192.168.0.0/16']}}, []),
    ({'sniffer': {'skip-src-address': ['geoip:lan,cn']}, 'geodata-mode': True}, ['geoip']),
])
def test_resource_selection(document, expected):
    assert required_geo_resources(document, {}) == expected


def test_classical_provider_and_mmdb_alias_are_recognized(monkeypatch):
    document = {'rule-providers': {'nested': {'behavior': 'classical', 'path': './rules/a.yaml'}}}
    assert required_geo_resources(document, {'rules/a.yaml': (b'payload: ["GEOSITE,cn"]', 'rule')}) == ['geosite']
    monkeypatch.setattr('backend.agents.mihomo_resources._fetch_bytes', lambda *_a: pytest.fail('existing MMDB'))
    original = bundle({'rules': ['GEOIP,CN,DIRECT']})
    assert prepare_mihomo_resources(original, {'mihomo_resources': ['Country.mmdb']}, {}) is original


def test_download_failure_names_resource_and_actual_manual_path(monkeypatch):
    def fetch(*args, **kwargs):
        raise requests.ConnectionError('EOF from https://user:password@source.test/data?token=secret')
    monkeypatch.setattr('backend.agents.deployment_bundle.request_rule', fetch)
    original = bundle({'rules': ['GEOSITE,cn,DIRECT'], 'geox-url': {'geosite': 'https://source.test/custom.dat'}})
    with pytest.raises(DeploymentPreparationError) as caught:
        prepare_mihomo_resources(original, {'mihomo_resources': [], 'config_directory': '/custom/mihomo'}, {})
    text = str(caught.value)
    for value in ('GeoSite 下载失败', 'geox-url.geosite', '/custom/mihomo/GeoSite.dat', 'GitHub 代理域名', '规则下载代理', '手动下载', 'EOF'):
        assert value in text
    assert 'password' not in text and 'secret' not in text
    assert len(original.manifest['files']) == 1


@pytest.mark.parametrize('content', [b'', b'<html>proxy error</html>'])
def test_empty_and_html_responses_are_not_bundled(monkeypatch, content):
    monkeypatch.setattr('backend.agents.mihomo_resources._fetch_bytes', lambda *_a: content)
    with pytest.raises(DeploymentPreparationError, match='GeoSite 下载失败'):
        prepare_mihomo_resources(bundle({'rules': ['GEOSITE,cn,DIRECT']}), {'mihomo_resources': []}, {})
