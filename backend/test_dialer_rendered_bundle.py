"""Global activation is decided from exact rendered bytes before publication."""
from unittest.mock import Mock
import pytest
from backend.test_qa_integrity import make_app
from backend.test_dialer_proxy import node, save_fixture
from backend.test_dialer_delivery_snapshot import agent_manager

PATHS = ['/api/generate/mihomo/preview', '/api/generate/mihomo',
         '/api/config/mihomo', '/api/agents/a/push-config']
DUPLICATE = 'proxies: [{name: Same, type: http, server: a.test, port: 80}, {name: Same, type: http, server: b.test, port: 80}]'


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    network = Mock(side_effect=AssertionError('Provider bundle tests must never make real network requests'))
    monkeypatch.setattr('requests.sessions.Session.request', network)
    yield
    network.assert_not_called()


def setup_bundle(tmp_path, monkeypatch, source, reverse, *, chain=True, text=DUPLICATE):
    app, repo = make_app(tmp_path)
    shared = repo.get_shared()
    p = repo.get_profile('default')
    shared['nodes'] = [node('relay'), node('exit', proxy_string='http://fixture.invalid/exit'),
                  node('plain', proxy_string='http://fixture.invalid/plain')]
    shared['subscriptions'] = [{'id': 's', 'name': 'Feed', 'enabled': True, 'url': 'https://fixture.invalid/feed'}]
    shared['subscription_aggregations'] = [
        {'id': 'chain', 'name': 'Chain', 'nodes': ['exit'], 'subscriptions': []}]
    group = {'id': 'g', 'name': 'Entry', 'type': 'select', 'manual_nodes': ['relay'],
             'aggregations': ['chain']}
    if source == 'aggregation':
        shared['subscription_aggregations'].append(
            {'id': 'other', 'name': 'Other', 'nodes': ['plain'], 'subscriptions': ['s']})
        group['aggregations'].append('other')
    else:
        group['subscriptions'] = ['s']
    if reverse:
        shared['subscription_aggregations'].reverse()
    p['proxy_groups'] = [group]
    save_fixture(repo, shared, p)
    conversions = []
    def convert(uri):
        conversions.append(uri)
        return {'type': 'http', 'server': 'fixture.test', 'port': 80,
                **({'dialer-proxy': 'relay'} if chain and uri.endswith('/exit') else {})}
    monkeypatch.setattr('backend.utils.sub_store_client.convert_proxy_string', convert)
    fetch = Mock(return_value=(text, 'rendered_yaml'))
    monkeypatch.setattr('backend.routes.subscriptions.get_subscription_proxies_yaml', fetch)
    monkeypatch.setattr('backend.routes.aggregations.get_subscription_proxies_yaml', fetch)
    writes = Mock()
    monkeypatch.setattr('backend.utils.provider_delivery.commit_cache_updates', writes)
    monkeypatch.setattr('backend.utils.subscription_cache.save_subscription_nodes', writes)
    monkeypatch.setattr('backend.routes.subscriptions.save_subscription_nodes', writes)
    manager = agent_manager(monkeypatch)
    output = repo.write_generated('default', 'config.yaml', 'last-good-main')
    provider = repo.write_profile_text('default', 'providers/other.yaml', 'last-good-provider')
    before = repo.path.read_bytes()
    return app, repo, fetch, writes, manager, output, provider, before, conversions


@pytest.mark.parametrize('path', PATHS)
@pytest.mark.parametrize('source', ['aggregation', 'subscription'])
@pytest.mark.parametrize('reverse', [False, True])
def test_provider_only_chain_globally_rejects_plain_duplicates_before_publication(
        tmp_path, monkeypatch, path, source, reverse):
    app, repo, fetch, writes, manager, output, provider, before, _ = setup_bundle(
        tmp_path, monkeypatch, source, reverse)
    client = app.test_client()
    response = client.get(path) if path.startswith('/api/config') else client.post(path, json={'restart': False})
    assert response.status_code == 400, response.get_data(as_text=True)
    fetch.assert_called_once()
    writes.assert_not_called()
    manager.push_config_to_agent.assert_not_called()
    assert output.read_text() == 'last-good-main'
    assert provider.read_text() == 'last-good-provider'
    assert repo.path.read_bytes() == before


@pytest.mark.parametrize('path', PATHS)
@pytest.mark.parametrize('source', ['aggregation', 'subscription'])
@pytest.mark.parametrize('reverse', [False, True])
def test_no_chain_bundle_retains_legacy_duplicates(tmp_path, monkeypatch, path, source, reverse):
    app, _, fetch, _, manager, _, _, _, conversions = setup_bundle(
        tmp_path, monkeypatch, source, reverse, chain=False)
    client = app.test_client()
    response = client.get(path) if path.startswith('/api/config') else client.post(path, json={'restart': False})
    assert response.status_code == 200, response.get_data(as_text=True)
    # Pure legacy exports must not start fetching direct subscriptions.
    assert fetch.call_count == (1 if source == 'aggregation' or '/agents/' in path else 0)
    assert conversions.count('http://fixture.invalid/exit') == 1
    if source == 'aggregation':
        assert conversions.count('http://fixture.invalid/plain') == 1
    if '/agents/' in path:
        manager.push_config_to_agent.assert_called_once()


@pytest.mark.parametrize('path', PATHS)
@pytest.mark.parametrize('source', ['aggregation', 'subscription'])
@pytest.mark.parametrize('target', ['relay', 'Static', 'DIRECT'])
def test_valid_provider_bundle_uses_one_materialization(tmp_path, monkeypatch, path, source, target):
    text = f'proxies: [{{name: Remote, type: http, server: good.test, port: 80, dialer-proxy: {target}}}]'
    app, repo, fetch, _, manager, _, _, _, conversions = setup_bundle(
        tmp_path, monkeypatch, source, True, text=text)
    repo.update_profile_transaction('default', lambda p: p['proxy_groups'].append(
        {'id': 'static', 'name': 'Static', 'type': 'select', 'manual_nodes': ['relay']}))
    client = app.test_client()
    response = client.get(path) if path.startswith('/api/config') else client.post(path, json={'restart': False})
    assert response.status_code == 200, response.get_data(as_text=True)
    fetch.assert_called_once()
    assert conversions.count('http://fixture.invalid/exit') == 1
    if source == 'aggregation':
        assert conversions.count('http://fixture.invalid/plain') == 1
    if '/agents/' in path:
        import yaml
        delivered = manager.push_config_to_agent.call_args.kwargs['extra_data']['provider_downloads']
        remote = next(p for item in delivered for p in yaml.safe_load(item['content'])['proxies']
                      if p['name'] == 'Remote')
        assert remote['dialer-proxy'] == target


@pytest.mark.parametrize('path', PATHS)
@pytest.mark.parametrize('source', ['aggregation', 'subscription'])
@pytest.mark.parametrize('text', ['proxies: [{name: DIRECT, type: http, server: bad.test, port: 80}]',
                                  'proxies: [{name: null}]', 'proxies: [null]', 'proxies: ['])
def test_bundle_validation_does_not_weaken_reserved_names_or_content_errors(
        tmp_path, monkeypatch, path, source, text):
    app, repo, _, writes, manager, output, provider, before, _ = setup_bundle(
        tmp_path, monkeypatch, source, True, text=text)
    client = app.test_client()
    response = client.get(path) if path.startswith('/api/config') else client.post(path, json={'restart': False})
    assert response.status_code == 400, response.get_data(as_text=True)
    writes.assert_not_called()
    manager.push_config_to_agent.assert_not_called()
    assert output.read_text() == 'last-good-main'
    assert provider.read_text() == 'last-good-provider'
    assert repo.path.read_bytes() == before
