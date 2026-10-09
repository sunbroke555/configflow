"""Invalid fetched content must never be mistaken for transport fallback."""
from unittest.mock import Mock
import pytest
from backend.test_qa_integrity import make_app
from backend.test_dialer_delivery_snapshot import seed_profile, agent_manager
from backend.utils.subscription_cache import load_subscription_cache


@pytest.mark.parametrize('direct', [True, False])
@pytest.mark.parametrize('consumer', ['preview', 'generate', 'download', 'provider', 'push'])
@pytest.mark.parametrize('content', ['proxies: [ {name: unterminated', '- not-a-root-object', 'proxies: [null]', 'proxies: not-an-array', 'proxies: [{name: Remote, type: []}]'])
def test_invalid_provider_content_rejected_without_fallback(tmp_path, monkeypatch, direct, consumer, content):
    app, repo = make_app(tmp_path)
    seed_profile(repo, direct=direct)
    monkeypatch.setattr(f'backend.routes.{"subscriptions" if direct else "aggregations"}.get_subscription_proxies_yaml', Mock(return_value=(content, 'rendered_yaml')))
    manager = agent_manager(monkeypatch)
    network = Mock(side_effect=AssertionError('No network'))
    monkeypatch.setattr('requests.get', network)
    cache = load_subscription_cache('s')
    profile = repo.path.read_bytes()
    artifacts = [repo.write_profile_text('default', name, 'last-good') for name in ('generated/config.yaml', 'providers/agg.yaml')]
    path = {'preview': '/api/generate/mihomo/preview', 'generate': '/api/generate/mihomo', 'download': '/api/config/mihomo', 'provider': '/api/subscriptions/s/proxies' if direct else '/api/aggregations/agg/provider', 'push': '/api/agents/a/push-config'}[consumer]
    client = app.test_client()
    response = client.get(path) if consumer in ('download', 'provider') else client.post(path, json={'restart': False})
    assert response.status_code == 400, response.get_data(as_text=True)
    manager.push_config_to_agent.assert_not_called()
    network.assert_not_called()
    assert load_subscription_cache('s') == cache
    assert repo.path.read_bytes() == profile
    assert all(p.read_text() == 'last-good' for p in artifacts)


@pytest.mark.parametrize('chain', ['managed', 'none'])
@pytest.mark.parametrize('direct', [True, False])
def test_transport_failure_uses_valid_cache(tmp_path, monkeypatch, chain, direct):
    app, repo = make_app(tmp_path)
    seed_profile(repo, direct=direct, chain=chain)
    monkeypatch.setattr(f'backend.routes.{"subscriptions" if direct else "aggregations"}.get_subscription_proxies_yaml', Mock(side_effect=ConnectionError('transport unavailable')))
    manager = agent_manager(monkeypatch)
    cache = load_subscription_cache('s')
    response = app.test_client().post('/api/agents/a/push-config', json={'restart': False})
    assert response.status_code == 200, response.get_data(as_text=True)
    manager.push_config_to_agent.assert_called_once()
    assert load_subscription_cache('s') == cache


@pytest.mark.parametrize('content', ['{}', 'proxies: []'])
def test_empty_provider_is_valid(content):
    from backend.utils.provider_delivery import parse_provider_proxies
    assert parse_provider_proxies(content) == []
