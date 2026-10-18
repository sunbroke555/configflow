"""Release regressions for ownership, effective main routes and NAT coverage."""
import copy

import pytest

from backend.routers.base import RouterError, RouterRollbackError
from backend.routers.plan import build_plan
from backend.routers.release import ReleaseError, _verify
from backend.routers.report import build_report
from backend.test_routeros_release import NAT, ROUTES, StatefulRouterOS, driver_for

CIDR = '198.18.0.0/16'
GATEWAY = '192.168.0.3'
DESIRED = {'proxy_gateway': GATEWAY, 'proxy_cidrs': [{'cidr': CIDR, 'sources': ['fake-ip']}]}
ROUTER = {'type': 'routeros', 'endpoint': 'http://192.168.0.1'}
ROUTE = {'.id': '*1', 'dst-address': CIDR, 'gateway': GATEWAY, 'comment': 'cf',
         'routing-table': 'main', 'disabled': 'false', 'active': 'true'}
MASQUERADE = {'.id': '*A', 'chain': 'srcnat', 'action': 'masquerade', 'disabled': 'false'}
BYPASS = {'.id': '*B', 'chain': 'srcnat', 'action': 'accept', 'disabled': 'false',
          'comment': 'cf', 'dst-address': CIDR}


def inspect(session, desired=DESIRED):
    driver = driver_for(session)
    state = driver.read_state()
    report = build_report(desired, state)
    plan = build_plan(ROUTER, desired, state, report, {'status': 'disabled'}, driver)
    return driver, report, plan


@pytest.mark.parametrize('field,value', [('routing-table', 'backup'), ('active', 'false')])
def test_non_main_or_inactive_route_never_satisfies_publication(field, value):
    session = StatefulRouterOS(routes=[{**ROUTE, field: value}], nats=[], addresses=[])
    driver, report, plan = inspect(session)
    assert report['routes']['items'][0]['status'] not in ('synced', 'adoptable')
    assert not any(op['kind'] == 'route_delete' for stage in plan['stages'] for op in stage['ops'])
    with pytest.raises(ReleaseError):
        _verify('routes', {'routes': [{'cidr': CIDR, 'gateway': GATEWAY}], 'removed_routes': []}, driver, None)


@pytest.mark.parametrize('kind', ['route_add', 'route_comment', 'nat_update'])
def test_rollback_preserves_objects_changed_by_external_writer(kind):
    session = StatefulRouterOS(routes=[ROUTE], nats=[BYPASS], addresses=[])
    driver = driver_for(session)
    if kind == 'route_add':
        undo = driver.apply({'kind': kind, 'cidr': '91.108.0.0/16', 'gateway': GATEWAY, 'comment': 'cf'})
        row = session.rows[ROUTES][-1]
    elif kind == 'route_comment':
        undo = driver.apply({'kind': kind, 'id': '*1', 'comment': 'cf-updated'})
        row = session.rows[ROUTES][0]
    else:
        undo = driver.apply({'kind': kind, 'id': '*B', 'add_dst': [], 'comment': 'cf-updated'})
        row = session.rows[NAT][0]
    row['comment'] = 'changed in WinBox'
    before = copy.deepcopy(session.rows)
    with pytest.raises(RouterRollbackError, match='变化|冲突'):
        driver.apply(undo)
    assert session.rows == before


def test_lost_create_response_does_not_infer_ownership_from_matching_values():
    session = StatefulRouterOS(routes=[], nats=[], addresses=[], timeout_after=('PUT', ROUTES, 1))
    # The response was lost: the same observable state can be our write or a
    # simultaneous external create. Values alone cannot establish ownership.
    with pytest.raises(RouterRollbackError, match='归属|对象 ID|确认'):
        driver_for(session).apply({'kind': 'route_add', 'cidr': CIDR, 'gateway': GATEWAY, 'comment': 'cf'})
    assert len(session.rows[ROUTES]) == 1
    assert not any(method == 'DELETE' for method, _, _ in session.writes)


@pytest.mark.parametrize('restriction', [
    {'src-address': '192.168.0.9'}, {'src-address-list': 'selected'}, {'protocol': 'tcp'},
    {'in-interface': 'lan-other'}, {'out-interface': 'wan'}, {'dst-port': '443'},
    {'connection-mark': 'other'}, {'dst-address-list': 'selected'},
])
def test_restricted_bypass_cannot_claim_all_clients_and_protocols_are_covered(restriction):
    session = StatefulRouterOS(routes=[ROUTE], nats=[{**BYPASS, **restriction}, MASQUERADE], addresses=[
        {'.id': '*L', 'list': 'selected', 'address': '192.168.0.9'},
    ])
    driver, report, plan = inspect(session)
    assert report['nat_bypass']['items'][0]['status'] == 'unknown'
    assert plan['blockers']
    with pytest.raises(RouterError):
        _verify('nat_bypass', {'gateway': GATEWAY, 'nat_bypass': [CIDR]}, driver, None)
    assert session.writes == []


def test_shadowed_bypass_is_not_reported_or_verified_as_effective():
    session = StatefulRouterOS(routes=[ROUTE], nats=[MASQUERADE, BYPASS], addresses=[])
    driver, report, plan = inspect(session)
    assert report['nat_bypass']['items'][0]['status'] == 'unknown'
    assert plan['blockers']
    with pytest.raises(RouterError):
        _verify('nat_bypass', {'gateway': GATEWAY, 'nat_bypass': [CIDR]}, driver, None)


def test_complete_bypass_before_translation_still_publishes_without_extra_rules():
    session = StatefulRouterOS(routes=[ROUTE], nats=[BYPASS, MASQUERADE], addresses=[])
    driver, report, plan = inspect(session)
    assert report['nat_bypass']['items'][0]['status'] == 'synced'
    assert not plan['blockers'] and plan['empty']
    _verify('nat_bypass', plan['checks'], driver, None)


def test_routeros_nat_report_does_not_reparse_legacy_destination_coverage(monkeypatch):
    from backend.routers import report

    session = StatefulRouterOS(routes=[ROUTE], nats=[BYPASS, MASQUERADE], addresses=[])
    state = driver_for(session).read_state()
    def legacy_coverage(_):
        pytest.fail('RouterOS must use the indexed raw selectors, not reparse legacy destination coverage')
    monkeypatch.setattr(report, '_networks', legacy_coverage)
    result = report._nat_items(DESIRED, state)
    assert result['items'][0]['status'] == 'synced'


def test_expansion_cannot_clone_a_restricted_bypass_and_claim_complete_coverage():
    session = StatefulRouterOS(routes=[ROUTE], nats=[{**BYPASS, 'protocol': 'tcp'}, MASQUERADE], addresses=[])
    desired = {**DESIRED, 'proxy_cidrs': [{'cidr': '91.108.0.0/16', 'sources': ['custom']}]}
    _, _, plan = inspect(session, desired)
    assert plan['blockers']
    assert session.writes == []


@pytest.mark.parametrize('list_covers_new_target', [False, True])
def test_expansion_keeps_destination_list_and_requires_it_to_cover_new_target(list_covers_new_target):
    from backend.routers.release import execute

    target = '91.108.0.0/16'
    addresses = [{'.id': '*L1', 'list': 'targets', 'address': CIDR}]
    if list_covers_new_target:
        addresses.append({'.id': '*L2', 'list': 'targets', 'address': target})
    session = StatefulRouterOS(routes=[ROUTE], nats=[
        {**BYPASS, 'dst-address-list': 'targets'}, MASQUERADE,
    ], addresses=addresses)
    desired = {**DESIRED, 'proxy_cidrs': [*DESIRED['proxy_cidrs'],
                                        {'cidr': target, 'sources': ['custom']}]}
    driver, _, plan = inspect(session, desired)
    if not list_covers_new_target:
        assert plan['blockers']
        assert session.writes == []
        assert session.rows[NAT][0]['dst-address-list'] == 'targets'
        return
    assert not plan['blockers']
    assert execute(plan, driver, None)['ok']
    clone = next(row for row in session.rows[NAT] if row.get('dst-address') == target)
    assert clone['dst-address-list'] == 'targets'
    _verify('nat_bypass', plan['checks'], driver, None)


def test_literal_and_address_list_selectors_are_intersected_and_complete_lists_work():
    session = StatefulRouterOS(routes=[ROUTE], nats=[{
        **BYPASS, 'src-address': '192.168.0.0/24', 'src-address-list': 'clients',
        'dst-address-list': 'targets'}, MASQUERADE], addresses=[
            {'.id': '*L1', 'list': 'clients', 'address': '192.168.0.0/25'},
            {'.id': '*L2', 'list': 'clients', 'address': '192.168.0.128/25'},
            {'.id': '*L3', 'list': 'targets', 'address': CIDR},
        ])
    driver, report, plan = inspect(session)
    assert report['nat_bypass']['items'][0]['status'] == 'synced'
    assert plan['empty'] and not plan['blockers']
    _verify('nat_bypass', plan['checks'], driver, None)
    # The source list cannot broaden the literal source selector on the same rule.
    session.rows[NAT][0]['src-address'] = '192.168.0.9'
    assert inspect(session)[1]['nat_bypass']['items'][0]['status'] == 'unknown'


def test_later_stage_failure_reports_rollback_conflict_and_preserves_external_route():
    from backend.routers.release import execute

    session = StatefulRouterOS(routes=[], nats=[MASQUERADE], addresses=[], fail=('PUT', NAT, 1))
    driver, _, plan = inspect(session)
    request = session.request

    def external_edit_before_nat_failure(method, url, **kwargs):
        if method == 'PUT' and url.endswith('/rest' + NAT):
            session.rows[ROUTES][0].update(gateway='192.168.0.250', comment='WinBox edit')
        return request(method, url, **kwargs)

    session.request = external_edit_before_nat_failure
    result = execute(plan, driver, None)
    assert not result['ok'] and result['failed_stage'] == 'nat_bypass'
    assert any(item['status'] == 'failed' and '外部变化' in item['summary'] for item in result['rollback'])
    assert session.rows[ROUTES][0]['gateway'] == '192.168.0.250'
    assert session.rows[ROUTES][0]['comment'] == 'WinBox edit'


def test_delete_rollback_does_not_duplicate_a_new_external_replacement():
    session = StatefulRouterOS(routes=[ROUTE], nats=[], addresses=[])
    driver = driver_for(session)
    undo = driver.apply({'kind': 'route_delete', 'id': '*1'})
    replacement = {**ROUTE, '.id': '*F', 'gateway': '192.168.0.250', 'comment': 'manual replacement'}
    session.rows[ROUTES].append(replacement)
    with pytest.raises(RouterRollbackError, match='其他程序'):
        driver.apply(undo)
    assert session.rows[ROUTES] == [replacement]
