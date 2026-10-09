"""Permanent real-route regressions for QA data integrity."""
from concurrent.futures import ThreadPoolExecutor
import pytest
from flask import Flask
from backend.common.config_repository import ProfileRepository
from backend.common import config as config_module
from backend.routes import register_blueprints


def make_app(tmp_path):
    repository = ProfileRepository(tmp_path)
    config_module.set_repository(repository)
    app = Flask(__name__)
    register_blueprints(app)
    return app, repository


def test_batch_ids_survive_deletion_and_delete_only_target(tmp_path):
    app, _ = make_app(tmp_path)
    client = app.test_client()
    payload = {"rule_type": "DOMAIN", "policy": "DIRECT", "domains": ["a.test", "b.test"]}
    rules = client.post('/api/rules/batch', json=payload).get_json()['rules']
    client.delete('/api/rules/' + rules[0]['id'])
    new = client.post('/api/rules/batch', json={**payload, 'domains': ['c.test']}).get_json()['rules'][0]
    assert new['id'] != rules[1]['id']
    client.delete('/api/rules/' + new['id'])
    assert client.get('/api/rules').get_json() == [rules[1]]


def test_concurrent_batches_have_unique_ids(tmp_path):
    app, _ = make_app(tmp_path)
    def create(i):
        with app.test_client() as client:
            response = client.post('/api/rules/batch', json={'rule_type': 'DOMAIN', 'policy': 'DIRECT', 'domains': [f'{i}.test']})
            assert response.status_code == 200
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(create, range(24)))
    rules = app.test_client().get('/api/rules').get_json()
    assert len(rules) == len({r['id'] for r in rules}) == 24


@pytest.mark.parametrize('method', ['post', 'put'])
@pytest.mark.parametrize('field', ['name', 'url'])
@pytest.mark.parametrize('value', ['', ' \t\n', None, 42])
def test_subscription_required_fields_rejected_without_mutation(tmp_path, method, field, value):
    app, repo = make_app(tmp_path)
    client = app.test_client()
    valid = {'id': 'sub1', 'name': 'Feed', 'url': 'ss://existing-supported-protocol', 'enabled': True}
    assert client.post('/api/subscriptions', json=valid).status_code == 200
    before = repo.get_shared()
    response = getattr(client, method)('/api/subscriptions' + ('/sub1' if method == 'put' else ''), json={**valid, field: value})
    assert response.status_code == 400
    assert repo.get_shared() == before


def seed_strategy(client):
    group = {'id': 'g1', 'name': 'Old', 'type': 'select', 'enabled': True, 'manual_nodes': ['DIRECT']}
    assert client.post('/api/proxy-groups', json=group).status_code == 200
    assert client.post('/api/rules', json={'id': 'r1', 'itemType': 'rule', 'rule_type': 'DOMAIN', 'value': 'a.test', 'policy': 'Old', 'enabled': True}).status_code == 200
    return group


@pytest.mark.parametrize('operation', ['delete', 'disable'])
@pytest.mark.parametrize('reference', ['rule', 'include_groups', 'follow_group'])
def test_referenced_group_removal_rejected_atomically(tmp_path, operation, reference):
    app, repo = make_app(tmp_path)
    client = app.test_client()
    group = seed_strategy(client)
    if reference != 'rule':
        client.delete('/api/rules/r1')
        client.post('/api/proxy-groups', json={'id': 'g2', 'name': 'Child', 'type': 'select', 'enabled': True, reference: ['g1'] if reference == 'include_groups' else 'g1'})
    before = repo.get_profile('default')
    response = client.delete('/api/proxy-groups/g1') if operation == 'delete' else client.put('/api/proxy-groups/g1', json={**group, 'enabled': False})
    assert response.status_code == 409
    assert '引用' in response.get_json()['message']
    assert repo.get_profile('default') == before


def test_rename_updates_rule_and_ruleset_policies_preserves_id_refs_and_profile_isolation(tmp_path):
    app, repo = make_app(tmp_path)
    client = app.test_client()
    group = seed_strategy(client)
    repo.update_shared_transaction(lambda shared: shared['rule_library'].append({
        'id': 'library', 'name': 'Set', 'source_type': 'content', 'content': 'DOMAIN,example.test',
        'behavior': 'classical', 'enabled': True}))
    assert client.post('/api/rule-sets', json={'id': 'rs1', 'library_rule_id': 'library', 'policy': 'Old', 'enabled': False}).status_code == 200
    client.post('/api/proxy-groups', json={'id': 'g2', 'name': 'Child', 'include_groups': ['g1'], 'follow_group': 'g1'})
    repo.create_profile({'id': 'other', 'name': 'Other'}, clone_from='default')
    other = repo.get_profile('other')
    assert client.put('/api/proxy-groups/g1', json={**group, 'name': 'New'}).status_code == 200
    current = repo.get_profile('default')
    assert {r['policy'] for r in current['rule_configs']} == {'New'}
    assert current['proxy_groups'][1]['include_groups'] == ['g1']
    assert current['proxy_groups'][1]['follow_group'] == 'g1'
    assert repo.get_profile('other') == other


def test_rename_write_failure_rolls_back_all_resources(tmp_path, monkeypatch):
    app, repo = make_app(tmp_path)
    client = app.test_client()
    group = seed_strategy(client)
    before = repo.get_profile('default')
    document = tmp_path / 'config.json'
    system_before = document.read_bytes()
    original_write = repo._write_json
    def fail(path, data):
        if path == document:
            raise OSError('injected write failure')
        return original_write(path, data)
    monkeypatch.setattr(repo, '_write_json', fail)
    response = client.put('/api/proxy-groups/g1', json={**group, 'name': 'New'})
    assert response.status_code == 500
    assert repo.get_profile('default') == before
    assert document.read_bytes() == system_before


@pytest.mark.parametrize('format', ['mihomo', 'surge'])
@pytest.mark.parametrize('preview', ['', '/preview'])
@pytest.mark.parametrize('invalid_policy', ['Missing', 'Disabled'])
def test_generation_rejects_dangling_policy_without_writing(tmp_path, format, preview, invalid_policy):
    app, repo = make_app(tmp_path)
    client = app.test_client()
    output = repo.generated_dir('default') / ('config.yaml' if format == 'mihomo' else 'config.conf')
    generated = client.post('/api/generate/' + format, json={})
    assert generated.status_code == 200, generated.get_data(as_text=True)
    last_known_good = output.read_bytes()
    assert last_known_good == generated.data
    assert last_known_good
    assert client.post('/api/proxy-groups', json={'id': 'disabled', 'name': 'Disabled', 'enabled': False, 'type': 'select', 'manual_nodes': ['DIRECT']}).status_code == 200
    assert client.post('/api/rules', json={'id': 'r1', 'itemType': 'rule', 'rule_type': 'MATCH', 'policy': invalid_policy, 'enabled': True}).status_code == 200
    before = repo.get_profile('default')
    document_before = (tmp_path / 'config.json').read_bytes()
    response = client.post('/api/generate/' + format + preview, json={})
    assert response.status_code == 400
    assert invalid_policy in response.get_json()['message']
    assert repo.get_profile('default') == before
    assert output.read_bytes() == last_known_good
    assert (tmp_path / 'config.json').read_bytes() == document_before


@pytest.mark.parametrize('format,policy', [
    ('mihomo', 'DIRECT'), ('mihomo', 'REJECT'), ('mihomo', 'REJECT-DROP'), ('mihomo', 'PASS'), ('mihomo', 'COMPATIBLE'),
    ('surge', 'DIRECT'), ('surge', 'REJECT'), ('surge', 'REJECT-DROP'), ('surge', 'REJECT-TINYGIF'),
    ('mihomo', 'PASS-RULE'), ('surge', 'REJECT-NO-DROP'), ('surge', 'CELLULAR'),
    ('surge', 'CELLULAR-ONLY'), ('surge', 'HYBRID'), ('surge', 'NO-HYBRID'),
    ('mihomo', 'PROXY'), ('surge', 'Proxy'), ('mihomo', 'Auto'), ('surge', 'Auto'),
    ('mihomo', 'Node'), ('surge', 'Node'), ('mihomo', 'Old'), ('surge', 'Old'),
])
def test_generation_accepts_builtins_referenced_nodes_and_groups(tmp_path, format, policy):
    app, _ = make_app(tmp_path)
    client = app.test_client()
    client.post('/api/nodes', json={'id': 'n1', 'name': 'Node', 'type': 'http', 'server': 'example.test', 'port': 80, 'enabled': True, 'proxy_string': '{"type":"http","server":"example.test","port":80}'})
    if policy == 'Node':
        client.post('/api/proxy-groups', json={'id': 'source', 'name': 'Source', 'type': 'select', 'enabled': True, 'manual_nodes': ['n1']})
    if policy in ('PROXY', 'Proxy', 'Auto'):
        client.post('/api/proxy-groups', json={'id': 'default', 'name': policy, 'type': 'select', 'enabled': True, 'manual_nodes': ['n1']})
    if policy == 'Old':
        seed_strategy(client)
    client.post('/api/rules', json={'id': 'match', 'itemType': 'rule', 'rule_type': 'MATCH', 'policy': policy, 'enabled': True})
    response = client.post('/api/generate/' + format + '/preview', json={})
    assert response.status_code == 200, response.get_data(as_text=True)


def test_legacy_group_preview_remains_supported(tmp_path):
    app, _ = make_app(tmp_path)
    client = app.test_client()
    client.post('/api/proxy-groups', json={'id': 'legacy', 'name': 'Legacy', 'type': 'select', 'source': 'node', 'proxies': ['DIRECT']})
    client.post('/api/rules', json={'id': 'match', 'itemType': 'rule', 'rule_type': 'MATCH', 'policy': 'Legacy', 'enabled': True})
    response = client.post('/api/generate/mihomo/preview', json={})
    assert response.status_code == 200, response.get_data(as_text=True)
    assert 'MATCH,Legacy' in response.get_data(as_text=True)


@pytest.mark.parametrize('format', ['mihomo', 'surge'])
def test_config_download_rejects_dangling_policy_with_400(tmp_path, format):
    app, repo = make_app(tmp_path)
    client = app.test_client()
    client.post('/api/rules', json={'id': 'match', 'itemType': 'rule', 'rule_type': 'MATCH', 'policy': 'Missing', 'enabled': True})
    before = repo.get_profile('default')
    response = client.get('/api/config/' + format)
    assert response.status_code == 400, response.get_data(as_text=True)
    assert 'Missing' in response.get_json()['message']
    assert repo.get_profile('default') == before


@pytest.mark.parametrize('service_type,operation', [('mihomo', 'push'), ('surge', 'push'), ('mihomo', 'get')])
def test_agent_consumers_reject_dangling_policy_without_network_push(tmp_path, monkeypatch, service_type, operation):
    from unittest.mock import Mock
    from backend.routes import agents as agents_routes
    app, _ = make_app(tmp_path)
    client = app.test_client()
    client.post('/api/rules', json={'id': 'match', 'itemType': 'rule', 'rule_type': 'MATCH', 'policy': 'Missing', 'enabled': True})
    agent = {'id': 'qa-agent', 'name': 'QA Agent', 'service_type': service_type, 'profile_id': 'default'}
    manager = Mock()
    manager.get_agent_by_id.return_value = agent
    manager.get_agent_by_token.return_value = agent
    monkeypatch.setattr(agents_routes, 'get_agent_manager', lambda: manager)
    send = Mock(side_effect=AssertionError('Network must not be reached for invalid configuration'))
    monkeypatch.setattr('requests.post', send)
    response = client.post('/api/agents/qa-agent/push-config', json={}) if operation == 'push' else client.get('/api/agents/qa-agent/config?token=synthetic-qa-agent-token')
    assert response.status_code == 400, response.get_data(as_text=True)
    assert 'Missing' in response.get_json()['message']
    send.assert_not_called()


def test_rename_cannot_change_stable_group_id_or_collide_with_existing_name(tmp_path):
    app, repo = make_app(tmp_path)
    client = app.test_client()
    group = seed_strategy(client)
    client.post('/api/proxy-groups', json={'id': 'g2', 'name': 'Taken', 'enabled': True})
    before = repo.get_profile('default')
    for data in [{**group, 'id': 'new-id', 'name': 'New'}, {**group, 'name': 'Taken'}]:
        response = client.put('/api/proxy-groups/g1', json=data)
        assert response.status_code in (400, 409)
        assert repo.get_profile('default') == before
