"""NAT exemption follows the router's actual source-NAT rules and proxy egress."""
import copy
import pytest

from backend.routers.plan import build_plan
from backend.routers.release import execute
from backend.routers.report import build_report
from backend.routers.routeros_nat import assess_nat
from backend.test_router_release import FakeAgent, SAFE_RETURN_STATUS
from backend.test_routeros_release import NAT, ROUTES, StatefulRouterOS, driver_for


CIDR = '198.18.0.0/16'
GATEWAY = '192.168.0.3'
DESIRED = {'fake_ip_range': CIDR, 'proxy_gateway': GATEWAY, 'warnings': [],
           'proxy_cidrs': [{'cidr': CIDR, 'sources': ['fake-ip']}]}
ROUTER = {'type': 'routeros', 'endpoint': 'https://192.168.0.1', 'manage_return_routing': True}
ACTIVE_PROXY_ROUTE = {'.id': '*1', 'dst-address': CIDR, 'gateway': GATEWAY,
                      'immediate-gw': f'{GATEWAY}%lan4', 'routing-table': 'main',
                      'active': 'true', 'disabled': 'false', 'dynamic': 'false', 'comment': 'cf-fake-ip'}
WAN_RULES = [
    {'.id': '*A', 'chain': 'srcnat', 'action': 'masquerade', 'out-interface': 'pppoe-out1',
     'disabled': 'false', 'bytes': '1234', 'packets': '10'},
    {'.id': '*B', 'chain': 'srcnat', 'action': 'masquerade', 'out-interface': 'wan', 'disabled': 'false'},
    {'.id': '*C', 'chain': 'srcnat', 'action': 'masquerade', 'protocol': 'tcp', 'disabled': 'true'},
    {'.id': '*D', 'chain': 'dstnat', 'action': 'dst-nat', 'dynamic': 'true', 'disabled': 'false',
     'comment': 'upnp 192.168.0.10'},
]


def nat_session(nats=None, routes=None):
    session = StatefulRouterOS(routes=[ACTIVE_PROXY_ROUTE] if routes is None else routes,
                               nats=WAN_RULES if nats is None else nats, addresses=[])
    session.rows['/interface/list'] = [{'.id': '*L1', 'name': 'WAN'}, {'.id': '*L2', 'name': 'LAN'}]
    session.rows['/interface/list/member'] = [
        {'.id': '*M1', 'list': 'WAN', 'interface': 'pppoe-out1', 'disabled': 'false'},
        {'.id': '*M2', 'list': 'WAN', 'interface': 'wan', 'disabled': 'false'},
        {'.id': '*M3', 'list': 'LAN', 'interface': 'lan4', 'disabled': 'false'},
    ]
    return session


def inspect(session):
    driver = driver_for(session)
    state = driver.read_state()
    report = build_report(DESIRED, state)
    plan = build_plan(ROUTER, DESIRED, state, report, SAFE_RETURN_STATUS, driver)
    return driver, state, report, plan


def nat_item(report):
    [item] = report['nat_bypass']['items']
    return item


def test_wan_only_masquerade_needs_no_explicit_bypass_and_preserves_agent_return_routing():
    session = nat_session()
    driver, state, report, plan = inspect(session)
    assert assess_nat(CIDR, GATEWAY, state)['status'] == 'not_required'
    assert nat_item(report)['status'] == 'not_required'
    assert nat_item(report)['detail']
    assert report['in_sync'] is True
    assert report['summary']['not_required'] == 1
    assert all('bytes' not in row and 'packets' not in row for row in state.nat_context['rules'])
    stages = {stage['key']: stage['ops'] for stage in plan['stages']}
    assert stages['nat_bypass'] == [] and stages['routes'] == []
    assert stages['return_routing'][0]['spec']['proxy_dst'] == [CIDR]
    assert plan['checks']['nat_not_required'] == [{'cidr': CIDR, 'gateway': GATEWAY}]
    assert plan['blockers'] == []
    result = execute(plan, driver, FakeAgent(SAFE_RETURN_STATUS))
    assert result['ok'], result
    assert session.writes == []


@pytest.mark.parametrize(('rule', 'expected'), [
    (None, 'not_required'),
    ({'action': 'masquerade'}, 'missing'),
    ({'action': 'src-nat', 'out-interface': 'lan4', 'to-addresses': '192.168.0.1'}, 'missing'),
    ({'action': 'masquerade', 'out-interface': 'lan4'}, 'missing'),
    ({'action': 'masquerade', 'out-interface': 'pppoe-out1'}, 'not_required'),
    ({'action': 'masquerade', 'out-interface': '!lan4'}, 'not_required'),
    ({'action': 'masquerade', 'out-interface': '!pppoe-out1'}, 'missing'),
    ({'action': 'masquerade', 'out-interface': 'all'}, 'missing'),
    ({'action': 'masquerade', 'out-interface': '!all'}, 'not_required'),
    ({'action': 'masquerade', 'disabled': 'true'}, 'not_required'),
    ({'action': 'dst-nat', 'chain': 'dstnat'}, 'not_required'),
])
def test_source_nat_matches_proxy_egress_and_ignores_disabled_or_dstnat(rule, expected):
    nats = [] if rule is None else [{'.id': '*A', 'chain': 'srcnat', 'disabled': 'false', **rule}]
    _, _, report, plan = inspect(nat_session(nats=nats))
    assert nat_item(report)['status'] == expected
    ops = next(stage['ops'] for stage in plan['stages'] if stage['key'] == 'nat_bypass')
    assert bool(ops) == (expected == 'missing')
    if ops:
        assert ops[0]['kind'] == 'nat_add'


@pytest.mark.parametrize('selector', ['all-ethernet', '!all-ppp'])
def test_legacy_interface_groups_cannot_be_excluded_as_literal_names(selector):
    rule = {'.id': '*A', 'chain': 'srcnat', 'action': 'masquerade', 'out-interface': selector}
    _, _, report, plan = inspect(nat_session(nats=[rule]))
    assert nat_item(report)['status'] == 'unknown'
    assert plan['blockers']


@pytest.mark.parametrize(('selector', 'expected'), [
    ('WAN', 'not_required'), ('LAN', 'missing'), ('!WAN', 'missing'),
    ('!LAN', 'not_required'), ('unknown-list', 'unknown'),
])
def test_interface_lists_and_negation_are_evaluated_conservatively(selector, expected):
    rule = {'.id': '*A', 'chain': 'srcnat', 'action': 'masquerade', 'disabled': 'false',
            'out-interface-list': selector}
    session = nat_session(nats=[rule])
    _, _, report, plan = inspect(session)
    assert nat_item(report)['status'] == expected
    assert bool(plan['blockers']) == (expected == 'unknown')
    assert session.writes == []


def test_unknown_egress_blocks_release_instead_of_claiming_no_nat():
    route = {key: value for key, value in ACTIVE_PROXY_ROUTE.items() if key != 'immediate-gw'}
    _, _, report, plan = inspect(nat_session(routes=[route]))
    assert nat_item(report)['status'] == 'unknown'
    assert report['in_sync'] is False and plan['blockers']


def test_dynamic_gateway_route_without_connected_flag_cannot_prove_egress():
    connected = {'.id': '*2', 'dst-address': '192.168.0.0/24', 'gateway': 'lan4',
                 'immediate-gw': 'lan4', 'active': 'true', 'dynamic': 'true',
                 'disabled': 'false', 'routing-table': 'main'}
    _, _, report, plan = inspect(nat_session(routes=[connected]))
    assert nat_item(report)['status'] == 'unknown'
    assert plan['blockers']


def test_missing_proxy_route_uses_connected_gateway_egress_during_publish():
    connected = {'.id': '*2', 'dst-address': '192.168.0.0/24', 'gateway': 'lan4',
                 'immediate-gw': 'lan4', 'active': 'true', 'dynamic': 'true',
                 'disabled': 'false', 'routing-table': 'main', 'connect': 'true'}
    session = nat_session(routes=[connected])
    original_response = session._written_response

    def resolve_written_route(method, collection, response):
        if method == 'PUT' and collection == ROUTES:
            response.data.update(active='true', **{'immediate-gw': f'{GATEWAY}%lan4', 'routing-table': 'main'})
        return original_response(method, collection, response)

    session._written_response = resolve_written_route
    driver, _, report, plan = inspect(session)
    assert nat_item(report)['status'] == 'not_required'
    assert next(stage['ops'] for stage in plan['stages'] if stage['key'] == 'nat_bypass') == []
    route_ops = next(stage['ops'] for stage in plan['stages'] if stage['key'] == 'routes')
    assert [op['kind'] for op in route_ops] == ['route_add']
    result = execute(plan, driver, FakeAgent(SAFE_RETURN_STATUS))
    assert result['ok'], result
    assert [(method, path) for method, path, _ in session.writes] == [('PUT', ROUTES)]
    assert nat_item(inspect(session)[2])['status'] == 'not_required'


@pytest.mark.parametrize('attribute', ['inactive', 'ecmp', 'invalid', 'more_specific'])
def test_uncertain_or_nonuniform_routes_cannot_claim_source_nat_is_unnecessary(attribute):
    session = nat_session()
    if attribute == 'more_specific':
        session.rows[ROUTES].append({'.id': '*3', 'dst-address': '198.18.0.0/24',
                                     'gateway': '192.168.0.9', 'immediate-gw': '192.168.0.9%pppoe-out1',
                                     'active': 'true', 'disabled': 'false', 'routing-table': 'main'})
    elif attribute == 'inactive':
        session.rows[ROUTES][0]['active'] = 'false'
    else:
        session.rows[ROUTES][0][attribute] = 'true'
    before = copy.deepcopy(session.rows)
    driver, _, report, plan = inspect(session)
    assert nat_item(report)['status'] == 'unknown'
    result = execute(plan, driver, FakeAgent(SAFE_RETURN_STATUS))
    if report['routes']['items'][0]['status'] == 'conflict':
        # Skipping the destination must not change its forwarding or NAT,
        # rather than blocking unrelated safe destinations.
        assert result['ok'], result
    else:
        assert not result['ok']
    assert session.rows == before
    assert session.writes == []


def test_disabled_interface_members_do_not_expand_wan_match():
    session = nat_session(nats=[{'.id': '*A', 'chain': 'srcnat', 'action': 'masquerade',
                                'out-interface-list': 'WAN', 'disabled': 'false'}])
    session.rows['/interface/list/member'].append(
        {'.id': '*M4', 'list': 'WAN', 'interface': 'lan4', 'disabled': 'true'})
    _, _, report, plan = inspect(session)
    assert nat_item(report)['status'] == 'not_required'
    assert plan['blockers'] == []


@pytest.mark.parametrize('field', ['include', 'exclude'])
def test_interface_list_inheritance_is_unknown_without_expanded_evidence(field):
    session = nat_session(nats=[{'.id': '*A', 'chain': 'srcnat', 'action': 'masquerade',
                                'out-interface-list': 'WAN', 'disabled': 'false'}])
    session.rows['/interface/list'][0][field] = 'LAN'
    _, _, report, plan = inspect(session)
    assert nat_item(report)['status'] == 'unknown'
    assert plan['blockers']


@pytest.mark.parametrize('path', ['/routing/rule', '/ip/firewall/mangle', '/ip/vrf',
                                  '/interface/list', '/interface/list/member'])
def test_missing_policy_or_interface_evidence_blocks_release(path):
    session = nat_session(nats=[{'.id': '*A', 'chain': 'srcnat', 'action': 'masquerade',
                                'out-interface-list': 'WAN', 'disabled': 'false'}])
    session.read_failures.add(path)
    _, _, report, plan = inspect(session)
    assert nat_item(report)['status'] == 'unknown'
    assert plan['blockers'] and session.writes == []


@pytest.mark.parametrize(('path', 'row'), [
    ('/routing/rule', {'.id': '*P', 'src-address': '192.168.0.0/24', 'action': 'lookup-only-in-table',
                      'table': 'other', 'disabled': 'false'}),
    ('/ip/firewall/mangle', {'.id': '*P', 'chain': 'prerouting', 'action': 'mark-routing',
                            'new-routing-mark': 'other', 'disabled': 'false'}),
    ('/ip/vrf', {'.id': '*P', 'name': 'other', 'interfaces': 'lan4', 'disabled': 'false'}),
])
def test_complex_policy_routing_prevents_proving_wan_rules_do_not_match(path, row):
    session = nat_session()
    session.rows[path] = [row]
    _, _, report, plan = inspect(session)
    assert nat_item(report)['status'] == 'unknown'
    assert plan['blockers']


@pytest.mark.parametrize('action', ['absent', None, 17, 'future-route-action'])
def test_incomplete_or_unknown_mangle_action_cannot_prove_no_policy_routing(action):
    session = nat_session()
    row = {} if action == 'absent' else {
        '.id': '*P', 'chain': 'prerouting', 'action': action, 'disabled': 'false'}
    session.rows['/ip/firewall/mangle'] = [row]
    _, _, report, plan = inspect(session)
    assert nat_item(report)['status'] == 'unknown'
    assert plan['blockers']


def test_source_nat_without_chain_is_unknown_instead_of_treated_as_irrelevant():
    session = nat_session(nats=[{'.id': '*A', 'action': 'masquerade',
                                'out-interface': 'pppoe-out1', 'disabled': 'false'}])
    _, _, report, plan = inspect(session)
    assert nat_item(report)['status'] == 'unknown'
    assert plan['blockers']


@pytest.mark.parametrize('member', [
    {'.id': '*M4', 'list': 'WAN', 'disabled': 'false'},
    {'.id': '*M4', 'interface': 'lan4', 'disabled': 'false'},
])
def test_incomplete_interface_members_cannot_prove_wan_excludes_proxy_egress(member):
    session = nat_session(nats=[{'.id': '*A', 'chain': 'srcnat', 'action': 'masquerade',
                                'out-interface-list': 'WAN', 'disabled': 'false'}])
    session.rows['/interface/list/member'].append(member)
    _, _, report, plan = inspect(session)
    assert nat_item(report)['status'] == 'unknown'
    assert plan['blockers']


def test_unknown_nat_jump_blocks_release():
    session = nat_session(nats=[{'.id': '*A', 'chain': 'srcnat', 'action': 'jump',
                                'jump-target': 'custom-nat', 'disabled': 'false'}])
    _, _, report, plan = inspect(session)
    assert nat_item(report)['status'] == 'unknown'
    assert plan['blockers']


@pytest.mark.parametrize('change', ['source_nat', 'egress'])
def test_safe_evidence_changes_invalidate_fingerprint_even_with_identical_operations(change):
    session = nat_session()
    _, _, before_report, before = inspect(session)
    if change == 'source_nat':
        session.rows[NAT][0]['out-interface'] = 'different-wan'
    else:
        session.rows[ROUTES][0]['immediate-gw'] = f'{GATEWAY}%lan5'
    _, _, after_report, after = inspect(session)
    assert nat_item(before_report)['status'] == nat_item(after_report)['status'] == 'not_required'
    assert before['stages'] == after['stages']
    assert before['fingerprint'] != after['fingerprint']


def test_nat_traffic_counters_do_not_invalidate_preview_fingerprint():
    session = nat_session()
    before = inspect(session)[3]
    for rule in session.rows[NAT]:
        rule['bytes'] = '999999'
        rule['packets'] = '7777'
    after = inspect(session)[3]
    assert before['fingerprint'] == after['fingerprint']


@pytest.mark.parametrize('change', ['source_nat', 'egress'])
def test_release_checks_empty_nat_stage_when_safety_changes_after_agent_apply(change):
    session = nat_session()
    driver, _, _, plan = inspect(session)
    assert next(stage['ops'] for stage in plan['stages'] if stage['key'] == 'nat_bypass') == []

    class ChangedNetworkAgent(FakeAgent):
        def apply(self, spec):
            result = super().apply(spec)
            if change == 'source_nat':
                session.rows[NAT][0].pop('out-interface')
            else:
                session.rows[ROUTES][0]['immediate-gw'] = f'{GATEWAY}%pppoe-out1'
            return result

    agent = ChangedNetworkAgent(SAFE_RETURN_STATUS)
    result = execute(plan, driver, agent)
    assert result['ok'] is False
    assert result['failed_stage'] == 'nat_bypass'
    assert any(item['stage'] == 'nat_bypass' and item['status'] == 'failed' for item in result['log'])
    assert agent.calls[-1] == ('remove', False)
    assert session.writes == []


def test_active_exact_route_with_disabled_backup_proves_lan_egress_without_mutation():
    backup = {**ACTIVE_PROXY_ROUTE, '.id': '*backup', 'gateway': '192.168.0.9',
              'immediate-gw': '192.168.0.9%wan', 'disabled': 'true', 'comment': 'manual-backup'}
    session = nat_session(routes=[ACTIVE_PROXY_ROUTE, backup])
    driver, _, report, plan = inspect(session)
    assert report['routes']['items'][0]['status'] == 'synced'
    assert nat_item(report)['status'] == 'not_required'
    assert 'lan4' in nat_item(report)['detail']
    assert plan['blockers'] == []
    result = execute(plan, driver, FakeAgent(SAFE_RETURN_STATUS))
    assert result['ok'], result
    assert session.rows[ROUTES][1] == backup and session.writes == []


def test_correct_specific_routes_share_exact_route_lan_egress():
    specific = {**ACTIVE_PROXY_ROUTE, '.id': '*specific', 'dst-address': '198.18.1.0/24',
                'comment': 'manual-specific'}
    session = nat_session(routes=[ACTIVE_PROXY_ROUTE, specific])
    driver, _, report, plan = inspect(session)
    assert report['routes']['items'][0]['status'] == 'synced'
    assert nat_item(report)['status'] == 'not_required'
    result = execute(plan, driver, FakeAgent(SAFE_RETURN_STATUS))
    assert result['ok'], result
    assert session.rows[ROUTES][1] == specific and session.writes == []


@pytest.mark.parametrize('uncertain', [None, 'different-interface', 'ecmp', 'invalid', 'inactive'])
def test_shadowed_wrong_aggregate_proves_nat_only_from_complete_valid_uniform_subnets(uncertain):
    wrong = {**ACTIVE_PROXY_ROUTE, 'gateway': '192.168.0.9', 'immediate-gw': '192.168.0.9%wan',
             'comment': 'manual-wrong'}
    left = {**ACTIVE_PROXY_ROUTE, '.id': '*left', 'dst-address': '198.18.0.0/17', 'comment': 'cf-left'}
    right = {**ACTIVE_PROXY_ROUTE, '.id': '*right', 'dst-address': '198.18.128.0/17', 'comment': 'cf-right'}
    if uncertain == 'different-interface':
        right['immediate-gw'] = f'{GATEWAY}%wan'
    elif uncertain:
        right[uncertain] = 'true'
    session = nat_session(routes=[wrong, left, right])
    driver, _, report, plan = inspect(session)
    assert nat_item(report)['status'] == ('unknown' if uncertain else 'not_required')
    if uncertain in {'invalid', 'inactive'}:
        assert report['routes']['items'][0]['status'] == 'conflict'
        assert plan['checks']['routes'] == []
    else:
        assert bool(plan['blockers']) == bool(uncertain)
    if uncertain is None:
        assert report['routes']['items'][0]['status'] == 'synced'
        assert report['routes']['extra'] == []
        result = execute(plan, driver, FakeAgent(SAFE_RETURN_STATUS))
        assert result['ok'], result
    assert session.rows[ROUTES][0] == wrong and session.writes == []


def test_absent_aggregate_wrong_specific_cannot_use_connected_gateway_as_nat_proof():
    connected = {'.id': '*connected', 'dst-address': '192.168.0.0/24', 'gateway': 'lan4',
                 'immediate-gw': 'lan4', 'active': 'true', 'connect': 'true'}
    specific = {**ACTIVE_PROXY_ROUTE, '.id': '*specific', 'dst-address': '198.18.1.0/24',
                'gateway': '192.168.0.9', 'immediate-gw': '192.168.0.9%wan',
                'comment': 'manual-specific'}
    session = nat_session(routes=[connected, specific])
    _, _, report, plan = inspect(session)
    assert report['routes']['items'][0]['status'] == 'conflict'
    assert nat_item(report)['status'] == 'unknown'
    assert next(stage['ops'] for stage in plan['stages'] if stage['key'] == 'routes') == []
    assert session.writes == []
