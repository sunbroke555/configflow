"""Actual provider bytes must pass before cache, artifact or Agent delivery."""
from unittest.mock import Mock
import pytest
import yaml
from backend.test_qa_integrity import make_app
from backend.test_dialer_proxy import node, save_fixture, chain_group
from backend.utils.subscription_cache import save_subscription_nodes, load_subscription_cache

GOOD = 'proxies: [{name: Remote, type: http, server: example.test, port: 80, dialer-proxy: relay}]'
BAD = GOOD.replace('dialer-proxy: relay', 'dialer-proxy: missing-target')


def seed_profile(repo, direct=True, chain='managed'):
    shared = repo.get_shared()
    p = repo.get_profile('default')
    shared['nodes'] = [node('relay'), node('exit')]
    if chain == 'raw':
        shared['nodes'][1] = node('exit', params={'dialer-proxy': 'relay'})
    elif chain == 'none':
        shared['nodes'][1] = node('exit')
    elif chain == 'uri':
        shared['nodes'][1] = node('exit', proxy_string='opaque://exit')
    shared['subscriptions'] = [{'id': 's', 'name': 'Feed', 'enabled': True, 'url': 'https://fixture.invalid/feed'}]
    shared['subscription_aggregations'] = [] if direct else [{'id': 'agg', 'name': 'Agg', 'subscriptions': ['s'], 'nodes': []}]
    p['proxy_groups'] = [{'id': 'g', 'name': 'Entry', 'type': 'select', 'manual_nodes': ['exit', 'relay'], 'subscriptions': ['s'] if direct else [], 'aggregations': [] if direct else ['agg']}]
    if chain == 'managed':
        p['proxy_groups'][0]['include_groups'] = ['chain-exit']
        p['proxy_groups'].append(chain_group('exit', {'type': 'node', 'id': 'relay'}))
    save_fixture(repo, shared, p)
    save_subscription_nodes('s', [node('old')])


def agent_manager(monkeypatch):
    manager = Mock()
    manager.get_agent_by_id.return_value = {'id': 'a', 'service_type': 'mihomo', 'profile_id': 'default'}
    manager.push_config_to_agent.return_value = {'success': True}
    monkeypatch.setattr('backend.routes.agents.get_agent_manager', lambda: manager)
    return manager


@pytest.mark.parametrize('chain', ['none', 'managed', 'raw', 'uri'])
@pytest.mark.parametrize('direct,bad', [(True, True), (False, True), (True, False)])
def test_exact_delivered_graph_is_validated_before_cache_and_agent_push(monkeypatch, tmp_path, direct, bad, chain):
    app, repo = make_app(tmp_path)
    seed_profile(repo, direct, chain)
    if chain == 'uri':
        monkeypatch.setattr('backend.utils.sub_store_client.convert_proxy_string',
            lambda _s: {'name': 'exit', 'type': 'http', 'server': 'example.test', 'port': 80, 'dialer-proxy': 'relay'})
    before = load_subscription_cache('s')
    profile_before = repo.path.read_bytes()
    artifact = repo.write_generated('default', 'config.yaml', 'last-good-main')
    provider = repo.write_profile_text('default', 'providers/agg.yaml', 'last-good-provider')
    manager = agent_manager(monkeypatch)
    responses = [(BAD if bad else GOOD, 'rendered_yaml')]
    fetch = Mock(side_effect=responses)
    target = 'subscriptions' if direct else 'aggregations'
    monkeypatch.setattr(f'backend.routes.{target}.get_subscription_proxies_yaml', fetch)
    network = Mock(side_effect=AssertionError('No self-HTTP or Agent network before validation'))
    monkeypatch.setattr('requests.get', network)
    response = app.test_client().post('/api/agents/a/push-config', json={'restart': False})
    assert repo.path.read_bytes() == profile_before
    assert artifact.read_text() == 'last-good-main'
    network.assert_not_called()
    assert fetch.call_count == len(responses)
    if bad:
        assert response.status_code == 400
        manager.push_config_to_agent.assert_not_called()
        assert load_subscription_cache('s') == before
        assert provider.read_text() == 'last-good-provider'
    else:
        assert response.status_code == 200
        manager.push_config_to_agent.assert_called_once()
        delivered = manager.push_config_to_agent.call_args.kwargs['extra_data']['provider_downloads'][0]['content']
        assert yaml.safe_load(delivered) == yaml.safe_load(GOOD)
        assert load_subscription_cache('s')['nodes'][0]['params']['dialer-proxy'] == 'relay'


@pytest.mark.parametrize('chain', ['none', 'managed', 'raw', 'uri'])
@pytest.mark.parametrize('content', [BAD, 'proxies: [null]', 'proxies: [{name: [invalid]}]', 'proxies: not-an-array', 'proxies: [{name: Remote, type: http, dialer-proxy: relay, proxies: null}]'])
def test_standalone_actual_raw_provider_invalid_metadata_is_400_without_cache_mutation(tmp_path, monkeypatch, chain, content):
    app, repo = make_app(tmp_path)
    seed_profile(repo, chain=chain)
    if chain == 'uri':
        monkeypatch.setattr('backend.utils.sub_store_client.convert_proxy_string',
            lambda _s: {'name': 'exit', 'type': 'http', 'server': 'example.test', 'port': 80, 'dialer-proxy': 'relay'})
    before = load_subscription_cache('s')
    monkeypatch.setattr('backend.routes.subscriptions.get_subscription_proxies_yaml', Mock(return_value=(content, 'sub_store')))
    response = app.test_client().get('/api/subscriptions/s/proxies')
    assert response.status_code == 400, response.get_data(as_text=True)
    assert load_subscription_cache('s') == before


def test_snapshot_is_not_redirected_by_profile_or_main_mutation(tmp_path, monkeypatch):
    app, repo = make_app(tmp_path)
    seed_profile(repo)
    repo.create_profile({'id': 'other', 'name': 'Other'}, clone_from='default')
    manager = agent_manager(monkeypatch)
    calls = []
    def fetch(*_args):
        calls.append(1)
        if len(calls) == 1:
            def change_groups(p):
                p['proxy_groups'] = [p['proxy_groups'][0]]
                p['proxy_groups'][0].pop('include_groups', None)
                p['proxy_groups'][0]['manual_nodes'] = ['exit']
                p['proxy_groups'][0]['name'] = 'Changed'
            repo.update_profile_transaction('default', change_groups)
            repo.update_profile_transaction('other', lambda p: p['proxy_groups'][0].update(name='Other'))
        return GOOD, 'rendered_yaml'
    monkeypatch.setattr('backend.routes.subscriptions.get_subscription_proxies_yaml', fetch)
    response = app.test_client().post('/api/agents/a/push-config', json={'restart': False}, headers={'X-ConfigFlow-Profile': 'other'})
    assert response.status_code == 200, response.get_data(as_text=True)
    main = yaml.safe_load(manager.push_config_to_agent.call_args.args[1])
    assert any(p['name'] == 'relay' for p in main['proxies'])
    proxies = {p['name']: p for p in main['proxies']}
    assert proxies['Via exit']['dialer-proxy'] == 'relay'
    assert 'dialer-proxy' not in proxies['exit']
    assert load_subscription_cache('s')['nodes'][0]['name'] == 'Remote'
    assert main['proxy-groups'][0]['name'] == 'Entry'
    assert repo.get_profile('default')['proxy_groups'][0]['manual_nodes'] == ['exit']
    assert len(repo.get_profile('default')['proxy_groups']) == 1
    assert repo.get_profile('other')['proxy_groups'][0]['name'] == 'Other'
    assert manager.push_config_to_agent.call_args.kwargs['extra_data']['provider_downloads'][0]['url'].find('/profiles/default/') != -1
