"""Gateway trials must be read-only; explicit saves/restores are atomic."""
from copy import deepcopy

import pytest
import yaml

from backend.common.linux_gateway_draft import RESTORE_KEY
from backend.converters.mihomo import generate_mihomo_config
from backend.test_linux_gateway import gateway_app  # noqa: F401
from backend.test_agent_upgrade_auth import authenticated_app  # noqa: F401

PATH = '/api/profiles/home/custom-config/linux-gateway'
SETTINGS = {'enabled': False, 'rule_set_ids': []}


def preview(client, repository, settings=SETTINGS, *, restore=False):
    response = client.post(PATH + ('/restore/preview' if restore else '/preview'), json={
        'settings': settings, 'expected_config_revision': repository.config_revision('home'),
    })
    assert response.status_code == 200, response.json
    assert response.headers['Cache-Control'] == 'no-store'
    return response.json


def save(client, review, settings=SETTINGS, *, restore=False):
    return client.post(PATH + ('/restore' if restore else ''), json={
        'settings': settings, 'expected_config_revision': review['config_revision'],
        'preview_token': review['preview_token'],
    })


def test_trial_preview_never_writes_downloads_or_calls_agent(gateway_app, monkeypatch):
    app, repository = gateway_app
    client = app.test_client()
    def forbidden(*args, **kwargs):
        pytest.fail('preview must not access network or write configuration')
    monkeypatch.setattr('requests.sessions.Session.request', forbidden)
    monkeypatch.setattr(repository, 'update_profile_transaction', forbidden)
    original = repository.export_all()
    for settings in (SETTINGS, {'enabled': True, 'rule_set_ids': ['r']}):
        review = preview(client, repository, settings)
        assert review['action'] == 'save'
        assert review['restore_point'] is None
    assert repository.export_all() == original
    assert any(change['path'].startswith('Mihomo.tun') for change in preview(client, repository)['changes'])


def test_explicit_save_keeps_bases_and_first_snapshot_then_restores_exactly(gateway_app):
    app, repository = gateway_app
    client = app.test_client()
    repository.update_profile_transaction('home', lambda c: c.update(oxidns={
        'config_mode': 'native', 'native_config': 'plugins: []\n',
        'remote_dns': 'tls://dns.example', '_common_disabled': {'custom': 'kept'},
    }))
    original = repository.get_profile('home')
    other = repository.get_profile('default')
    result = save(client, preview(client, repository))
    assert result.status_code == 200, result.json
    current = repository.get_profile('home')
    point = current['mihomo'][RESTORE_KEY]
    assert point['mihomo'] == original['mihomo']
    assert point['oxidns'] == original['oxidns']
    assert current['mihomo']['custom_config'] == original['mihomo']['custom_config']
    assert current['oxidns'] == original['oxidns']
    assert result.json['publish_guard'] == {
        'expected_profile_id': 'home', 'expected_service_type': 'mihomo',
        'expected_config_revision': repository.config_revision('home'),
    }
    assert set(result.json['restore_point']) == {'id', 'created_at'}
    # More gateway saves retain the first baseline, even across re-enabling.
    enabled = {'enabled': True, 'rule_set_ids': []}
    assert save(client, preview(client, repository, enabled), enabled).status_code == 200
    assert repository.get_profile('home')['mihomo'][RESTORE_KEY] == point
    # Reveal intervening service edits before restoring them; unrelated profile
    # settings and shared resources are never rolled back.
    repository.update_profile_transaction('home', lambda c: (
        c['oxidns'].update(remote_dns='https://later.example/dns-query'),
        c['mihomo'].update(custom_config='mixed-port: 8888\n'),
    ) and None)
    review = preview(client, repository, restore=True)
    assert any(item['path'] == 'OxiDNS 草稿.remote_dns' for item in review['changes'])
    result = save(client, review, restore=True)
    assert result.status_code == 200, result.json
    restored = repository.get_profile('home')
    assert restored['mihomo'] == original['mihomo']
    assert restored['oxidns'] == original['oxidns']
    assert restored['rule_configs'] == original['rule_configs']
    assert result.json['restore_point'] is None
    assert repository.get_profile('default') == other


@pytest.mark.parametrize('kind', ['profile', 'shared', 'system'])
def test_intervening_edits_block_stale_save_atomically(gateway_app, kind):
    app, repository = gateway_app
    client = app.test_client()
    review = preview(client, repository)
    if kind == 'profile':
        repository.update_profile_transaction('home', lambda c: c['mihomo'].update(custom_config='mixed-port: 8888\n'))
    elif kind == 'shared':
        repository.update_shared_transaction(lambda c: c['rule_library'][0].update(name='Updated'))
    else:
        repository.update_system_transaction(lambda c: c['system_config'].update(server_domain='https://changed.example'))
    before = repository.export_all()
    result = save(client, review)
    assert result.status_code == 409, result.json
    assert repository.export_all() == before


def test_stale_restore_preview_and_cross_profile_preview_cannot_overwrite(gateway_app):
    app, repository = gateway_app
    client = app.test_client()
    review = preview(client, repository)
    copied = client.post('/api/profiles/default/custom-config/linux-gateway', json={
        'settings': SETTINGS, 'expected_config_revision': repository.config_revision('default'),
        'preview_token': review['preview_token'],
    })
    assert copied.status_code == 409
    assert save(client, review).status_code == 200
    review = preview(client, repository, restore=True)
    repository.update_profile_transaction('home', lambda c: c['oxidns'].update(remote_dns='8.8.8.8'))
    before = repository.export_all()
    assert save(client, review, restore=True).status_code == 409
    assert repository.export_all() == before


def test_mismatched_settings_missing_revision_and_missing_snapshot_are_rejected(gateway_app):
    app, repository = gateway_app
    client = app.test_client()
    before = repository.export_all()
    assert client.post(PATH + '/preview', json={'settings': SETTINGS}).status_code == 400
    assert client.post(PATH + '/restore/preview', json={
        'expected_config_revision': repository.config_revision('home')}).status_code == 400
    review = preview(client, repository)
    assert save(client, review, {'enabled': True, 'rule_set_ids': []}).status_code == 409
    for token in ('bad', '中文', None):
        review['preview_token'] = token
        assert save(client, review).status_code == 409
    assert repository.export_all() == before


def test_changed_public_origin_invalidates_preview(gateway_app):
    app, repository = gateway_app
    client = app.test_client()
    review = preview(client, repository)
    before = repository.export_all()
    response = client.post(PATH, base_url='https://another.example', json={
        'settings': SETTINGS, 'expected_config_revision': review['config_revision'],
        'preview_token': review['preview_token'],
    })
    assert response.status_code == 409
    assert repository.export_all() == before


def test_guard_is_rechecked_inside_commit_lock(gateway_app, monkeypatch):
    app, repository = gateway_app
    client = app.test_client()
    review = preview(client, repository)
    original_update = repository.update_profile_transaction
    def racing(profile_id, updater, **kwargs):
        original_update(profile_id, lambda c: c['mihomo'].update(custom_config='mixed-port: 9090\n'))
        return original_update(profile_id, updater, **kwargs)
    monkeypatch.setattr(repository, 'update_profile_transaction', racing)
    assert save(client, review).status_code == 409
    assert repository.get_profile('home')['mihomo']['custom_config'] == 'mixed-port: 9090\n'
    assert RESTORE_KEY not in repository.get_profile('home')['mihomo']


def test_runtime_backup_updates_do_not_invalidate_a_configuration_preview(gateway_app):
    app, repository = gateway_app
    client = app.test_client()
    review = preview(client, repository)
    repository.update_system_transaction(lambda c: c['backup'].update(last_backup='just now'))
    assert save(client, review).status_code == 200


def test_broken_snapshot_and_incompatible_target_are_atomic(gateway_app):
    app, repository = gateway_app
    client = app.test_client()
    repository.update_profile_transaction('home', lambda c: c['mihomo'].update({RESTORE_KEY: {'version': 1}}))
    before = repository.export_all()
    assert save(client, preview(client, repository)).status_code == 400
    assert repository.export_all() == before
    assert client.post(PATH + '/restore/preview', json={
        'expected_config_revision': repository.config_revision('home')}).status_code == 400
    repository.update_profile_transaction('home', lambda c: c['mihomo'].update(custom_config='routing-mark: 10\n'))
    before = repository.export_all()
    result = client.post(PATH + '/preview', json={
        'expected_config_revision': repository.config_revision('home'),
        'settings': {'enabled': True, 'rule_set_ids': ['r']},
    })
    assert result.status_code == 400
    assert 'routing-mark' in result.json['message']
    assert repository.export_all() == before


def test_restore_preview_hides_opaque_yaml_and_credentials(gateway_app):
    app, repository = gateway_app
    client = app.test_client()
    assert client.post(PATH, json=SETTINGS).status_code == 200
    repository.update_profile_transaction('home', lambda c: c['oxidns'].update(
        native_config='secret: opaque-native-secret', password='plaintext-secret',
        remote_dns='https://user:url-secret@dns.example/dns-query?token=query-secret'))
    review = preview(client, repository, restore=True)
    encoded = str(review['changes'])
    assert not any(secret in encoded for secret in ('opaque-native-secret', 'plaintext-secret', 'url-secret', 'query-secret'))
    assert any(item['path'] == 'OxiDNS 草稿.native_config' for item in review['changes'])


def test_legacy_save_snapshot_does_not_change_generated_yaml_and_share_export_omits_secrets(gateway_app):
    app, repository = gateway_app
    client = app.test_client()
    assert client.post(PATH, json=SETTINGS).status_code == 200
    config = repository.get_compat_config('home')
    without_snapshot = deepcopy(config)
    without_snapshot['mihomo'].pop(RESTORE_KEY)
    assert generate_mihomo_config(config, preflight_providers=False) == generate_mihomo_config(without_snapshot, preflight_providers=False)
    assert RESTORE_KEY in repository.export_all()['profiles']['home']['mihomo']
    assert RESTORE_KEY not in repository.export_all(desensitize=True)['profiles']['home']['mihomo']
    assert yaml.safe_load(generate_mihomo_config(config, preflight_providers=False))['mixed-port'] == 7890


def test_new_draft_endpoints_require_panel_auth_not_agent_or_subscription_tokens(authenticated_app):
    client, manager, (agent, _), admin, _ = authenticated_app
    profile_id = manager.get_agent_by_id(agent['id'])['profile_id']
    path = f'/api/profiles/{profile_id}/custom-config/linux-gateway'
    for suffix in ('/preview', '/restore/preview', '/restore'):
        assert client.post(path + suffix, json={}).status_code == 401
        assert client.post(path + suffix, headers={'Authorization': 'Bearer ' + agent['token']}, json={}).status_code == 401
    response = client.get(path, headers=admin)
    assert response.status_code == 200, response.json
    review = client.post(path + '/preview', headers=admin, json={
        'settings': SETTINGS, 'expected_config_revision': response.json['config_revision'],
    })
    assert review.status_code == 200, review.json
