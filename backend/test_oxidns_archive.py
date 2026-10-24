"""OxiDNS archives include converted shared rules and raw AdGuard sources."""
from copy import deepcopy
import zipfile
import pytest
import yaml
from backend.common.oxidns_settings import get_default_oxidns_settings
from backend.converters.oxidns import generate_oxidns_config, get_oxidns_ruleset_downloads, get_oxidns_custom_files
from backend.routes import mosdns
from backend.utils.oxidns_archive import build_oxidns_zip, OxidnsArchiveError


def test_complete_mixed_archive_deduplicates_fetch_and_excludes_runtime_state(monkeypatch):
    settings = get_default_oxidns_settings()
    settings.update(direct_rulesets=['mixed'], custom_hosts='192.0.2.10 host.test')
    settings['query_recorder']['enabled'] = True
    settings['dynamic_domain_set']['enabled'] = True
    settings['adguard'].update(enabled=True, content='||custom-ads.test^', urls=['https://rules.test/adguard'])
    config = {'profile_id': 'office', 'system_config': {'server_domain': 'https://config.test', 'rule_proxy_token': 'fixture'},
              'oxidns': settings, 'rule_configs': [{'id': 'mixed', 'name': '中文混合', 'itemType': 'ruleset',
                                                  'behavior': 'classical', 'url': 'https://rules.test/mixed'}]}
    original = deepcopy(config)
    calls = []
    def fetch(url, config_data, **_kwargs):
        assert config_data['profile_id'] == 'office'
        calls.append(url)
        if url.endswith('mixed'):
            return r'DOMAIN-REGEX,^node[0-9]{1,3}\.test$' + '\nIP-CIDR,203.0.113.0/24'
        return '||download-ads.test^$dnstype=A\n@@||safe.download-ads.test^'
    monkeypatch.setattr(mosdns, '_fetch_remote_content', fetch)
    text = generate_oxidns_config(config)
    archive = build_oxidns_zip(config, text, get_oxidns_ruleset_downloads(config), get_oxidns_custom_files(config))
    with zipfile.ZipFile(archive) as output:
        files = {name: output.read(name).decode() for name in output.namelist()}
    assert sorted(calls) == ['https://rules.test/adguard', 'https://rules.test/mixed']
    assert config == original
    assert len(files) == 6
    assert not any(name.startswith('state/') or name.endswith('.db') for name in files)
    assert 'host.test 192.0.2.10' == files['rules/custom_hosts.txt']
    assert '||custom-ads.test^' == files['rules/adguard-custom.txt']
    assert r'regexp:^node[0-9]{1,3}\.test$' in files.values()
    assert '203.0.113.0/24' in files.values()
    assert any('$dnstype=A' in content for name, content in files.items() if name != 'config.yaml')
    for plugin in yaml.safe_load(files['config.yaml'])['plugins']:
        if isinstance(plugin.get('args'), dict):
            assert all(path.removeprefix('./') in files for path in plugin['args'].get('files', []))


def test_extra_provider_cannot_export_incomplete_archive():
    config = {'oxidns': {'custom_config': 'plugins:\n- tag: extra\n  type: domain_set\n  args:\n    files: [./missing.txt]\n'}}
    with pytest.raises(OxidnsArchiveError, match='未打包'):
        build_oxidns_zip(config, generate_oxidns_config(config), [], [])


@pytest.mark.parametrize('kind,args', [
    ('geosite', {'file': './missing.dat'}),
    ('ros_address_list', {'persistent': {'files': ['./missing.txt']}}),
])
def test_singular_and_nested_rule_files_must_also_be_packaged(kind, args):
    content = yaml.safe_dump({'plugins': [{'tag': 'external', 'type': kind, 'args': args}]})
    with pytest.raises(OxidnsArchiveError, match='未打包'):
        build_oxidns_zip({}, content, [], [])


def test_runtime_state_is_never_added_to_managed_archive():
    with pytest.raises(OxidnsArchiveError, match='运行数据'):
        build_oxidns_zip({}, 'plugins: []', [], [{'path': './state/learned.txt', 'content': ''}])
