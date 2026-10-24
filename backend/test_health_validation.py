import pytest
from backend.utils.health_check import PROVIDER_DEFAULTS

@pytest.mark.parametrize('path,payload', [('/api/subscriptions', {'id':'s','name':'S','url':'https://example.test/sub'}), ('/api/aggregations', {'name':'A'})])
def test_new_sources_receive_shared_defaults(tmp_path, path, payload):
    from flask import Flask
    from backend.common import config
    from backend.routes import register_blueprints
    config.set_repository(ProfileRepository(tmp_path))
    app = Flask(__name__)
    register_blueprints(app)
    client = app.test_client()
    response = client.post(path, json=payload)
    assert response.status_code == 200
    assert response.json['data'].get('health_check') == PROVIDER_DEFAULTS

import pytest
from backend.common.config_repository import ProfileRepository, ProfileValidationError

@pytest.mark.parametrize('path,payload', [('/api/subscriptions', {'id':'s','name':'S','url':'https://example.test/sub'}), ('/api/aggregations', {'name':'A'})])
def test_create_legacy_probe_url_remains_explicit(tmp_path,path,payload):
    from flask import Flask
    from backend.common import config
    from backend.routes import register_blueprints
    config.set_repository(ProfileRepository(tmp_path))
    app=Flask(__name__)
    register_blueprints(app)
    response=app.test_client().post(path,json={**payload,'health_check_url':'https://example.test/legacy'})
    assert response.status_code==200
    assert response.json['data']['health_check']['url']=='https://example.test/legacy'


@pytest.mark.parametrize('path', ['/api/subscriptions','/api/aggregations'])
@pytest.mark.parametrize('payload', [[], 'bad', False])
def test_source_api_rejects_nonobject(tmp_path,path,payload):
    from flask import Flask
    from backend.common import config
    from backend.routes import register_blueprints
    config.set_repository(ProfileRepository(tmp_path))
    app = Flask(__name__)
    register_blueprints(app)
    client=app.test_client()
    assert client.post(path,json=payload).status_code == 400
    created=client.post(path,json={'id':'s','name':'Source','url':'https://example.test'})
    assert created.status_code == 200
    ident=created.json['data']['id']
    assert client.put(path+'/'+ident,json=payload).status_code == 400


@pytest.mark.parametrize('tool,path', [('manage_subscription','/api/subscriptions'),('manage_aggregation','/api/aggregations')])
def test_mcp_partial_health_update_preserves_other_explicit_fields(tmp_path,tool,path):
    from flask import Flask
    from backend.common import config
    from backend.routes import register_blueprints
    from backend.mcp_server import mcp_bp
    repo=ProfileRepository(tmp_path)
    repo.save_system({'system_config':{'config_token':'health-fixture-token'}})
    config.set_repository(repo)
    app=Flask(__name__)
    register_blueprints(app)
    app.register_blueprint(mcp_bp)
    client=app.test_client()
    created=client.post(path,json={'id':'s','name':'S','url':'https://example.test','health_check':{'enable':False,'timeout':8250,'lazy':True}}).json['data']
    response=client.post('/mcp',headers={'Authorization':'Bearer health-fixture-token'},json={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':tool,'arguments':{'action':'update','id':created['id'],'data':{'health_check':{'lazy':False}}}}})
    assert response.json['result']['isError'] is False
    saved=next(s for s in client.get(path).json if s['id']==created['id'])
    assert saved['health_check']=={'enable':False,'timeout':8250,'lazy':False}


def test_switch_to_select_cleans_engine_fields(tmp_path):
    from flask import Flask
    from backend.common import config
    from backend.routes import register_blueprints
    config.set_repository(ProfileRepository(tmp_path))
    app = Flask(__name__)
    register_blueprints(app)
    client = app.test_client()
    payload = {'id':'g','name':'G','type':'url-test','manual_nodes':['DIRECT']}
    assert client.post('/api/proxy-groups', json=payload).status_code == 200
    assert client.put('/api/proxy-groups/g', json={**payload, 'type':'select'}).status_code == 200
    group = client.get('/api/proxy-groups').json[0]
    assert not set(group) & {'url','interval','lazy','timeout','expected-status','tolerance'}
    assert client.put('/api/proxy-groups/g', json={**payload, 'type':'url-test'}).status_code == 200
    converted=client.get('/api/proxy-groups').json[0]
    assert converted.get('timeout') == 5000
    assert converted.get('lazy') is False


def test_new_group_cannot_silently_become_direct(tmp_path):
    from flask import Flask
    from backend.common import config
    from backend.routes import register_blueprints
    config.set_repository(ProfileRepository(tmp_path))
    app = Flask(__name__)
    register_blueprints(app)
    response = app.test_client().post('/api/proxy-groups', json={'id':'g','name':'G','type':'url-test'})
    assert response.status_code == 400


def test_mcp_advertises_typed_health_fields():
    from backend.mcp_server.tools import list_tools
    tools = {t['name']: t for t in list_tools()}
    for name in ('manage_subscription', 'manage_aggregation'):
        props = tools[name]['inputSchema']['properties']['data']['properties']['health_check']['properties']
        assert props['lazy']['type'] == 'boolean'
        assert props['timeout']['type'] == 'integer'
    assert tools['manage_proxy_group']['inputSchema']['properties']['data']['properties']['timeout']['type'] == 'integer'


@pytest.mark.parametrize('field,value', [('enable', 'false'), ('lazy', 0), ('timeout', None), ('expected-status', '299-200'), ('unknown', 1)])
def test_shared_health_validation_is_atomic(tmp_path, field, value):
    repo = ProfileRepository(tmp_path)
    before = repo._store.fingerprint()
    with pytest.raises(ProfileValidationError):
        repo.update_shared_transaction(lambda p: p['subscriptions'].append({'id':'s','name':'S','url':'https://example.test', 'health_check': {field:value}}))
    assert repo._store.fingerprint() == before

@pytest.mark.parametrize('field,value', [('interval', True), ('interval', 0), ('timeout', '5000'), ('timeout', -1), ('lazy', 'false'), ('url', 'ftp://probe.test'), ('url', 'http://@example.test'), ('url', 'http://example.test:0'), ('expected-status', '204,abc'), ('tolerance', -1)])
def test_repository_rejects_invalid_group_health_atomically(tmp_path, field, value):
    repo = ProfileRepository(tmp_path)
    before = repo._store.fingerprint()
    with pytest.raises(ProfileValidationError):
        repo.update_profile_transaction('default', lambda p: p['proxy_groups'].append({'id':'g','name':'G','type':'url-test','manual_nodes':['DIRECT'],field:value}))
    assert repo._store.fingerprint() == before
