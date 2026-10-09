"""Standalone consumers activate from the actual main conversion, never metadata."""
from unittest.mock import Mock

import pytest
import yaml

from backend.test_dialer_delivery_snapshot import seed_profile
from backend.test_dialer_proxy import node
from backend.test_qa_integrity import make_app
from backend.utils.subscription_cache import load_subscription_cache, save_subscription_nodes


def plain(name):
    return {'name': name, 'type': 'http', 'server': 'fixture.test', 'port': 80}


def setup(tmp_path, monkeypatch, *, chain=True, profile='default'):
    app, repo = make_app(tmp_path)
    seed_profile(repo, direct=False, chain='uri')
    if profile != 'default':
        repo.create_profile({'id': profile, 'name': profile}, clone_from='default')
    convert = Mock(return_value={**plain('exit'), **({'dialer-proxy': 'relay'} if chain else {})})
    monkeypatch.setattr('backend.utils.sub_store_client.convert_proxy_string', convert)
    network = Mock(side_effect=AssertionError('No unrelated provider/self-HTTP fetch'))
    monkeypatch.setattr('requests.sessions.Session.request', network)
    return app, repo, convert, network


def install_fetch(monkeypatch, proxies, *, fallback=False):
    fetch = Mock(side_effect=OSError('offline')) if fallback else Mock(
        return_value=(yaml.safe_dump({'proxies': proxies}), 'rendered_yaml'))
    for module in ('subscriptions', 'aggregations'):
        monkeypatch.setattr(f'backend.routes.{module}.get_subscription_proxies_yaml', fetch)
    return fetch


def call(client, consumer, profile):
    prefix = '' if profile == 'default' else '/profiles/' + profile
    path = {
        'aggregation': f'/api{prefix}/aggregations/agg/provider',
        'aggregation-preview': f'/api{prefix}/aggregations/agg/preview',
        'subscription': f'/api{prefix}/subscriptions/s/proxies',
        'combined': f'/api{prefix}/subscriptions/proxies',
        'main-preview': f'/api{prefix}/generate/mihomo/preview',
    }[consumer]
    # Only individual providers/main preview have explicit profile aliases.
    headers = {'X-ConfigFlow-Profile': profile}
    if consumer in ('combined', 'aggregation-preview'):
        path = path.replace(prefix, '', 1) if prefix else path
    return client.post(path, json={}, headers=headers) if consumer == 'main-preview' else client.get(path, headers=headers)


@pytest.mark.parametrize('profile', ['default', 'other'])
@pytest.mark.parametrize('consumer', ['aggregation', 'aggregation-preview', 'subscription', 'combined', 'main-preview'])
@pytest.mark.parametrize('case', ['duplicate', 'reserved', 'malformed'])
def test_uri_main_chain_rejects_plain_provider_before_any_publication(tmp_path, monkeypatch, profile, consumer, case):
    app, repo, convert, network = setup(tmp_path, monkeypatch, profile=profile)
    proxies = {'duplicate': [plain('Same'), plain('Same')],
               'reserved': [plain('DIRECT')], 'malformed': [{'name': None}]}[case]
    fetch = install_fetch(monkeypatch, proxies)
    before = load_subscription_cache('s')
    document_before = repo.path.read_bytes()
    artifacts = [repo.write_profile_text(p, 'providers/agg.yaml', 'last-good') for p in {'default', profile}]
    writes = Mock(side_effect=AssertionError('Must validate before cache/artifact publication'))
    monkeypatch.setattr(repo, 'write_profile_text', writes)
    for target in ('backend.utils.subscription_cache.save_subscription_nodes',
                   'backend.routes.subscriptions.save_subscription_nodes',
                   'backend.utils.provider_delivery.commit_cache_updates'):
        monkeypatch.setattr(target, writes)
    response = call(app.test_client(), consumer, profile)
    assert response.status_code == 400, response.get_data(as_text=True)
    fetch.assert_called_once()
    convert.assert_called_once_with('opaque://exit')
    writes.assert_not_called()
    network.assert_not_called()
    assert load_subscription_cache('s') == before
    assert repo.path.read_bytes() == document_before
    assert all(p.read_text() == 'last-good' for p in artifacts)


@pytest.mark.parametrize('consumer', ['aggregation', 'subscription', 'combined'])
@pytest.mark.parametrize('fallback', [False, True])
@pytest.mark.parametrize('chain', [False, True])
def test_standalone_uri_activation_and_transport_cache_controls(tmp_path, monkeypatch, consumer, fallback, chain):
    app, repo, convert, network = setup(tmp_path, monkeypatch, chain=chain)
    proxies = [plain('Same'), plain('Same'), plain('DIRECT')]
    if fallback:
        save_subscription_nodes('s', [node('Same'), node('Same'), node('DIRECT')])
    before = load_subscription_cache('s')
    fetch = install_fetch(monkeypatch, proxies, fallback=fallback)
    artifact = repo.write_profile_text('default', 'providers/agg.yaml', 'last-good')
    response = call(app.test_client(), consumer, 'default')
    assert response.status_code == (400 if chain else 200), response.get_data(as_text=True)
    fetch.assert_called_once()
    convert.assert_called_once_with('opaque://exit')
    network.assert_not_called()
    if chain or fallback or consumer == 'combined':
        assert load_subscription_cache('s') == before
    if chain:
        assert artifact.read_text() == 'last-good'
    else:
        assert [p['name'] for p in yaml.safe_load(response.get_data(as_text=True))['proxies']] == ['Same', 'Same', 'DIRECT']


@pytest.mark.parametrize('case', ['duplicate', 'reserved'])
def test_direct_cached_surge_cannot_bypass_actual_main_activation(tmp_path, monkeypatch, case):
    app, repo, convert, network = setup(tmp_path, monkeypatch)
    nodes = [node('Same'), node('Same')] if case == 'duplicate' else [node('DIRECT')]
    save_subscription_nodes('s', nodes)
    before = load_subscription_cache('s')
    fetch = install_fetch(monkeypatch, [], fallback=True)
    response = app.test_client().get('/api/subscriptions/s/proxies?format=surge')
    assert response.status_code == 400, response.get_data(as_text=True)
    fetch.assert_called_once()
    convert.assert_called_once_with('opaque://exit')
    network.assert_not_called()
    assert load_subscription_cache('s') == before


@pytest.mark.parametrize('consumer', ['aggregation', 'subscription', 'combined'])
def test_valid_uri_main_chain_standalone_uses_single_materialization(tmp_path, monkeypatch, consumer):
    app, _, convert, network = setup(tmp_path, monkeypatch)
    fetch = install_fetch(monkeypatch, [plain('Remote')])
    response = call(app.test_client(), consumer, 'default')
    assert response.status_code == 200, response.get_data(as_text=True)
    fetch.assert_called_once()
    convert.assert_called_once_with('opaque://exit')
    network.assert_not_called()
    assert yaml.safe_load(response.get_data(as_text=True))['proxies'] == [plain('Remote')]
