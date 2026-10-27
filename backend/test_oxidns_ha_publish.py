"""HA publication must install one snapshot, standby first, even across workers."""
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import io
import json
import tarfile
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
import requests

from backend.agents import oxidns_ha
from backend.agents.deployment_bundle import build_deployment_bundle
from backend.agents.manager import AgentManager
from backend.common import config as config_store
from backend.common.config_repository import ProfileRepository


PRIMARY = '192.0.2.20'
SECONDARY = '192.0.2.21'
VIP = '192.0.2.53'
CONFIG = 'plugins:\n  - tag: shared_rules\n    type: domain_set\n    args:\n      files: [rules/shared.txt]\n'


def archive_files(raw):
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as archive:
        manifest = json.load(archive.extractfile('manifest.json'))
        files = {item['path']: archive.extractfile('files/' + item['path']).read()
                 for item in manifest['files']}
    return manifest, files


class PairDevices:
    """Protocol boundary only: preparation, receipts and persistence stay real."""

    def __init__(self):
        self.calls = []
        self.uploads = []
        self.journals = {}
        self.candidates = {}
        self.installed = {PRIMARY: {'config.yaml': b'primary original'},
                          SECONDARY: {'config.yaml': b'secondary original'}}
        self.upload_status = {}
        self.activation_status = {}
        self.unreachable_status = set()
        self.before_upload = None
        self.ha_applied = []
        self.vip_holder = PRIMARY
        self.unhealthy = set()

    def request(self, agent, method, path, **kwargs):
        host = agent['host']
        self.calls.append((host, method, path))
        if (method, path) == ('get', 'capabilities'):
            return {'success': True, 'service_type': 'oxidns', 'deployment_protocols': [1],
                    'oxidns_ha_protocols': [1], 'reload_only_protocols': [1], 'config_path': 'config.yaml'}
        if (method, path) == ('post', 'deployments'):
            manifest, files = archive_files(kwargs['data'])
            ident = manifest['deployment_id']
            assert manifest['agent_id'] == agent['id']
            assert kwargs['headers']['X-Content-SHA256'] == hashlib.sha256(kwargs['data']).hexdigest()
            self.uploads.append({'host': host, 'id': ident, 'files': files,
                                 'activate': kwargs['headers']['X-Activate'] == 'true'})
            self.candidates[host, ident] = files
            if self.before_upload:
                self.before_upload(host)
            status = self.upload_status.get(host, 'succeeded' if self.uploads[-1]['activate'] else 'ready')
            self.journals[host, ident] = {'success': status not in ('failed', 'rolled_back', 'rollback_failed'),
                                         'deployment_id': ident, 'status': status,
                                         'sha256': kwargs['headers']['X-Content-SHA256'],
                                         'config_version': hashlib.sha256(files['config.yaml']).hexdigest()[:8]}
            if status == 'succeeded':
                self.installed[host] = deepcopy(files)
            return deepcopy(self.journals[host, ident])
        assert path.startswith('deployments/'), (method, path)
        ident = path.split('/')[1]
        key = host, ident
        if method == 'get':
            if host in self.unreachable_status:
                raise requests.Timeout('status reply lost')
            return deepcopy(self.journals.get(key, {'success': False, 'http_status': 404}))
        if method == 'post' and path.endswith('/activate'):
            status = self.activation_status.get(host, 'succeeded')
            self.journals[key].update(status=status, success=status not in ('failed', 'rolled_back'))
            if status == 'succeeded':
                self.installed[host] = deepcopy(self.candidates[key])
            return deepcopy(self.journals[key])
        pytest.fail(f'unexpected deployment request: {host} {method} {path}')

    def status(self, agent):
        master = agent['host'] == self.vip_holder
        return {'protocol': 1, 'managed': {'active': True, 'pending': False},
                'runtime': {'keepalived_running': True, 'role': 'master' if master else 'backup',
                            'vip_present': master, 'healthy': agent['host'] not in self.unhealthy}, 'error': ''}

    def preview(self, _agent, _spec):
        return {'ok': True, 'blockers': [], 'interface': 'eth0', 'cidr': '192.0.2.0/24'}

    def apply(self, agent, _spec):
        self.ha_applied.append(agent['host'])
        return {'protocol': 1}

    def mutations(self):
        return [(host, path) for host, method, path in self.calls if method == 'post']


@dataclass
class Harness:
    repository: ProfileRepository
    manager: AgentManager
    devices: PairDevices
    primary: dict
    secondary: dict
    config: dict

    def pair(self):
        def save(items):
            for item in items:
                if item['id'] not in (self.primary['id'], self.secondary['id']):
                    continue
                is_primary = item['id'] == self.primary['id']
                item['oxidns_ha'] = {'enabled': True, 'group_id': 'ha-fixture', 'vip': VIP, 'vrid': 53,
                                     'peer_id': self.secondary['id'] if is_primary else self.primary['id'],
                                     'role': 'primary' if is_primary else 'secondary'}
                item['oxidns_ha_state'] = {'status': 'succeeded', 'error': ''}
        self.manager._update_agents(save)

    def bundle(self, owner=None, ident='ha-publish', rule='domain:shared.example'):
        owner = owner or self.primary
        return build_deployment_bundle(owner, self.config, CONFIG, deployment_id=ident,
                                       custom_files=[{'path': 'rules/shared.txt', 'content': rule}],
                                       local_dns_watchdog=False)

    def drain(self, owner=None, manager=None, attempts=16):
        from backend.agents import oxidns_ha_publish
        for _ in range(attempts):
            oxidns_ha_publish.advance(manager or self.manager, (owner or self.primary)['id'])


@pytest.fixture
def ha_publish(tmp_path, monkeypatch):
    from backend.agents import oxidns_ha_publish
    previous = config_store._repository
    repository = ProfileRepository(tmp_path)
    config_store.set_repository(repository)
    manager = AgentManager(repository)
    devices = PairDevices()
    monkeypatch.setattr(oxidns_ha_publish, 'start_loop', lambda _manager: None)
    monkeypatch.setattr(oxidns_ha, 'SETTLE_SECONDS', 0)
    monkeypatch.setattr(AgentManager, '_deployment_request',
                        lambda _self, agent, method, path, **kwargs: devices.request(agent, method, path, **kwargs))
    for name in ('status', 'preview', 'apply'):
        monkeypatch.setattr(AgentManager, f'oxidns_ha_{name}',
                            lambda _self, *args, _name=name: getattr(devices, _name)(*args))
    monkeypatch.setattr(AgentManager, 'oxidns_ha_remove', lambda _self, _agent: {'removed': True})
    monkeypatch.setattr('requests.sessions.Session.request',
                        lambda *_args, **_kwargs: pytest.fail('unexpected external request'))
    primary = manager.register_agent({'name': 'DNS primary', 'host': PRIMARY, 'port': 8082,
                                      'profile_id': 'default', 'service_type': 'oxidns',
                                      'deployment_method': 'shell', 'version': '1.8.45-go'})
    secondary = manager.register_agent({'name': 'DNS secondary', 'host': SECONDARY, 'port': 8082,
                                        'profile_id': 'default', 'service_type': 'oxidns',
                                        'deployment_method': 'shell', 'version': '1.8.45-go'})
    for agent in (primary, secondary):
        manager.update_heartbeat(agent['id'], {'service_status': 'running'})
    primary, secondary = (manager.get_agent_by_id(agent['id']) for agent in (primary, secondary))
    config = {'profile_id': 'default', 'rule_library': [], 'system_config': {'server_domain': 'https://config.test'}}
    yield Harness(repository, manager, devices, primary, secondary, config)
    config_store.set_repository(previous)


@pytest.mark.parametrize('entry', ['primary', 'secondary'])
def test_publish_from_either_member_installs_identical_files_standby_first(ha_publish, entry):
    env = ha_publish
    env.pair()
    owner = getattr(env, entry)
    candidate = env.bundle(owner)
    expected = archive_files(candidate.archive)[1]
    checked = []

    def before_upload(host):
        if host == PRIMARY:
            assert env.devices.installed[SECONDARY] == expected
            checked.append(host)
    env.devices.before_upload = before_upload
    env.manager.publish_deployment(owner['id'], candidate)
    env.drain(owner)
    result = env.manager.get_deployment(owner['id'], candidate.manifest['deployment_id'])
    assert result['status'] == 'succeeded'
    assert [item['host'] for item in env.devices.uploads] == [SECONDARY, PRIMARY]
    assert checked == [PRIMARY]
    assert env.devices.installed[PRIMARY] == env.devices.installed[SECONDARY] == expected
    first = env.manager.get_agent_by_id(env.primary['id'])
    second = env.manager.get_agent_by_id(env.secondary['id'])
    assert first['applied_content']['fingerprint'] == second['applied_content']['fingerprint']


def test_initial_enable_materializes_once_and_synchronizes_different_configs_before_keepalived(ha_publish, monkeypatch):
    from backend.routes import agents as agent_routes
    env = ha_publish
    env.repository.save_profile('default', {'oxidns': {
        'config_mode': 'native', 'native_config': CONFIG,
        'additional_files': [{'path': 'rules/shared.txt', 'content': 'domain:first-enable.example'}]}})
    prepare = agent_routes._prepare_transactional_content
    preparations = []

    def counted_prepare(*args, **kwargs):
        prepared = prepare(*args, **kwargs)
        preparations.append(prepared['bundle'])
        return prepared
    monkeypatch.setattr(agent_routes, '_prepare_transactional_content', counted_prepare)
    applied = env.devices.apply

    def apply_only_after_both_configs_match(agent, spec):
        assert env.devices.installed[PRIMARY] == env.devices.installed[SECONDARY]
        assert env.devices.installed[PRIMARY]['rules/shared.txt'] == b'domain:first-enable.example'
        return applied(agent, spec)
    monkeypatch.setattr(env.devices, 'apply', apply_only_after_both_configs_match)
    oxidns_ha.configure(env.manager, env.primary['id'], {'peer_id': env.secondary['id'], 'vip': VIP})
    # Enabling returns before network preparation; the persisted job is recoverable.
    assert preparations == [] and env.devices.uploads == []
    env.repository.save_profile('default', {'oxidns': {
        'config_mode': 'native', 'native_config': CONFIG,
        'additional_files': [{'path': 'rules/shared.txt', 'content': 'domain:newer-draft.example'}]}})
    env.drain()
    state = oxidns_ha.status(env.manager, env.primary['id'])
    assert state['state']['status'] == 'succeeded'
    assert len(preparations) == 1
    assert [item['host'] for item in env.devices.uploads] == [SECONDARY, PRIMARY]
    assert env.devices.ha_applied == [SECONDARY, PRIMARY]
    assert env.devices.installed[PRIMARY] == env.devices.installed[SECONDARY]


@pytest.mark.parametrize('standby_status', ['failed', 'rolled_back', 'rollback_failed'])
def test_standby_failure_never_uploads_or_activates_primary(ha_publish, standby_status):
    env = ha_publish
    env.pair()
    env.devices.upload_status[SECONDARY] = standby_status
    original = deepcopy(env.devices.installed[PRIMARY])
    candidate = env.bundle()
    env.manager.publish_deployment(env.primary['id'], candidate)
    env.drain()
    result = env.manager.get_deployment(env.primary['id'], candidate.manifest['deployment_id'])
    assert result['status'] in ('failed', 'rolled_back', 'rollback_failed')
    assert all(host == SECONDARY for host, _ in env.devices.mutations())
    assert env.devices.installed[PRIMARY] == original
    assert 'applied_content' not in env.manager.get_agent_by_id(env.primary['id'])


def test_unhealthy_standby_blocks_primary_despite_successful_deployment_receipt(ha_publish):
    env = ha_publish
    env.pair()
    env.devices.unhealthy.add(SECONDARY)
    candidate = env.bundle()
    env.manager.publish_deployment(env.primary['id'], candidate)
    env.drain()
    result = env.manager.get_deployment(env.primary['id'], candidate.manifest['deployment_id'])
    assert result['status'] == 'failed'
    assert '健康检查' in result['message']
    assert [item['host'] for item in env.devices.uploads] == [SECONDARY]
    assert env.devices.installed[PRIMARY] == {'config.yaml': b'primary original'}


def test_primary_failure_is_not_hidden_by_successful_secondary_entry_record(ha_publish):
    env = ha_publish
    env.pair()
    env.devices.upload_status[PRIMARY] = 'failed'
    candidate = env.bundle(env.secondary)
    env.manager.publish_deployment(env.secondary['id'], candidate)
    env.drain(env.secondary)
    result = env.manager.get_deployment(env.secondary['id'], candidate.manifest['deployment_id'])
    assert result['status'] == 'failed' and not result['success']
    secondary = env.manager.get_agent_by_id(env.secondary['id'])
    assert secondary['deployments'][candidate.manifest['deployment_id']]['status'] == 'succeeded'
    assert env.devices.installed[PRIMARY] == {'config.yaml': b'primary original'}


@pytest.mark.parametrize('status_unavailable', [False, True], ids=['unknown', 'status-timeout'])
def test_unknown_or_unconfirmed_standby_blocks_primary_and_new_publications(ha_publish, status_unavailable):
    env = ha_publish
    env.pair()
    env.devices.upload_status[SECONDARY] = 'unknown' if not status_unavailable else 'verifying'
    if status_unavailable:
        env.devices.unreachable_status.add(SECONDARY)
    candidate = env.bundle()
    env.manager.publish_deployment(env.primary['id'], candidate)
    env.drain()
    result = env.manager.get_deployment(env.primary['id'], candidate.manifest['deployment_id'])
    assert result['status'] not in ('succeeded', 'failed', 'ready')
    assert all(host == SECONDARY for host, _ in env.devices.mutations())
    competing = env.manager.begin_deployment(env.secondary['id'], 'competing')
    assert not competing['success'] and competing['http_status'] == 409


def test_confirmed_standby_result_resumes_after_lost_reply_without_reupload(ha_publish):
    env = ha_publish
    env.pair()
    env.devices.upload_status[SECONDARY] = 'unknown'
    candidate = env.bundle()
    env.manager.publish_deployment(env.primary['id'], candidate)
    env.drain()
    assert [item['host'] for item in env.devices.uploads] == [SECONDARY]
    secondary_id = env.devices.uploads[0]['id']
    # The Agent completed its durable task while the management server was away.
    env.devices.journals[SECONDARY, secondary_id].update(status='succeeded', success=True)
    env.devices.installed[SECONDARY] = deepcopy(env.devices.candidates[SECONDARY, secondary_id])
    restarted = AgentManager(env.repository)
    env.drain(manager=restarted)
    assert restarted.get_deployment(env.primary['id'], candidate.manifest['deployment_id'])['status'] == 'succeeded'
    assert [item['host'] for item in env.devices.uploads] == [SECONDARY, PRIMARY]
    assert env.devices.installed[PRIMARY] == env.devices.installed[SECONDARY]


def test_current_vip_holder_is_published_last_after_failover(ha_publish):
    env = ha_publish
    env.pair()
    env.devices.vip_holder = SECONDARY
    candidate = env.bundle(env.secondary)
    env.manager.publish_deployment(env.secondary['id'], candidate)
    env.drain(env.secondary)
    result = env.manager.get_deployment(env.secondary['id'], candidate.manifest['deployment_id'])
    assert result['status'] == 'succeeded'
    assert [item['host'] for item in env.devices.uploads] == [PRIMARY, SECONDARY]


def test_completed_group_replay_does_not_reupload_either_member(ha_publish):
    env = ha_publish
    env.pair()
    candidate = env.bundle()
    env.manager.publish_deployment(env.primary['id'], candidate)
    env.drain()
    assert len(env.devices.uploads) == 2
    replay = env.manager.publish_deployment(env.primary['id'], candidate)
    env.drain()
    assert replay['status'] == 'succeeded'
    assert len(env.devices.uploads) == 2


def test_staged_pair_resumes_after_manager_restart_and_activates_standby_before_primary(ha_publish):
    env = ha_publish
    env.pair()
    candidate = env.bundle()
    original = deepcopy(env.devices.installed)
    env.manager.publish_deployment(env.primary['id'], candidate, activate=False)
    env.drain()
    result = env.manager.get_deployment(env.primary['id'], candidate.manifest['deployment_id'])
    assert result['status'] == 'ready'
    assert len(env.devices.uploads) == 2
    assert not any(item['activate'] for item in env.devices.uploads)
    assert env.devices.installed == original
    secondary_id = next(item['id'] for item in env.devices.uploads if item['host'] == SECONDARY)
    # A newer draft must not replace the already prepared pair candidate.
    env.repository.save_profile('default', {'oxidns': {'config_mode': 'native', 'native_config': 'plugins: []\n'}})
    restarted = AgentManager(env.repository)
    restarted.activate_deployment(env.secondary['id'], secondary_id)
    env.drain(env.secondary, restarted)
    result = restarted.get_deployment(env.primary['id'], candidate.manifest['deployment_id'])
    assert result['status'] == 'succeeded'
    activations = [host for host, path in env.devices.mutations() if path.endswith('/activate')]
    assert activations == [SECONDARY, PRIMARY]
    assert env.devices.installed[PRIMARY] == env.devices.installed[SECONDARY] == archive_files(candidate.archive)[1]


def test_competing_publish_from_other_worker_cannot_reserve_peer_during_upload(ha_publish):
    env = ha_publish
    env.pair()
    entered = Event()
    release = Event()
    candidate = env.bundle()
    other = AgentManager(env.repository)

    def block_secondary(host):
        if host == SECONDARY:
            entered.set()
            assert release.wait(5), 'concurrency test did not release the first upload'
    env.devices.before_upload = block_secondary
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(env.manager.publish_deployment, env.primary['id'], candidate)
        try:
            assert entered.wait(5), 'first publication did not reach the secondary'
            rejection = other.begin_deployment(env.secondary['id'], 'other-worker')
            assert rejection['http_status'] == 409 and not rejection['success']
        finally:
            release.set()
        future.result(timeout=5)
    env.drain()
    assert [item['host'] for item in env.devices.uploads] == [SECONDARY, PRIMARY]


def test_non_ha_oxidns_preserves_single_agent_publish_behavior(ha_publish):
    env = ha_publish
    candidate = env.bundle()
    result = env.manager.publish_deployment(env.primary['id'], candidate)
    assert result['status'] == 'succeeded'
    assert [item['host'] for item in env.devices.uploads] == [PRIMARY]
    assert env.devices.uploads[0]['activate'] is True
    assert env.devices.installed[SECONDARY] == {'config.yaml': b'secondary original'}
    assert 'oxidns_ha_publication' not in env.manager.get_agent_by_id(env.primary['id'])
