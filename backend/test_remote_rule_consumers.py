"""All managed consumers validate the source before conversion or publication."""
import io
from urllib.parse import urlencode, parse_qs, urlsplit
import zipfile
import yaml
import requests

import pytest
from flask import Flask

from backend.common import config as config_module
from backend.common.config_repository import ProfileRepository
from backend.agents.deployment_bundle import materialize_rule, DeploymentPreparationError
from backend.converters.mosdns import _ruleset_files
from backend.routes import mosdns
from backend.utils.mosdns_archive import build_mosdns_zip, MosdnsArchiveError
from backend.utils.remote_rule_sources import resolve_remote_rule_source
from backend.utils.ruleset_content import RemoteRuleValidationError
from backend.utils.rule_utils import read_validated_rule_cache, write_validated_rule_cache


@pytest.fixture
def source_env(tmp_path, monkeypatch):
    repo = ProfileRepository(tmp_path)
    monkeypatch.setattr(config_module, '_repository', repo)
    config_module.reset_config_context()
    source = {'id': 'source', 'name': '域名订阅', 'source_type': 'url', 'behavior': 'domain',
              'format': 'text', 'url': 'https://rules.example/domains.list', 'enabled': True}
    repo.save_shared({'rule_library': [source]})
    repo.save_profile('default', {'rule_configs': [{'id': 'selected', 'itemType': 'ruleset',
        'library_rule_id': 'source', 'policy': 'DIRECT', 'enabled': True}]})
    config = repo.get_compat_config('default')
    config['system_config'].update(server_domain='https://config.example', rule_proxy_token='fixture')
    yield repo, source, config
    config_module.reset_config_context()


def proxy_url(source, **extra):
    return 'https://config.example/api/profiles/default/mosdns/rule-proxy?' + urlencode({
        'url': source['url'], 'source_id': source['id'], **extra})


@pytest.mark.parametrize('callback', ['direct', 'local', 'proxy'])
def test_agent_rejects_mismatched_remote_before_conversion(source_env, callback):
    _repo, source, config = source_env
    url = {'direct': source['url'],
           'local': 'https://config.example/api/profiles/default/rules/local/域名订阅',
           'proxy': proxy_url(source)}[callback]
    with pytest.raises(DeploymentPreparationError, match='第 2 行') as exc:
        materialize_rule(url, config, '', fetch_raw=lambda _url: b'# source\nDOMAIN,wrong.example\n')
    assert 'domain/text' in str(exc.value)
    assert 'DOMAIN,wrong.example' in str(exc.value)
    assert read_validated_rule_cache(source, config) is None


@pytest.mark.parametrize('callback', ['direct', 'proxy'])
def test_agent_invalid_refresh_retains_valid_raw_cache(source_env, callback):
    _repo, source, config = source_env
    original = b'# keep original\r\n+.valid.example\r\n'
    write_validated_rule_cache(source, original, config)
    result = materialize_rule(source['url'] if callback == 'direct' else proxy_url(source), config, '',
                              fetch_raw=lambda _url: b'DOMAIN,bad.example')
    assert result == (original if callback == 'direct' else b'domain:valid.example')
    assert read_validated_rule_cache(source, config) == original


def test_rule_proxy_reports_line_without_a_good_cache(source_env, monkeypatch):
    _repo, source, config = source_env
    monkeypatch.setattr(mosdns, 'get_config', lambda: config)
    monkeypatch.setattr(mosdns, '_require_rule_proxy_auth', lambda: True)
    monkeypatch.setattr(mosdns, '_fetch_remote_content', lambda *a, **kw: '# comment\nDOMAIN,bad.example')
    with Flask(__name__).test_request_context(proxy_url(source)):
        response, status = mosdns.mosdns_rule_proxy()
        assert status == 422
        assert response.json['code'] == 'RULE_CONTENT_INVALID'
        assert '第 2 行' in response.json['message']
    assert read_validated_rule_cache(source, config) is None


def test_rule_proxy_uses_valid_cache_after_invalid_refresh(source_env, monkeypatch):
    _repo, source, config = source_env
    write_validated_rule_cache(source, b'+.valid.example\n', config)
    monkeypatch.setattr(mosdns, 'get_config', lambda: config)
    monkeypatch.setattr(mosdns, '_require_rule_proxy_auth', lambda: True)
    monkeypatch.setattr(mosdns, '_fetch_remote_content', lambda *a, **kw: 'DOMAIN,bad.example')
    with Flask(__name__).test_request_context(proxy_url(source)):
        content, status, headers = mosdns.mosdns_rule_proxy()
    assert (content, status) == ('domain:valid.example', 200)
    assert headers['X-ConfigFlow-Rule-Cache'] == 'stale'
    assert read_validated_rule_cache(source, config) == b'+.valid.example\n'


@pytest.mark.parametrize('cached', [False, True])
def test_archive_validates_before_converting_and_keeps_cache(source_env, monkeypatch, cached):
    _repo, source, config = source_env
    if cached:
        write_validated_rule_cache(source, b'+.valid.example\n', config)
    monkeypatch.setattr(mosdns, '_fetch_remote_content', lambda *a, **kw: 'DOMAIN,bad.example')
    downloads = [{'url': proxy_url(source), 'name': '域名订阅', 'local_path': 'rules/domain.txt'}]
    if not cached:
        with pytest.raises(MosdnsArchiveError, match='第 1 行'):
            build_mosdns_zip(config, 'plugins: []', downloads, [])
    else:
        with zipfile.ZipFile(build_mosdns_zip(config, 'plugins: []', downloads, [])) as archive:
            assert archive.read('rules/domain.txt') == b'domain:valid.example'
        assert read_validated_rule_cache(source, config) == b'+.valid.example\n'


def test_generated_proxy_identifies_source_and_legacy_ambiguity_is_rejected(source_env):
    _repo, source, config = source_env
    second = {**source, 'id': 'other', 'name': 'Classical', 'behavior': 'classical'}
    config['rule_library'].append(second)
    item = {**source, 'id': 'selected', 'library_rule_id': source['id']}
    url = _ruleset_files(config, 'https://config.example', item)[0]['url']
    assert parse_qs(urlsplit(url).query)['source_id'] == [source['id']]
    assert resolve_remote_rule_source(source['url'], config, source['id']) == source
    with pytest.raises(RemoteRuleValidationError, match='多个不同规则类型'):
        resolve_remote_rule_source(source['url'], config)
    with pytest.raises(RemoteRuleValidationError, match='地址已变更'):
        resolve_remote_rule_source('https://other.example/list', config, source['id'])


def test_untyped_native_oxidns_resource_keeps_native_format(source_env):
    _repo, _source, config = source_env
    native = b'regexp:^native\\.example$\n'
    assert materialize_rule('https://native.example/custom.txt', config, '',
                            fetch_raw=lambda _url: native) == native


def test_ros_download_revalidates_shared_transport_response_for_each_declaration(source_env, monkeypatch):
    from backend.routers.rule_sync import Downloads, DownloadError
    _repo, source, config = source_env
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def raise_for_status(self): pass
        def iter_content(self, *_args): yield b'valid.example\n'
    monkeypatch.setattr('backend.utils.rule_fetch.request_rule', lambda *a, **kw: Response())
    download = Downloads()
    assert download(source, config) == 'valid.example\n'
    with pytest.raises(DownloadError, match='ipcidr'):
        download({**source, 'behavior': 'ipcidr'}, config)
    assert read_validated_rule_cache(source, config) is None


@pytest.mark.parametrize('behavior,invalid', [('domain', b'DOMAIN,bad.example'), ('ipcidr', b'bad.example')])
def test_custom_mihomo_provider_declaration_is_checked_without_library(source_env, monkeypatch, behavior, invalid):
    from backend.agents import deployment_bundle as bundles
    _repo, _source, config = source_env
    main = {'rule-providers': {'custom': {'type': 'http', 'url': 'https://native.example/list',
        'path': 'rules/custom.list', 'behavior': behavior, 'format': 'text'}}}
    monkeypatch.setattr(bundles, '_fetch_bytes', lambda *_args: invalid)
    with pytest.raises(DeploymentPreparationError, match='第 1 行'):
        bundles.build_deployment_bundle({'id': 'agent', 'service_type': 'mihomo'}, config, yaml.safe_dump(main))


def test_declared_proxy_validates_original_non_utf8_bytes(source_env, monkeypatch):
    _repo, source, config = source_env
    response = requests.Response()
    response.status_code = 200
    response._content = b'valid.example\ninvalid\xff.example'
    response._content_consumed = True
    monkeypatch.setattr(mosdns, 'request_rule', lambda *a, **kw: response)
    monkeypatch.setattr(mosdns, 'get_config', lambda: config)
    monkeypatch.setattr(mosdns, '_require_rule_proxy_auth', lambda: True)
    with Flask(__name__).test_request_context(proxy_url(source)):
        result, status = mosdns.mosdns_rule_proxy()
        assert status == 422
        assert 'UTF-8' in result.json['message']
    assert read_validated_rule_cache(source, config) is None


def test_stale_remote_identity_cannot_refer_to_new_inline_source(source_env):
    _repo, source, config = source_env
    config['rule_library'][0] = {**source, 'source_type': 'content', 'content': 'valid.example'}
    with pytest.raises(RemoteRuleValidationError, match='改为手写'):
        resolve_remote_rule_source(source['url'], config, source['id'])


def test_final_mihomo_provider_type_must_match_delivered_library_bytes(source_env, monkeypatch):
    from backend.agents import deployment_bundle as bundles
    _repo, source, config = source_env
    config['rule_library'][0]['behavior'] = 'classical'
    main = {'rule-providers': {'custom': {'type': 'http', 'url': source['url'],
        'path': 'rules/custom.list', 'behavior': 'domain', 'format': 'text'}}}
    monkeypatch.setattr(bundles, '_fetch_bytes', lambda *_args: b'DOMAIN,valid.example')
    with pytest.raises(DeploymentPreparationError, match='domain/text.*第 1 行'):
        bundles.build_deployment_bundle({'id': 'agent', 'service_type': 'mihomo'}, config, yaml.safe_dump(main))


@pytest.mark.parametrize('consumer', ['proxy', 'archive'])
def test_mrs_dns_consumer_explains_unsupported_format(source_env, monkeypatch, consumer):
    _repo, source, config = source_env
    config['rule_library'][0]['format'] = 'mrs'
    monkeypatch.setattr(mosdns, 'get_config', lambda: config)
    monkeypatch.setattr(mosdns, '_require_rule_proxy_auth', lambda: True)
    monkeypatch.setattr(mosdns, '_fetch_remote_content', lambda *a, **kw: pytest.fail('Unsupported format must not fetch'))
    if consumer == 'proxy':
        with Flask(__name__).test_request_context(proxy_url(source)):
            response, status = mosdns.mosdns_rule_proxy()
            assert status == 422
            assert '不支持 MRS' in response.json['message']
            assert 'Text 或 YAML' in response.json['message']
    else:
        downloads = [{'url': proxy_url(source), 'name': 'MRS', 'local_path': 'rules/domain.txt'}]
        with pytest.raises(MosdnsArchiveError, match='不支持 MRS.*Text 或 YAML'):
            build_mosdns_zip(config, 'plugins: []', downloads, [])


def test_custom_mihomo_invalid_declared_behavior_is_rejected(source_env, monkeypatch):
    from backend.agents import deployment_bundle as bundles
    _repo, _source, config = source_env
    main = {'rule-providers': {'custom': {'type': 'http', 'url': 'https://native.example/list',
        'path': 'rules/custom.list', 'behavior': 'domain-shuffix', 'format': 'text'}}}
    monkeypatch.setattr(bundles, '_fetch_bytes', lambda *_args: b'valid.example')
    with pytest.raises(DeploymentPreparationError, match='规则集类型必须'):
        bundles.build_deployment_bundle({'id': 'agent', 'service_type': 'mihomo'}, config, yaml.safe_dump(main))
