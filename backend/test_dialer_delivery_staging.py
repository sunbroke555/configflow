"""Delivery stages all provider caches before publication."""
from unittest.mock import Mock
import pytest
from pathlib import Path
from backend.test_dialer_delivery_snapshot import seed_profile, agent_manager, GOOD, BAD
from backend.test_qa_integrity import make_app
from backend.test_dialer_proxy import node, save_fixture
from backend.utils.subscription_cache import save_subscription_nodes, load_subscription_cache


def test_later_invalid_provider_preserves_all_earlier_staged_caches(tmp_path, monkeypatch):
    app, repo = make_app(tmp_path)
    seed_profile(repo)
    shared = repo.get_shared()
    p = repo.get_profile('default')
    shared['subscriptions'].append({'id': 's2', 'name': 'Second', 'url': 'https://fixture.invalid/second'})
    p['proxy_groups'][0]['subscriptions'].append('s2')
    save_fixture(repo, shared, p)
    save_subscription_nodes('s2', [node('old-second')])
    before = {s: load_subscription_cache(s) for s in ('s', 's2')}
    counts = {'s': 0, 's2': 0}
    def fetch(sub_id, _url):
        counts[sub_id] += 1
        return (BAD if sub_id == 's2' else GOOD), 'rendered_yaml'
    monkeypatch.setattr('backend.routes.subscriptions.get_subscription_proxies_yaml', fetch)
    manager = agent_manager(monkeypatch)
    response = app.test_client().post('/api/agents/a/push-config', json={'restart': False})
    assert response.status_code == 400
    manager.push_config_to_agent.assert_not_called()
    assert counts == {'s': 1, 's2': 1}
    assert before == {s: load_subscription_cache(s) for s in ('s', 's2')}


@pytest.mark.parametrize('chain,expected', [('none', 200), ('managed', 400)])
def test_unavailable_subscription_retains_only_legacy_no_chain_url_fallback(tmp_path, monkeypatch, chain, expected):
    import requests
    app, repo = make_app(tmp_path)
    seed_profile(repo, chain=chain)
    # No cache exists for this subscription.
    from backend.utils.subscription_cache import _get_cache_path
    Path(_get_cache_path('s')).unlink()
    monkeypatch.setattr('backend.routes.subscriptions.get_subscription_proxies_yaml',
                        Mock(side_effect=requests.Timeout('unavailable fixture')))
    manager = agent_manager(monkeypatch)
    response = app.test_client().post('/api/agents/a/push-config', json={'restart': False})
    assert response.status_code == expected
    if expected == 200:
        delivered = manager.push_config_to_agent.call_args.kwargs['extra_data']['provider_downloads'][0]
        assert delivered['content'] == ''
        assert delivered['url'].endswith('/subscriptions/s/proxies')
    else:
        manager.push_config_to_agent.assert_not_called()


def test_provider_discovered_chain_cannot_use_unvalidated_url_fallback(tmp_path, monkeypatch):
    import requests
    app, repo = make_app(tmp_path)
    seed_profile(repo, chain='none')
    shared = repo.get_shared()
    p = repo.get_profile('default')
    shared['subscriptions'].append({'id': 's2', 'name': 'Missing', 'url': 'https://fixture.invalid/missing'})
    p['proxy_groups'][0]['subscriptions'].insert(0, 's2')
    save_fixture(repo, shared, p)
    before = load_subscription_cache('s')
    def fetch(sub_id, _url):
        if sub_id == 's2':
            raise requests.Timeout('unavailable fixture')
        return GOOD, 'rendered_yaml'
    monkeypatch.setattr('backend.routes.subscriptions.get_subscription_proxies_yaml', fetch)
    manager = agent_manager(monkeypatch)
    response = app.test_client().post('/api/agents/a/push-config', json={'restart': False})
    assert response.status_code == 400
    manager.push_config_to_agent.assert_not_called()
    assert load_subscription_cache('s') == before


def test_late_chain_activates_duplicate_checks_for_earlier_actual_provider(tmp_path, monkeypatch):
    app, repo = make_app(tmp_path)
    seed_profile(repo, chain='none')
    shared = repo.get_shared()
    p = repo.get_profile('default')
    shared['subscriptions'].append({'id': 's2', 'name': 'Chain', 'url': 'https://fixture.invalid/chain'})
    p['proxy_groups'][0]['subscriptions'].append('s2')
    save_fixture(repo, shared, p)
    before = load_subscription_cache('s')
    duplicate = 'proxies: [{name: Same, type: http, server: a.test, port: 80}, {name: Same, type: http, server: b.test, port: 80}]'
    monkeypatch.setattr('backend.routes.subscriptions.get_subscription_proxies_yaml',
        lambda sub_id, _url: (duplicate if sub_id == 's' else GOOD, 'rendered_yaml'))
    manager = agent_manager(monkeypatch)
    response = app.test_client().post('/api/agents/a/push-config', json={'restart': False})
    assert response.status_code == 400
    manager.push_config_to_agent.assert_not_called()
    assert load_subscription_cache('s') == before


def test_no_chain_direct_subscription_preserves_duplicate_legacy_names(tmp_path, monkeypatch):
    from backend.utils.provider_delivery import DeliverySnapshot, prepare_subscription_provider
    from backend.common.config import get_config
    import yaml
    app, repo = make_app(tmp_path)
    seed_profile(repo, chain='none')
    profile = get_config('default')
    text = 'proxies: [{name: Same, type: http, server: a.test, port: 80}, {name: Same, type: http, server: b.test, port: 80}]'
    snapshot = DeliverySnapshot.capture('default', {'proxies': [], 'proxy-groups': []})
    result = prepare_subscription_provider(profile['subscriptions'][0], profile, snapshot,
                                           fetch=Mock(return_value=(text, 'rendered_yaml')))
    proxies = yaml.safe_load(result['content'])['proxies']
    assert [p['server'] for p in proxies] == ['a.test', 'b.test']
