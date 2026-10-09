"""Permanent regressions for independently reproduced dialer review blockers."""
from unittest.mock import Mock
import pytest
from backend.test_qa_integrity import make_app
from backend.test_dialer_proxy import node, save_fixture, chain_group, add_subscription, seed_nodes


def aggregation_fixture(tmp_path, monkeypatch):
    app, repo = make_app(tmp_path)
    shared = repo.get_shared()
    p = repo.get_profile('default')
    shared['nodes'] = [node('exit', proxy_string='http://fixture.invalid:80')]
    shared['subscription_aggregations'] = [{'id': 'agg', 'name': 'Agg', 'enabled': True, 'nodes': ['exit'], 'subscriptions': []}]
    p['proxy_groups'] = [{'id': 'g', 'name': 'Entry', 'type': 'select', 'aggregations': ['agg']}]
    save_fixture(repo, shared, p)
    monkeypatch.setattr('backend.utils.sub_store_client.convert_proxy_string', lambda s: {'type': 'http', 'server': 'example.test', 'port': 80, 'dialer-proxy': 'gone'})
    return app, repo


@pytest.mark.parametrize('path', ['/api/generate/mihomo/preview', '/api/generate/mihomo', '/api/config/mihomo', '/api/agents/a/push-config'])
def test_invalid_final_provider_rejects_all_consumers(tmp_path, monkeypatch, path):
    app, repo = aggregation_fixture(tmp_path, monkeypatch)
    manager = Mock()
    manager.get_agent_by_id.return_value = {'id': 'a', 'service_type': 'mihomo', 'profile_id': 'default'}
    monkeypatch.setattr('backend.routes.agents.get_agent_manager', lambda: manager)
    output = repo.write_generated('default', 'config.yaml', 'known good')
    provider = repo.write_profile_text('default', 'providers/agg.yaml', 'known good provider')
    before = repo.path.read_bytes()
    client = app.test_client()
    response = client.get(path) if path.startswith('/api/config') else client.post(path, json={'restart': False})
    assert response.status_code == 400, response.get_data(as_text=True)
    assert output.read_text() == 'known good'
    assert provider.read_text() == 'known good provider'
    assert repo.path.read_bytes() == before
    manager.push_config_to_agent.assert_not_called()


@pytest.mark.parametrize('consumer', ['main', 'provider'])
def test_chain_exit_conversion_failure_is_not_silently_dropped(tmp_path, monkeypatch, consumer):
    app, repo = make_app(tmp_path)
    shared = repo.get_shared()
    p = repo.get_profile('default')
    shared['nodes'] = [node('relay'), node('exit', proxy_string='http://fixture.invalid:80')]
    p['proxy_groups'] = [{'id': 'g', 'name': 'Entry', 'type': 'select', 'manual_nodes': ['exit'],
                          'include_groups': ['chain-exit'], 'aggregations': ['agg']}]
    shared['subscription_aggregations'] = [{'id': 'agg', 'name': 'Agg', 'nodes': ['exit'], 'subscriptions': []}]
    p['proxy_groups'].append(chain_group('exit', {'type': 'node', 'id': 'relay'}))
    save_fixture(repo, shared, p)
    monkeypatch.setattr('backend.utils.sub_store_client.convert_proxy_string', lambda s: None)
    output = repo.write_generated('default', 'config.yaml', 'known good')
    before = repo.get_profile('default')
    client = app.test_client()
    response = client.post('/api/generate/mihomo', json={}) if consumer == 'main' else client.get('/api/aggregations/agg/provider')
    assert response.status_code == 400
    assert output.read_text() == 'known good'
    assert repo.get_profile('default') == before


@pytest.mark.parametrize('payload', ['null', '[]', '5', '"bad"'])
def test_bad_node_put_never_means_delete(tmp_path, payload):
    app, repo = make_app(tmp_path)
    client = app.test_client()
    assert client.post('/api/nodes', json=node('victim')).status_code == 200
    before = repo.path.read_bytes()
    response = client.put('/api/nodes/victim', data=payload, content_type='application/json')
    assert response.status_code == 400
    assert repo.path.read_bytes() == before


def test_no_dialer_legacy_duplicate_names_still_generate(tmp_path):
    app, repo = make_app(tmp_path)
    client = app.test_client()
    for payload in [node('a', 'Same'), node('b', 'Same')]:
        assert client.post('/api/nodes', json=payload).status_code == 200
    assert client.post('/api/proxy-groups', json={'id': 'g', 'name': 'Entry', 'type': 'select', 'manual_nodes': ['a', 'b']}).status_code == 200
    response = client.post('/api/generate/mihomo/preview', json={})
    assert response.status_code == 200
    import yaml
    assert [p['name'] for p in yaml.safe_load(response.get_json()['content'])['proxies']] == ['Same', 'Same']


def test_invalid_chain_rejected_before_network_cache_or_provider_write(tmp_path, monkeypatch):
    app, repo = make_app(tmp_path)
    shared = repo.get_shared()
    p = repo.get_profile('default')
    shared['nodes'] = [node('exit')]
    shared['subscriptions'] = [{'id': 's', 'name': 'S', 'enabled': True, 'url': 'https://fixture.invalid/feed'}]
    shared['subscription_aggregations'] = [{'id': 'agg', 'name': 'Agg', 'nodes': ['exit'], 'subscriptions': ['s']}]
    save_fixture(repo, shared, p)
    from backend.utils.subscription_cache import save_subscription_nodes, load_subscription_cache
    save_subscription_nodes('s', [node('cached')])
    cache_before = load_subscription_cache('s')
    fetch = Mock(return_value=('proxies: [{name: Snode, type: http, server: example.test, port: 80}]', 'rendered_yaml'))
    cache_write = Mock()
    monkeypatch.setattr('backend.routes.aggregations.get_subscription_proxies_yaml', fetch)
    monkeypatch.setattr('backend.utils.subscription_cache.save_subscription_nodes', cache_write)
    before = repo.path.read_bytes()
    provider = repo.write_profile_text('default', 'providers/agg.yaml', 'known good')
    response = app.test_client().post('/api/proxy-groups', json=chain_group('exit', {'type': 'node', 'id': 'gone'}))
    assert response.status_code == 409
    fetch.assert_not_called()
    cache_write.assert_not_called()
    assert repo.path.read_bytes() == before
    assert provider.read_text() == 'known good'
    assert load_subscription_cache('s') == cache_before


def test_prefetch_nonprovider_http400_preserves_transport_fallback(monkeypatch):
    import requests
    from backend.routes.agents import _prefetch_download_contents
    response = requests.Response()
    response.status_code = 400
    monkeypatch.setattr('requests.get', lambda *a, **kw: response)
    downloads = [{'name': 'rules', 'url': 'https://fixture.invalid/rules'}]
    _prefetch_download_contents(downloads, '')
    assert downloads[0]['content'] == ''


def test_no_chain_generation_does_not_add_provider_fetches(tmp_path, monkeypatch):
    app, repo = make_app(tmp_path)
    shared = repo.get_shared()
    p = repo.get_profile('default')
    shared['nodes'] = [node('plain')]
    shared['subscriptions'] = [{'id': 's', 'name': 'S', 'enabled': True, 'url': 'https://fixture.invalid/feed'}]
    shared['subscription_aggregations'] = [{'id': 'agg', 'name': 'Agg', 'nodes': ['plain'], 'subscriptions': ['s']}]
    p['proxy_groups'] = [{'id': 'g', 'name': 'Entry', 'type': 'select', 'aggregations': ['agg']}]
    save_fixture(repo, shared, p)
    fetch = Mock(return_value=('proxies: []', 'rendered_yaml'))
    monkeypatch.setattr('backend.routes.aggregations.get_subscription_proxies_yaml', fetch)
    response = app.test_client().post('/api/generate/mihomo/preview', json={})
    assert response.status_code == 200
    fetch.assert_not_called()


def test_invalid_converted_provider_does_not_commit_subscription_cache(tmp_path, monkeypatch):
    app, repo = aggregation_fixture(tmp_path, monkeypatch)
    shared = repo.get_shared()
    p = repo.get_profile('default')
    shared['subscriptions'] = [{'id': 's', 'name': 'S', 'enabled': True, 'url': 'https://fixture.invalid/feed'}]
    shared['subscription_aggregations'][0]['subscriptions'] = ['s']
    save_fixture(repo, shared, p)
    fetch = Mock(return_value=('proxies: [{name: Other, type: http, server: example.test, port: 80}]', 'rendered_yaml'))
    cache_write = Mock()
    monkeypatch.setattr('backend.routes.aggregations.get_subscription_proxies_yaml', fetch)
    monkeypatch.setattr('backend.utils.subscription_cache.save_subscription_nodes', cache_write)
    response = app.test_client().get('/api/aggregations/agg/provider')
    assert response.status_code == 400
    assert fetch.call_count == 1
    cache_write.assert_not_called()


def test_late_provider_validation_400_blocks_agent_push(tmp_path, monkeypatch):
    import requests
    app, repo = make_app(tmp_path)
    shared = repo.get_shared()
    p = repo.get_profile('default')
    shared['nodes'] = [node('plain')]
    shared['subscription_aggregations'] = [{'id': 'agg', 'name': 'Agg', 'nodes': ['plain'], 'subscriptions': []}]
    p['proxy_groups'] = [{'id': 'g', 'name': 'Entry', 'type': 'select', 'aggregations': ['agg']}]
    save_fixture(repo, shared, p)
    manager = Mock()
    manager.get_agent_by_id.return_value = {'id': 'a', 'service_type': 'mihomo', 'profile_id': 'default'}
    monkeypatch.setattr('backend.routes.agents.get_agent_manager', lambda: manager)
    # Delivery now renders locally against an explicit snapshot rather than
    # refetching an HTTP route with independently resolved profile state.
    from backend.utils.strategy_references import StrategyReferenceError
    def reject(*args, **kwargs):
        raise StrategyReferenceError('Provider validation failed')
    monkeypatch.setattr('backend.routes.aggregations.generate_aggregation_provider', reject)
    before = repo.path.read_bytes()
    result = app.test_client().post('/api/agents/a/push-config', json={'restart': False})
    assert result.status_code == 400
    manager.push_config_to_agent.assert_not_called()
    assert repo.path.read_bytes() == before


@pytest.mark.parametrize('failure', ['timeout', 'http500'])
def test_provider_transport_failures_retain_url_fallback(monkeypatch, failure):
    import requests
    from backend.routes.agents import _prefetch_download_contents
    response = requests.Response()
    response.status_code = 500
    def fetch(*args, **kw):
        if failure == 'timeout':
            raise requests.Timeout('fixture')
        return response
    monkeypatch.setattr('requests.get', fetch)
    downloads = [{'name': 'Provider', 'url': 'https://fixture.invalid/provider'}]
    _prefetch_download_contents(downloads, '', validation_urls={downloads[0]['url']})
    assert downloads[0]['content'] == ''


def test_emitted_dialer_graph_checks_ordinary_group_members():
    from backend.utils.dialer_references import validate_emitted, DialerReferenceError
    with pytest.raises(DialerReferenceError, match='未生成'):
        validate_emitted({'proxies': [{'name': 'exit', 'dialer-proxy': 'relay'}, {'name': 'relay'}],
                          'proxy-groups': [{'name': 'Entry', 'proxies': ['gone']}]})


def test_raw_name_subscription_metadata_retains_separate_compatibility_mode(tmp_path):
    app, repo = make_app(tmp_path)
    client = app.test_client()
    add_subscription(repo, {'id': 'old-sub', 'name': 'Old', 'url': ''})
    assert client.post('/api/nodes', json=node('relay', subscription_id='old-sub')).status_code == 200
    assert client.post('/api/nodes', json=node('exit', params={'dialer-proxy': 'relay'})).status_code == 200
    assert client.post('/api/proxy-groups', json={'id': 'g', 'name': 'Entry', 'type': 'select',
        'manual_nodes': ['relay', 'exit'], 'subscriptions': ['old-sub']}).status_code == 200
    assert repo.get_compat_config('default')['nodes'][1].get('dialer_ref') is None
    assert 'node_dialers' not in repo.get_profile('default')
    assert all(g['type'] != 'chain' for g in repo.get_profile('default')['proxy_groups'])


@pytest.mark.parametrize('format', ['mihomo', 'surge'])
@pytest.mark.parametrize('bad', [{'params': None}, {'proxy_string': 123}, {'id': ['cached']}])
def test_subscription_cached_metadata_validation_is_not_swallowed(tmp_path, format, bad):
    from backend.utils.subscription_cache import save_subscription_nodes
    app, repo = make_app(tmp_path)
    add_subscription(repo, {'id': 's', 'name': 'Cached', 'enabled': True, 'url': ''})
    save_subscription_nodes('s', [{**node('cached'), **bad}])
    before = repo.path.read_bytes()
    response = app.test_client().get('/api/subscriptions/s/proxies?format=' + format)
    assert response.status_code == 400
    assert repo.path.read_bytes() == before


@pytest.mark.parametrize('bad', [{'params': None}, {'proxy_string': 123}, {'id': ['cached']}, {'name': ['Cached']}])
def test_aggregation_cached_metadata_rejects_before_conversion(tmp_path, monkeypatch, bad):
    from backend.utils.subscription_cache import save_subscription_nodes
    app, repo = make_app(tmp_path)
    shared = repo.get_shared()
    p = repo.get_profile('default')
    shared['subscriptions'] = [{'id': 's', 'name': 'Cached', 'enabled': True, 'url': ''}]
    shared['subscription_aggregations'] = [{'id': 'agg', 'name': 'Agg', 'nodes': [], 'subscriptions': ['s']}]
    save_fixture(repo, shared, p)
    save_subscription_nodes('s', [{**node('cached'), **bad}])
    monkeypatch.setattr('backend.routes.aggregations.get_subscription_proxies_yaml', Mock(side_effect=ConnectionError('fixture transport failure')))
    response = app.test_client().get('/api/aggregations/agg/provider')
    assert response.status_code == 400


@pytest.mark.parametrize('conversion_failure', [False, True])
def test_legacy_subscription_cache_without_generated_id_remains_supported(tmp_path, monkeypatch, conversion_failure):
    from backend.utils.subscription_cache import save_subscription_nodes
    app, repo = make_app(tmp_path)
    shared = repo.get_shared()
    p = repo.get_profile('default')
    shared['subscriptions'] = [{'id': 's', 'name': 'Cached', 'enabled': True, 'url': ''}]
    shared['subscription_aggregations'] = [{'id': 'agg', 'name': 'Agg', 'nodes': [], 'subscriptions': ['s']}]
    save_fixture(repo, shared, p)
    cached = node('cached')
    del cached['id']
    if conversion_failure:
        cached['proxy_string'] = 'http://fixture.invalid:80'
        monkeypatch.setattr('backend.utils.sub_store_client.convert_proxy_string', lambda s: None)
    save_subscription_nodes('s', [cached])
    monkeypatch.setattr('backend.routes.aggregations.get_subscription_proxies_yaml', Mock(side_effect=ConnectionError('fixture transport failure')))
    assert app.test_client().get('/api/subscriptions/s/proxies').status_code == 200
    assert app.test_client().get('/api/aggregations/agg/provider').status_code == 200


BAD_SHAPES = [{'params': None}, {'proxy_string': 123}, {'id': ['relay']}, {'name': ['Relay']}, {'group_order': [5]}]


@pytest.mark.parametrize('mode', ['source', 'target', 'group', 'nested_group'])
def test_named_chains_require_manual_nodes_throughout(tmp_path, mode):
    app, repo = make_app(tmp_path)
    client = app.test_client()
    seed_nodes(repo, [
        node('relay', **({'subscription_id': 'foreign-sub'} if mode != 'source' else {})),
        node('exit', **({'subscription_id': 'foreign-sub'} if mode == 'source' else {})),
    ], subscriptions=[{'id': 'foreign-sub', 'name': 'Foreign', 'url': ''}], groups=[
        {'id': 'g', 'name': 'G', 'type': 'select', 'manual_nodes': ['relay']},
        {'id': 'nested', 'name': 'Nested', 'type': 'select', 'include_groups': ['g']},
    ])
    ref = {'type': 'group', 'id': 'nested' if mode == 'nested_group' else 'g'} if 'group' in mode else {'type': 'node', 'id': 'relay'}
    before = repo.path.read_bytes()
    response = client.post('/api/proxy-groups', json=chain_group('exit', ref))
    assert response.status_code == 409
    assert repo.path.read_bytes() == before



@pytest.mark.parametrize('bad', BAD_SHAPES)
def test_malformed_dialer_metadata_write_is_controlled(tmp_path, bad):
    app, repo = make_app(tmp_path)
    client = app.test_client()
    client.post('/api/nodes', json=node('relay'))
    before = repo.path.read_bytes()
    if 'group_order' in bad:
        response = client.post('/api/proxy-groups', json={'id': 'g', 'name': 'G', 'type': 'select', 'manual_nodes': ['relay'], 'proxies_order': bad['group_order']})
    else:
        response = client.post('/api/nodes', json={**node('bad'), **bad})
    assert response.status_code == 400
    assert repo.path.read_bytes() == before


@pytest.mark.parametrize('bad', BAD_SHAPES)
def test_malformed_metadata_mutations_preserve_storage_cache_and_artifacts(tmp_path, monkeypatch, bad):
    from backend.utils.subscription_cache import load_subscription_cache, save_subscription_nodes
    app, repo = make_app(tmp_path)
    group = {'id': 'g', 'name': 'Entry', 'type': 'select', 'manual_nodes': ['exit'],
             'include_groups': ['chain-exit'], 'aggregations': ['agg']}
    seed_nodes(repo, [node('relay'), node('exit')],
               chains=[chain_group('exit', {'type': 'node', 'id': 'relay'})], groups=[group],
               subscriptions=[{'id': 's', 'name': 'Feed', 'url': ''}],
               aggregations=[{'id': 'agg', 'name': 'Agg', 'nodes': ['exit'], 'subscriptions': ['s']}])
    save_subscription_nodes('s', [node('cached')])
    cache_before = load_subscription_cache('s')
    manager = Mock()
    monkeypatch.setattr('backend.routes.agents.get_agent_manager', lambda: manager)
    network = Mock(side_effect=AssertionError('Invalid metadata must not trigger delivery'))
    monkeypatch.setattr('requests.sessions.Session.request', network)
    paths = [repo.write_profile_text('default', name, 'known good')
             for name in ('generated/config.yaml', 'generated/config.conf', 'providers/agg.yaml')]
    before = repo.path.read_bytes()
    client = app.test_client()
    if 'group_order' in bad:
        response = client.put('/api/proxy-groups/g', json={**group, 'proxies_order': bad['group_order']})
    else:
        response = client.put('/api/nodes/relay', json={**node('relay'), **bad})
    assert response.status_code == 400, response.get_data(as_text=True)
    assert all(output.read_text() == 'known good' for output in paths)
    assert repo.path.read_bytes() == before
    assert load_subscription_cache('s') == cache_before
    manager.push_config_to_agent.assert_not_called()
    network.assert_not_called()
