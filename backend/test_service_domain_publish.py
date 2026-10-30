"""A domain change must reach the YAML delivered by a subsequent full publish."""
from copy import deepcopy
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

from backend.agents.rule_sync_store import bundle_files
from backend.common import config as config_module
from backend.common.config_repository import ProfileRepository
from backend.converters.oxidns import generate_oxidns_config
from backend.routes.agents import _prepare_transactional_content
from backend.test_profile_api import make_app
from backend.test_agent_content_preparation import content_env, prepare, files  # noqa: F401


OLD = 'http://old-panel.test:5010/old-prefix'
NEW = 'https://new-panel.test/new-prefix'


@pytest.mark.parametrize('service', ['mihomo', 'oxidns'])
def test_save_domain_then_republish_updates_runtime_rule_urls(tmp_path, monkeypatch, service):
    repository = ProfileRepository(tmp_path, default_config_factory=config_module.get_default_config,
                                   initial_config_factory=config_module.get_default_config)
    monkeypatch.setattr(config_module, '_repository', repository)
    config_module.reset_config_context()
    repository.save_system({'system_config': {'server_domain': OLD}})
    repository.save_shared({'rule_library': [{
        'id': 'local-rule', 'name': 'Local', 'source_type': 'content', 'behavior': 'domain',
        'format': 'text', 'content': '+.example.test', 'enabled': True,
    }]})
    for profile_id in ('home', 'office'):
        repository.create_profile({'id': profile_id, 'name': profile_id})
        repository.save_profile(profile_id, {'rule_configs': [{
            'id': 'selected', 'name': 'Local', 'itemType': 'ruleset', 'enabled': True,
            'library_rule_id': 'local-rule', 'behavior': 'domain', 'policy': 'DIRECT',
        }], 'oxidns': {'direct_rulesets': ['selected']}})
        config, _ = repository.get_publish_snapshot(profile_id)
        repository.save_profile(profile_id, {'oxidns': {
            **config['oxidns'], 'config_mode': 'native', 'native_config': generate_oxidns_config(config),
        }})
    saved_profiles = {profile_id: repository.get_profile(profile_id) for profile_id in ('home', 'office')}
    client = make_app(repository, monkeypatch).test_client()
    monkeypatch.setattr('requests.sessions.Session.request',
                        lambda *args, **kwargs: pytest.fail('Publication must materialize local rules locally'))
    try:
        response = client.post('/api/server-domain', json={'new_domain': NEW},
                               headers={'X-ConfigFlow-Profile': 'home'})
        assert response.status_code == 200
        for profile_id in ('home', 'office'):
            source, _ = repository.get_publish_snapshot(profile_id)
            original = deepcopy(source)
            agent = {'id': service + '-' + profile_id, 'service_type': service, 'profile_id': profile_id}
            prepared = _prepare_transactional_content(agent, source, base_url=OLD)
            bundle = prepared['bundle']
            files = bundle_files(bundle)
            runtime = yaml.safe_load(files[bundle.manifest['config_path']][0])
            if service == 'oxidns':
                download = next(p for p in runtime['plugins'] if p['tag'] == 'rules_download')['args']['downloads'][0]
                runtime_url = download['url']
                assert runtime_url == prepared['ruleset_downloads'][0]['url']
                nested = parse_qs(urlsplit(runtime_url).query)['url'][0]
                assert nested.startswith(NEW + '/api/profiles/' + profile_id + '/')
            else:
                runtime_url = runtime['rule-providers']['Local']['url']
                assert runtime_url == prepared['ruleset_downloads'][0]['url']
            assert runtime_url.startswith(NEW + '/api/profiles/' + profile_id + '/')
            assert OLD not in str(runtime)
            assert any(b'example.test' in content for content, role in files.values() if role == 'rule')
            assert source == original
            assert repository.get_profile(profile_id) == saved_profiles[profile_id]
    finally:
        config_module.reset_config_context()


@pytest.mark.parametrize('aggregation', [False, True], ids=['subscription', 'aggregation'])
def test_mihomo_republication_updates_subscription_and_rule_links(content_env, aggregation):
    config, remote, fetches, repository = content_env
    if aggregation:
        config['subscription_aggregations'] = [{
            'id': 'selected-aggregation', 'name': 'Aggregate', 'enabled': True,
            'subscriptions': ['selected-sub'], 'nodes': [],
        }]
        config['proxy_groups'][0].update(subscriptions=[], aggregations=['selected-aggregation'])
    before = yaml.safe_load(files(prepare(config, 'mihomo'))['config.yaml'])
    config['system_config']['server_domain'] = NEW
    after = yaml.safe_load(files(prepare(config, 'mihomo'))['config.yaml'])
    for section in ('proxy-providers', 'rule-providers'):
        assert after[section]
        assert set(before[section]) == set(after[section])
        for name, provider in after[section].items():
            assert provider['url'].startswith(NEW + '/api/profiles/default/')
            assert provider['url'] != before[section][name]['url']
    assert config['subscriptions'][0]['url'] == 'https://subscription.test/selected'
