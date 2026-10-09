"""Fail-closed regressions from the second independent dialer review."""
from unittest.mock import Mock
import pytest
import yaml
from backend.test_qa_integrity import make_app
from backend.test_dialer_proxy import node, save_fixture, chain_group, add_subscription, seed_nodes


@pytest.mark.parametrize('path', ['/api/generate/mihomo/preview', '/api/generate/mihomo', '/api/config/mihomo', '/api/agents/a/push-config'])
def test_converted_main_chain_preflights_subscription_only_provider(tmp_path, monkeypatch, path):
    app, repo = make_app(tmp_path)
    shared = repo.get_shared()
    p = repo.get_profile('default')
    shared['nodes'] = [node('relay'), node('exit', proxy_string='http://fixture.invalid:80')]
    shared['subscriptions'] = [{'id': 's', 'name': 'S', 'enabled': True, 'url': 'https://fixture.invalid/feed'}]
    shared['subscription_aggregations'] = [{'id': 'agg', 'name': 'Agg', 'nodes': [], 'subscriptions': ['s']}]
    p['proxy_groups'] = [{'id': 'g', 'name': 'Entry', 'type': 'select', 'manual_nodes': ['relay', 'exit'], 'aggregations': ['agg']}]
    save_fixture(repo, shared, p)
    monkeypatch.setattr('backend.utils.sub_store_client.convert_proxy_string', lambda s: {'type': 'http', 'server': 'example.test', 'port': 80, 'dialer-proxy': 'relay'})
    fetch = Mock(return_value=('proxies: [{name: Bad, type: http, server: example.test, port: 80, dialer-proxy: gone}]', 'rendered_yaml'))
    monkeypatch.setattr('backend.routes.aggregations.get_subscription_proxies_yaml', fetch)
    cache_write = Mock()
    monkeypatch.setattr('backend.utils.subscription_cache.save_subscription_nodes', cache_write)
    manager = Mock()
    manager.get_agent_by_id.return_value = {'id': 'a', 'service_type': 'mihomo', 'profile_id': 'default'}
    monkeypatch.setattr('backend.routes.agents.get_agent_manager', lambda: manager)
    outbound = Mock(side_effect=AssertionError('invalid graph must precede outbound agent requests'))
    monkeypatch.setattr('requests.get', outbound)
    output = repo.write_generated('default', 'config.yaml', 'known good')
    before = repo.path.read_bytes()
    client = app.test_client()
    response = client.get(path) if path.startswith('/api/config') else client.post(path, json={'restart': False})
    assert response.status_code == 400, response.get_data(as_text=True)
    assert fetch.call_count == 1
    cache_write.assert_not_called()
    outbound.assert_not_called()
    manager.push_config_to_agent.assert_not_called()
    assert output.read_text() == 'known good'
    assert repo.path.read_bytes() == before


@pytest.mark.parametrize('provider_chain', [True, False])
@pytest.mark.parametrize('path', ['/api/aggregations/agg/provider', '/api/generate/mihomo'])
def test_duplicate_emitted_provider_dialers_rejected_before_commit(tmp_path, monkeypatch, path, provider_chain):
    app, repo = make_app(tmp_path)
    shared = repo.get_shared()
    p = repo.get_profile('default')
    shared['nodes'] = [node('exit', params={'dialer-proxy': 'DIRECT'})]
    shared['subscriptions'] = [{'id': 's', 'name': 'S', 'enabled': True, 'url': 'https://fixture.invalid/feed'}]
    shared['subscription_aggregations'] = [{'id': 'agg', 'name': 'Agg', 'nodes': [], 'subscriptions': ['s']}]
    p['proxy_groups'] = [{'id': 'g', 'name': 'Entry', 'type': 'select', 'manual_nodes': ['exit'], 'aggregations': ['agg']}]
    save_fixture(repo, shared, p)
    text = 'proxies: [{name: Dup, type: http, server: example.test, port: 80, dialer-proxy: DIRECT}, {name: Dup, type: http, server: other.test, port: 81, dialer-proxy: DIRECT}]'
    if not provider_chain:
        text = text.replace(', dialer-proxy: DIRECT', '')
    monkeypatch.setattr('backend.routes.aggregations.get_subscription_proxies_yaml', Mock(return_value=(text, 'rendered_yaml')))
    cache_write = Mock()
    monkeypatch.setattr('backend.utils.subscription_cache.save_subscription_nodes', cache_write)
    provider = repo.write_profile_text('default', 'providers/agg.yaml', 'known good provider')
    output = repo.write_generated('default', 'config.yaml', 'known good')
    response = app.test_client().get(path) if '/provider' in path else app.test_client().post(path, json={})
    assert response.status_code == 400
    cache_write.assert_not_called()
    assert provider.read_text() == 'known good provider'
    assert output.read_text() == 'known good'


@pytest.mark.parametrize('source', ['rendered_yaml', 'sub_store', 'direct_url_fallback'])
def test_surge_remote_dialer_rejected_before_cache_write(tmp_path, monkeypatch, source):
    from backend.utils.subscription_cache import save_subscription_nodes, load_subscription_cache
    app, repo = make_app(tmp_path)
    add_subscription(repo, {'id': 's', 'name': 'S', 'enabled': True, 'url': 'https://fixture.invalid/feed'})
    save_subscription_nodes('s', [node('old')])
    before = load_subscription_cache('s')
    text = 'proxies: [{name: Exit, type: http, server: example.test, port: 80, dialer-proxy: DIRECT}]'
    monkeypatch.setattr('backend.routes.subscriptions.get_subscription_proxies_yaml', Mock(return_value=(text, source)))
    response = app.test_client().get('/api/subscriptions/s/proxies?format=surge')
    assert response.status_code == 400
    assert load_subscription_cache('s') == before


@pytest.mark.parametrize('reverse_order', [False, True])
def test_provider_discovered_chain_preflights_other_used_providers(tmp_path, monkeypatch, reverse_order):
    app, repo = make_app(tmp_path)
    shared = repo.get_shared()
    p = repo.get_profile('default')
    shared['nodes'] = [node('relay'), node('exit', proxy_string='http://fixture.invalid:80')]
    shared['subscriptions'] = [{'id': 's', 'name': 'S', 'enabled': True, 'url': 'https://fixture.invalid/feed'}]
    shared['subscription_aggregations'] = [{'id': 'first', 'name': 'First', 'nodes': ['exit'], 'subscriptions': []}, {'id': 'second', 'name': 'Second', 'nodes': [], 'subscriptions': ['s']}]
    p['proxy_groups'] = [{'id': 'g', 'name': 'Entry', 'type': 'select', 'manual_nodes': ['relay'], 'aggregations': ['first', 'second']}]
    if reverse_order:
        shared['subscription_aggregations'].reverse()
    save_fixture(repo, shared, p)
    monkeypatch.setattr('backend.utils.sub_store_client.convert_proxy_string', lambda s: {'type': 'http', 'server': 'example.test', 'port': 80, 'dialer-proxy': 'relay'})
    fetch = Mock(return_value=('proxies: [{name: Bad, type: http, server: example.test, port: 80, dialer-proxy: gone}]', 'rendered_yaml'))
    monkeypatch.setattr('backend.routes.aggregations.get_subscription_proxies_yaml', fetch)
    response = app.test_client().post('/api/generate/mihomo/preview', json={})
    assert response.status_code == 400
    assert fetch.call_count == 1


def test_direct_subscription_provider_is_preflighted_for_converted_main_chain(tmp_path, monkeypatch):
    app, repo = make_app(tmp_path)
    shared = repo.get_shared()
    p = repo.get_profile('default')
    shared['nodes'] = [node('relay'), node('exit', proxy_string='http://fixture.invalid:80')]
    shared['subscriptions'] = [{'id': 's', 'name': 'S', 'enabled': True, 'url': 'https://fixture.invalid/feed'}]
    p['proxy_groups'] = [{'id': 'g', 'name': 'Entry', 'type': 'select', 'manual_nodes': ['relay', 'exit'], 'subscriptions': ['s']}]
    save_fixture(repo, shared, p)
    monkeypatch.setattr('backend.utils.sub_store_client.convert_proxy_string', lambda s: {'type': 'http', 'server': 'example.test', 'port': 80, 'dialer-proxy': 'relay'})
    monkeypatch.setattr('backend.routes.subscriptions.get_subscription_proxies_yaml', Mock(return_value=('proxies: [{name: Bad, type: http, server: example.test, port: 80, dialer-proxy: gone}]', 'rendered_yaml')))
    response = app.test_client().post('/api/generate/mihomo/preview', json={})
    assert response.status_code == 400


@pytest.mark.parametrize('path', ['/api/generate/mihomo/preview', '/api/generate/mihomo', '/api/config/mihomo', '/api/aggregations/agg/provider'])
def test_good_converted_main_and_raw_provider_graph_still_exports(tmp_path, monkeypatch, path):
    app, repo = make_app(tmp_path)
    shared = repo.get_shared()
    p = repo.get_profile('default')
    shared['nodes'] = [node('relay'), node('exit', proxy_string='http://fixture.invalid:80')]
    shared['subscriptions'] = [{'id': 's', 'name': 'S', 'enabled': True, 'url': 'https://fixture.invalid/feed'}]
    shared['subscription_aggregations'] = [{'id': 'agg', 'name': 'Agg', 'nodes': [], 'subscriptions': ['s']}]
    p['proxy_groups'] = [{'id': 'g', 'name': 'Entry', 'type': 'select', 'manual_nodes': ['relay', 'exit'], 'aggregations': ['agg']}]
    save_fixture(repo, shared, p)
    monkeypatch.setattr('backend.utils.sub_store_client.convert_proxy_string', lambda s: {'type': 'http', 'server': 'example.test', 'port': 80, 'dialer-proxy': 'relay'})
    monkeypatch.setattr('backend.routes.aggregations.get_subscription_proxies_yaml', Mock(return_value=('proxies: [{name: Good, type: http, server: example.test, port: 80, dialer-proxy: relay}]', 'rendered_yaml')))
    client = app.test_client()
    response = client.get(path) if path.startswith('/api/config') or '/provider' in path else client.post(path, json={})
    assert response.status_code == 200, response.get_data(as_text=True)
    content = response.get_json()['content'] if 'preview' in path else (repo.generated_dir('default') / 'config.yaml').read_text() if path == '/api/generate/mihomo' else response.get_data(as_text=True)
    proxies = yaml.safe_load(content)['proxies']
    assert next(p for p in proxies if p['name'] == ('Good' if '/provider' in path else 'exit'))['dialer-proxy'] == 'relay'


@pytest.mark.parametrize('subscription_overlap', [False, True])
def test_named_chain_keeps_provider_manual_exit_direct_and_subscription_priority(tmp_path, monkeypatch, subscription_overlap):
    app, repo = make_app(tmp_path)
    shared = repo.get_shared()
    p = repo.get_profile('default')
    shared['nodes'] = [node('relay'), node('exit')]
    shared['subscriptions'] = [{'id': 's', 'name': 'S', 'enabled': True, 'url': 'https://fixture.invalid/feed'}]
    shared['subscription_aggregations'] = [{'id': 'agg', 'name': 'Agg', 'nodes': ['exit'], 'subscriptions': ['s']}]
    p['proxy_groups'] = [{'id': 'g', 'name': 'Entry', 'type': 'select', 'aggregations': ['agg'],
                          'include_groups': ['chain-exit']}]
    p['proxy_groups'].append(chain_group('exit', {'type': 'node', 'id': 'relay'}))
    save_fixture(repo, shared, p)
    remote = [{'name': 'exit', 'type': 'http', 'server': 'other.test', 'port': 81}] if subscription_overlap else []
    monkeypatch.setattr('backend.routes.aggregations.get_subscription_proxies_yaml',
                        Mock(return_value=(yaml.safe_dump({'proxies': remote}), 'rendered_yaml')))
    response = app.test_client().get('/api/aggregations/agg/provider')
    assert response.status_code == 200
    proxies = yaml.safe_load(response.data)['proxies']
    assert len(proxies) == 1
    assert proxies[0]['name'] == 'exit'
    assert 'dialer-proxy' not in proxies[0]
    assert proxies[0]['server'] == ('other.test' if subscription_overlap else 'example.test')
    if subscription_overlap:
        assert proxies == remote


def test_disabled_managed_source_keeps_manual_only_closure_contract():
    from backend.utils.dialer_references import validate_dialers, DialerReferenceError
    profile = {'nodes': [{**node('source', dialer_ref={'type': 'node', 'id': 'relay'}), 'enabled': False}, node('relay', subscription_id='s')]}
    with pytest.raises(DialerReferenceError, match='手动'):
        validate_dialers(profile)


def deep_nodes(cycle=False):
    return [node(str(i), **({'dialer_ref': {'type': 'node', 'id': str((i + 1) % 1100)}}
                           if i < 1099 or cycle else {})) for i in range(1100)]


@pytest.mark.parametrize('cycle', [False, True])
def test_deep_graph_validation_and_closure_are_stack_safe(cycle):
    from backend.utils.dialer_references import validate_dialers, dependency_node_ids, validate_emitted, DialerReferenceError
    profile = {'nodes': deep_nodes(cycle)}
    if cycle:
        with pytest.raises(DialerReferenceError, match='循环'):
            validate_dialers(profile)
    else:
        validate_dialers(profile)
        assert len(dependency_node_ids(profile, ['0'])) == 1100
    emitted = {'proxies': [{'name': n['name'], **({'dialer-proxy': n['dialer_ref']['id']} if n.get('dialer_ref') else {})} for n in profile['nodes']]}
    if cycle:
        with pytest.raises(DialerReferenceError, match='循环'):
            validate_emitted(emitted)
    else:
        validate_emitted(emitted)


@pytest.mark.parametrize('cycle', [False, True])
@pytest.mark.parametrize('path', ['/api/generate/mihomo/preview', '/api/nodes'])
def test_deep_group_chain_api_never_returns_500(tmp_path, monkeypatch, cycle, path):
    from backend.converters import mihomo
    app, repo = make_app(tmp_path)
    groups = [{'id': f'g{i}', 'name': f'G{i}', 'type': 'select', 'manual_nodes': [str(i)],
               'include_groups': [f'g{i + 1}'] if i < 1099 else []} for i in range(1100)]
    seed_nodes(repo, [node(str(i)) for i in range(1100)], groups=groups,
               chains=[chain_group('1099', {'type': 'group', 'id': 'g0'})])
    before = repo.path.read_bytes()
    convert = Mock(wraps=mihomo.convert_node_to_mihomo)
    monkeypatch.setattr(mihomo, 'convert_node_to_mihomo', convert)
    if cycle:
        response = app.test_client().put('/api/proxy-groups/g1099',
            json={**groups[-1], 'include_groups': ['chain-1099']})
    else:
        response = app.test_client().post(path, json={} if 'preview' in path else node('new'))
    assert response.status_code == (409 if cycle else 200), response.get_data(as_text=True)
    if cycle or 'preview' in path:
        assert repo.path.read_bytes() == before
    if not cycle and 'preview' in path:
        proxies = yaml.safe_load(response.get_json()['content'])['proxies']
        assert len(proxies) == 1101
        assert next(p for p in proxies if p['name'] == 'Via 1099')['dialer-proxy'] == 'G0'
        assert 'dialer-proxy' not in next(p for p in proxies if p['name'] == '1099')
        assert convert.call_count == 1101
