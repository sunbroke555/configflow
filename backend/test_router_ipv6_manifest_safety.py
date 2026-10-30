"""IPv6 Fake-IP ownership survives unrelated edits and only publication can withdraw it."""
import copy

import pytest

from backend.common.config_repository import ProfileInUse
from backend.routers.rule_sync_store import job_lock
from backend.routers.store import RouterValidationError, normalize, public_view, redact_routers
from backend.test_router_repository_safety import (  # noqa: F401 - shared isolated repository fixture
    CONNECTION, MANIFEST, replace_routers, repository,
)

IPV6_MANIFEST = {
    'version': 1,
    'spec': {'cidr': 'fc00::/64', 'gateway': 'fdda::3%lan'},
    'resources': [{
        'path': '/ipv6/route', 'id': '*6',
        'config': {'dst-address': 'fc00::/64', 'gateway': 'fdda::3%lan',
                   'routing-table': 'main', 'disabled': 'false', 'comment': 'cf-fakeip6:test'},
    }],
}


def persist_ipv6(repository):
    router = repository.get_profile('live')['routers']['items'][0]
    with job_lock('live', router['id'], router) as acquired:
        assert acquired
        repository.save_router_publication('live', router, ipv6_fakeip=IPV6_MANIFEST)
    return repository.get_profile('live')['routers']['items'][0]


@pytest.mark.parametrize('writer', ['profile-import', 'whole-import', 'fields', 'save', 'transaction'])
@pytest.mark.parametrize('change', ['remove-router', 'remove-manifest', 'replace-manifest', 'id', 'type', 'endpoint'])
def test_every_writer_preserves_ipv6_route_ownership(repository, writer, change):
    persist_ipv6(repository)
    before = repository.export_all()
    routers = copy.deepcopy(before['profiles']['live']['routers'])
    router = routers['items'][0]
    if change == 'remove-router':
        routers['items'] = []
    elif change == 'remove-manifest':
        router.pop('ipv6_fakeip_state')
    elif change == 'replace-manifest':
        router['ipv6_fakeip_state']['resources'] = []
    else:
        router[change] = 'changed'
    with pytest.raises(ProfileInUse, match='IPv6 Fake-IP'):
        replace_routers(repository, writer, routers)
    assert repository.export_all() == before


def test_publication_persists_readback_then_withdrawal_without_losing_other_manifests(repository):
    original = repository.get_profile('live')['routers']['items'][0]
    guard = {'resources': [{'id': '*D', 'path': '/ip/firewall/nat'}]}
    with job_lock('live', original['id'], original) as acquired:
        assert acquired
        repository.save_router_publication('live', original, manifest=MANIFEST, dns_guard=guard,
                                           ipv6_fakeip=IPV6_MANIFEST)
        repository.update_profile_transaction('live',
            lambda config: config['routers']['items'][0].update(name='Edited while publishing'))
        restored = copy.deepcopy(IPV6_MANIFEST)
        restored['resources'][0]['id'] = '*7'
        repository.save_router_publication('live', original, ipv6_fakeip=restored,
                                           last_release={'ok': False, 'failed_stage': 'routes'})
        current = repository.get_profile('live')['routers']['items'][0]
        assert current['ipv6_fakeip_state'] == restored
        assert current['name'] == 'Edited while publishing'
        assert current['ros_policy_state'] == MANIFEST
        assert current['dns_guard_state'] == guard
        repository.save_router_publication('live', original, last_release={'ok': True})
        assert repository.get_profile('live')['routers']['items'][0]['ipv6_fakeip_state'] == restored
        repository.save_router_publication('live', original, ipv6_fakeip=None)
    current = repository.get_profile('live')['routers']['items'][0]
    assert 'ipv6_fakeip_state' not in current
    assert current['ros_policy_state'] == MANIFEST and current['dns_guard_state'] == guard
    assert current['last_release'] == {'ok': True}


@pytest.mark.parametrize('field', ['endpoint', 'username', 'password', 'verify_tls'])
def test_ipv6_publication_rejects_a_changed_connection(repository, field):
    original = persist_ipv6(repository)
    stale = {**original, field: False if field == 'verify_tls' else 'changed'}
    before = repository.export_all()
    with job_lock('live', original['id'], original) as acquired:
        assert acquired
        with pytest.raises(ProfileInUse, match='连接已变化'):
            repository.save_router_publication('live', stale, ipv6_fakeip=None)
    assert repository.export_all() == before


@pytest.mark.parametrize('field', ['username', 'password', 'verify_tls'])
def test_credentials_can_be_repaired_without_abandoning_ipv6_ownership(repository, field):
    original = persist_ipv6(repository)
    replacement = {**original, field: False if field == 'verify_tls' else 'replacement'}
    replace_routers(repository, 'fields', {'items': [replacement]})
    current = repository.get_profile('live')['routers']['items'][0]
    assert current[field] == replacement[field]
    assert current['ipv6_fakeip_state'] == IPV6_MANIFEST


@pytest.mark.parametrize('operation', ['delete-profile', 'reset', 'delete-router'])
def test_destructive_changes_wait_for_ipv6_route_withdrawal(repository, operation):
    router = persist_ipv6(repository)
    before = repository.export_all()
    with pytest.raises(ProfileInUse, match='IPv6 Fake-IP'):
        if operation == 'delete-profile':
            repository.delete_profile('live')
        elif operation == 'reset':
            repository.reset_all()
        else:
            with job_lock('live', router['id'], router) as acquired:
                assert acquired
                repository.save_router_settings('live', router, None)
    assert repository.export_all() == before
    with job_lock('live', router['id'], router) as acquired:
        assert acquired
        repository.save_router_publication('live', router, ipv6_fakeip=None)
    repository.delete_profile('live')
    assert all(item['id'] != 'live' for item in repository.list_profiles())


def test_clone_has_no_authority_over_source_ipv6_routes(repository):
    persist_ipv6(repository)
    repository.clone_profile('live', {'id': 'copy', 'name': 'Copy'})
    assert repository.get_profile('copy')['routers'] == {'items': []}
    assert repository.get_profile('live')['routers']['items'][0]['ipv6_fakeip_state'] == IPV6_MANIFEST


def test_settings_cannot_replace_receipt_and_exports_do_not_disclose_it():
    existing = {**CONNECTION, 'ipv6_fakeip_state': copy.deepcopy(IPV6_MANIFEST)}
    updated = normalize({'name': 'Renamed', 'password': 'new password', 'ipv6_fakeip_state': {}}, existing)
    assert updated['name'] == 'Renamed' and updated['password'] == 'new password'
    assert updated['ipv6_fakeip_state'] == IPV6_MANIFEST
    assert 'ipv6_fakeip_state' not in public_view(existing)
    exported = redact_routers({'items': [existing]})['items'][0]
    assert 'ipv6_fakeip_state' not in exported and 'password' not in exported
    assert existing['ipv6_fakeip_state'] == IPV6_MANIFEST


@pytest.mark.parametrize('change', [{'endpoint': 'http://192.168.0.9'}, {'type': 'ikuai'}])
def test_settings_require_withdrawal_before_changing_router_identity(change):
    existing = {**CONNECTION, 'ipv6_fakeip_state': copy.deepcopy(IPV6_MANIFEST)}
    with pytest.raises(RouterValidationError, match='IPv6 Fake-IP'):
        normalize(change, existing)
