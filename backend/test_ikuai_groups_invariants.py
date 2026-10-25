"""iKuai IP groups for proxy destinations leave every other router path exactly as 4f07e227 had it.

RouterOS plans, fingerprints, REST writes and final rows, and the iKuai static
route stage (``static_rt`` one row per CIDR), are pinned here. The helpers only
use code that already existed in 4f07e227, so the fixture in
``testdata/router_plans_pre_ikuai_groups_4f07e227.json`` was produced by running
them against that commit (``python -m backend.test_ikuai_groups_invariants``).
"""
import copy
import json
import sys
from pathlib import Path

import pytest

BASELINE = Path(__file__).parent / 'testdata' / 'router_plans_pre_ikuai_groups_4f07e227.json'
PLAN_KEYS = ('stages', 'checks', 'blockers', 'warnings', 'fingerprint', 'empty')
STATIC_CHECKS = ('gateway', 'routes', 'removed_routes', 'restored_routes', 'removed_policy_routes')


def _json(value):
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))


def _plan(plan):
    return _json({key: plan.get(key) for key in PLAN_KEYS})


# --- RouterOS ------------------------------------------------------------------------

def _ros_release_scenarios():
    from backend.routers.release import execute
    from backend.test_router_release import FakeAgent, SAFE_RETURN_STATUS, DESIRED
    from backend.test_routeros_release import (
        BROAD_SOURCE_NAT, NAT, driver_for, release_plan, release_session,
    )
    result = {}

    session = release_session()
    driver = driver_for(session)
    agent = FakeAgent(SAFE_RETURN_STATUS)
    plan = release_plan(driver)
    outcome = execute(plan, driver, agent)
    again = release_plan(driver, status={**agent.status(), 'status': 'ok'})
    result['ros_home_release'] = {'plan': _plan(plan), 'result': _json(outcome), 'writes': _json(session.writes),
                                  'rows': _json(session.rows), 'again': _plan(again)}

    session = release_session(routes=[], nats=[BROAD_SOURCE_NAT])
    driver = driver_for(session)
    agent = FakeAgent(SAFE_RETURN_STATUS)
    execute(release_plan(driver), driver, agent)
    expanded = copy.deepcopy(DESIRED)
    expanded['proxy_cidrs'].append({'cidr': '185.76.151.0/24', 'sources': ['Telegram']})
    first = len(session.writes)
    plan = release_plan(driver, status={**agent.status(), 'status': 'ok'}, desired=expanded)
    outcome = execute(plan, driver, agent)
    result['ros_nat_expand'] = {'plan': _plan(plan), 'result': _json(outcome),
                                'writes': _json(session.writes[first:]), 'rows': _json(session.rows)}

    session = release_session(fail=('PUT', NAT, 2))
    driver = driver_for(session)
    plan = release_plan(driver)
    outcome = execute(plan, driver, FakeAgent(SAFE_RETURN_STATUS))
    result['ros_nat_failure'] = {'plan': _plan(plan), 'result': _json(outcome), 'writes': _json(session.writes),
                                 'rows': _json(session.rows)}
    return result


def _ros_policy_scenarios():
    from backend.test_ros_mode_transitions import static_release
    from backend.test_ros_policy import NAT, MemoryROS, desired_for, inspect, release
    result = {}

    driver = MemoryROS()
    report, plan = release(driver)
    _, again = inspect(driver)
    result['ros_policy_release'] = {'plan': _plan(plan), 'writes': _json(driver.writes), 'rows': _json(driver.rows),
                                    'manifest': _json(driver.ros_policy_manifest), 'again': _plan(again)}

    driver = MemoryROS()
    driver.rows[NAT][0].pop('out-interface')
    desired = desired_for(driver, selected=False)
    desired.pop('ros_policy')
    static_release(driver, desired)
    first = len(driver.writes)
    _, plan = release(driver)
    result['ros_static_to_policy'] = {'plan': _plan(plan), 'writes': _json(driver.writes[first:]),
                                      'rows': _json(driver.rows)}
    return result


def _ros_guard_scenarios():
    from backend.test_router_dns_guard import device, preview, publish, router
    session, driver = device()
    _, plan = preview(driver, router())
    outcome, item = publish(driver, router())
    # Reads move live counters in this fake; the writes and owned rows are what matter.
    return {'ros_dns_guard_enable': {
        'plan_stages': _json(plan['stages']), 'blockers': plan['blockers'], 'warnings': plan['warnings'],
        'result': _json(outcome), 'writes': _json(session.writes),
        'resources': _json(item['dns_guard_state']['resources'])}}


def routeros_snapshot():
    return {**_ros_release_scenarios(), **_ros_policy_scenarios(), **_ros_guard_scenarios()}


# --- iKuai static routes ---------------------------------------------------------------

def _static_stage(plan):
    routes = next(stage for stage in plan['stages'] if stage['key'] == 'routes')
    return {'ops': _json(routes['ops']), 'checks': _json({key: plan['checks'].get(key) for key in STATIC_CHECKS})}


def _static_writes(session):
    return _json([[func, action, param] for func, action, param in session.writes if func == 'static_rt'])


def _static_rows(session):
    return _json(session.rows['static_rt'])


def ikuai_static_snapshot():
    from backend.routers.release import execute
    from backend.test_ikuai_scale import PagedIKuai, _bulk, _cidrs, _route, _static_plan, _tagname
    from backend.test_router_release import GATEWAY, HOME_NATS, MIGRATED, _driver, _plan_without_return
    result = {}

    def run(name, session, cidrs=None, plan=None, fail_nat=False):
        driver = _driver(session)
        plan = plan(driver) if plan else _static_plan(driver, cidrs)
        if fail_nat:
            session.fail = ('nat_rule', 'add')
        outcome = execute(plan, driver, None)
        result[name] = {**_static_stage(plan), 'ok': outcome['ok'], 'failed_stage': outcome.get('failed_stage'),
                        'rollback': [entry['status'] for entry in outcome['rollback'] if entry['stage'] == 'routes'],
                        'writes': _static_writes(session), 'rows': _static_rows(session)}

    run('static_add_100', PagedIKuai(_bulk(20), []), _cidrs(100))
    managed = [_route(1501 + index, cidr, GATEWAY, 'cf-lab', _tagname('rt', cidr))
               for index, cidr in enumerate(_cidrs(100))]
    run('static_delete_100', PagedIKuai(_bulk(20) + managed, []), [])
    manual = [_route(1501 + index, cidr, GATEWAY, f'manual {index}') for index, cidr in enumerate(_cidrs(50))]
    run('static_adopt_50', PagedIKuai(_bulk(20) + manual, []), _cidrs(50))
    adopt = [_route(11 + index, cidr, GATEWAY, 'hand') for index, cidr in enumerate(_cidrs(3, '10.210.0.0/16'))]
    stale = [_route(21 + index, cidr, GATEWAY, 'cf-old', _tagname('rt', cidr))
             for index, cidr in enumerate(_cidrs(3, '10.220.0.0/16'))]
    run('static_mixed', PagedIKuai(_bulk(5) + adopt + stale, []), _cidrs(4) + _cidrs(3, '10.210.0.0/16'))
    run('static_from_policy', PagedIKuai([], HOME_NATS, policies=MIGRATED), plan=_plan_without_return)
    run('static_rollback', PagedIKuai(_bulk(20), []), _cidrs(30), fail_nat=True)
    return result


def snapshot():
    return {'routeros': routeros_snapshot(), 'ikuai_static': ikuai_static_snapshot()}


def baseline():
    return json.loads(BASELINE.read_text(encoding='utf-8'))


@pytest.fixture(scope='module')
def current():
    return snapshot()


@pytest.mark.parametrize('name', ['ros_home_release', 'ros_nat_expand', 'ros_nat_failure', 'ros_policy_release',
                                  'ros_static_to_policy', 'ros_dns_guard_enable'])
def test_routeros_plans_and_writes_match_the_previous_release(current, name):
    assert current['routeros'][name] == baseline()['routeros'][name]


@pytest.mark.parametrize('name', ['static_add_100', 'static_delete_100', 'static_adopt_50', 'static_mixed',
                                  'static_from_policy', 'static_rollback'])
def test_ikuai_static_routes_match_the_previous_release(current, name):
    assert current['ikuai_static'][name] == baseline()['ikuai_static'][name]


def test_static_routes_stay_one_row_per_cidr(current):
    for scenario in current['ikuai_static'].values():
        for func_action_param in scenario['writes']:
            _, action, param = func_action_param
            if action == 'add':
                assert set(param) >= {'dst_addr', 'netmask', 'gateway', 'tagname'}
                assert param['tagname'].startswith('cf_rt_') and 'object' not in json.dumps(param)


def test_routeros_state_has_no_ikuai_groups():
    from backend.routers.plan import build_plan
    from backend.routers.report import build_report
    from backend.test_router_release import SAFE_RETURN_STATUS, DESIRED
    from backend.test_routeros_release import driver_for, release_session
    driver = driver_for(release_session())
    state = driver.read_state()
    assert getattr(state, 'ikuai_groups', None) is None
    report = build_report(DESIRED, state)
    assert 'ikuai_groups' not in report
    plan = build_plan({'type': 'routeros', 'endpoint': 'https://10.0.0.1', 'manage_return_routing': True},
                      DESIRED, state, report, SAFE_RETURN_STATUS, driver)
    assert not any(op['kind'].startswith('ikuai_group') for stage in plan['stages'] for op in stage['ops'])
    assert 'ikuai_groups' not in plan['checks']


if __name__ == '__main__':
    # Regenerate only from the commit the fixture is named after.
    BASELINE.write_text(json.dumps(snapshot(), ensure_ascii=False, indent=1, sort_keys=True) + '\n',
                        encoding='utf-8')
    sys.exit(0)
