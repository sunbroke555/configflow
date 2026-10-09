"""Chain regressions using shared resources and profile-local named groups."""
import pytest
import yaml
from backend.test_qa_integrity import make_app


def node(id, name=None, **extra):
    return dict(id=id, name=name or id, enabled=True, type='http', server='example.test', port=80, **extra)


def save_fixture(repo, shared, profile, profile_id='default'):
    """Persist exactly the catalog and explicit group references supplied by a test."""
    repo.save_shared(shared)
    repo.save_profile(profile_id, profile)


def add_subscription(repo, subscription):
    repo.update_shared_transaction(lambda shared: shared['subscriptions'].append(subscription))


def chain_group(exit_node, entry, *, id=None, name=None):
    return {'id': id or 'chain-' + exit_node, 'name': name or 'Via ' + exit_node,
            'type': 'chain', 'chain': {'entry': entry, 'exit': {'type': 'node', 'id': exit_node}}}


def create_chain(client, exit_node, entry, profile='default', **kwargs):
    response = client.post(f'/api/profiles/{profile}/proxy-groups',
                           json=chain_group(exit_node, entry, **kwargs))
    assert response.status_code == 200, response.get_data(as_text=True)
    return response


def seed_nodes(repo, nodes, *, chains=None, groups=None, aggregations=None, subscriptions=None):
    shared = repo.get_shared()
    shared['nodes'] = nodes
    if subscriptions is not None:
        shared['subscriptions'] = subscriptions
    if aggregations is not None:
        shared['subscription_aggregations'] = aggregations
    profile = repo.get_profile('default')
    profile['proxy_groups'] = list(groups or []) + list(chains or [])
    save_fixture(repo, shared, profile)


def test_stable_reference_emits_unused_relay_and_tracks_rename(tmp_path):
    app, repo = make_app(tmp_path)
    client = app.test_client()
    seed_nodes(repo, [node('relay'), node('exit')])
    create_chain(client, 'exit', {'type': 'node', 'id': 'relay'})
    assert client.post('/api/proxy-groups', json={'id': 'g', 'name': 'Entry', 'type': 'select', 'manual_nodes': ['exit']}).status_code == 200
    assert client.put('/api/nodes/relay', json=node('relay', 'Renamed')).status_code == 200
    response = client.post('/api/generate/mihomo/preview', json={})
    assert response.status_code == 200, response.get_data(as_text=True)
    proxies = {p['name']: p for p in yaml.safe_load(response.get_json()['content'])['proxies']}
    assert proxies['Via exit']['dialer-proxy'] == 'Renamed'
    assert 'dialer-proxy' not in proxies['exit']
    assert 'Renamed' in proxies
    assert all('dialer_ref' not in n for n in repo.get_shared()['nodes'])
    assert repo.get_profile('default')['proxy_groups'][0]['chain']['entry'] == {'type': 'node', 'id': 'relay'}


def test_referenced_shared_node_delete_never_cleans_profile_or_aggregation(tmp_path, monkeypatch):
    from unittest.mock import Mock
    app, repo = make_app(tmp_path)
    seed_nodes(repo, [node('used')], aggregations=[{'id': 'agg', 'name': 'Agg', 'nodes': ['used'], 'subscriptions': []}],
               groups=[{'id': 'g', 'name': 'Group', 'type': 'select', 'manual_nodes': ['used', 'DIRECT'], 'aggregations': ['agg'], 'proxies_order': [{'type': 'node', 'id': 'used'}, {'type': 'node', 'id': 'DIRECT'}]}])
    before = repo.path.read_bytes()
    write = Mock(side_effect=AssertionError('Referenced deletion must not write'))
    monkeypatch.setattr(repo, '_write_json', write)
    response = app.test_client().delete('/api/nodes/used')
    assert response.status_code == 409
    assert repo.path.read_bytes() == before
    write.assert_not_called()
    assert repo.get_shared()['subscription_aggregations'][0]['nodes'] == ['used']
    assert repo.get_profile('default')['proxy_groups'][0]['manual_nodes'] == ['used', 'DIRECT']


@pytest.mark.parametrize('resource', ['nodes', 'proxy-groups'])
@pytest.mark.parametrize('conflict', ['name', 'builtin', 'id'])
def test_api_rejects_ambiguous_dialer_identity_without_write(tmp_path, resource, conflict):
    app, repo = make_app(tmp_path)
    client = app.test_client()
    seed_nodes(repo, [node('relay'), node('exit')], chains=[chain_group('exit', {'type': 'node', 'id': 'relay'})],
               groups=[{'id': 'g', 'name': 'Group', 'type': 'select', 'manual_nodes': ['DIRECT']}])
    before = repo.path.read_bytes()
    data = node('exit') if resource == 'nodes' else {'id': 'g', 'name': 'Group', 'type': 'select', 'manual_nodes': ['DIRECT']}
    if conflict == 'name':
        data['name'] = 'relay'
    elif conflict == 'builtin':
        data['name'] = 'DIRECT'
    else:
        data['id'] = 'relay' if resource == 'nodes' else 'g'
    response = (client.post('/api/' + resource, json=data) if conflict == 'id' else
                client.put('/api/' + resource + ('/exit' if resource == 'nodes' else '/g'), json=data))
    assert response.status_code == (409 if conflict == 'name' or (resource == 'nodes' and conflict == 'builtin') else 400)
    assert repo.path.read_bytes() == before


@pytest.mark.parametrize('resource,key', [('nodes', 'nodes'), ('proxy-groups', 'groups')])
def test_legacy_reorder_cannot_bypass_dialer_validation(tmp_path, resource, key):
    app, repo = make_app(tmp_path)
    client = app.test_client()
    group = {'id': 'g', 'name': 'Group', 'type': 'select', 'manual_nodes': ['relay']}
    seed_nodes(repo, [node('relay'), node('exit')], groups=[group])
    create_chain(client, 'exit', {'type': 'node', 'id': 'relay'} if resource == 'nodes' else {'type': 'group', 'id': 'g'})
    before = repo.get_compat_config('default')
    replacement = [node('exit', params={'dialer-proxy': 'missing'})] if resource == 'nodes' else [{**group, 'manual_nodes': ['exit']}]
    response = client.post('/api/' + resource + '/reorder', json={key: replacement})
    assert response.status_code == 200
    after = repo.get_compat_config('default')
    assert {n['id']: n for n in after['nodes']} == {n['id']: n for n in before['nodes']}
    assert after['proxy_groups'] == before['proxy_groups']


def test_surge_policy_path_never_silently_drops_raw_dialer():
    from backend.converters.surge import convert_proxies_to_surge_text
    from backend.utils.strategy_references import StrategyReferenceError
    with pytest.raises(StrategyReferenceError, match='Surge'):
        convert_proxies_to_surge_text([{'name': 'Exit', 'type': 'http', 'server': 'example.test', 'port': 80, 'dialer-proxy': 'Relay'}])


def test_subscription_surge_consumer_reports_unsupported_dialer_as_400(tmp_path):
    from backend.utils.subscription_cache import save_subscription_nodes
    app, repo = make_app(tmp_path)
    seed_nodes(repo, [], subscriptions=[{'id': 's', 'name': 'Cached', 'enabled': True, 'url': ''}])
    save_subscription_nodes('s', [node('cached', params={'dialer-proxy': 'Relay'})])
    response = app.test_client().get('/api/subscriptions/s/proxies?format=surge')
    assert response.status_code == 400, response.get_data(as_text=True)
    assert 'Surge' in response.get_json()['message']


@pytest.mark.parametrize('method', ['post', 'put'])
def test_shared_node_api_rejects_profile_binding_without_mutation(tmp_path, method):
    app, repo = make_app(tmp_path)
    seed_nodes(repo, [node('relay'), node('exit')])
    before = repo.path.read_bytes()
    response = getattr(app.test_client(), method)('/api/nodes' + ('/exit' if method == 'put' else ''),
        json=node('exit', dialer_ref={'type': 'node', 'id': 'relay'}))
    assert response.status_code == 400
    assert repo.path.read_bytes() == before


def test_builtin_looking_resource_id_does_not_bypass_cycle_validation(tmp_path):
    app, repo = make_app(tmp_path)
    client = app.test_client()
    seed_nodes(repo, [node('DIRECT', 'Relay'), node('exit')])
    create_chain(client, 'exit', {'type': 'node', 'id': 'DIRECT'})
    before = repo.path.read_bytes()
    response = client.post('/api/proxy-groups', json=chain_group('DIRECT', {'type': 'node', 'id': 'DIRECT'}))
    assert response.status_code == 409
    assert repo.path.read_bytes() == before


@pytest.mark.parametrize('storage', ['structured', 'uri'])
def test_chain_includes_raw_multihop_dependencies(tmp_path, monkeypatch, storage):
    app, repo = make_app(tmp_path)
    payload = node('exit')
    if storage == 'uri':
        payload['proxy_string'] = 'http://fixture.invalid:80'
        monkeypatch.setattr('backend.utils.sub_store_client.convert_proxy_string', lambda s: {'type': 'http', 'server': 'example.test', 'port': 80})
    seed_nodes(repo, [node('first'), node('second', params={'dialer-proxy': 'first'}), payload])
    client = app.test_client()
    create_chain(client, 'exit', {'type': 'node', 'id': 'second'})
    assert client.post('/api/proxy-groups', json={'id': 'g', 'name': 'Entry', 'type': 'select', 'manual_nodes': ['exit'], 'include_groups': ['chain-exit']}).status_code == 200
    response = client.post('/api/generate/mihomo/preview', json={})
    assert response.status_code == 200
    proxies = {p['name']: p for p in yaml.safe_load(response.get_json()['content'])['proxies']}
    assert set(proxies) == {'first', 'second', 'exit', 'Via exit'}
    assert proxies['Via exit']['dialer-proxy'] == 'second'
    assert 'dialer-proxy' not in proxies['exit']
    assert proxies['second']['dialer-proxy'] == 'first'


def test_scope_isolation_and_stable_group_rename(tmp_path):
    app, repo = make_app(tmp_path)
    client = app.test_client()
    group = {'id': 'g', 'name': 'RelayGroup', 'type': 'select', 'manual_nodes': ['relay']}
    seed_nodes(repo, [node('relay'), node('exit')], groups=[group])
    create_chain(client, 'exit', {'type': 'group', 'id': 'g'})
    repo.create_profile({'id': 'other', 'name': 'Other'})
    create_chain(client, 'exit', {'type': 'node', 'id': 'relay'}, profile='other', name='Other route')
    other = repo.get_profile('other')
    assert client.put('/api/proxy-groups/g', json={**group, 'name': 'RenamedGroup'}).status_code == 200
    assert client.post('/api/proxy-groups', json={'id': 'e', 'name': 'Entry', 'type': 'select', 'manual_nodes': ['exit']}).status_code == 200
    response = client.post('/api/generate/mihomo/preview', json={})
    proxies = {p['name']: p for p in yaml.safe_load(response.get_json()['content'])['proxies']}
    assert proxies['Via exit']['dialer-proxy'] == 'RenamedGroup'
    assert 'dialer-proxy' not in proxies['exit']
    assert repo.get_profile('other') == other
    response = client.post('/api/profiles/other/generate/mihomo/preview', json={})
    assert response.status_code == 200, response.get_data(as_text=True)
    other_proxies = {p['name']: p for p in yaml.safe_load(response.get_json()['content'])['proxies']}
    assert other_proxies['Other route']['dialer-proxy'] == 'relay'
    assert 'Via exit' not in other_proxies


def test_editing_and_deleting_chain_keeps_direct_exit_and_other_profile(tmp_path):
    app, repo = make_app(tmp_path)
    seed_nodes(repo, [node('relay'), node('other'), node('exit')],
               groups=[{'id': 'direct', 'name': 'Direct exit', 'type': 'select', 'manual_nodes': ['exit']}])
    client = app.test_client()
    create_chain(client, 'exit', {'type': 'node', 'id': 'relay'})
    repo.clone_profile('default', {'id': 'other', 'name': 'Other'})
    shared_before = repo.get_shared()
    other_before = repo.get_profile('other')
    response = client.put('/api/proxy-groups/chain-exit',
                          json=chain_group('exit', {'type': 'node', 'id': 'other'}, name='Alternate route'))
    assert response.status_code == 200, response.get_data(as_text=True)
    response = client.post('/api/generate/mihomo/preview', json={})
    assert response.status_code == 200, response.get_data(as_text=True)
    proxies = {p['name']: p for p in yaml.safe_load(response.get_json()['content'])['proxies']}
    assert proxies['Alternate route']['dialer-proxy'] == 'other'
    assert 'dialer-proxy' not in proxies['exit']
    assert 'relay' not in proxies
    assert client.delete('/api/proxy-groups/chain-exit').status_code == 200
    response = client.post('/api/generate/mihomo/preview', json={})
    assert response.status_code == 200, response.get_data(as_text=True)
    proxies = yaml.safe_load(response.get_json()['content'])['proxies']
    assert [p['name'] for p in proxies] == ['exit']
    assert 'dialer-proxy' not in proxies[0]
    assert repo.get_shared() == shared_before
    assert repo.get_profile('other') == other_before


def test_chain_write_failure_rolls_back_atomic_document(tmp_path, monkeypatch):
    app, repo = make_app(tmp_path)
    seed_nodes(repo, [node('relay'), node('exit')])
    before = repo.path.read_bytes()
    original = repo._write_json
    def injected(path, data):
        if path == repo.path:
            raise OSError('injected write failure')
        original(path, data)
    monkeypatch.setattr(repo, '_write_json', injected)
    response = app.test_client().post('/api/proxy-groups', json=chain_group('exit', {'type': 'node', 'id': 'relay'}))
    assert response.status_code == 500
    assert repo.path.read_bytes() == before


def test_concurrent_chain_creation_preserves_independent_routes(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    app, repo = make_app(tmp_path)
    seed_nodes(repo, [node('a'), node('b')])
    def create(pair):
        exit_node, entry = pair
        with app.test_client() as client:
            return client.post('/api/proxy-groups', json=chain_group(exit_node, {'type': 'node', 'id': entry})).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(pool.map(create, [('a', 'b'), ('b', 'a')]))
    assert codes == [200, 200]
    assert {g['id'] for g in repo.get_profile('default')['proxy_groups']} == {'chain-a', 'chain-b'}
    response = app.test_client().post('/api/generate/mihomo/preview', json={})
    assert response.status_code == 200, response.get_data(as_text=True)
    proxies = {p['name']: p for p in yaml.safe_load(response.get_json()['content'])['proxies']}
    assert proxies['Via a']['dialer-proxy'] == 'b'
    assert proxies['Via b']['dialer-proxy'] == 'a'
    assert 'dialer-proxy' not in proxies['a']
    assert 'dialer-proxy' not in proxies['b']


@pytest.mark.parametrize('operation', ['delete', 'disable'])
def test_disabled_chain_still_protects_its_stable_reference(tmp_path, operation):
    app, repo = make_app(tmp_path)
    seed_nodes(repo, [node('relay'), node('exit')], chains=[{**chain_group('exit', {'type': 'node', 'id': 'relay'}), 'enabled': False}])
    before = repo.path.read_bytes()
    client = app.test_client()
    response = client.delete('/api/nodes/relay') if operation == 'delete' else client.put('/api/nodes/relay', json={**node('relay'), 'enabled': False})
    assert response.status_code == 409
    assert repo.path.read_bytes() == before


def test_disabling_selected_shared_source_preserves_binding_and_metadata(tmp_path):
    app, repo = make_app(tmp_path)
    seed_nodes(repo, [node('relay'), node('exit')], chains=[chain_group('exit', {'type': 'node', 'id': 'relay'})])
    before = repo.path.read_bytes()
    response = app.test_client().put('/api/nodes/exit', json={**node('exit'), 'enabled': False})
    assert response.status_code == 409
    assert repo.path.read_bytes() == before
    assert repo.get_profile('default')['proxy_groups'][0]['chain'] == {'entry': {'type': 'node', 'id': 'relay'}, 'exit': {'type': 'node', 'id': 'exit'}}


@pytest.mark.parametrize('kind', ['aggregation_relay', 'legacy_static'])
def test_dependency_closure_emits_relays_not_in_direct_selection(tmp_path, kind):
    app, repo = make_app(tmp_path)
    aggregations = [{'id': 'agg', 'name': 'Agg', 'nodes': ['relay', 'exit'], 'subscriptions': []}] if kind == 'aggregation_relay' else []
    group = {'id': 'g', 'name': 'Entry', 'type': 'select', 'aggregations': ['agg']} if aggregations else {'id': 'g', 'name': 'Entry', 'type': 'select', 'source': 'node', 'proxies': ['exit']}
    seed_nodes(repo, [node('relay'), node('exit')], aggregations=aggregations, groups=[group], chains=[chain_group('exit', {'type': 'node', 'id': 'relay'})])
    response = app.test_client().post('/api/generate/mihomo/preview', json={})
    assert response.status_code == 200, response.get_data(as_text=True)
    assert 'relay' in {p['name'] for p in yaml.safe_load(response.get_json()['content'])['proxies']}


@pytest.mark.parametrize('format', ['mihomo', 'surge'])
def test_provider_rejects_untranslatable_dialer_before_write(tmp_path, monkeypatch, format):
    app, repo = make_app(tmp_path)
    payload = node('exit') if format == 'surge' else node('exit', proxy_string='http://fixture.invalid:80')
    seed_nodes(repo, [node('relay'), payload], chains=[chain_group('exit', {'type': 'node', 'id': 'relay'})] if format == 'surge' else [],
               aggregations=[{'id': 'agg', 'name': 'Agg', 'nodes': ['exit'], 'subscriptions': []}],
               groups=[{'id': 'g', 'name': 'Entry', 'type': 'select', 'aggregations': ['agg']}])
    monkeypatch.setattr('backend.utils.sub_store_client.convert_proxy_string', lambda s: {'type': 'http', 'server': 'example.test', 'port': 80, 'dialer-proxy': 'gone'})
    output = repo.write_profile_text('default', 'providers/agg.yaml', 'known good')
    response = app.test_client().get('/api/aggregations/agg/provider' + ('?format=surge' if format == 'surge' else ''))
    assert response.status_code == 400, response.get_data(as_text=True)
    assert output.read_text() == 'known good'


def test_aggregation_manual_node_remains_direct_beside_chain(tmp_path):
    from pathlib import Path
    from backend.routes.aggregations import generate_aggregation_provider
    app, repo = make_app(tmp_path)
    agg = {'id': 'agg', 'name': 'Agg', 'enabled': True, 'nodes': ['exit'], 'subscriptions': []}
    seed_nodes(repo, [node('relay'), node('exit')], aggregations=[agg], chains=[chain_group('exit', {'type': 'node', 'id': 'relay'})],
               groups=[{'id': 'g', 'name': 'Entry', 'type': 'select', 'aggregations': ['agg']}])
    with app.test_request_context('/api/profiles/default/aggregations/agg/provider'):
        result = generate_aggregation_provider(agg)
    provider_proxies = yaml.safe_load(Path(result['file_path']).read_text())['proxies']
    assert provider_proxies[0]['name'] == 'exit'
    assert 'dialer-proxy' not in provider_proxies[0]
    response = app.test_client().post('/api/generate/mihomo/preview', json={})
    assert response.status_code == 200, response.get_data(as_text=True)
    config = yaml.safe_load(response.get_json()['content'])
    proxies = {p['name']: p for p in config['proxies']}
    assert proxies['Via exit']['dialer-proxy'] == 'relay'
    assert 'relay' in proxies
    assert 'Agg' in config['proxy-providers']


@pytest.mark.parametrize('format', ['mihomo', 'surge'])
@pytest.mark.parametrize('failure', ['missing', 'cycle', 'reserved', 'duplicate', 'unsupported_surge', 'uri'])
def test_invalid_topology_rejected_without_artifact_write(tmp_path, monkeypatch, format, failure):
    app, repo = make_app(tmp_path)
    nodes = [node('relay'), node('exit')]
    if failure == 'uri':
        nodes[1]['proxy_string'] = 'http://fixture.invalid:80'
    seed_nodes(repo, nodes, groups=[{'id': 'g', 'name': 'Entry', 'type': 'select', 'manual_nodes': ['exit']}])
    client = app.test_client()
    if failure != 'uri':
        create_chain(client, 'exit', {'type': 'node', 'id': 'relay'})
    output = repo.write_generated('default', 'config.yaml' if format == 'mihomo' else 'config.conf', 'known good')
    before = repo.path.read_bytes()
    if failure in ('missing', 'cycle'):
        entry = {'type': 'node', 'id': 'gone' if failure == 'missing' else 'exit'}
        response = client.put('/api/proxy-groups/chain-exit', json=chain_group('exit', entry))
    elif failure in ('reserved', 'duplicate'):
        response = client.put('/api/nodes/relay', json=node('relay', 'DIRECT' if failure == 'reserved' else 'exit'))
    else:
        if failure == 'uri':
            monkeypatch.setattr('backend.utils.sub_store_client.convert_proxy_string', lambda s: {'type': 'http', 'server': 'example.test', 'port': 80, 'dialer-proxy': 'gone'})
        elif format == 'mihomo':
            pytest.skip('Valid stable topology is supported by Mihomo')
        response = client.post('/api/generate/' + format, json={})
    assert response.status_code == (409 if failure in ('missing', 'cycle', 'reserved', 'duplicate') else 400), response.get_data(as_text=True)
    assert repo.path.read_bytes() == before
    assert output.read_text() == 'known good'


@pytest.mark.parametrize('storage', ['json', 'yaml', 'params'])
@pytest.mark.parametrize('operation', ['preserve', 'override', 'missing', 'cycle', 'rename', 'delete', 'disable'])
def test_raw_dialer_validation_and_precedence(tmp_path, storage, operation):
    import json
    app, repo = make_app(tmp_path)
    seed_nodes(repo, [node('relay'), node('other'), node('exit')],
               groups=[{'id': 'g', 'name': 'Entry', 'type': 'select', 'manual_nodes': ['exit']}])
    client = app.test_client()
    raw = {'type': 'http', 'server': 'example.test', 'port': 80, 'dialer-proxy': 'missing' if operation == 'missing' else 'relay'}
    payload = node('exit')
    if storage == 'params':
        payload['params'] = {'dialer-proxy': raw['dialer-proxy']}
    else:
        payload['proxy_string'] = json.dumps(raw) if storage == 'json' else yaml.safe_dump(raw)
    if operation == 'override':
        create_chain(client, 'exit', {'type': 'node', 'id': 'other'})
    before = repo.path.read_bytes()
    response = client.put('/api/nodes/exit', json=payload)
    if operation == 'missing':
        assert response.status_code == 409
        assert repo.path.read_bytes() == before
        return
    assert response.status_code == 200
    if operation in ('cycle', 'rename', 'delete', 'disable'):
        before = repo.path.read_bytes()
        if operation == 'cycle':
            response = client.put('/api/nodes/relay', json=node('relay', params={'dialer-proxy': 'exit'}))
        elif operation == 'delete':
            response = client.delete('/api/nodes/relay')
        else:
            data = node('relay', 'Renamed') if operation == 'rename' else {**node('relay'), 'enabled': False}
            response = client.put('/api/nodes/relay', json=data)
        assert response.status_code == 409
        assert repo.path.read_bytes() == before
    else:
        response = client.post('/api/generate/mihomo/preview', json={})
        assert response.status_code == 200
        proxies = {p['name']: p for p in yaml.safe_load(response.get_json()['content'])['proxies']}
        assert proxies['exit']['dialer-proxy'] == 'relay'
        assert 'relay' in proxies
        if operation == 'override':
            assert proxies['Via exit']['dialer-proxy'] == 'other'
            assert 'other' in proxies


@pytest.mark.parametrize('ref,status', [
    (False, 400), ('', 400), ([], 400), ({}, 400),
    ({'type': 'bad', 'id': 'relay'}, 400), ({'type': 'node', 'id': 3}, 400),
    ({'type': 'node', 'id': 'missing'}, 409), ({'type': 'node', 'id': 'exit'}, 409),
    ({'type': 'group', 'id': 'missing-group'}, 409),
])
def test_invalid_stable_reference_rejected_without_write(tmp_path, ref, status):
    app, repo = make_app(tmp_path)
    seed_nodes(repo, [node('relay'), node('exit')])
    before = repo.path.read_bytes()
    response = app.test_client().post('/api/proxy-groups', json=chain_group('exit', ref))
    assert response.status_code == status, response.get_data(as_text=True)
    assert repo.path.read_bytes() == before


@pytest.mark.parametrize('operation', ['delete', 'disable', 'cycle', 'id'])
def test_node_mutations_protect_references_atomically(tmp_path, operation):
    app, repo = make_app(tmp_path)
    seed_nodes(repo, [node('relay'), node('exit')], chains=[chain_group('exit', {'type': 'node', 'id': 'relay'})])
    before = repo.path.read_bytes()
    client = app.test_client()
    if operation == 'delete':
        response = client.delete('/api/nodes/relay')
    elif operation == 'cycle':
        response = client.put('/api/proxy-groups/chain-exit', json=chain_group('exit', {'type': 'node', 'id': 'exit'}))
    else:
        data = {**node('relay'), 'enabled': False} if operation == 'disable' else node('spoof')
        response = client.put('/api/nodes/relay', json=data)
    assert response.status_code == (400 if operation == 'id' else 409)
    assert repo.path.read_bytes() == before


@pytest.mark.parametrize('operation', ['direct_exit_member', 'membership_cycle', 'delete', 'disable', 'dynamic_nested'])
def test_group_dialer_validates_all_paths(tmp_path, operation):
    app, repo = make_app(tmp_path)
    group = {'id': 'g', 'name': 'RelayPool', 'type': 'select', 'manual_nodes': ['relay', 'DIRECT']}
    if operation == 'direct_exit_member':
        group['manual_nodes'].append('exit')
    seed_nodes(repo, [node('relay'), node('exit')], groups=[group], subscriptions=[{'id': 'sub', 'name': 'Feed', 'url': ''}])
    client = app.test_client()
    response = client.post('/api/proxy-groups', json=chain_group('exit', {'type': 'group', 'id': 'g'}))
    assert response.status_code == 200, response.get_data(as_text=True)
    if operation == 'direct_exit_member':
        response = client.post('/api/generate/mihomo/preview', json={})
        assert response.status_code == 200, response.get_data(as_text=True)
        proxies = {p['name']: p for p in yaml.safe_load(response.get_json()['content'])['proxies']}
        assert proxies['Via exit']['dialer-proxy'] == 'RelayPool'
        assert 'dialer-proxy' not in proxies['exit']
        return
    before = repo.path.read_bytes()
    if operation == 'delete':
        response = client.delete('/api/proxy-groups/g')
    elif operation == 'disable':
        response = client.put('/api/proxy-groups/g', json={**group, 'enabled': False})
    elif operation == 'membership_cycle':
        response = client.put('/api/proxy-groups/g', json={**group, 'include_groups': ['chain-exit']})
    else:
        from backend.utils.subscription_cache import save_subscription_nodes
        save_subscription_nodes('sub', [node('cached', 'Remote')])
        assert client.post('/api/proxy-groups', json={'id': 'd', 'name': 'Dynamic', 'type': 'select', 'subscriptions': ['sub']}).status_code == 200
        response = client.put('/api/proxy-groups/g', json={**group, 'include_groups': ['d']})
        assert response.status_code == 200, response.get_data(as_text=True)
        shared_before = repo.get_shared()
        response = client.post('/api/generate/mihomo/preview', json={})
        assert response.status_code == 200, response.get_data(as_text=True)
        config = yaml.safe_load(response.get_json()['content'])
        proxies = {proxy['name']: proxy for proxy in config['proxies']}
        groups = {item['name']: item for item in config['proxy-groups']}
        assert proxies['Via exit']['dialer-proxy'] == 'RelayPool'
        assert 'Dynamic' in groups['RelayPool']['proxies']
        assert groups['Dynamic']['use'] == ['Feed']
        assert 'Feed' in config['proxy-providers']
        assert repo.get_shared() == shared_before
        return
    assert response.status_code == 409
    assert repo.path.read_bytes() == before
