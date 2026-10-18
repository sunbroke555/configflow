"""Pending publication follows actual converter output and all bundled bytes."""
from copy import deepcopy
import io
import tarfile

import pytest
import yaml

from backend.agents.deployment_bundle import deployment_content_fingerprint
from backend.common import config as config_module
from backend.common.config_repository import ProfileRepository
from backend.routes import agents


BASE_URL = 'https://config.test'


@pytest.fixture
def content_env(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path, default_config_factory=config_module.get_default_config,
                                   initial_config_factory=config_module.get_default_config)
    monkeypatch.setattr(config_module, '_repository', repository)
    config_module.reset_config_context()
    remote = {'provider': 'proxies: [{name: Remote, type: http, server: first.test, port: 8080}]'}
    fetches = []

    def subscription_fetch(sub_id, url, **kwargs):
        fetches.append((sub_id, url))
        assert sub_id == 'selected-sub'
        return remote['provider'], 'rendered_yaml'

    monkeypatch.setattr('backend.routes.subscriptions.get_subscription_proxies_yaml', subscription_fetch)
    monkeypatch.setattr('backend.routes.aggregations.get_subscription_proxies_yaml', subscription_fetch)
    monkeypatch.setattr('backend.utils.sub_store_client.get_subscription_proxies_yaml', subscription_fetch)
    monkeypatch.setattr('requests.sessions.Session.request',
                        lambda *_args, **_kwargs: pytest.fail('unexpected network request'))
    monkeypatch.setattr('backend.utils.provider_delivery.commit_provider_bundle',
                        lambda *_args, **_kwargs: pytest.fail('content checks must not commit providers'))
    config = config_module.get_default_config()
    config.update(profile_id='default', profile_name='Original label')
    config['system_config'].update(server_domain=BASE_URL, rule_proxy_token='fixture-rule-token')
    config['mihomo']['custom_config'] = 'mode: rule\nlog-level: info\ndns: {enable: false}\n'
    config['mosdns'].update(local_dns='udp://192.0.2.1:53', remote_dns='udp://192.0.2.2:53',
                            fallback_dns='udp://192.0.2.3:53')
    config['rule_library'] = [
        {'id': 'selected-rule', 'name': 'Selected', 'source_type': 'content',
         'content': 'DOMAIN-SUFFIX,first.test', 'enabled': True},
        {'id': 'unused-rule', 'name': 'Unused', 'source_type': 'content',
         'content': 'DOMAIN-SUFFIX,unused.test', 'enabled': True},
    ]
    config['rule_configs'] = [{
        'id': 'selected-rule-ref', 'itemType': 'ruleset', 'name': 'Selected',
        'library_rule_id': 'selected-rule', 'policy': 'DIRECT', 'enabled': True,
        'url': BASE_URL + '/api/profiles/default/rules/local/Selected', 'behavior': 'domain',
    }]
    config['mosdns']['direct_rulesets'] = ['selected-rule-ref']
    config['subscriptions'] = [
        {'id': 'selected-sub', 'name': 'Feed', 'url': 'https://subscription.test/selected', 'enabled': True},
        {'id': 'unused-sub', 'name': 'Unused feed', 'url': 'https://subscription.test/unused', 'enabled': True},
    ]
    config['proxy_groups'] = [{'id': 'group', 'name': 'Proxy', 'type': 'select', 'subscriptions': ['selected-sub']}]
    yield config, remote, fetches, repository
    config_module.reset_config_context()


def prepare(config, service, deployment_id='content-check'):
    agent = {'id': f'{service}-agent', 'profile_id': 'default', 'service_type': service}
    return agents._prepare_transactional_content(agent, deepcopy(config), base_url=BASE_URL,
                                                  deployment_id=deployment_id)


def fingerprint(prepared):
    return deployment_content_fingerprint(prepared['bundle'].manifest)


def files(prepared):
    with tarfile.open(fileobj=io.BytesIO(prepared['bundle'].archive), mode='r:gz') as archive:
        return {item['path']: archive.extractfile('files/' + item['path']).read()
                for item in prepared['bundle'].manifest['files']}


@pytest.mark.parametrize('service', ['mihomo', 'mosdns'])
def test_management_metadata_and_transport_settings_do_not_change_delivered_content(content_env, service):
    config, _remote, _fetches, _repository = content_env
    original = prepare(config, service)
    changed = deepcopy(config)
    changed.update(profile_name='Renamed label', updated_at='later', _revision=999,
                   backup={'schedule': 'daily'}, agents=[{'name': 'Agent label changed'}])
    changed['system_config']['rule_fetch_proxy'] = 'http://proxy.test:7890'
    current = prepare(changed, service, 'another-transaction')
    assert files(current) == files(original)
    assert fingerprint(current) == fingerprint(original)


@pytest.mark.parametrize('setting,affected', [('mihomo', 'mihomo'), ('mosdns', 'mosdns')])
def test_service_specific_configuration_only_changes_its_own_delivery(content_env, setting, affected):
    config, _remote, _fetches, _repository = content_env
    changed = deepcopy(config)
    if setting == 'mihomo':
        main = yaml.safe_load(changed['mihomo']['custom_config'])
        main['log-level'] = 'debug'
        changed['mihomo']['custom_config'] = yaml.safe_dump(main)
    else:
        changed['mosdns']['remote_dns'] = 'udp://192.0.2.99:53'
    for service in ('mihomo', 'mosdns'):
        original, current = prepare(config, service), prepare(changed, service)
        assert (fingerprint(original) != fingerprint(current)) is (service == affected)


@pytest.mark.parametrize('service', ['mihomo', 'mosdns'])
def test_selected_rule_bytes_change_fingerprint_without_changing_main_yaml(content_env, service):
    config, _remote, _fetches, _repository = content_env
    original = prepare(config, service)
    changed = deepcopy(config)
    changed['rule_library'][0]['content'] = 'DOMAIN-SUFFIX,second.test'
    current = prepare(changed, service)
    original_files, current_files = files(original), files(current)
    assert original_files['config.yaml'] == current_files['config.yaml']
    assert original_files != current_files
    assert fingerprint(original) != fingerprint(current)


@pytest.mark.parametrize('aggregation', [False, True], ids=['subscription', 'aggregation'])
def test_selected_provider_bytes_change_fingerprint_without_changing_main_yaml(content_env, aggregation):
    config, remote, _fetches, repository = content_env
    if aggregation:
        config['subscription_aggregations'] = [{
            'id': 'selected-aggregation', 'name': 'Aggregate', 'enabled': True,
            'subscriptions': ['selected-sub'], 'nodes': [],
        }]
        config['proxy_groups'][0].update(subscriptions=[], aggregations=['selected-aggregation'])
    original = prepare(config, 'mihomo')
    remote['provider'] = remote['provider'].replace('first.test', 'second.test')
    # Upstream changes are intentionally invisible while the source interval
    # is active, even when background content checks run repeatedly.
    assert fingerprint(prepare(config, 'mihomo')) == fingerprint(original)
    from backend.utils.subscription_refresh import _download_cache_path
    cache_path = _download_cache_path(config['subscriptions'][0]['url'])
    downloaded = repository.read_shared_json(cache_path)
    downloaded['fetched_at'] = '2000-01-01T00:00:00+00:00'
    repository.write_shared_json(cache_path, downloaded)
    current = prepare(config, 'mihomo')
    original_files, current_files = files(original), files(current)
    assert original_files['config.yaml'] == current_files['config.yaml']
    assert original_files != current_files
    assert fingerprint(original) != fingerprint(current)
    # Real provider rendering stages updates, but content inspection never publishes them.
    assert current['prepared_providers'][0]['cache_updates']
    assert not repository.shared_path('subscribes/selected-sub.json').exists()
    assert not repository.profile_path('default', 'providers/selected-aggregation.yaml').exists()


@pytest.mark.parametrize('service', ['mihomo', 'mosdns'])
def test_unused_resource_edits_do_not_change_or_fetch_delivered_content(content_env, service):
    config, _remote, fetches, _repository = content_env
    original = prepare(config, service)
    changed = deepcopy(config)
    changed['rule_library'][1]['content'] = 'DOMAIN-SUFFIX,changed-unused.test'
    changed['subscriptions'][1].update(name='Renamed unused feed', url='https://subscription.test/changed-unused')
    changed['nodes'].append({'id': 'unused-node', 'name': 'Unused node', 'type': 'http',
                             'server': 'unused.test', 'port': 8080, 'enabled': True})
    changed['subscription_aggregations'].append({
        'id': 'unused-aggregation', 'name': 'Unused aggregate', 'enabled': True,
        'subscriptions': ['unused-sub'], 'nodes': ['unused-node'],
    })
    current = prepare(changed, service)
    assert files(original) == files(current)
    assert fingerprint(original) == fingerprint(current)
    assert all(sub_id == 'selected-sub' for sub_id, _url in fetches)
    if service == 'mosdns':
        assert fetches == []


def test_mosdns_custom_file_change_is_included_even_when_main_yaml_is_unchanged(content_env):
    config, _remote, _fetches, _repository = content_env
    config['mosdns']['custom_hosts'] = '192.0.2.10 host.test'
    original = prepare(config, 'mosdns')
    config['mosdns']['custom_hosts'] = '192.0.2.11 host.test'
    current = prepare(config, 'mosdns')
    original_files, current_files = files(original), files(current)
    assert original_files['config.yaml'] == current_files['config.yaml']
    assert original_files['rules/custom_hosts.txt'] != current_files['rules/custom_hosts.txt']
    assert fingerprint(original) != fingerprint(current)


def test_background_bundle_helper_uses_the_same_complete_preparation(content_env):
    config, _remote, _fetches, _repository = content_env
    for service in ('mihomo', 'mosdns'):
        expected = prepare(config, service)
        agent = {'id': f'{service}-agent', 'profile_id': 'default', 'service_type': service}
        actual = agents._prepare_content_bundle(agent, deepcopy(config), base_url=BASE_URL,
                                                deployment_id='background-check')
        assert deployment_content_fingerprint(actual.manifest) == fingerprint(expected)


def test_optional_preparation_progress_keeps_the_same_bundle(content_env):
    config, _remote, _fetches, _repository = content_env
    expected = prepare(config, 'mosdns')
    progress = []
    actual = agents._prepare_transactional_content(
        {'id': 'mosdns-agent', 'profile_id': 'default', 'service_type': 'mosdns'},
        config, base_url=BASE_URL, progress=lambda done, total: progress.append((done, total)))
    assert fingerprint(actual) == fingerprint(expected)
    total = len(actual['ruleset_downloads'])
    assert progress == [(done, total) for done in range(total + 1)]


def test_named_preparation_progress_reports_real_subscription_rule_and_pack_objects(content_env):
    config, _remote, fetches, _repository = content_env
    phases, rules = [], []
    actual = agents._prepare_transactional_content(
        {'id': 'mihomo-agent', 'profile_id': 'default', 'service_type': 'mihomo'},
        config, base_url=BASE_URL,
        phase_progress=lambda message, **_kwargs: phases.append(message),
        detail_progress=lambda done, total, name, phase: rules.append((done, total, name, phase)))
    assert any('正在准备订阅「Feed」' in message for message in phases)
    assert any('校验节点来源与拨号引用关系' in message for message in phases)
    assert phases[-1] == f'正在打包发布文件（{len(actual["bundle"].manifest["files"])} 个文件）'
    assert fetches and rules
    assert any(name == 'Selected' and phase == 'preparing' for _, _, name, phase in rules)
    assert any(name == 'Selected' and done == total and phase == 'prepared' for done, total, name, phase in rules)
    assert not any('https://' in message for message in phases)
