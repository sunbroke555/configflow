"""IPv6 Fake-IP routes share normal previews/releases but keep precise ownership."""
import copy
from urllib.parse import urlsplit

import pytest
import requests

from backend.routers import ipv6_fakeip as fake6
from backend.routers.base import RouterError, RouterRollbackError
from backend.routers.plan import build_plan
from backend.routers.release import execute
from backend.routers.report import build_report
from backend.routers.routeros import RouterOSDriver
from backend.test_router_dns_guard import RouterOS, Response

CONFIG = {'mihomo': {'custom_config': '''ipv6: true
dns:
  enable: true
  ipv6: true
  enhanced-mode: fake-ip
  fake-ip-range6: fc00::1/64
tun:
  enable: true
'''}}
AGENT = {'id': 'gateway', 'host': '10.0.0.4', 'service_type': 'mihomo', 'enabled': True}
HOST = {'addresses': [{'address': 'fdda::4', 'usable': True}]}
ROUTER = {'id': 'router_test', 'type': 'routeros', 'endpoint': 'https://10.0.0.1'}
DESIRED = {'fake_ip_range': '198.18.0.0/16', 'proxy_gateway': '10.0.0.4', 'warnings': [],
           'proxy_cidrs': [{'cidr': '198.18.0.0/16', 'sources': ['fake-ip']}], 'dns_server': '', 'dns_error': ''}


class IPv6Router(RouterOS):
    def __init__(self):
        super().__init__(nats=[])
        self.rows[fake6.ROUTES] = []
        self.rows[fake6.ADDRESSES] = [{'.id': '*A', 'address': 'fdda::1/64', 'interface': 'bridge'}]
        self.rows[fake6.IPV4_ADDRESSES] = [{'.id': '*B', 'address': '10.0.0.1/24', 'interface': 'bridge'}]
        self.inactive_new = False
        self.lose_reply = False

    def request(self, method, url, json=None, **kwargs):
        reply = super().request(method, url, json=json, **kwargs)
        if method == 'PUT' and urlsplit(url).path == '/rest' + fake6.ROUTES and reply.status_code == 201:
            row = self.rows[fake6.ROUTES][-1]
            row.update(active='false' if self.inactive_new else 'true', dynamic='false')
            reply = Response(row, 201)
            if self.lose_reply:
                raise requests.ConnectionError('lost reply')
        return reply


def device():
    driver = RouterOSDriver('https://10.0.0.1', 'admin', 'pw')
    driver.session = IPv6Router()
    return driver.session, driver


def row(gateway='fdda::4%bridge', **extra):
    return {'.id': '*50', 'dst-address': 'fc00::/64', 'gateway': gateway,
            'routing-table': 'main', 'distance': '1', 'disabled': 'false', 'active': 'true',
            'dynamic': 'false', 'comment': 'my route', **extra}


def preview(driver, router=None, config=None, host=None):
    router = router or ROUTER
    state = driver.read_state()
    state.ipv6_fakeip_plan = fake6.inspect(config or CONFIG, router, AGENT, HOST if host is None else host, driver)
    report = build_report(DESIRED, state)
    plan = build_plan(router, DESIRED, state, report, {'status': 'disabled'}, driver)
    return state.ipv6_fakeip_plan, report, plan


def publish(driver):
    part, _, plan = preview(driver)
    result = execute(plan, driver, None)
    assert result['ok'], result
    return {**ROUTER, 'ipv6_fakeip_state': copy.deepcopy(driver.ipv6_fakeip_manifest)}, part


def test_missing_route_appears_in_diff_and_release_manifest_without_touching_ipv4():
    session, driver = device()
    part, report, plan = preview(driver)
    assert part['status'] == 'missing' and not part['blockers']
    assert report['ipv6_fakeip']['items'][0]['cidr'] == 'fc00::/64'
    assert report['in_sync'] is False
    assert [stage['key'] for stage in plan['stages']][:3] == ['return_routing', 'routes', 'ipv6_fakeip']
    before4 = copy.deepcopy(session.rows['/ip/route'])
    result = execute(plan, driver, None)
    assert result['ok'], result
    assert session.rows['/ip/route'] == before4
    assert driver.ipv6_fakeip_manifest['spec'] == {'cidr': 'fc00::/64', 'gateway': 'fdda::4%bridge'}
    assert driver.ipv6_fakeip_manifest['resources'][0]['id'] == session.rows[fake6.ROUTES][0]['.id']
    assert all(path == fake6.ROUTES for _, path, _ in session.writes)


def test_managed_route_is_idempotent_and_noop_stage_still_verifies():
    _, driver = device()
    router, _ = publish(driver)
    part, report, plan = preview(driver, router)
    assert part['status'] == 'synced' and not part['ops']
    assert report['in_sync']
    assert execute(plan, driver, None)['ok']


@pytest.mark.parametrize('comment', ['my route', 'cf-ipv6-readiness:123456'])
def test_correct_existing_route_is_borrowed_without_comment_adoption(comment):
    session, driver = device()
    session.rows[fake6.ROUTES] = [row(comment=comment)]
    part, report, plan = preview(driver)
    assert part['status'] == 'borrowed' and not part['ops']
    assert report['in_sync']
    assert execute(plan, driver, None)['ok']
    assert session.rows[fake6.ROUTES][0]['comment'] == comment
    assert not driver.ipv6_fakeip_manifest and not session.writes


def test_disabled_feature_withdraws_only_owned_route_without_online_host():
    session, driver = device()
    router, _ = publish(driver)
    other = row(**{'.id': '*51', 'dst-address': 'fc01::/64', 'comment': 'cf-ipv6-readiness:legacy'})
    session.rows[fake6.ROUTES].append(other)
    disabled = {'mihomo': {'custom_config': CONFIG['mihomo']['custom_config'].replace('  ipv6: true', '  ipv6: false')}}
    part, _, plan = preview(driver, router, disabled, {'error': 'offline'})
    assert not part['enabled'] and not part['blockers']
    assert execute(plan, driver, None)['ok']
    assert session.rows[fake6.ROUTES] == [other]
    assert driver.ipv6_fakeip_manifest is None


@pytest.mark.parametrize('extra', [{'gateway': 'fdda::9%bridge'}, {'disabled': 'true'}, {'active': 'false'},
                                 {'blackhole': 'true'}, {'dst-address': 'fc00::/80'}])
def test_conflicting_or_inactive_existing_routes_block_instead_of_overwriting(extra):
    session, driver = device()
    session.rows[fake6.ROUTES] = [row(**extra)]
    part, report, plan = preview(driver)
    assert part['status'] == 'conflict' and part['blockers'] and plan['blockers']
    assert not report['in_sync'] and not execute(plan, driver, None)['ok']
    assert session.writes == []


def test_duplicate_correct_routes_are_not_adopted():
    session, driver = device()
    session.rows[fake6.ROUTES] = [row(), row(**{'.id': '*51'})]
    assert preview(driver)[0]['blockers']


@pytest.mark.parametrize('host', [{}, {'error': 'agent offline'}, {'addresses': [
    {'address': 'fdda::4', 'usable': True}, {'address': 'fdda::5', 'usable': True}]}])
def test_missing_or_ambiguous_next_hop_is_visible_and_blocks_publish(host):
    _, driver = device()
    part, report, plan = preview(driver, host=host)
    assert part['blockers'] and plan['blockers'] and report['ipv6_fakeip']['blockers']


def test_link_local_next_hop_is_scoped_to_unique_ros_lan():
    _, driver = device()
    part, _, _ = preview(driver, host={'addresses': [{'address': 'fe80::4'}]})
    assert not part['blockers']
    assert part['check']['spec']['gateway'] == 'fe80::4%bridge'


def test_lan_overlap_and_missing_tun_are_blockers():
    session, driver = device()
    session.rows[fake6.ADDRESSES].append({'.id': '*C', 'address': 'fc00::1/64', 'interface': 'other'})
    assert preview(driver)[0]['blockers']
    session.rows[fake6.ADDRESSES].pop()
    config = {'mihomo': {'custom_config': CONFIG['mihomo']['custom_config'].replace('tun:\n  enable: true', 'tun:\n  enable: false')}}
    assert preview(driver, config=config)[0]['blockers']


def test_invalid_saved_range_does_not_fall_back_or_withdraw_previous_route():
    session, driver = device()
    router, _ = publish(driver)
    before = copy.deepcopy(session.rows[fake6.ROUTES])
    config = {'mihomo': {'custom_config': CONFIG['mihomo']['custom_config'].replace('fc00::1/64', 'not-an-ip')}}
    part, _, plan = preview(driver, router, config)
    assert part['blockers'] and not part['ops'] and plan['blockers']
    assert session.rows[fake6.ROUTES] == before


def test_external_edit_to_owned_route_prevents_removal_or_replacement():
    session, driver = device()
    router, _ = publish(driver)
    session.rows[fake6.ROUTES][0]['comment'] = 'edited outside'
    assert preview(driver, router)[0]['blockers']


def test_preview_fingerprint_tracks_ipv6_scope_without_route_writes():
    session, driver = device()
    _, _, before = preview(driver)
    session.rows[fake6.ADDRESSES][0]['address'] = 'fdda::2/64'
    _, _, after = preview(driver)
    assert before['fingerprint'] != after['fingerprint']
    assert session.writes == []


def test_apply_rechecks_the_ipv6_snapshot_before_writing():
    session, driver = device()
    part, _, _ = preview(driver)
    session.rows[fake6.ROUTES].append(row())
    with pytest.raises(RouterError, match='预览后变化'):
        driver.apply(part['ops'][0])
    assert session.writes == []


def test_apply_rechecks_address_actual_interface_before_writing():
    session, driver = device()
    session.rows[fake6.IPV4_ADDRESSES][0]['actual-interface'] = 'bridge'
    part, _, _ = preview(driver)
    assert not part['blockers']
    session.rows[fake6.IPV4_ADDRESSES][0]['actual-interface'] = 'other-bridge'
    with pytest.raises(RouterError, match='预览后变化'):
        driver.apply(part['ops'][0])
    assert session.writes == []


def test_inactive_created_route_is_rolled_back_and_not_registered():
    session, driver = device()
    session.inactive_new = True
    _, _, plan = preview(driver)
    result = execute(plan, driver, None)
    assert not result['ok']
    assert session.rows[fake6.ROUTES] == []
    assert driver.ipv6_fakeip_manifest is None


def test_lost_write_reply_preserves_unproven_object_without_claiming_ownership():
    session, driver = device()
    session.lose_reply = True
    part, _, _ = preview(driver)
    with pytest.raises(RouterRollbackError, match='归属'):
        driver.apply(part['ops'][0])
    assert len(session.rows[fake6.ROUTES]) == 1
    assert driver.ipv6_fakeip_manifest is None
    assert not any(method == 'DELETE' for method, _, _ in session.writes)


def test_later_stage_failure_rolls_back_new_route_and_manifest():
    session, driver = device()
    _, _, plan = preview(driver)
    plan['stages'].append({'key': 'later', 'manual': False, 'ops': [{'kind': 'unsupported', 'summary': 'fail'}]})
    result = execute(plan, driver, None)
    assert not result['ok'] and result['failed_stage'] == 'later'
    assert session.rows[fake6.ROUTES] == []
    assert driver.ipv6_fakeip_manifest is None


def test_replacing_owned_route_rolls_back_to_remapped_previous_id_on_later_failure():
    session, driver = device()
    router, _ = publish(driver)
    previous = copy.deepcopy(driver.ipv6_fakeip_manifest)
    _, _, plan = preview(driver, router, host={'addresses': [{'address': 'fdda::5', 'usable': True}]})
    plan['stages'].append({'key': 'later', 'manual': False, 'ops': [{'kind': 'unsupported', 'summary': 'fail'}]})
    result = execute(plan, driver, None)
    assert not result['ok']
    restored = driver.ipv6_fakeip_manifest
    assert restored['spec'] == previous['spec']
    assert restored['resources'][0]['id'] != previous['resources'][0]['id']
    assert session.rows[fake6.ROUTES][0]['gateway'] == 'fdda::4%bridge'
    fake6.verify(driver, {'spec': previous['spec']})


def test_runtime_route_lifetimes_do_not_change_preview_fingerprint():
    session, driver = device()
    session.rows[fake6.ROUTES] = [row(**{'.id': '*52', 'dst-address': '::/0', 'expires-after': '5m'})]
    before = preview(driver)[2]['fingerprint']
    session.rows[fake6.ROUTES][0]['expires-after'] = '4m59s'
    assert preview(driver)[2]['fingerprint'] == before


def test_unordered_route_table_does_not_invalidate_preview():
    session, driver = device()
    session.rows[fake6.ROUTES] = [row(**{'.id': '*52', 'dst-address': '::/0'}),
                                row(**{'.id': '*53', 'dst-address': 'fd01::/64'})]
    part, _, before = preview(driver)
    session.rows[fake6.ROUTES].reverse()
    assert preview(driver)[2]['fingerprint'] == before['fingerprint']
    driver.apply(part['ops'][0])
    fake6.verify(driver, part['check'])


def test_corrupted_manifest_spec_is_not_silently_accepted_as_a_successful_noop():
    _, driver = device()
    router, _ = publish(driver)
    router['ipv6_fakeip_state']['spec']['gateway'] = 'fdda::99%bridge'
    part, _, plan = preview(driver, router)
    assert part['blockers'] and not part['ops']
    assert plan['blockers']


def test_noop_preview_also_fingerprints_the_owned_manifest():
    _, driver = device()
    router, _ = publish(driver)
    _, _, first = preview(driver, router)
    router['ipv6_fakeip_state']['audit_note'] = 'changed internal ownership receipt'
    _, _, second = preview(driver, router)
    assert first['empty'] and second['empty']
    assert first['fingerprint'] != second['fingerprint']
