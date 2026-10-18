"""Static main-table releases use RouterOS runtime and preference evidence."""
import copy
from types import SimpleNamespace

import pytest

from backend.routers.base import RouteEntry, RouterState
from backend.routers.release import ReleaseError, _verify
from backend.routers.report import build_report
from backend.test_router_release import DESIRED, GATEWAY
from backend.test_routeros_release import ROUTES, StatefulRouterOS, driver_for, release_plan


CIDR = '198.18.0.0/16'
DESIRED_STATIC = {**DESIRED, 'proxy_cidrs': [{'cidr': CIDR, 'sources': ['fake-ip']}]}
CHECKS = {'routes': [{'cidr': CIDR, 'gateway': GATEWAY}], 'removed_routes': []}


def route(row_id='*1', **fields):
    return {'.id': row_id, 'dst-address': CIDR, 'gateway': GATEWAY,
            'comment': 'cf-fake-ip', 'disabled': 'false', 'routing-table': 'main',
            'distance': '1', 'active': 'true', **fields}


def setup(rows):
    session = StatefulRouterOS(routes=rows, nats=[], addresses=[])
    return driver_for(session), session


def item(driver):
    return build_report(DESIRED_STATIC, driver.read_state())['routes']['items'][0]


def route_ops(driver):
    plan = release_plan(driver, desired=DESIRED_STATIC)
    return next(stage['ops'] for stage in plan['stages'] if stage['key'] == 'routes')


def test_routeros_preserves_table_preference_and_runtime_evidence():
    driver, _ = setup([
        route(**{'routing-table': 'backup', 'active': 'false', 'distance': '17'}),
        route('*2', **{'active': 'true', 'inactive': 'true'}),
        {'.id': '*3', 'dst-address': CIDR, 'gateway': GATEWAY},
        {'.id': '*4', 'dst-address': CIDR, 'gateway': GATEWAY, 'inactive': 'false'},
    ])
    entries = driver.read_state().routes
    assert [(entry.table, entry.active, entry.distance) for entry in entries] == [
        ('backup', False, 17), ('main', False, 1), ('main', None, None), ('main', True, None)]


@pytest.mark.parametrize('comment', ['cf-fake-ip', 'manual'])
def test_non_main_only_route_is_missing_and_new_main_does_not_touch_other_tables(comment):
    rows = [route(**{'routing-table': 'backup', 'comment': comment}),
            route('*2', **{'routing-table': 'backup', 'dst-address': '203.0.113.0/24',
                           'comment': 'cf-old'})]
    driver, session = setup(rows)
    report = build_report(DESIRED_STATIC, driver.read_state())
    assert report['routes']['items'][0]['status'] == 'missing'
    assert report['routes']['items'][0]['route_ids'] == []
    assert report['routes']['extra'] == []
    assert not report['in_sync']
    with pytest.raises(ReleaseError):
        _verify('routes', CHECKS, driver, None)
    [op] = route_ops(driver)
    assert op['kind'] == 'route_add' and op['cidr'] == CIDR
    driver.apply(op)
    _verify('routes', CHECKS, driver, None)
    assert session.rows[ROUTES][:2] == rows
    assert not any(method in {'PATCH', 'DELETE'} for method, _, _ in session.writes)


@pytest.mark.parametrize(('correct', 'wrong'), [
    ({'active': 'false', 'distance': '10'}, {'active': 'true', 'distance': '1'}),
    ({'active': 'true', 'distance': '10'}, {'active': 'false', 'distance': '1'}),
    ({'active': 'true', 'distance': '1'}, {'active': 'true', 'distance': '1'}),
    ({'active': 'true', 'distance': '1'}, {'active': 'false', 'distance': '1'}),
    ({'active': 'true', 'distance': '1'}, {'active': 'true', 'distance': '10'}),
])
def test_competing_wrong_gateway_never_reports_or_verifies_synced(correct, wrong):
    driver, session = setup([route(**correct), route('*2', gateway='10.0.0.99', **wrong)])
    before = copy.deepcopy(session.rows[ROUTES])
    assert item(driver)['status'] == 'conflict'
    assert route_ops(driver) == []
    with pytest.raises(ReleaseError):
        _verify('routes', CHECKS, driver, None)
    assert session.rows[ROUTES] == before and session.writes == []


@pytest.mark.parametrize('comment', ['cf-fake-ip', 'manual'])
@pytest.mark.parametrize(('correct', 'specific'), [
    ({}, {'active': 'true', 'distance': '200'}),
    ({}, {'active': None, 'distance': '200'}),
    ({}, {'active': None, 'distance': None}),
    ({'active': None}, {'active': None, 'distance': '200'}),
    ({'active': None, 'distance': None}, {'active': 'true'}),
    ({'active': None, 'distance': None}, {'active': None, 'distance': '200'}),
])
def test_more_specific_wrong_path_blocks_report_verification_and_adoption(comment, correct, specific):
    rows = [
        route(comment=comment, **correct),
        route('*2', **{'dst-address': '198.18.1.0/24', 'gateway': '10.0.0.99',
                      'comment': 'manual-specific', **specific}),
    ]
    rows = [{key: value for key, value in row.items() if value is not None} for row in rows]
    driver, session = setup(rows)
    report = build_report(DESIRED_STATIC, driver.read_state())
    assert report['routes']['items'][0]['status'] == 'conflict'
    assert report['routes']['items'][0]['route_ids'] == ['*1']
    assert report['routes']['extra'] == []
    assert report['nat_bypass']['items'][0]['status'] == 'not_required'
    assert not report['in_sync']
    assert route_ops(driver) == []
    with pytest.raises(ReleaseError):
        _verify('routes', CHECKS, driver, None)
    assert session.rows[ROUTES] == rows and session.writes == []


@pytest.mark.parametrize('alternative', [
    {'active': 'false', 'distance': '0'},
    {'inactive': 'true', 'distance': '0'},
    {'invalid': 'true', 'distance': '0'},
    {'disabled': 'true'},
    {'routing-table': 'backup'},
    {'gateway': GATEWAY},
    {'dst-address': '198.19.1.0/24'},
    {'dst-address': '198.18.0.0/15'},
    {'dst-address': '2001:db8::/64'},
])
def test_noncompeting_specific_routes_preserve_synced_report_and_verification(alternative):
    rows = [route(), route('*2', **{
        'dst-address': '198.18.1.0/24', 'gateway': '10.0.0.99',
        'comment': 'manual-specific', **alternative,
    })]
    driver, session = setup(rows)
    report = build_report(DESIRED_STATIC, driver.read_state())
    assert report['routes']['items'][0]['status'] == 'synced'
    assert report['routes']['items'][0]['route_ids'] == ['*1']
    assert report['in_sync']
    assert route_ops(driver) == []
    _verify('routes', CHECKS, driver, None)
    assert session.rows[ROUTES] == rows and session.writes == []


def test_safe_specific_routes_do_not_change_exact_adoption_or_managed_main_cleanup():
    rows = [
        route(comment='manual'),
        route('*2', **{'dst-address': '198.18.1.0/24', 'comment': 'manual-specific'}),
        route('*3', **{'dst-address': '198.18.2.0/24', 'gateway': '10.0.0.99',
                      'active': 'false', 'comment': 'manual-inactive'}),
        route('*4', **{'dst-address': '203.0.113.0/24', 'comment': 'cf-old'}),
        route('*5', **{'dst-address': '203.0.113.0/24', 'comment': 'cf-backup',
                      'routing-table': 'backup'}),
    ]
    driver, session = setup(rows)
    report = build_report(DESIRED_STATIC, driver.read_state())
    assert report['routes']['items'][0]['status'] == 'adoptable'
    assert report['routes']['items'][0]['route_ids'] == ['*1']
    assert [entry['route_id'] for entry in report['routes']['extra']] == ['*4']
    ops = route_ops(driver)
    assert [(op['kind'], op['id']) for op in ops] == [('route_comment', '*1'), ('route_delete', '*4')]
    for op in ops:
        driver.apply(op)
    assert session.rows[ROUTES][1:] == [rows[1], rows[2], rows[4]]
    assert [(method, path) for method, path, _ in session.writes] == [
        ('PATCH', f'{ROUTES}/*1'), ('DELETE', f'{ROUTES}/*4'),
    ]
    assert build_report(DESIRED_STATIC, driver.read_state())['in_sync']
    _verify('routes', {**CHECKS, 'removed_routes': ['*4']}, driver, None)


@pytest.mark.parametrize('runtime', [{'active': 'false'}, {'inactive': 'true'}, {'invalid': 'true'}])
def test_inactive_correct_route_alone_is_not_synced(runtime):
    row = route()
    row.pop('active')
    row.update(runtime)
    driver, _ = setup([row])
    assert item(driver)['status'] == 'conflict'
    with pytest.raises(ReleaseError):
        _verify('routes', CHECKS, driver, None)


@pytest.mark.parametrize('wrong_distance', ['1', '10'])
def test_distance_without_active_flags_rejects_lower_or_equal_wrong_gateway(wrong_distance):
    rows = [route(distance='10'), route('*2', gateway='10.0.0.99', distance=wrong_distance)]
    for row in rows:
        row.pop('active')
    driver, _ = setup(rows)
    assert item(driver)['status'] == 'conflict'
    with pytest.raises(ReleaseError):
        _verify('routes', CHECKS, driver, None)


@pytest.mark.parametrize('alternative', [
    {'active': 'false', 'distance': '10'},
    {'active': 'true', 'distance': '0', 'routing-table': 'backup'},
    {'active': 'false', 'distance': '0', 'disabled': 'true'},
])
def test_active_main_route_accepts_only_noncompeting_alternatives(alternative):
    driver, _ = setup([route(), route('*2', gateway='10.0.0.99', **alternative)])
    assert item(driver)['status'] == 'synced'
    assert route_ops(driver) == []
    _verify('routes', CHECKS, driver, None)


def test_adoption_uses_effective_main_route_not_backup_or_inactive_sibling():
    rows = [route(**{'routing-table': 'backup', 'comment': 'backup'}),
            route('*2', active='false', distance='10', comment='inactive'),
            route('*3', comment='active')]
    driver, session = setup(rows)
    assert item(driver)['status'] == 'adoptable'
    [op] = route_ops(driver)
    assert op['kind'] == 'route_comment' and op['id'] == '*3'
    driver.apply(op)
    assert session.rows[ROUTES][:2] == rows[:2]
    assert item(driver)['status'] == 'synced'
    _verify('routes', CHECKS, driver, None)


@pytest.mark.parametrize('distance', ['1', '10'])
def test_managed_inactive_sibling_cannot_make_manual_active_route_synced(distance):
    driver, _ = setup([route(active='false', distance=distance), route('*2', comment='manual')])
    assert item(driver)['status'] == 'adoptable'
    with pytest.raises(ReleaseError):
        _verify('routes', CHECKS, driver, None)


@pytest.mark.parametrize('alternative', [CIDR, '198.18.1.0/24'])
def test_legacy_route_entries_without_runtime_evidence_keep_configured_semantics(alternative):
    entry = RouteEntry('1', CIDR, GATEWAY, True, 'cf-fake-ip')
    state = RouterState(routes=[entry, RouteEntry('2', alternative, '10.0.0.99', True)])
    driver = SimpleNamespace(read_state=lambda: state)
    assert (entry.table, entry.active, entry.distance) == ('main', None, None)
    assert item(driver)['status'] == 'synced'
    _verify('routes', CHECKS, driver, None)


def test_policy_named_main_route_is_competing_evidence_but_not_static_cleanup():
    rows = [route(active='false', distance='10'),
            route('*2', gateway='10.0.0.99', comment='cf-ros-policy-unproven'),
            route('*3', **{'dst-address': '203.0.113.0/24', 'comment': 'cf-ros-policy-old'})]
    driver, session = setup(rows)
    assert len(driver.read_state().routes) == 3
    report = build_report(DESIRED_STATIC, driver.read_state())
    assert report['routes']['items'][0]['status'] == 'conflict'
    assert report['routes']['extra'] == []
    assert route_ops(driver) == []
    assert session.writes == []
    with pytest.raises(ReleaseError):
        _verify('routes', CHECKS, driver, None)


def test_missing_aggregate_with_wrong_manual_specific_is_conflict_before_publication():
    cidr = '198.18.0.0/22'
    desired = {**DESIRED_STATIC, 'proxy_cidrs': [{'cidr': cidr, 'sources': ['fake-ip']}]}
    rows = [route('*specific', **{'dst-address': '198.18.1.0/24', 'gateway': '10.0.0.99',
                                 'comment': 'manual-specific'})]
    driver, session = setup(rows)
    report = build_report(desired, driver.read_state())
    assert report['routes']['items'][0]['status'] == 'conflict'
    assert report['routes']['items'][0]['route_ids'] == []
    plan = release_plan(driver, desired=desired)
    assert next(stage['ops'] for stage in plan['stages'] if stage['key'] == 'routes') == []
    with pytest.raises(ReleaseError):
        _verify('routes', {**CHECKS, 'routes': [{'cidr': cidr, 'gateway': GATEWAY}]}, driver, None)
    assert session.rows[ROUTES] == rows and session.writes == []


@pytest.mark.parametrize('comments', [
    ('cf-left', 'cf-right'), ('manual-left', 'manual-right'), ('cf-left', 'manual-right'),
])
def test_fully_shadowed_wrong_aggregate_owns_only_complete_correct_subnet_coverage(comments):
    rows = [
        route('*wrong', gateway='10.0.0.99', comment='manual-wrong'),
        route('*left', **{'dst-address': '198.18.0.0/17', 'comment': comments[0]}),
        route('*right', **{'dst-address': '198.18.128.0/17', 'comment': comments[1]}),
    ]
    driver, session = setup(rows)
    report = build_report(DESIRED_STATIC, driver.read_state())
    owned = all(comment.startswith('cf-') for comment in comments)
    assert report['routes']['items'][0]['status'] == ('synced' if owned else 'adoptable')
    assert report['routes']['extra'] == []
    if owned:
        _verify('routes', CHECKS, driver, None)
    else:
        with pytest.raises(ReleaseError):
            _verify('routes', CHECKS, driver, None)
    ops = route_ops(driver)
    assert [(op['kind'], op['id']) for op in ops] == [
        ('route_comment', row_id) for row_id, comment in zip(('*left', '*right'), comments)
        if not comment.startswith('cf-')]
    for op in ops:
        driver.apply(op)
    assert session.rows[ROUTES][0] == rows[0]
    assert build_report(DESIRED_STATIC, driver.read_state())['in_sync']
    assert route_ops(driver) == []
    _verify('routes', CHECKS, driver, None)


@pytest.mark.parametrize('gap', ['missing-half', 'inactive-half', 'wrong-specific'])
def test_shadowed_aggregate_requires_correct_effective_coverage_of_every_address(gap):
    rows = [
        route('*wrong', gateway='10.0.0.99', comment='manual-wrong'),
        route('*left', **{'dst-address': '198.18.0.0/17', 'comment': 'cf-left'}),
        route('*right', **{'dst-address': '198.18.128.0/17', 'comment': 'cf-right'}),
    ]
    if gap == 'missing-half':
        rows.pop()
    elif gap == 'inactive-half':
        rows[-1]['active'] = 'false'
    else:
        rows.append(route('*specific', **{'dst-address': '198.18.1.0/24',
                                         'gateway': '10.0.0.98', 'comment': 'manual-specific'}))
    driver, session = setup(rows)
    assert item(driver)['status'] == 'conflict'
    with pytest.raises(ReleaseError):
        _verify('routes', CHECKS, driver, None)
    assert session.rows[ROUTES] == rows and session.writes == []
