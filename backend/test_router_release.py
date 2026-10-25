"""Router releases: writable drivers, release plans, staged execution with rollback, API."""
import copy

from backend.routers.base import retire_policy_routes
from backend.routers.ikuai import IKuaiDriver, decode_value, encode_value
from backend.routers.plan import build_plan
from backend.routers.release import execute
from backend.routers.report import build_report
from backend.routers.routeros import RouterOSDriver
from backend.test_agent_upgrade_auth import authenticated_app  # noqa: F401  (pytest fixture)
from backend.test_routers import FakeResponse

GATEWAY = '10.0.0.4'


class StatefulIKuai:
    """Minimal iKuai 4.x: show/add/edit/del on static_rt, nat_rule, stream_ipport and route_object
    (IP groups), strings stored encoded."""

    def __init__(self, routes, nats, fail=None, policies=None, stale=None, groups=None):
        self.rows = {'static_rt': copy.deepcopy(routes), 'nat_rule': copy.deepcopy(nats),
                     'route_object': copy.deepcopy(groups or []),
                     'stream_ipport': copy.deepcopy(policies or [])}
        # 当前路由表 mirrors enabled static routes, plus kernel leftovers no config explains.
        self.stale = copy.deepcopy(stale or [])
        self.writes = []
        self.fail = fail
        self.next_id = 100
        self.trust_env = True
        self.verify = True

    def close(self):
        pass

    def post(self, url, json=None, **_):
        if url.endswith('/Action/login'):
            return FakeResponse({'Result': 10000, 'code': 0})
        func, action, param = json['func_name'], json['action'], json['param']
        if func == 'static_rt_table':
            active = [{'id': row['id'], 'dst_addr': row['dst_addr'], 'netmask': row['netmask'],
                       'gateway': row['gateway'], 'interface': 'lan1', 'prio': row.get('prio', 1)}
                      for row in self.rows['static_rt'] if row.get('enabled', 'yes') == 'yes'] + self.stale
            return FakeResponse({'code': 0, 'results': {'total': len(active), 'data': active}})
        rows = self.rows[func]
        if action == 'show':
            return FakeResponse({'code': 0, 'results': {'total': len(rows), 'data': copy.deepcopy(rows)}})
        self.writes.append((func, action, copy.deepcopy(param)))
        if self.fail == (func, action):
            return FakeResponse({'code': 2, 'message': '参数错误'})
        # Real iKuai 4.x: name is required, 1-15 characters (web UI form rule).
        name = 'group_name' if func == 'route_object' else 'tagname'
        if action in ('add', 'edit') and not 1 <= len(str(param.get(name) or '')) <= 15:
            return FakeResponse({'code': 2, 'message': '请求参数不合法',
                                 'details': [{'field': name, 'msg': '名称长度为1-15'}]})
        if func == 'route_object' and action in ('add', 'edit') and any(
                row['group_name'] == param['group_name'] and row['id'] != param.get('id') for row in rows):
            return FakeResponse({'code': 3001, 'message': '请求参数不合法',
                                 'details': [{'field': 'group_name', 'msg': '名称已存在，请重新输入'}]})
        if func == 'route_object' and action == 'add':
            # IP groups (4.0.314): referenced by group_id IPGP<n>, never renumbered.
            rows.append({**param, 'id': self.next_id, 'group_id': f'IPGP{self.next_id}'})
            self.next_id += 1
        elif action == 'add':
            rows.append({'enabled': 'yes', **param, 'id': self.next_id})
            self.next_id += 1
        elif action == 'edit':
            row = next(row for row in rows if row['id'] == param['id'])
            row.update(param)
        elif action == 'del':
            rows[:] = [row for row in rows if row['id'] != param['id']]
        return FakeResponse({'code': 0, 'results': {}})


HOME_ROUTES = [
    {'id': 1, 'dst_addr': '198.18.0.0', 'netmask': '255.255.0.0', 'gateway': GATEWAY, 'enabled': 'yes',
     'comment': 'fake%20ip', 'tagname': 'RT_1', 'ip_type': '4', 'interface': 'auto', 'prio': 1},
    {'id': 2, 'dst_addr': '91.108.4.0', 'netmask': '255.255.252.0', 'gateway': GATEWAY, 'enabled': 'yes',
     'comment': 'tg', 'tagname': 'RT_2', 'ip_type': '4', 'interface': 'auto', 'prio': 1},
    {'id': 3, 'dst_addr': '1.1.1.1', 'netmask': '255.255.255.255', 'gateway': GATEWAY, 'enabled': 'yes',
     'comment': 'dns', 'tagname': 'RT_3', 'ip_type': '4', 'interface': 'auto', 'prio': 1},
]
HOME_NATS = [
    {'id': 1, 'action': 'filter', 'enabled': 'yes', 'tagname': 'NAT_1', 'comment': 'mihomo', 'protocol': 'any',
     'iinterface': 'any', 'ointerface': 'any', 'nat_addr': '', 'nat_port': '', 'src_addr_inv': 0, 'dst_addr_inv': 0,
     'src_addr': {'custom': ['10.0.0.1-10.0.0.3', '10.0.0.5-10.0.0.255'], 'object': {}},
     'dst_addr': {'custom': ['198.18.0.0/16', '91.108.4.0/22', '185.76.151.0/24'], 'object': {}},
     'src_port': {'custom': {}, 'object': {}}, 'dst_port': {'custom': {}, 'object': {}}},
]
DESIRED = {'fake_ip_range': '198.18.0.0/16', 'proxy_gateway': GATEWAY, 'warnings': [],
           'proxy_cidrs': [{'cidr': '91.108.0.0/16', 'sources': ['Telegram']},
                           {'cidr': '149.154.160.0/20', 'sources': ['Telegram']},
                           {'cidr': '198.18.0.0/16', 'sources': ['fake-ip']}]}
SAFE_RETURN_ALLOCATION = {'version': 1, 'table': 104, 'priority': 104, 'mark': '0x400000', 'reused': False}
SAFE_RETURN_STATUS = {'status': 'ok', 'managed': None, 'return_tables': [], 'fwmark_rules': [],
                      'safety': {'version': 1, 'ok': True, 'blockers': [], 'allocation': SAFE_RETURN_ALLOCATION}}
ROUTER = {'id': 'r1', 'type': 'ikuai', 'endpoint': 'http://10.0.0.1', 'manage_return_routing': True}


class FakeAgent:
    def __init__(self, status, fail_apply=False):
        self.state = copy.deepcopy(status)
        self.calls = []
        self.fail_apply = fail_apply

    def apply(self, spec):
        self.calls.append(('apply', spec))
        if self.fail_apply:
            raise RuntimeError('缺少 nft（nftables），无法管理回流规则')
        assert spec['safety_version'] == 1 and not spec.get('adopt_legacy')
        self.state['return_tables'] = [table for table in self.state['return_tables']
                                       if table['name'] != 'configflow_return']
        self.state['return_tables'].append({'family': 'inet', 'name': 'configflow_return', 'marks': [spec['mark']]})
        self.state['fwmark_rules'] = [rule for rule in self.state['fwmark_rules']
                                      if str(rule['priority']) != str(spec['priority'])]
        self.state['fwmark_rules'].append({'priority': str(spec['priority']),
                                          'mark': spec['mark'] + '/' + spec['mark'], 'table': str(spec['table'])})
        self.state['managed'] = {'spec': copy.deepcopy(spec), 'table_present': True}
        self.state['safety']['allocation'] = {
            'version': 1, 'reused': True, **{key: spec[key] for key in ('table', 'priority', 'mark')}}
        return {'success': True}

    def remove(self, restore_legacy):
        self.calls.append(('remove', restore_legacy))
        assert restore_legacy is False
        spec = self.state['managed']['spec']
        self.state['managed'] = None
        self.state['return_tables'] = [table for table in self.state['return_tables']
                                       if table['name'] != 'configflow_return']
        self.state['fwmark_rules'] = [rule for rule in self.state['fwmark_rules']
                                      if rule != {'priority': str(spec['priority']),
                                                  'mark': spec['mark'] + '/' + spec['mark'],
                                                  'table': str(spec['table'])}]
        return {'success': True}

    def status(self):
        return copy.deepcopy(self.state)


def _driver(session):
    driver = IKuaiDriver('http://10.0.0.1', 'admin', 'pw')
    driver.session = session
    return driver


def _plan(driver, status=SAFE_RETURN_STATUS, desired=DESIRED):
    state = driver.read_state()
    return build_plan(ROUTER, desired, state, build_report(desired, state), status, driver)


def test_encode_matches_ikuai_ui():
    encoded = encode_value({'comment': ' cf:Unraid SS\nnext ', 'gateway': '10.0.0.4', 'prio': 1,
                            'dst_addr': {'custom': ['a b'], 'object': []}, 'x': '100% "a";$b'})
    assert encoded == {'comment': 'cf:Unraid%20SS%0Anext', 'gateway': '10.0.0.4', 'prio': 1,
                       'dst_addr': {'custom': ['a%20b'], 'object': []}, 'x': '100%25%20%22a%22%3B%24b'}
    assert decode_value(encoded)['x'] == '100% "a";$b'
    assert decode_value({'comment': 'Unraid%20SS%0Anext'})['comment'] == 'Unraid SS\nnext'


def test_plan_for_home_setup_adopts_and_fills_gaps():
    plan = _plan(_driver(StatefulIKuai(HOME_ROUTES, HOME_NATS)))
    assert plan['blockers'] == [] and not plan['empty']
    stages = {stage['key']: stage['ops'] for stage in plan['stages']}

    [agent_op] = stages['return_routing']
    # NAT_1 also exempts 185.76.151.0/24, so it must keep getting return marks.
    assert agent_op['spec'] == {'router_ip': '10.0.0.1', 'mark': '0x400000', 'table': 104, 'priority': 104,
                                'proxy_dst': ['91.108.0.0/16', '149.154.160.0/20', '185.76.151.0/24', '198.18.0.0/16'],
                                'safety_version': 1}
    assert agent_op['previous'] is None

    # Static routes match destinations only and would bounce the gateway's own
    # traffic back to it: rules excluding the gateway first, then the routes go.
    # Destinations go into ConfigFlow's IP groups first; the rules reference them.
    routes = [(op['kind'], op.get('dst') or op.get('cidr') or op.get('values'), op.get('comment'))
              for op in stages['routes']]
    assert routes == [('ikuai_group_add', ['91.108.0.0/16', '149.154.160.0/20', '198.18.0.0/16'], 'cf-mihomo'),
                      ('ikuai_group_add', ['1.1.1.1/32'], 'cf-mihomo-manual'),
                      ('policy_route_add', ['91.108.0.0/16', '149.154.160.0/20', '198.18.0.0/16'], 'cf-mihomo'),
                      ('policy_route_add', ['1.1.1.1/32'], 'cf-mihomo-manual'),
                      ('route_delete', '198.18.0.0/16', None),
                      ('route_delete', '91.108.4.0/22', None),   # inside 91.108.0.0/16: no rule of its own
                      ('route_delete', '1.1.1.1/32', None)]
    # An emptied 静态路由 page looks like data loss: lead with where the routes went.
    assert plan['warnings'][0].startswith(f'将删除 3 条指向 {GATEWAY} 的静态路由，网段迁移到 iKuai「端口分流」')
    assert plan['warnings'][0].endswith('198.18.0.0/16（fake ip）、91.108.4.0/22（tg）、1.1.1.1/32（dns）')
    assert plan['checks']['routes'] == []
    assert plan['checks']['policy_routes'] == [
        {'comment': 'cf-mihomo', 'gateway': GATEWAY, 'dst': ['91.108.0.0/16', '149.154.160.0/20', '198.18.0.0/16']},
        {'comment': 'cf-mihomo-manual', 'gateway': GATEWAY, 'dst': ['1.1.1.1/32']}]
    assert [op['groups'] for op in stages['routes'] if op['kind'] == 'policy_route_add'] == [['cf_pa_1'], ['cf_pm_1']]
    group, nat = stages['nat_bypass']
    assert (group['kind'], group['name'], group['values']) == (
        'ikuai_group_add', 'cf_nat_1',
        ['198.18.0.0/16', '91.108.4.0/22', '185.76.151.0/24', '91.108.0.0/16', '149.154.160.0/20'])
    assert (nat['kind'], nat['id'], nat['comment'], nat['add_dst'], nat['groups']) == \
        ('nat_update', '1', 'cf-mihomo', ['91.108.0.0/16', '149.154.160.0/20'], ['cf_nat_1'])
    assert len(plan['fingerprint']) == 16


def test_plan_skips_conflicts_and_blocks_without_agent():
    routes = HOME_ROUTES + [{'id': 9, 'dst_addr': '149.154.160.0', 'netmask': '255.255.240.0', 'gateway': '10.0.0.9',
                             'enabled': 'yes', 'comment': 'other'}]
    plan = _plan(_driver(StatefulIKuai(routes, HOME_NATS)))
    cidrs = [cidr for stage in plan['stages'] for op in stage['ops'] for cidr in op.get('dst') or [op.get('cidr')]]
    assert '149.154.160.0/20' not in cidrs
    assert '9' not in [op.get('id') for op in plan['stages'][1]['ops']]       # other gateways are left alone
    assert '149.154.160.0/20' not in plan['stages'][0]['ops'][0]['spec']['proxy_dst']
    assert any('149.154.160.0/20' in warning and '跳过' in warning for warning in plan['warnings'])

    blocked = _plan(_driver(StatefulIKuai(HOME_ROUTES, HOME_NATS)),
                    status={'status': 'offline', 'message': 'Agent 离线'})
    assert blocked['empty'] and 'Agent 离线' in blocked['blockers'][0]


def test_execute_applies_in_order_and_verifies():
    session = StatefulIKuai(HOME_ROUTES, HOME_NATS)
    driver = _driver(session)
    agent = FakeAgent(SAFE_RETURN_STATUS)
    plan = _plan(driver)
    result = execute(plan, driver, agent)
    assert result['ok'], result
    assert [entry['status'] for entry in result['log'] if entry['status'] == 'verified'] == ['verified'] * 3
    assert agent.calls[0][0] == 'apply'

    add = next(param for func, action, param in session.writes if (func, action) == ('stream_ipport', 'add'))
    # The 4.0.308 web UI form: every source except the gateway, next hop the gateway.
    assert add == {'enabled': 'yes', 'tagname': add['tagname'], 'interface': '', 'iface_band': 0,
                   'src_addr': {'custom': [GATEWAY], 'object': []}, 'src_addr_inv': 1,
                   'dst_addr': {'custom': [], 'object': [{'type': 0, 'gid': _gid(session, 'cf_pa_1'),
                                                          'gp_name': 'cf_pa_1'}]},
                   'dst_addr_inv': 0, 'type': 1, 'nexthop': GATEWAY, 'protocol': 'any', 'comment': 'cf-mihomo',
                   'time': {'custom': [{'type': 'weekly', 'weekdays': '1234567', 'start_time': '00:00',
                                        'end_time': '23:59'}], 'object': []},
                   'prio': 0, 'mode': 0, 'area_code': '', 'dst_type': 0,
                   'src_port': {'custom': [], 'object': []}, 'dst_port': {'custom': [], 'object': []}}
    assert add['tagname'].startswith('cf_pr_') and len(add['tagname']) <= 15
    assert session.rows['static_rt'] == []                  # every loop-prone route is gone
    assert {row['comment'] for row in session.rows['stream_ipport']} == {'cf-mihomo', 'cf-mihomo-manual'}
    assert _group(session, 'cf_pa_1') == ['91.108.0.0/16', '149.154.160.0/20', '198.18.0.0/16']
    nat = next(param for func, action, param in session.writes if func == 'nat_rule')
    assert nat['dst_addr'] == {'custom': [], 'object': [{'type': 0, 'gid': _gid(session, 'cf_nat_1'),
                                                         'gp_name': 'cf_nat_1'}]}
    assert _group(session, 'cf_nat_1') == ['198.18.0.0/16', '91.108.4.0/22', '185.76.151.0/24',
                                           '91.108.0.0/16', '149.154.160.0/20']
    assert nat['src_port'] == {'custom': [], 'object': []} and nat['comment'] == 'cf-mihomo'
    assert nat['src_addr']['custom'] == ['10.0.0.1-10.0.0.3', '10.0.0.5-10.0.0.255']

    # A second preview is empty: everything is managed and in place.
    again = _plan(driver, status={**agent.status(), 'status': 'ok'})
    assert again['empty'], again['stages']


def test_execute_failure_rolls_back_in_reverse():
    session = StatefulIKuai(HOME_ROUTES, HOME_NATS, fail=('nat_rule', 'edit'))
    driver = _driver(session)
    agent = FakeAgent(SAFE_RETURN_STATUS)
    result = execute(_plan(driver), driver, agent)
    assert not result['ok'] and result['failed_stage'] == 'nat_bypass'
    assert all(entry['status'] == 'ok' for entry in result['rollback'])
    # The NAT group goes first, then the routes stage (2 groups, 2 rules, 3 routes) and the Agent.
    assert [entry['stage'] for entry in result['rollback']] == ['nat_bypass'] + ['routes'] * 7 + ['return_routing']
    assert agent.calls[-1] == ('remove', False)          # remove only this release's owned rules
    routes = {(row['dst_addr'], row['comment']) for row in session.rows['static_rt']}
    assert routes == {(row['dst_addr'], row['comment']) for row in HOME_ROUTES}
    assert session.rows['stream_ipport'] == [] and session.rows['route_object'] == []
    assert session.rows['nat_rule'] == HOME_NATS


def test_execute_stops_before_router_when_agent_fails():
    session = StatefulIKuai(HOME_ROUTES, HOME_NATS)
    driver = _driver(session)
    result = execute(_plan(driver), driver, FakeAgent(SAFE_RETURN_STATUS, fail_apply=True))
    assert not result['ok'] and result['failed_stage'] == 'return_routing'
    assert session.writes == [] and result['rollback'] == []


def _policy(rule_id, comment, dst, **changes):
    row = {'id': rule_id, 'enabled': 'yes', 'tagname': f'cf_pr_{rule_id}', 'comment': comment, 'type': 1,
           'nexthop': GATEWAY, 'protocol': 'any', 'interface': '', 'mode': 0, 'prio': 0, 'iface_band': 0,
           'area_code': '', 'dst_type': 0, 'src_addr_inv': 1, 'dst_addr_inv': 0,
           'src_addr': {'custom': [GATEWAY], 'object': {}}, 'dst_addr': {'custom': list(dst), 'object': {}},
           'src_port': {'custom': {}, 'object': {}}, 'dst_port': {'custom': {}, 'object': {}},
           'time': {'custom': [{'type': 'weekly', 'weekdays': '1234567', 'start_time': '00:00', 'end_time': '23:59'}],
                    'object': {}}}
    row.update(changes)
    return row


MIGRATED = [_policy(1, 'cf-mihomo', ['91.108.0.0/16', '149.154.160.0/20', '198.18.0.0/16']),
            _policy(2, 'cf-mihomo-manual', ['1.1.1.1/32'])]


def _ip_group(row_id, name, comment, values):
    return {'id': row_id, 'group_id': f'IPGP{row_id}', 'group_name': name, 'type': 0, 'comment': comment,
            'group_value': [{'ip': value, 'comment': ''} for value in values]}


def _ref(row_id, name):
    return {'type': 0, 'gid': f'IPGP{row_id}', 'gp_name': name}


def _grouped(rule_id, comment, refs, **changes):
    """A 端口分流 rule as ConfigFlow writes it: no literal destinations, only group references."""
    return _policy(rule_id, comment, [], dst_addr={'custom': {}, 'object': list(refs)}, **changes)


# The same rules as MIGRATED, as written since destinations live in IP groups.
GROUPS = [_ip_group(31, 'cf_pa_1', 'cf-mihomo', ['91.108.0.0/16', '149.154.160.0/20', '198.18.0.0/16']),
          _ip_group(32, 'cf_pm_1', 'cf-mihomo-manual', ['1.1.1.1/32'])]
GROUPED = [_grouped(1, 'cf-mihomo', [_ref(31, 'cf_pa_1')]), _grouped(2, 'cf-mihomo-manual', [_ref(32, 'cf_pm_1')])]


def _group(session, name):
    [row] = [row for row in session.rows['route_object'] if row['group_name'] == name]
    return [decode_value(item['ip']) for item in row['group_value']]


def _gid(session, name):
    [row] = [row for row in session.rows['route_object'] if row['group_name'] == name]
    return row['group_id']


def _expanded(session, rule):
    """A rule's destinations as iKuai forwards them: literal ones and its groups' entries."""
    by_gid = {row['group_id']: [decode_value(item['ip']) for item in row['group_value']]
              for row in session.rows['route_object']}
    dst = rule['dst_addr']
    return list(dst['custom'] or []) + [value for ref in dst['object'] or [] for value in by_gid.get(ref['gid'], [])]


def test_migrated_policy_routes_are_in_sync():
    session = StatefulIKuai([], HOME_NATS, policies=GROUPED, groups=GROUPS)
    driver = _driver(session)
    state = driver.read_state()
    report = build_report(DESIRED, state)
    assert {item['status'] for item in report['routes']['items']} == {'synced'}
    assert report['routes']['extra'] == [] and report['routes']['mode'] == 'policy'
    plan = _plan(driver)
    assert plan['stages'][1]['ops'] == []


def test_policy_routes_drop_stale_cidrs_but_keep_manual_ones():
    groups = [_ip_group(31, 'cf_pa_1', 'cf-mihomo',
                        ['91.108.0.0/16', '149.154.160.0/20', '198.18.0.0/16', '203.0.113.0/24']),
              _ip_group(32, 'cf_pm_1', 'cf-mihomo-manual', ['1.1.1.1/32', '8.8.8.8/32'])]
    routes = [{'id': 7, 'dst_addr': '9.9.9.9', 'netmask': '255.255.255.255', 'gateway': GATEWAY, 'enabled': 'yes',
               'comment': 'DNS', 'tagname': 'RT_7', 'ip_type': '4', 'interface': 'auto', 'prio': 1},
              {'id': 8, 'dst_addr': '9.9.9.10', 'netmask': '255.255.255.255', 'gateway': GATEWAY, 'enabled': 'no',
               'comment': 'off', 'tagname': 'RT_8', 'ip_type': '4', 'interface': 'auto', 'prio': 1}]
    session = StatefulIKuai(routes, HOME_NATS, policies=GROUPED, groups=groups)
    driver = _driver(session)
    plan = _plan(driver)
    ops = [(op['kind'], op.get('name') or op.get('cidr'), op.get('added'), op.get('removed'))
           for op in plan['stages'][1]['ops']]
    # The derived group loses 203.0.113.0/24; the add-only manual group gains 9.9.9.9;
    # the rules keep referencing the same groups and are not written at all.
    # A disabled static route carries no traffic and is left alone.
    assert ops == [('ikuai_group_update', 'cf_pa_1', [], ['203.0.113.0/24']),
                   ('ikuai_group_update', 'cf_pm_1', ['9.9.9.9/32'], []),
                   ('route_delete', '9.9.9.9/32', None, None)]
    agent = FakeAgent(SAFE_RETURN_STATUS)
    result = execute(plan, driver, agent)
    assert result['ok'], result
    # The routes stage (the NAT stage then adopts HOME_NATS into its own group).
    assert [(func, action) for func, action, _ in session.writes][:3] == [('route_object', 'edit')] * 2 + [
        ('static_rt', 'del')]
    assert not any(func == 'stream_ipport' for func, _, _ in session.writes)
    assert _group(session, 'cf_pa_1') == ['91.108.0.0/16', '149.154.160.0/20', '198.18.0.0/16']
    assert _group(session, 'cf_pm_1') == ['1.1.1.1/32', '8.8.8.8/32', '9.9.9.9/32']
    assert [row['id'] for row in session.rows['static_rt']] == [8]
    assert _plan(driver, status={**agent.status(), 'status': 'ok'})['empty']


def test_rule_edits_keep_the_whole_form():
    policies = [_policy(1, 'cf-mihomo', ['91.108.0.0/16'], prio=31)]
    session = StatefulIKuai([], HOME_NATS, policies=policies)
    driver = _driver(session)
    result = execute(_plan(driver), driver, FakeAgent(SAFE_RETURN_STATUS))
    assert result['ok'], result
    edit = next(param for func, action, param in session.writes if (func, action) == ('stream_ipport', 'edit'))
    assert edit['tagname'] == 'cf_pr_1' and edit['time']['custom'][0]['weekdays'] == '1234567'  # form kept


def _plan_without_return(driver, desired=DESIRED):
    state = driver.read_state()
    retire_policy_routes(state)
    return build_plan({**ROUTER, 'manage_return_routing': False}, desired, state, build_report(desired, state),
                      {'status': 'disabled'}, driver)


def _rows(session, func):
    return [decode_value(row) for row in session.rows[func]]


def test_without_return_routing_policy_routes_go_back_to_static_and_forth():
    # With NAT bypass, mihomo answers clients directly. iKuai 4.0.314 then stops
    # forwarding a 端口分流 connection (only its SYN reaches mihomo); static routes
    # keep working. So 端口分流 is used only together with managed return routing.
    session = StatefulIKuai([], HOME_NATS, policies=MIGRATED)
    driver = _driver(session)
    plan = _plan_without_return(driver)
    assert plan['blockers'] == []
    assert plan['warnings'][0].startswith('未开启自动配置回流：将把 iKuai 端口分流')
    ops = [(op['kind'], op.get('cidr') or op.get('id')) for op in plan['stages'][1]['ops']]
    # Static routes first: they take precedence, so nothing is unrouted in between.
    assert ops == [('route_add', '91.108.0.0/16'), ('route_add', '149.154.160.0/20'),
                   ('route_add', '198.18.0.0/16'), ('route_add', '1.1.1.1/32'),
                   ('policy_route_delete', '1'), ('policy_route_delete', '2')]
    result = execute(plan, driver, None)
    assert result['ok'], result
    assert [(row['dst_addr'], row['comment']) for row in _rows(session, 'static_rt')] == [
        ('91.108.0.0', 'cf-Telegram'), ('149.154.160.0', 'cf-Telegram'), ('198.18.0.0', 'cf-fake-ip'),
        ('1.1.1.1', 'mihomo-manual')]   # the hand-made CIDR stays hand-made
    assert session.rows['stream_ipport'] == []
    report = build_report(DESIRED, driver.read_state())
    assert report['routes']['retired_policy'] == [] and _plan_without_return(driver)['empty']

    # Return routing switched back on: the same CIDRs move to 端口分流 again, through IP groups.
    plan = _plan(driver)
    result = execute(plan, driver, FakeAgent(SAFE_RETURN_STATUS))
    assert result['ok'], result
    assert session.rows['static_rt'] == []
    assert [(row['comment'], row['dst_addr']['custom'], _expanded(session, row))
            for row in _rows(session, 'stream_ipport')] == [
        ('cf-mihomo', [], ['91.108.0.0/16', '149.154.160.0/20', '198.18.0.0/16']),
        ('cf-mihomo-manual', [], ['1.1.1.1/32'])]
    groups = sorted(row['group_name'] for row in session.rows['route_object'])
    assert groups == ['cf_nat_1', 'cf_pa_1', 'cf_pm_1']

    # Off again: the rules go, then their groups; the NAT group stays in use.
    plan = _plan_without_return(driver)
    assert [op['kind'] for op in plan['stages'][1]['ops']][-4:] == [
        'policy_route_delete', 'policy_route_delete', 'ikuai_group_delete', 'ikuai_group_delete']
    result = execute(plan, driver, None)
    assert result['ok'], result
    assert [row['group_name'] for row in session.rows['route_object']] == ['cf_nat_1']
    assert _plan_without_return(driver)['empty']


def test_retiring_policy_routes_keeps_existing_static_routes_and_blocks_on_conflicts():
    manual = {'id': 9, 'dst_addr': '1.1.1.1', 'netmask': '255.255.255.255', 'gateway': GATEWAY, 'enabled': 'yes',
              'comment': 'dns', 'tagname': 'RT_9', 'ip_type': '4', 'interface': 'auto', 'prio': 1}
    plan = _plan_without_return(_driver(StatefulIKuai([manual], HOME_NATS, policies=MIGRATED)))
    assert ('route_add', '1.1.1.1/32') not in [(op['kind'], op.get('cidr')) for op in plan['stages'][1]['ops']]
    assert '已有同网段静态路由、保持不变：1.1.1.1/32' in plan['warnings'][0]

    # Deleting cf-mihomo would leave a skipped (conflicting) CIDR unrouted.
    conflict = {**manual, 'dst_addr': '198.18.0.0', 'netmask': '255.255.0.0', 'gateway': '10.0.0.9'}
    plan = _plan_without_return(_driver(StatefulIKuai([conflict], HOME_NATS, policies=MIGRATED)))
    assert any(blocker.startswith('198.18.0.0/16 存在路由冲突，改回静态路由前') for blocker in plan['blockers'])


def test_disabled_static_route_does_not_conflict_with_policy_routes():
    disabled = {'id': 9, 'dst_addr': '198.18.0.0', 'netmask': '255.255.0.0', 'gateway': GATEWAY, 'enabled': 'no',
                'comment': 'fake ip', 'tagname': 'RT_9', 'ip_type': '4', 'interface': 'auto', 'prio': 1}
    driver = _driver(StatefulIKuai([disabled], HOME_NATS, policies=GROUPED, groups=GROUPS))
    report = build_report(DESIRED, driver.read_state())
    assert {item['status'] for item in report['routes']['items']} == {'synced'}
    plan = _plan(driver)
    assert plan['blockers'] == [] and plan['stages'][1]['ops'] == []   # left alone, not deleted


def test_policy_route_that_includes_the_gateway_is_repaired():
    policies = [_grouped(1, 'cf-mihomo', [_ref(31, 'cf_pa_1')], src_addr_inv=0)]
    driver = _driver(StatefulIKuai([], HOME_NATS, policies=policies, groups=GROUPS[:1]))
    [op] = _plan(driver)['stages'][1]['ops']
    assert op['kind'] == 'policy_route_update' and op['id'] == '1' and op['groups'] == ['cf_pa_1']


def test_policy_route_below_top_priority_is_raised():
    # A lower 端口分流 priority lets multi-WAN or protocol rules take proxy traffic first.
    policies = [_grouped(1, 'cf-mihomo', [_ref(31, 'cf_pa_1')], prio=31)]
    session = StatefulIKuai([], HOME_NATS, policies=policies, groups=GROUPS[:1])
    driver = _driver(session)
    [op] = _plan(driver)['stages'][1]['ops']
    assert op['kind'] == 'policy_route_update'
    assert execute(_plan(driver), driver, FakeAgent(SAFE_RETURN_STATUS))['ok']
    assert session.rows['stream_ipport'][0]['prio'] == 0


def test_manual_policy_route_is_repaired_without_new_migrations():
    policies = [GROUPED[0], _grouped(2, 'cf-mihomo-manual', [_ref(32, 'cf_pm_1')], prio=31)]
    session = StatefulIKuai([], HOME_NATS, policies=policies, groups=GROUPS)
    driver = _driver(session)
    [op] = _plan(driver)['stages'][1]['ops']
    assert (op['kind'], op['id'], op['dst'], op['groups']) == ('policy_route_update', '2', ['1.1.1.1/32'], ['cf_pm_1'])
    assert execute(_plan(driver), driver, FakeAgent(SAFE_RETURN_STATUS))['ok']
    assert [row['prio'] for row in session.rows['stream_ipport']] == [0, 0]
    assert _plan(driver)['stages'][1]['ops'] == []


def test_kernel_leftover_routes_are_reported_not_rolled_back():
    # iKuai can keep a kernel route after its static route is deleted; it beats
    # 端口分流 and keeps the loop alive, and no API call removes it.
    stale = [{'id': 9, 'dst_addr': '8.8.8.8', 'netmask': '255.255.255.255', 'gateway': GATEWAY,
              'interface': 'lan1', 'prio': 20}]
    session = StatefulIKuai(HOME_ROUTES, HOME_NATS, stale=stale)
    driver = _driver(session)
    plan = _plan(driver)
    assert any('8.8.8.8/32' in warning and '重启' in warning for warning in plan['warnings'])
    result = execute(plan, driver, FakeAgent(SAFE_RETURN_STATUS))
    assert result['ok'] and result['rollback'] == []
    [warning] = [entry for entry in result['log'] if entry['status'] == 'warning']
    assert warning['stage'] == 'routes' and '8.8.8.8/32' in warning['summary']
    assert session.rows['static_rt'] == []           # the release itself still completes


def test_routes_without_leftovers_need_no_warning():
    session = StatefulIKuai(HOME_ROUTES, HOME_NATS)
    driver = _driver(session)
    plan = _plan(driver)
    assert not any('残留' in warning for warning in plan['warnings'])
    result = execute(plan, driver, FakeAgent(SAFE_RETURN_STATUS))
    assert result['ok'] and not [entry for entry in result['log'] if entry['status'] == 'warning']


def test_policy_route_verification_rejects_a_router_that_ignores_the_exclusion():
    session = StatefulIKuai(HOME_ROUTES, HOME_NATS)
    driver = _driver(session)
    plan = _plan(driver)
    original = session.post

    def post(url, json=None, **kwargs):
        if json and json.get('func_name') == 'stream_ipport' and json.get('action') == 'add':
            json = copy.deepcopy(json)
            json['param']['src_addr_inv'] = 0
        return original(url, json=json, **kwargs)

    session.post = post
    result = execute(plan, driver, FakeAgent(SAFE_RETURN_STATUS))
    assert not result['ok'] and result['failed_stage'] == 'routes'
    assert '策略路由' in result['log'][-1]['summary']
    routes = {(row['dst_addr'], row['comment']) for row in session.rows['static_rt']}
    assert routes == {(row['dst_addr'], row['comment']) for row in HOME_ROUTES}
    assert session.rows['stream_ipport'] == []


class FakeROS(RouterOSDriver):
    def __init__(self):
        pass

    def read_state(self):
        from backend.routers.base import RouterState
        return RouterState()

    def close(self):
        pass


def test_routeros_router_stages_are_automatic():
    driver = FakeROS()
    state = driver.read_state()
    plan = build_plan({**ROUTER, 'type': 'routeros', 'endpoint': 'https://10.0.0.1'}, DESIRED, state,
                      build_report(DESIRED, state), SAFE_RETURN_STATUS, driver)
    manual = {stage['key']: stage['manual'] for stage in plan['stages']}
    assert manual == {'return_routing': False, 'routes': False, 'nat_bypass': False, 'dhcp_dns': False}
    assert plan['script'] == ''
    assert all(stage['ops'] for stage in plan['stages'] if stage.get('enabled') is not False)


def test_router_plan_and_apply_api(authenticated_app, monkeypatch):  # noqa: F811
    client, _, agents, admin, _ = authenticated_app
    router_id = client.post('/api/routers', headers=admin, json={
        'name': '主路由', 'type': 'ikuai', 'endpoint': 'http://10.0.0.1', 'username': 'admin',
        'password': 'pw', 'proxy_gateway_agent_id': agents[0]['id'],
        'manage_return_routing': True}).get_json()['router']['id']

    from backend.routes import routers as routes_module
    session = StatefulIKuai(HOME_ROUTES, HOME_NATS)
    agent = FakeAgent(SAFE_RETURN_STATUS)
    monkeypatch.setitem(routes_module.DRIVERS, 'ikuai', lambda *args, **kwargs: _driver(session))
    monkeypatch.setattr(routes_module, 'compute_desired', lambda *args, **kwargs: DESIRED)
    monkeypatch.setattr(routes_module, '_return_routing', lambda record: {**agent.status(), 'status': 'ok'})
    monkeypatch.setattr(routes_module, 'AgentReturnRouting', lambda manager, record: agent)

    preview = client.post(f'/api/routers/{router_id}/plan', headers=admin)
    assert preview.status_code == 200, preview.get_json()
    plan = preview.get_json()['plan']
    assert session.writes == [] and agent.calls == []       # preview never writes

    stale = client.post(f'/api/routers/{router_id}/apply', headers=admin, json={'fingerprint': 'deadbeef'})
    assert stale.status_code == 409 and session.writes == []

    applied = client.post(f'/api/routers/{router_id}/apply', headers=admin, json={'fingerprint': plan['fingerprint']})
    assert applied.status_code == 200, applied.get_json()
    assert applied.get_json()['result']['ok'] is True
    router = client.get('/api/routers', headers=admin).get_json()['routers'][0]
    assert router['last_release']['ok'] is True and router['last_release']['fingerprint'] == plan['fingerprint']
    assert client.post('/api/routers/missing/apply', headers=admin, json={}).status_code == 404


def test_ikuai_error_details_surface_in_message():
    session = StatefulIKuai(HOME_ROUTES, HOME_NATS)
    driver = _driver(session)
    import pytest
    from backend.routers.base import RouterError
    with pytest.raises(RouterError, match='tagname: 名称长度为1-15'):
        driver._write('static_rt', 'add', {'tagname': '', 'dst_addr': '192.0.2.0'})


CLEANUP_SPEC = {'safety_version': 1, 'router_ip': '10.0.0.1', 'proxy_dst': ['198.18.0.0/16'], 'mark': '0x400000',
                'table': 104, 'priority': 104, 'lan_interface': 'eth0', 'tun_device': 'Meta'}


def _cleanup(**changes):
    return {'status': 'disabled', 'message': '未启用自动配置回流',
            'cleanup': {'spec': CLEANUP_SPEC, 'complete': True, 'shared_with': [],
                        'safety': copy.deepcopy(SAFE_RETURN_STATUS['safety']), **changes}}


def _plan_off(driver, return_routing):
    state = driver.read_state()
    retire_policy_routes(state)
    return build_plan({**ROUTER, 'manage_return_routing': False}, DESIRED, state, build_report(DESIRED, state),
                      return_routing, driver)


def test_switching_return_routing_off_removes_agent_rules_last():
    plan = _plan_off(_driver(StatefulIKuai([], HOME_NATS, policies=MIGRATED)), _cleanup())
    assert [stage['key'] for stage in plan['stages']][-1] == 'return_cleanup'
    [op] = plan['stages'][-1]['ops']
    assert op['kind'] == 'return_routing_remove' and op['previous'] == CLEANUP_SPEC
    assert '已有规则在最后一个阶段移除' in plan['stages'][0]['note']
    # Turned on, the switch keeps managing: nothing is removed.
    on = _plan(_driver(StatefulIKuai([], HOME_NATS, policies=MIGRATED)))
    assert 'return_cleanup' not in [stage['key'] for stage in on['stages']]


def test_return_cleanup_is_skipped_with_a_reason_when_unsafe_or_shared():
    cases = {'仍被「旁路由 B」使用': _cleanup(shared_with=['旁路由 B']),
             '可能有未完成的事务': _cleanup(complete=False),
             '上次回流事务未完成': _cleanup(safety={**SAFE_RETURN_STATUS['safety'], 'ok': False,
                                                  'blockers': ['上次回流事务未完成，不能重新分配资源']})}
    for reason, return_routing in cases.items():
        plan = _plan_off(_driver(StatefulIKuai([], HOME_NATS, policies=MIGRATED)), return_routing)
        assert 'return_cleanup' not in [stage['key'] for stage in plan['stages']], reason
        assert any(reason in warning for warning in plan['warnings']), (reason, plan['warnings'])
        assert '已有回流规则保留' in plan['stages'][0]['note']


def test_return_cleanup_runs_after_static_routes_and_rolls_back_with_them():
    session = StatefulIKuai([], HOME_NATS, policies=MIGRATED)
    driver = _driver(session)
    agent = FakeAgent(SAFE_RETURN_STATUS)
    agent.apply(CLEANUP_SPEC)
    agent.calls.clear()
    plan = _plan_off(driver, _cleanup())
    result = execute(plan, driver, agent)
    assert result['ok'], result
    assert agent.calls == [('remove', False)]
    assert session.rows['stream_ipport'] == [] and len(session.rows['static_rt']) == 4
    assert agent.state['managed'] is None

    # The Agent keeps reporting its rules: readback fails and everything goes back.
    session = StatefulIKuai([], HOME_NATS, policies=MIGRATED)
    driver = _driver(session)
    agent = FakeAgent(SAFE_RETURN_STATUS)
    agent.apply(CLEANUP_SPEC)
    agent.calls.clear()
    stuck = agent.state['managed']
    agent.remove = lambda restore_legacy: agent.calls.append(('remove', restore_legacy)) or {'success': True}
    agent.state['managed'] = stuck
    result = execute(_plan_off(driver, _cleanup()), driver, agent)
    assert not result['ok'] and result['failed_stage'] == 'return_cleanup'
    assert [call[0] for call in agent.calls] == ['remove', 'apply']
    assert sorted(row['comment'] for row in _rows(session, 'stream_ipport')) == ['cf-mihomo', 'cf-mihomo-manual']
    assert session.rows['static_rt'] == []
