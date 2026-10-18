"""DNS following publishes only explicit bindings and reconciles exact transactions."""
from copy import deepcopy
import io
import tarfile

import pytest
import requests
import yaml

from backend.agents import dns_follow
from backend.agents.deployment_bundle import build_deployment_bundle
from backend.agents.manager import AgentManager
from backend.common import config as config_store
from backend.common.config_repository import ProfileRepository
from backend.routes.agents import _prepare_transactional_content


class FollowEnvironment:
    def __init__(self, repository, manager, source, target):
        self.repository, self.manager = repository, manager
        self.source_id, self.target_id = source['id'], target['id']
        self.calls = []
        self.remote = {}
        self.uploads = []
        self.target_status = 'succeeded'
        self.lose_upload_response = False
        self.poll_offline = False
        self.same_peer = True
        self.active_matches = True
        self.listener = {'enabled': True, 'listen': '0.0.0.0:2053',
                         'host': '0.0.0.0', 'port': 2053}

    def agent(self, target=True):
        return self.manager.get_agent_by_id(self.target_id if target else self.source_id)

    def state(self):
        return self.agent().get('dns_follow_state', {})

    def request(self, agent, method, path, **kwargs):
        self.calls.append((agent['id'], method, path, deepcopy(kwargs)))
        if path == 'capabilities':
            return {'success': True, 'deployment_protocols': [1],
                    'service_type': agent['service_type'], 'config_path': 'config.yaml'}
        if path == 'dns/listener':
            return {'success': True, 'agent_id': self.source_id,
                    'deployment_id': kwargs['params']['deployment_id'], **self.listener}
        if path == 'dns/peer':
            return {'success': True, 'agent_id': self.target_id, 'same_namespace': self.same_peer,
                    'service_type': 'mihomo',
                    'peer_agent_id': self.source_id if self.same_peer else 'another-mihomo'}
        if path == 'dns/active':
            return {'success': self.active_matches, 'agent_id': self.target_id,
                    'deployment_id': kwargs['params']['deployment_id'] if self.active_matches else 'other',
                    'message': '设备当前配置与已发布版本不一致' if not self.active_matches else ''}
        if method == 'post' and path == 'deployments':
            ident = kwargs['headers']['X-Deployment-ID']
            status = self.target_status if agent['id'] == self.target_id else 'succeeded'
            result = {'success': status != 'failed', 'status': status,
                      'config_version': f'config-{ident}'}
            if status == 'failed':
                result.update(message='OxiDNS service failed to start', error_details='port already in use')
            self.remote[(agent['id'], ident)] = result
            if agent['id'] == self.target_id:
                self.uploads.append((ident, kwargs['data']))
                if self.lose_upload_response:
                    raise requests.Timeout('response lost after acceptance')
            return deepcopy(result)
        if method == 'get' and path.startswith('deployments/'):
            if self.poll_offline:
                raise requests.Timeout('Agent temporarily unreachable')
            return deepcopy(self.remote[(agent['id'], path.split('/')[1])])
        pytest.fail(f'Unexpected Agent request: {method} {path}')

    def publish_source(self, ident='source-next', status='succeeded'):
        agent = self.agent(False)
        config = self.repository.get_compat_config(agent['profile_id'])
        bundle = build_deployment_bundle(agent, config, 'dns:\n  enable: true\n  listen: 0.0.0.0:2053\n',
                                         deployment_id=ident)
        result = self.manager.publish_deployment(self.source_id, bundle, base_url='https://config.test')
        if status != 'succeeded':
            def update(agents):
                source = next(item for item in agents if item['id'] == self.source_id)
                source['deployments'][ident].update(status=status, success=False)
                source['latest_deployment'] = deepcopy(source['deployments'][ident])
            self.manager._update_agents(update)
            self.remote[(self.source_id, ident)].update(status=status, success=False)
        return result

    def bind(self, **overrides):
        return dns_follow.configure(self.manager, self.target_id, {
            'source_agent_id': self.source_id, 'upstream_tag': 'forward_remote',
            'upstream_index': 1, 'connection': 'auto', **overrides,
        })

    def advance(self, status, count=6):
        for _ in range(count):
            dns_follow.tick(self.manager)
            if self.state().get('status') == status:
                return self.state()
        pytest.fail(f'Follow did not reach {status}: {self.state()}')

    def uploaded_document(self, index=-1):
        with tarfile.open(fileobj=io.BytesIO(self.uploads[index][1]), mode='r:gz') as archive:
            return yaml.safe_load(archive.extractfile('files/config.yaml').read())


@pytest.fixture
def follow_env(tmp_path, monkeypatch):
    previous = config_store._repository
    repository = ProfileRepository(tmp_path)
    config_store.set_repository(repository)
    repository.save_system({'system_config': {'server_domain': 'https://config.test'}})
    document = {'runtime': {'worker_threads': 2}, 'plugins': [
        {'tag': 'forward_remote', 'type': 'forward', 'args': {'concurrent': 1, 'upstreams': [
            {'addr': 'udp://192.0.2.10:5353', 'timeout': '3s'},
            {'addr': 'udp://127.0.0.1:1053', 'timeout': '5s'},
        ]}},
        {'tag': 'forward_local', 'type': 'forward', 'args': {
            'upstreams': [{'addr': 'udp://192.0.2.11:53'}]}},
    ]}
    repository.save_profile('default', {'oxidns': {
        'config_mode': 'native', 'native_config': yaml.safe_dump(document, sort_keys=False),
    }})
    manager = AgentManager(repository)
    source = manager.register_agent({'name': 'Mihomo', 'host': 'mihomo.test', 'port': 8080,
                                     'service_type': 'mihomo'})
    target = manager.register_agent({'name': 'OxiDNS', 'host': 'oxidns.test', 'port': 8081,
                                     'service_type': 'oxidns'})
    env = FollowEnvironment(repository, manager, source, target)
    monkeypatch.setattr(AgentManager, '_deployment_request', lambda _self, *args, **kwargs: env.request(*args, **kwargs))
    target = env.agent()
    config, revision = repository.get_publish_snapshot('default')
    prepared = _prepare_transactional_content(target, config, base_url='https://config.test',
                                               deployment_id='target-baseline')
    result = manager.publish_deployment(target['id'], prepared['bundle'], config_revision=revision,
                                        base_url='https://config.test', resource_config=config)
    assert result['status'] == 'succeeded', result
    env.publish_source('source-before-binding')
    env.calls.clear()
    env.uploads.clear()
    yield env
    config_store.set_repository(previous)


def test_binding_draft_edits_and_failed_source_do_not_publish(follow_env):
    env = follow_env
    env.bind()
    for _ in range(2):
        dns_follow.tick(env.manager)
    env.repository.save_profile('default', {'mihomo': {'dns': {'listen': '0.0.0.0:9999'}}})
    assert env.repository.get_profile('default')['mihomo']['dns']['listen'] == '0.0.0.0:9999'
    dns_follow.tick(env.manager)
    env.publish_source('source-failed', status='failed')
    dns_follow.tick(env.manager)
    assert env.uploads == []


def test_confirmed_publication_changes_only_selected_upstream_and_is_idempotent(follow_env):
    env = follow_env
    original = yaml.safe_load(env.repository.get_profile('default')['oxidns']['native_config'])
    env.bind()
    env.publish_source()
    state = env.advance('succeeded')
    expected = deepcopy(original)
    expected['plugins'][0]['args']['upstreams'][1]['addr'] = 'udp://127.0.0.1:2053'
    assert env.uploaded_document() == expected
    assert yaml.safe_load(env.repository.get_profile('default')['oxidns']['native_config']) == expected
    assert state['source_deployment_id'] == 'source-next'
    assert state['address'] == 'udp://127.0.0.1:2053'
    assert '127.0.0.1:2053' in str(state['events'])
    assert '[INVALID URL]' not in str(state)
    assert len(env.uploads) == 1
    for _ in range(3):
        dns_follow.tick(env.manager)
    assert len(env.uploads) == 1
    listener_calls = [call for call in env.calls if call[2] == 'dns/listener']
    assert listener_calls[0][3]['params']['deployment_id'] == 'source-next'


def test_lost_child_response_reconciles_after_restart_without_duplicate_upload(follow_env):
    env = follow_env
    env.bind()
    env.lose_upload_response = True
    env.poll_offline = True
    env.publish_source()
    dns_follow.tick(env.manager)
    assert len(env.uploads) == 1
    ident = env.uploads[0][0]
    assert env.agent()['deployments'][ident]['submitted'] is True
    assert env.agent()['deployments'][ident]['status'] == 'unknown'
    env.manager = AgentManager(ProfileRepository(env.repository.data_dir))
    dns_follow.tick(env.manager)
    assert len(env.uploads) == 1
    env.poll_offline = False
    state = env.advance('succeeded')
    assert state['target_deployment_id'] == ident
    assert len(env.uploads) == 1


def test_failed_child_keeps_source_success_and_retry_creates_one_new_attempt(follow_env):
    env = follow_env
    env.bind()
    env.target_status = 'failed'
    env.publish_source()
    state = env.advance('failed')
    assert env.agent(False)['latest_deployment']['status'] == 'succeeded'
    assert 'OxiDNS' in state.get('message', '')
    assert 'port already in use' in str(state) or 'service failed' in str(state)
    assert len(env.uploads) == 1
    env.target_status = 'succeeded'
    dns_follow.retry(env.manager, env.target_id)
    env.advance('succeeded')
    assert len(env.uploads) == 2
    assert env.uploads[0][0] != env.uploads[1][0]


def test_unpublished_target_changes_block_automatic_publication(follow_env):
    env = follow_env
    env.bind()
    settings = deepcopy(env.repository.get_profile('default')['oxidns'])
    document = yaml.safe_load(settings['native_config'])
    document['runtime']['worker_threads'] = 7
    settings['native_config'] = yaml.safe_dump(document)
    env.repository.save_profile('default', {'oxidns': settings})
    env.publish_source()
    state = env.advance('failed')
    assert env.uploads == []
    assert '草稿' in str(state) or '未发布' in str(state)
    assert yaml.safe_load(env.repository.get_profile('default')['oxidns']['native_config']) == document


def test_changed_device_baseline_blocks_automatic_publication(follow_env):
    env = follow_env
    env.bind()
    env.active_matches = False
    env.publish_source()
    env.advance('failed')
    assert env.uploads == []


def test_concurrent_draft_edit_is_preserved_when_child_finishes(follow_env):
    env = follow_env
    env.bind()
    env.target_status = 'starting'
    env.publish_source()
    dns_follow.tick(env.manager)
    assert len(env.uploads) == 1
    ident = env.uploads[0][0]
    settings = deepcopy(env.repository.get_profile('default')['oxidns'])
    document = yaml.safe_load(settings['native_config'])
    document['runtime']['worker_threads'] = 8
    settings['native_config'] = yaml.safe_dump(document)
    env.repository.save_profile('default', {'oxidns': settings})
    env.remote[(env.target_id, ident)].update(status='succeeded', success=True)
    env.advance('succeeded')
    assert yaml.safe_load(env.repository.get_profile('default')['oxidns']['native_config']) == document
    assert env.agent()['applied_content']['deployment_id'] == ident


@pytest.mark.parametrize(('host', 'address'), [
    ('0.0.0.0', 'udp://127.0.0.1:2053'),
    ('127.0.0.1', 'udp://127.0.0.1:2053'),
    ('::', 'udp://[::1]:2053'),
    ('192.0.2.33', 'udp://192.0.2.33:2053'),
])
def test_verified_same_namespace_resolves_listener_address(follow_env, host, address):
    env = follow_env
    env.listener.update(host=host, listen=f'[{host}]:2053' if ':' in host else f'{host}:2053')
    env.bind()
    env.publish_source()
    env.advance('succeeded')
    assert env.uploaded_document()['plugins'][0]['args']['upstreams'][1]['addr'] == address


def test_management_address_does_not_prove_same_namespace(follow_env):
    env = follow_env
    env.manager.update_agent(env.target_id, {'host': 'mihomo.test'})
    env.bind()
    env.same_peer = False
    env.publish_source()
    env.advance('failed')
    assert env.uploads == []


def test_network_binding_uses_explicit_reachable_host(follow_env):
    env = follow_env
    env.bind(connection='network', address='192.0.2.44')
    env.same_peer = False
    env.publish_source()
    env.advance('succeeded')
    assert env.uploaded_document()['plugins'][0]['args']['upstreams'][1]['addr'] == 'udp://192.0.2.44:2053'


def test_network_binding_rejects_loopback_listener(follow_env):
    env = follow_env
    env.bind(connection='network', address='192.0.2.44')
    env.listener.update(host='127.0.0.1', listen='127.0.0.1:2053')
    env.publish_source()
    env.advance('failed')
    assert env.uploads == []


def test_disabled_binding_does_not_follow_next_source_publication(follow_env):
    env = follow_env
    env.bind()
    dns_follow.disable(env.manager, env.target_id)
    env.publish_source()
    dns_follow.tick(env.manager)
    assert env.uploads == []


def test_reregistration_retains_binding_and_inflight_job(follow_env):
    env = follow_env
    env.bind()
    env.target_status = 'starting'
    env.publish_source()
    dns_follow.tick(env.manager)
    before = env.agent()
    env.manager.register_agent({key: before[key] for key in ('name', 'host', 'port', 'service_type')},
                               existing_token=before['token'])
    after = env.agent()
    assert after['dns_follow'] == before['dns_follow']
    assert after['dns_follow_job'] == before['dns_follow_job']
    assert after['dns_follow_state'] == before['dns_follow_state']


def test_pending_source_is_polled_and_followed_without_browser_requests(follow_env):
    env = follow_env
    env.bind()
    env.publish_source(status='starting')
    dns_follow.tick(env.manager)
    assert env.uploads == []
    env.remote[(env.source_id, 'source-next')].update(status='succeeded', success=True)
    env.advance('succeeded')
    assert len(env.uploads) == 1
    assert any(call[:3] == (env.source_id, 'get', 'deployments/source-next') for call in env.calls)


@pytest.mark.parametrize('source', [False, True])
def test_profile_change_invalidates_binding_before_publication(follow_env, source):
    env = follow_env
    env.bind()
    env.publish_source()
    env.repository.create_profile({'id': 'other', 'name': 'Other'})
    env.manager.update_agent(env.source_id if source else env.target_id, {'profile_id': 'other'})
    state = env.advance('failed')
    assert '配置绑定' in state['message']
    assert env.uploads == []


@pytest.mark.parametrize('action', ['disable', 'configure', 'retry'])
def test_inflight_child_prevents_rebinding_disabling_or_new_retry(follow_env, action):
    env = follow_env
    env.bind()
    env.target_status = 'starting'
    env.publish_source()
    dns_follow.tick(env.manager)
    assert len(env.uploads) == 1
    with pytest.raises(dns_follow.FollowError):
        if action == 'configure':
            env.bind()
        else:
            getattr(dns_follow, action)(env.manager, env.target_id)
    assert len(env.uploads) == 1


@pytest.mark.parametrize('address', ['127.0.0.1', '::1', '0.0.0.0', '::', 'localhost',
                                    'udp://192.0.2.1:2053', '192.0.2.1:2053'])
def test_network_binding_rejects_ambiguous_or_unreachable_address(follow_env, address):
    with pytest.raises(dns_follow.FollowError):
        follow_env.bind(connection='network', address=address)
    assert 'dns_follow' not in follow_env.agent()


def test_listener_disabled_or_wrong_deployment_cannot_publish(follow_env):
    env = follow_env
    env.bind()
    env.listener['enabled'] = False
    env.publish_source()
    env.advance('failed')
    assert env.uploads == []
    env.listener.update(enabled=True, deployment_id='stale-source')
    dns_follow.retry(env.manager, env.target_id)
    env.advance('failed')
    assert env.uploads == []


def test_changed_bound_entry_is_not_overwritten(follow_env):
    env = follow_env
    env.bind()
    settings = deepcopy(env.repository.get_profile('default')['oxidns'])
    document = yaml.safe_load(settings['native_config'])
    document['plugins'][0]['args']['upstreams'][1]['addr'] = 'tcp://192.0.2.55:5053'
    settings['native_config'] = yaml.safe_dump(document)
    env.repository.save_profile('default', {'oxidns': settings})
    env.publish_source()
    state = env.advance('failed')
    assert '上游已被编辑或移动' in state['message']
    assert env.uploads == []
    assert yaml.safe_load(env.repository.get_profile('default')['oxidns']['native_config']) == document


def test_unchanged_address_skips_child_deployment(follow_env):
    env = follow_env
    env.bind()
    env.listener.update(port=1053, listen='0.0.0.0:1053')
    env.publish_source()
    state = env.advance('succeeded')
    assert state['target_deployment_id'] is None
    assert env.uploads == []


def test_error_state_redacts_agent_tokens_and_url_credentials(follow_env):
    env = follow_env
    env.bind()
    target_secret = env.agent()['token']
    source_secret = env.agent(False)['token']
    env.listener.update(success=False,
        message=f'Failed {target_secret} {source_secret} https://reader:private-password@dns.test/query')
    env.publish_source()
    state = env.advance('failed')
    text = str(state)
    assert target_secret not in text and source_secret not in text
    assert 'private-password' not in text
    assert 'REDACTED' in text


def test_private_job_is_not_returned_with_agent_or_binding_options(follow_env):
    from backend.routes.agents import _public_agent

    env = follow_env
    env.bind()
    env.target_status = 'starting'
    env.publish_source()
    dns_follow.tick(env.manager)
    assert 'candidate_settings' in env.agent()['dns_follow_job']
    public = _public_agent(env.agent())
    assert 'dns_follow_job' not in public
    assert 'candidate_settings' not in str(public)
    assert 'candidate_settings' not in str(dns_follow.options(env.manager, env.target_id))


@pytest.mark.parametrize(('method', 'suffix'), [('GET', ''), ('PUT', ''), ('DELETE', ''), ('POST', '/retry')])
def test_binding_routes_require_panel_authentication(follow_env, monkeypatch, method, suffix):
    from flask import Flask
    from backend.common.auth import generate_token
    from backend.routes import register_blueprints

    env = follow_env
    monkeypatch.setattr('backend.common.auth.is_auth_enabled', lambda: True)
    monkeypatch.setattr('backend.routes.agents.get_agent_manager', lambda: env.manager)
    app = Flask(__name__)
    register_blueprints(app)
    client = app.test_client()
    url = f'/api/agents/{env.target_id}/dns-follow{suffix}'
    assert client.open(url, method=method, json={}).status_code == 401
    # Authenticated reads work, while invalid authenticated mutations still
    # reach the binding validator instead of bypassing the route entirely.
    response = client.open(url, method=method, json={},
        headers={'Authorization': f"Bearer {generate_token('admin')}"})
    assert response.status_code == (200 if method in ('GET', 'DELETE') else 409)


def test_rollback_failure_remains_pending_and_cannot_start_duplicate_retry(follow_env):
    env = follow_env
    env.bind()
    env.target_status = 'rollback_failed'
    env.publish_source()
    dns_follow.tick(env.manager)
    assert env.state()['status'] == 'publishing'
    assert '回滚失败' in env.state()['message']
    with pytest.raises(dns_follow.FollowError):
        dns_follow.retry(env.manager, env.target_id)
    dns_follow.tick(env.manager)
    assert len(env.uploads) == 1


@pytest.mark.parametrize('change', ['profile_id', 'service_type'])
def test_reregistration_cannot_change_inflight_job_ownership(follow_env, change):
    env = follow_env
    env.bind()
    env.target_status = 'starting'
    env.publish_source()
    dns_follow.tick(env.manager)
    env.repository.create_profile({'id': 'other', 'name': 'Other'})
    before = env.agent()
    registration = {key: before[key] for key in ('name', 'host', 'port', 'service_type')}
    registration[change] = 'other' if change == 'profile_id' else 'mihomo'
    with pytest.raises(ValueError):
        env.manager.register_agent(registration, existing_token=before['token'])
    assert env.agent()['profile_id'] == before['profile_id']
    assert env.agent()['service_type'] == before['service_type']


def test_invalidation_before_new_source_publish_does_not_claim_mihomo_updated(follow_env):
    env = follow_env
    env.bind()
    env.manager.update_agent(env.source_id, {'enabled': False})
    state = env.advance('failed')
    assert not state['message'].startswith('Mihomo 已更新')
    assert env.uploads == []


def test_newer_source_publish_during_preparation_blocks_stale_child(follow_env, monkeypatch):
    env = follow_env
    env.bind()
    env.publish_source()
    prepare = dns_follow._prepare
    calls = []
    def change_source(*args, **kwargs):
        result = prepare(*args, **kwargs)
        calls.append(result)
        if len(calls) == 2:
            # A concurrent old worker/restored record may bypass the new
            # reservation guard; the final ownership check must still hold.
            def replace(agents):
                source = next(item for item in agents if item['id'] == env.source_id)
                source['latest_deployment'] = {**source['latest_deployment'], 'deployment_id': 'source-newer'}
                source['deployments']['source-newer'] = deepcopy(source['latest_deployment'])
            env.manager._update_agents(replace)
        return result
    monkeypatch.setattr(dns_follow, '_prepare', change_source)
    dns_follow.tick(env.manager)
    assert env.uploads == []
    assert env.state()['status'] == 'failed'
    assert '新的发布' in env.state()['message']


def test_source_profile_change_during_preparation_invalidates_binding(follow_env, monkeypatch):
    env = follow_env
    env.bind()
    env.publish_source()
    env.repository.create_profile({'id': 'other', 'name': 'Other'})
    prepare = dns_follow._prepare
    calls = []
    def change_profile(*args, **kwargs):
        result = prepare(*args, **kwargs)
        calls.append(result)
        if len(calls) == 2:
            env.manager.update_agent(env.source_id, {'profile_id': 'other'})
        return result
    monkeypatch.setattr(dns_follow, '_prepare', change_profile)
    dns_follow.tick(env.manager)
    assert env.uploads == []
    assert env.state()['status'] == 'failed'


def test_changed_managed_resource_blocks_automatic_publication(follow_env):
    env = follow_env
    settings = deepcopy(env.repository.get_profile('default')['oxidns'])
    document = yaml.safe_load(settings['native_config'])
    document['plugins'].append({'tag': 'domains', 'type': 'domain_set',
                                'args': {'files': ['rules/domains.txt']}})
    settings.update(native_config=yaml.safe_dump(document), additional_files=[{
        'path': 'rules/domains.txt', 'encoding': 'text', 'content': 'full:before.example.test\n',
    }])
    env.repository.save_profile('default', {'oxidns': settings})
    config, revision = env.repository.get_publish_snapshot('default')
    prepared = _prepare_transactional_content(env.agent(), config, base_url='https://config.test',
                                               deployment_id='baseline-with-resource')
    result = env.manager.publish_deployment(env.target_id, prepared['bundle'], config_revision=revision,
                                            base_url='https://config.test', resource_config=config)
    assert result['status'] == 'succeeded'
    env.uploads.clear()
    env.bind()
    settings['additional_files'][0]['content'] = 'full:after.example.test\n'
    env.repository.save_profile('default', {'oxidns': settings})
    env.publish_source()
    state = env.advance('failed')
    assert env.uploads == []
    assert '未发布' in state['message']


@pytest.mark.parametrize('operation', ['publish', 'rollback'])
def test_manual_publication_or_rollback_cannot_race_follow_finalization(follow_env, monkeypatch, operation):
    env = follow_env
    env.bind()
    env.target_status = 'starting'
    env.publish_source()
    dns_follow.tick(env.manager)
    ident = env.uploads[0][0]
    env.remote[(env.target_id, ident)].update(status='succeeded', success=True)
    finalize = dns_follow._finalize
    def competing_action(manager, target, state, job):
        if operation == 'publish':
            result = manager.begin_deployment(env.target_id, 'manual-overlap')
            manager.finish_deployment_preparation(env.target_id, 'manual-overlap')
        else:
            result = manager.rollback_deployment(env.target_id, ident)
        assert result.get('http_status') == 409, result
        assert not result.get('success')
        return finalize(manager, target, state, job)
    monkeypatch.setattr(dns_follow, '_finalize', competing_action)
    env.advance('succeeded')
    assert len(env.uploads) == 1


def test_newer_target_success_cannot_be_replaced_by_old_follow_finalization(follow_env, monkeypatch):
    env = follow_env
    env.bind()
    original_settings = deepcopy(env.repository.get_profile('default')['oxidns'])
    env.target_status = 'starting'
    env.publish_source()
    dns_follow.tick(env.manager)
    ident = env.uploads[0][0]
    env.remote[(env.target_id, ident)].update(status='succeeded', success=True)
    finalize = dns_follow._finalize
    def competing_success(manager, target, state, job):
        # Simulate state restored by a separate process/version which did not
        # honor the follow reservation; finalization still must fail closed.
        def replace(agents):
            current = next(item for item in agents if item['id'] == env.target_id)
            current['latest_deployment'] = {'deployment_id': 'manual-newer', 'status': 'succeeded',
                'success': True, 'profile_id': 'default', 'service_type': 'oxidns'}
            current['deployments']['manual-newer'] = deepcopy(current['latest_deployment'])
            current['applied_content'] = {**current['applied_content'], 'deployment_id': 'manual-newer',
                                          'fingerprint': 'manual-newer-fingerprint'}
        manager._update_agents(replace)
        return finalize(manager, target, state, job)
    monkeypatch.setattr(dns_follow, '_finalize', competing_success)
    dns_follow.tick(env.manager)
    assert env.repository.get_profile('default')['oxidns'] == original_settings
    assert env.agent()['applied_content']['deployment_id'] == 'manual-newer'
    assert env.state()['status'] == 'failed'


@pytest.mark.parametrize('operation', ['publish', 'rollback'])
@pytest.mark.parametrize('stage', ['preparing', 'publishing'])
def test_source_publication_or_rollback_is_blocked_during_follow(follow_env, monkeypatch, operation, stage):
    env = follow_env
    env.bind()
    env.publish_source()
    attempted = []
    def competing_action():
        assert env.state()['status'] == stage
        if operation == 'publish':
            result = env.manager.begin_deployment(env.source_id, 'source-overlap')
            env.manager.finish_deployment_preparation(env.source_id, 'source-overlap')
        else:
            result = env.manager.rollback_deployment(env.source_id, 'source-next')
        assert result.get('http_status') == 409, result
        assert not result.get('success')
        attempted.append(result)
    if stage == 'preparing':
        prepare = dns_follow._prepare
        def prepare_with_competitor(*args, **kwargs):
            if not attempted:
                competing_action()
            return prepare(*args, **kwargs)
        monkeypatch.setattr(dns_follow, '_prepare', prepare_with_competitor)
        env.advance('succeeded')
    else:
        env.target_status = 'starting'
        dns_follow.tick(env.manager)
        competing_action()
        ident = env.uploads[0][0]
        env.remote[(env.target_id, ident)].update(status='succeeded', success=True)
        env.advance('succeeded')
    assert attempted and len(env.uploads) == 1
    assert env.agent(False)['latest_deployment']['deployment_id'] == 'source-next'
    assert env.publish_source('source-after-follow')['status'] == 'succeeded'


@pytest.mark.parametrize('change', ['deployment_id', 'status', 'manual_rollback', 'profile_id'])
def test_source_state_change_cannot_confirm_old_follow_or_overwrite_draft(follow_env, monkeypatch, change):
    env = follow_env
    env.bind()
    original_settings = deepcopy(env.repository.get_profile('default')['oxidns'])
    env.target_status = 'starting'
    env.publish_source()
    dns_follow.tick(env.manager)
    ident = env.uploads[0][0]
    env.remote[(env.target_id, ident)].update(status='succeeded', success=True)
    env.repository.create_profile({'id': 'other', 'name': 'Other'})
    finalize = dns_follow._finalize
    def stale_source(manager, target, state, job):
        # Simulate a state change from an old worker or restored store. The
        # publication guards protect normal writes; finalization checks again.
        def replace(agents):
            source = next(item for item in agents if item['id'] == env.source_id)
            if change == 'profile_id':
                source['profile_id'] = 'other'
            else:
                value = {'deployment_id': 'source-newer', 'status': 'rolled_back',
                         'manual_rollback': True}[change]
                source['latest_deployment'][change] = value
        manager._update_agents(replace)
        return finalize(manager, target, state, job)
    monkeypatch.setattr(dns_follow, '_finalize', stale_source)
    dns_follow.tick(env.manager)
    assert env.repository.get_profile('default')['oxidns'] == original_settings
    assert env.state()['status'] == 'failed'
    assert '均已发布成功' not in env.state()['message']
    assert env.agent()['applied_content']['deployment_id'] == ident


def test_worker_lock_excludes_another_process(follow_env):
    import subprocess
    import sys

    env = follow_env
    code = '''
import sys
from backend.agents import dns_follow
from backend.agents.manager import AgentManager
from backend.common.config_repository import ProfileRepository
manager = AgentManager(ProfileRepository(sys.argv[1]))
try:
    with dns_follow._job_lock(manager):
        raise SystemExit('second process acquired an owned lock')
except dns_follow.FollowError:
    pass
'''
    with dns_follow._job_lock(env.manager):
        result = subprocess.run([sys.executable, '-c', code, str(env.repository.data_dir)],
                                capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr or result.stdout
    # Releasing the owner permits work to continue on the next round.
    with dns_follow._job_lock(AgentManager(ProfileRepository(env.repository.data_dir))):
        pass


def test_start_loop_is_registered_once_and_agent_initialization_starts_it(follow_env, monkeypatch):
    from backend.common import agent_manager

    env = follow_env
    started = []
    class CapturedThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
        def start(self):
            started.append(self.kwargs)
    monkeypatch.setattr(dns_follow, '_loops', set())
    monkeypatch.setattr(dns_follow.threading, 'Thread', CapturedThread)
    dns_follow.start_loop(env.manager)
    dns_follow.start_loop(AgentManager(ProfileRepository(env.repository.data_dir)))
    assert len(started) == 1 and started[0]['daemon'] is True
    called = []
    monkeypatch.setattr(dns_follow, 'start_loop', called.append)
    monkeypatch.setattr(agent_manager, '_agent_manager', None)
    monkeypatch.setattr(agent_manager, 'get_repository', lambda: env.repository)
    manager = agent_manager.init_agent_manager()
    assert called == [manager]
