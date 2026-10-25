"""Confirmation, profile/router isolation, compensation, and legacy output boundaries."""
from copy import deepcopy
from types import SimpleNamespace
import json

import pytest

from backend.common import client_discovery, client_fixed_ip as fixed
from backend.common.client_devices import list_devices
from backend.routers.base import RouterConflictError, RouterError
from backend.test_oxidns_integration import oxidns_app  # noqa: F401

BASE = '/api/client-devices/fixed-ip/'
MAC = '02:11:22:33:44:55'
IP = '192.0.2.50'


class Adapter:
    def __init__(self):
        self.records, self.calls = {}, []
        self.version, self.fail = 1, ''

    def read(self, driver):
        return self.records

    def inspect(self, snapshot, ip, mac):
        binding = snapshot.get(ip)
        return {'owned': bool(binding and binding['mac'] == mac), 'binding': binding}

    def preview(self, driver, ip, mac, name):
        if ip in self.records:
            raise RouterConflictError('该 MAC 已有静态租约')
        return {'ip': ip, 'mac': mac, 'comment': 'cf-client:' + name, 'summary': '固定 ' + ip,
                'version': self.version}

    def apply(self, driver, op):
        if self.fail == op['ip']:
            raise RouterError('注入写入失败')
        binding = {key: op[key] for key in ('ip', 'mac', 'comment')}
        binding.update(id='*1', vendor=driver.vendor, server='lan')
        self.records[op['ip']] = binding
        self.calls.append(('add', driver.router_id, op['ip']))
        return binding

    def rollback(self, driver, binding):
        self.records.pop(binding['ip'])
        self.calls.append(('rollback', driver.router_id, binding['ip']))

    def preview_delete(self, driver, binding):
        if not binding.get('comment', '').startswith('cf-client:'):
            raise RouterConflictError('不会删除其他静态租约')
        if self.records.get(binding['ip']) != {k: v for k, v in binding.items() if k != 'router_id'}:
            raise RouterConflictError('绑定已经变化')
        return {**binding, 'summary': '删除 ' + binding['ip']}

    def delete(self, driver, op):
        self.calls.append(('delete', driver.router_id, op['ip']))
        return self.records.pop(op['ip'])

    def restore_deleted(self, driver, receipt):
        restored = {**receipt, 'id': '*2'}
        self.records[receipt['ip']] = restored
        return restored


@pytest.fixture
def fixed_app(oxidns_app, monkeypatch):
    app, repo = oxidns_app
    module = Adapter()
    opened = []
    def open_driver(router):
        opened.append(router['id'])
        return SimpleNamespace(vendor=router['type'], router_id=router['id'], supports_write=True, close=lambda: None)
    monkeypatch.setattr(client_discovery, 'open_driver', open_driver)
    monkeypatch.setattr(fixed, 'adapter', lambda vendor: module)
    monkeypatch.setattr(fixed, '_seen', lambda *args: True)
    def seed(config):
        config['device_notes'] = {IP: '电脑', '192.0.2.51': '手机'}
        config['routers'] = {'items': [
            {'id': 'r1', 'name': 'ROS', 'type': 'routeros', 'endpoint': 'http://192.0.2.1', 'username': 'a'},
            {'id': 'r2', 'name': 'iKuai', 'type': 'ikuai', 'endpoint': 'http://192.0.2.2', 'username': 'b'},
        ]}
    repo.update_profile_transaction('office', seed)
    return app.test_client(), repo, module, opened


def request_body(operation='add', ip=IP, router='r1'):
    return {'profile_id': 'office', 'operation': operation,
            'devices': [{'ip': ip, 'mac': MAC, 'router_id': router, 'name': '电脑'}]}


def preview(client, body=None):
    response = client.post(BASE + 'preview?profile=office', json=body or request_body())
    assert response.status_code == 200, response.get_json()
    return response.get_json()['token']


def apply(client, token, profile='office'):
    return client.post(BASE + 'apply?profile=' + profile, json={'profile_id': profile, 'token': token})


def test_preview_is_read_only_and_apply_targets_only_the_discovery_router(fixed_app):
    client, repo, module, opened = fixed_app
    before = deepcopy(repo.get_profile('office'))
    token = preview(client, request_body(router='r2'))
    assert module.calls == [] and repo.get_profile('office') == before
    assert opened == ['r2']
    response = apply(client, token)
    assert response.status_code == 200, response.get_json()
    binding = repo.get_profile('office')[fixed.KEY][IP]
    assert binding['router_id'] == 'r2' and binding['mac'] == MAC
    assert module.calls == [('add', 'r2', IP)]
    assert response.get_json()['devices'][0]['fixed_ip'] == binding
    assert apply(client, token).status_code == 409


def test_missing_tampered_wrong_profile_confirmation_never_writes(fixed_app):
    client, _, module, _ = fixed_app
    token = preview(client)
    for value, profile in [('', 'office'), ('fake', 'office'), (token + 'x', 'office'), (token, 'default')]:
        assert apply(client, value, profile).status_code == 409
    assert not module.calls


def test_expired_confirmation_never_writes(fixed_app, monkeypatch):
    import itsdangerous.timed
    client, _, module, _ = fixed_app
    token = preview(client)
    original = itsdangerous.timed.TimestampSigner.get_timestamp
    monkeypatch.setattr(itsdangerous.timed.TimestampSigner, 'get_timestamp', lambda self: original(self) + 601)
    assert apply(client, token).status_code == 409
    assert not module.calls


def test_changed_router_or_lease_requires_new_preview(fixed_app):
    client, repo, module, _ = fixed_app
    token = preview(client)
    module.version += 1
    assert apply(client, token).status_code == 409
    token = preview(client)
    repo.update_profile_transaction('office', lambda c: c['routers']['items'][0].update(endpoint='http://192.0.2.3'))
    assert apply(client, token).status_code == 409
    assert not module.calls


def test_mac_and_source_must_still_match_discovery(fixed_app, monkeypatch):
    client, _, module, _ = fixed_app
    monkeypatch.setattr(fixed, '_seen', lambda *args: False)
    response = client.post(BASE + 'preview?profile=office', json=request_body(router='r2'))
    assert response.status_code == 409 and '来源' in response.get_json()['message']
    assert not module.calls


def test_batch_second_failure_rolls_back_first(fixed_app):
    client, repo, module, _ = fixed_app
    body = request_body()
    body['devices'] += request_body(ip='192.0.2.51')['devices']
    body['devices'][1]['mac'] = '02:11:22:33:44:56'
    token = preview(client, body)
    module.fail = '192.0.2.51'
    assert apply(client, token).status_code == 502
    assert module.records == {} and repo.get_profile('office').get(fixed.KEY, {}) == {}
    assert module.calls == [('add', 'r1', IP), ('rollback', 'r1', IP)]


def test_config_save_failure_rolls_back_router(fixed_app, monkeypatch):
    from backend.common import config
    client, repo, module, _ = fixed_app
    token = preview(client)
    def fail(*args):
        raise OSError('disk full')
    monkeypatch.setattr(config, 'update_config_transaction', fail)
    assert apply(client, token).status_code == 502
    assert module.records == {} and repo.get_profile('office').get(fixed.KEY, {}) == {}


def test_record_removed_after_preview_never_creates_an_orphan(fixed_app):
    client, repo, module, _ = fixed_app
    token = preview(client)
    repo.update_profile_transaction('office', lambda c: c['device_notes'].pop(IP) and None)
    assert apply(client, token).status_code == 409
    assert module.records == {}


def test_delete_needs_record_marker_and_its_own_confirmation(fixed_app):
    client, repo, module, _ = fixed_app
    response = client.post(BASE + 'preview?profile=office', json=request_body('delete'))
    assert response.status_code == 409
    assert apply(client, preview(client)).status_code == 200
    token = preview(client, request_body('delete'))
    assert IP in module.records
    assert apply(client, token).status_code == 200
    assert module.records == {} and repo.get_profile('office')[fixed.KEY] == {}
    assert repo.get_profile('office')['device_notes'][IP] == '电脑'


def test_clearing_record_keeps_router_binding_by_default(fixed_app):
    client, repo, module, _ = fixed_app
    assert apply(client, preview(client)).status_code == 200
    response = client.put('/api/client-devices?profile=office', json={
        'profile_id': 'office', 'ip': IP, 'name': '', 'mode': '', 'previous_name': '电脑',
        'previous_mode': '', 'base_url': ''})
    assert response.status_code == 200
    assert IP in module.records and IP not in repo.get_profile('office')[fixed.KEY]
    assert module.calls == [('add', 'r1', IP)]


def test_setup_guard_blocks_preview_and_confirm(fixed_app, monkeypatch):
    from backend.common import setup_flow
    client, _, module, _ = fixed_app
    token = preview(client)
    monkeypatch.setattr(setup_flow, 'profile_sessions', lambda *args: [{'completed': False}])
    assert client.post(BASE + 'preview?profile=office', json=request_body()).status_code == 409
    assert apply(client, token).status_code == 409
    assert not module.calls


def test_delete_save_failure_restores_new_id_in_metadata(fixed_app, monkeypatch):
    from backend.common import config
    client, repo, module, _ = fixed_app
    assert apply(client, preview(client)).status_code == 200
    token = preview(client, request_body('delete'))
    original, attempts = config.update_config_transaction, []
    def fail_once(*args):
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError('injected')
        return original(*args)
    monkeypatch.setattr(config, 'update_config_transaction', fail_once)
    assert apply(client, token).status_code == 502
    assert repo.get_profile('office')[fixed.KEY][IP]['id'] == '*2'
    assert IP in module.records


def test_legacy_profiles_and_all_generated_outputs_are_byte_identical(oxidns_app):
    from backend.converters.mihomo import generate_mihomo_config
    from backend.converters.surge import generate_surge_config
    from backend.converters.loon import generate_loon_config
    from backend.converters.mosdns import generate_mosdns_config
    from backend.converters.oxidns import generate_oxidns_config
    from backend.routers.desired import compute_desired
    from backend.common.oxidns_device_policy import apply_policies
    _, repo = oxidns_app
    config = repo.get_compat_config('office')
    config.pop(fixed.KEY, None)
    config['device_notes'] = {IP: '电脑'}
    config['oxidns'] = apply_policies(config['oxidns'], config, [{'ip': IP, 'name': '电脑', 'mode': 'smart'}])
    def outputs(value):
        generators = (lambda c: generate_mihomo_config(c, preflight_providers=False),
                      generate_surge_config, generate_loon_config, generate_mosdns_config, generate_oxidns_config)
        return [generate(deepcopy(value)).encode() for generate in generators] + [
            json.dumps(compute_desired(deepcopy(value), '192.0.2.254'), sort_keys=True).encode()]
    baseline = outputs(config)
    for metadata in ({}, {IP: {'router_id': 'r1', 'mac': MAC, 'id': '*1', 'comment': 'cf-client:电脑'}}):
        assert outputs({**config, fixed.KEY: metadata}) == baseline
    assert list_devices(config)['devices'] == [{'ip': IP, 'name': '电脑', 'mode': 'smart'}]


def test_cloning_profile_keeps_source_binding_and_never_contacts_or_copies_routers(fixed_app):
    client, repo, module, opened = fixed_app
    assert apply(client, preview(client)).status_code == 200
    original = deepcopy(repo.get_profile('office'))
    original_records = deepcopy(module.records)
    previous_opens, previous_calls = list(opened), list(module.calls)
    response = client.post('/api/profiles/office/clone', json={'id': 'copy', 'name': '复制配置'})
    assert response.status_code == 201, response.get_json()
    cloned = repo.get_profile('copy')
    assert cloned['routers'] == {'items': []} and cloned[fixed.KEY] == {}
    assert cloned['device_notes'] == original['device_notes']
    assert repo.get_profile('office') == original and module.records == original_records
    assert opened == previous_opens and module.calls == previous_calls


def test_live_owned_reservation_without_saved_metadata_still_requires_delete_confirmation(fixed_app):
    client, repo, module, _ = fixed_app
    assert apply(client, preview(client)).status_code == 200
    # The row was removed while keeping its lease, then added by IP again.
    repo.update_profile_transaction('office', lambda c: c.update({fixed.KEY: {}}))
    original_records = deepcopy(module.records)
    calls_before_preview = list(module.calls)
    token = preview(client, request_body('delete'))
    assert module.records == original_records and module.calls == calls_before_preview
    assert not repo.get_profile('office').get(fixed.KEY)
    response = apply(client, token)
    assert response.status_code == 200, response.get_json()
    assert module.records == {} and repo.get_profile('office')[fixed.KEY] == {}
    assert repo.get_profile('office')['device_notes'][IP] == '电脑'


def test_untracked_foreign_reservation_is_not_deleted_by_live_discovery_fallback(fixed_app):
    client, repo, module, _ = fixed_app
    module.records[IP] = {'ip': IP, 'mac': MAC, 'id': '*7', 'server': 'lan',
                          'vendor': 'routeros', 'comment': 'manually-created'}
    before = deepcopy(module.records)
    response = client.post(BASE + 'preview?profile=office', json=request_body('delete'))
    assert response.status_code == 409
    assert module.records == before and not module.calls
    assert not repo.get_profile('office').get(fixed.KEY)


def test_batch_same_router_mac_with_different_ips_fails_before_any_router_access(fixed_app):
    client, _, module, opened = fixed_app
    body = request_body()
    body['devices'] += request_body(ip='192.0.2.51')['devices']
    response = client.post(BASE + 'preview?profile=office', json=body)
    assert response.status_code == 400 and '同一 MAC' in response.get_json()['message']
    assert not module.calls and not opened
