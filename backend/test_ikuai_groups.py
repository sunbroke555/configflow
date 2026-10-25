"""iKuai proxy destinations live in ConfigFlow's IP groups (route_object), never listed in the rules.

端口分流 cf-mihomo/cf-mihomo-manual and the NAT bypass rule reference groups of
at most 2000 entries, split so each group fits one request. Adding or removing
a CIDR edits a group, not the rule. Old rules listing CIDRs are migrated on a
manual release and restored exactly if it fails, without leaving groups behind.
"""
import copy

import pytest
import requests

from backend.routers.ikuai import MAX_REQUEST_BYTES, _addresses, decode_value, request_bytes
from backend.routers.ikuai_groups import (
    GROUP_MAX_BYTES, GROUP_MAX_ENTRIES, MAX_RULE_GROUPS, canon, digest, group_bytes, layout, payload,
)
from backend.routers.release import execute
from backend.routers.report import build_report
from backend.test_ikuai_scale import PagedIKuai, _cidrs, _route, _tagname
from backend.test_router_release import (
    FakeAgent, GATEWAY, SAFE_RETURN_STATUS, _driver, _grouped, _ip_group, _plan, _plan_without_return, _policy, _ref,
)
from backend.test_routers import FakeResponse

CIDRS = _cidrs(2500, '10.0.0.0/8', 24)
MANY = _cidrs(7000, '10.0.0.0/8', 24)


class Device(PagedIKuai):
    """Writes can be refused (nothing changes), lose their reply after changing, or never arrive."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.refuse, self.lose, self.drop = set(), set(), set()
        self.counts = {}

    def post(self, url, json=None, **kwargs):
        if url.endswith('/Action/login') or json['action'] == 'show':
            return super().post(url, json=json, **kwargs)
        key = (json['func_name'], json['action'])
        self.counts[key] = self.counts.get(key, 0) + 1
        point = (*key, self.counts[key])
        if point in self.drop:
            raise requests.ConnectionError('reset before the router saw it')
        if point in self.refuse:
            self.writes.append((*key, copy.deepcopy(json['param'])))
            return FakeResponse({'code': 2, 'message': '注入的失败'})
        response = super().post(url, json=json, **kwargs)
        if point in self.lose:
            raise requests.Timeout('response lost after the router committed it')
        return response


def _desired(cidrs):
    return {'fake_ip_range': '', 'proxy_gateway': GATEWAY, 'warnings': [],
            'proxy_cidrs': [{'cidr': cidr, 'sources': ['lab']} for cidr in cidrs]}


def _nat(rule_id, comment, dst, objects=()):
    return {'id': rule_id, 'action': 'filter', 'enabled': 'yes', 'tagname': f'NAT_{rule_id}', 'comment': comment,
            'protocol': 'any', 'iinterface': 'any', 'ointerface': 'any', 'nat_addr': '', 'nat_port': '',
            'src_addr_inv': 0, 'dst_addr_inv': 0,
            'src_addr': {'custom': ['10.0.0.1-10.0.0.3', '10.0.0.5-10.0.0.255'], 'object': {}},
            'dst_addr': {'custom': list(dst), 'object': list(objects)},
            'src_port': {'custom': {}, 'object': {}}, 'dst_port': {'custom': {}, 'object': {}}}


def _legacy(cidrs=CIDRS, routes=(), **kwargs):
    """The rules as releases before IP groups wrote them: every CIDR listed in the rule."""
    return Device(list(routes), [_nat(1, 'cf-mihomo', cidrs)],
                  policies=[_policy(1, 'cf-mihomo', cidrs), _policy(2, 'cf-mihomo-manual', ['1.1.1.1/32'])], **kwargs)


def _publish(session, cidrs, agent=None):
    driver = _driver(session)
    agent = agent or FakeAgent(SAFE_RETURN_STATUS)
    status = {**agent.status(), 'status': 'ok'}
    plan = _plan(driver, status=status, desired=_desired(cidrs))
    assert plan['blockers'] == [], plan['blockers']
    session.writes.clear()
    result = execute(plan, driver, agent)
    return plan, result, agent


def _ops(plan, stage):
    return next(item['ops'] for item in plan['stages'] if item['key'] == stage)


def _writes(session, func=None):
    return [(name, action) for name, action, _ in session.writes if func is None or name == func]


def _groups(session):
    return {row['group_name']: [decode_value(item['ip']) for item in row['group_value']]
            for row in session.rows['route_object']}


def _rule(session, func, comment):
    [row] = [row for row in session.rows[func] if decode_value(row['comment']) == comment]
    return row


def _resolved(session, row):
    """A rule's destinations as iKuai forwards them; None if a reference points nowhere."""
    by_gid = {group['group_id']: [decode_value(item['ip']) for item in group['group_value']]
              for group in session.rows['route_object']}
    dst = row['dst_addr']
    values = list(dst.get('custom') or [])
    for ref in dst.get('object') or []:
        if ref['gid'] not in by_gid:
            return None
        values += by_gid[ref['gid']]
    return values


def _address(value):
    value = value if isinstance(value, dict) else {}
    return (tuple(value.get('custom') or ()), tuple(sorted(ref['gp_name'] for ref in value.get('object') or ())))


def _device(session):
    """What forwards traffic, independent of row IDs (a rollback may re-create objects)."""
    rules = {func: sorted((decode_value(row.get('tagname')), decode_value(row.get('comment')), row.get('enabled'),
                           row.get('prio'), _address(row.get('src_addr')), _address(row.get('dst_addr')),
                           tuple(_resolved(session, row) or ()))
                          for row in session.rows[func]) for func in ('stream_ipport', 'nat_rule')}
    return {**rules, 'route_object': sorted((row['group_name'],
                                             tuple((item['ip'], item['comment']) for item in row['group_value']))
                                            for row in session.rows['route_object']),
            'static_rt': sorted((row['dst_addr'], row['netmask'], row['gateway'], row['comment'])
                                for row in session.rows['static_rt'])}


def _existing(role, sizes, start=0):
    groups, position = [], start
    for index, size in enumerate(sizes, 1):
        groups.append({'name': f'cf_{role}_{index}', 'index': index,
                       'values': MANY[position:position + size]})
        position += size
    return groups


# --- layout ----------------------------------------------------------------------------------

@pytest.mark.parametrize('count, groups', [(0, 0), (1, 1), (50, 1), (2000, 1), (2001, 2), (6000, 3), (20000, 10)])
def test_groups_hold_at_most_2000_entries_and_fit_one_request(count, groups):
    values = _cidrs(count, '10.0.0.0/8', 24) if count <= 2500 else _cidrs(count, '10.0.0.0/8', 28)
    result = layout('pa', [], values, set())
    assert [name for name, _ in result['final']] == [f'cf_pa_{index}' for index in range(1, groups + 1)]
    assert [value for _, entries in result['final'] for value in entries] == values
    for name, entries in result['final']:
        assert len(entries) <= GROUP_MAX_ENTRIES and group_bytes(name, 'pa', entries) <= GROUP_MAX_BYTES
    assert result == layout('pa', [], values, set())      # deterministic


def test_longest_cidrs_still_fill_a_group_and_ranges_split_by_size():
    longest = [f'255.255.{200 + index // 50}.{200 + index % 50}/32' for index in range(2000)]
    result = layout('nat', [], longest, set())
    assert [len(entries) for _, entries in result['final']] == [2000]
    assert group_bytes('cf_nat_9999', 'nat', longest) <= GROUP_MAX_BYTES
    ranges = [f'255.255.{200 + index // 50}.{200 + index % 50}-255.255.255.255' for index in range(2000)]
    result = layout('nat', [], ranges, set())
    sizes = [len(entries) for _, entries in result['final']]
    assert len(sizes) == 2 and sizes[0] < 2000 and sum(sizes) == 2000
    assert all(group_bytes(name, 'nat', entries) <= GROUP_MAX_BYTES for name, entries in result['final'])


def test_changes_write_only_the_groups_they_touch():
    existing = _existing('pa', [2000, 2000, 1999])
    want = MANY[:5999]
    unchanged = layout('pa', existing, want, set())
    assert (unchanged['adds'], unchanged['updates'], unchanged['deletes']) == ([], [], [])
    one_more = layout('pa', existing, want + ['192.0.2.0/24'], set())
    assert [name for name, _, _ in one_more['updates']] == ['cf_pa_3'] and one_more['adds'] == []
    full = _existing('pa', [2000, 2000, 2000])
    crossing = layout('pa', full, MANY[:6000] + ['192.0.2.0/24'], set())
    assert crossing['adds'] == [('cf_pa_4', ['192.0.2.0/24'])] and crossing['updates'] == []
    one_less = layout('pa', full, MANY[1:6000], set())
    assert [name for name, _, _ in one_less['updates']] == ['cf_pa_1']
    emptied = layout('pa', full, MANY[:4000], set())
    assert emptied['deletes'] == [('cf_pa_3', MANY[4000:6000])] and emptied['updates'] == []
    assert [name for name, _ in emptied['final']] == ['cf_pa_1', 'cf_pa_2']


def test_reading_entries_back_differently_needs_no_write():
    existing = [{'name': 'cf_pa_1', 'index': 1, 'values': ['1.1.1.1', '10.0.0.0/24']}]
    result = layout('pa', existing, ['10.0.0.0/24', '1.1.1.1/32'], set())
    assert (result['adds'], result['updates'], result['deletes']) == ([], [], [])
    assert canon('1.1.1.1') == canon('1.1.1.1/32') and digest(['1.1.1.1']) == digest(['1.1.1.1/32'])


def test_a_fragmented_family_is_repacked_downwards():
    existing = _existing('pa', [100] * 6)
    result = layout('pa', existing, CIDRS[:600], set())
    assert [name for name, _ in result['final']] == ['cf_pa_1']
    assert [name for name, _, _ in result['updates']] == ['cf_pa_1'] and result['adds'] == []
    assert [name for name, _ in result['deletes']] == [f'cf_pa_{index}' for index in range(2, 7)]
    # Moderate fragmentation is left alone: nothing moves.
    calm = layout('pa', _existing('pa', [100] * 3), CIDRS[:300], set())
    assert (calm['updates'], calm['deletes']) == ([], [])


def test_more_groups_than_one_rule_can_reference_block():
    values = _cidrs(MAX_RULE_GROUPS * GROUP_MAX_ENTRIES + 1, '10.0.0.0/8', 28)
    result = layout('pa', [], values, set())
    assert '超过单条规则可引用的 16 个' in result['error']


# --- first release, migration, ownership ---------------------------------------------------

def test_first_release_writes_groups_then_rules_that_only_reference_them():
    cidrs = _cidrs(6000, '10.0.0.0/8', 24)
    session = Device([], [], policies=[])
    driver = _driver(session)
    session.shows.clear()
    plan = _plan(driver, desired=_desired(cidrs))
    assert [func for func, _ in session.shows].count('route_object') == 1
    assert [op['kind'] for op in _ops(plan, 'routes')] == ['ikuai_group_add'] * 3 + ['policy_route_add']
    assert [op['kind'] for op in _ops(plan, 'nat_bypass')] == ['ikuai_group_add'] * 3 + ['nat_add']
    marks = []
    session.on_write = lambda func, action, param, current: marks.append((func, action, len(current.shows)))
    session.shows.clear()
    result = execute(plan, driver, FakeAgent(SAFE_RETURN_STATUS))
    assert result['ok'], result['log'][-3:]
    # The NAT stage starts with the 4th group add, after its own snapshot read.
    nat_start = [shows for func, action, shows in marks if (func, action) == ('route_object', 'add')][3]
    assert [func for func, _ in session.shows[:nat_start]].count('route_object') - 1 <= 4
    rule = _rule(session, 'stream_ipport', 'cf-mihomo')
    assert rule['dst_addr']['custom'] == [] and [ref['gp_name'] for ref in rule['dst_addr']['object']] == [
        'cf_pa_1', 'cf_pa_2', 'cf_pa_3']
    nat = _rule(session, 'nat_rule', 'cf-mihomo')
    assert nat['dst_addr']['custom'] == [] and nat['src_addr']['custom'] == ['10.0.0.1-10.0.0.3', '10.0.0.5-10.0.0.254']
    assert _resolved(session, rule) == cidrs and _resolved(session, nat) == cidrs
    assert max(request_bytes(func, action, param) for func, action, param in session.writes) <= MAX_REQUEST_BYTES


def test_ownership_marker_is_the_first_entry_comment_since_groups_keep_no_comment():
    # 4.0.314 accepts a group comment and drops it; a marker there made every group
    # look like the user's: the first release failed and its rollback kept the group.
    session = Device([], [], policies=[])
    plan, result, agent = _publish(session, CIDRS[:3])
    assert result['ok'], result['log'][-3:]
    for row in session.rows['route_object']:
        assert 'comment' not in row
        assert [item['comment'] for item in row['group_value']] == [
            {'cf_pa_1': 'cf-mihomo', 'cf_nat_1': 'cf-mihomo-nat'}[row['group_name']], '', '']
    driver = _driver(session)
    assert _plan(driver, status={**agent.status(), 'status': 'ok'}, desired=_desired(CIDRS[:3]))['empty']
    # A same-named group without the marker on its first entry stays the user's.
    unmarked = _ip_group(50, 'cf_pa_1', '', CIDRS[:3])
    unmarked['group_value'][1]['comment'] = 'cf-mihomo'
    session = Device([], [], policies=[], groups=[unmarked])
    plan, result, _ = _publish(session, CIDRS[:3])
    assert result['ok'] and session.rows['route_object'][0] == unmarked
    assert [ref['gp_name'] for ref in _rule(session, 'stream_ipport', 'cf-mihomo')['dst_addr']['object']] == ['cf_pa_2']


def test_rules_listing_cidrs_are_migrated_only_by_a_release():
    session = _legacy()
    driver = _driver(session)
    state = driver.read_state()
    report = build_report(_desired(CIDRS), state)
    assert report['ikuai_groups']['migrations'] == [
        {'rule': 'cf-mihomo', 'kind': '端口分流', 'count': 2500},
        {'rule': 'cf-mihomo-manual', 'kind': '端口分流', 'count': 1},
        {'rule': 'cf-mihomo', 'kind': 'NAT 免转换', 'count': 2500}]
    assert report['in_sync'] is False and report['summary']['pending'] == 3
    plan = _plan(driver, desired=_desired(CIDRS))
    warning = plan['warnings'][0]
    assert warning.startswith('将把 iKuai 端口分流「cf-mihomo」和端口分流「cf-mihomo-manual」和NAT 免转换「cf-mihomo」')
    assert all(word in warning for word in ('上限', '超时', '只改分组', '自动恢复原规则'))
    routes = _ops(plan, 'routes')
    assert [op['kind'] for op in routes] == ['ikuai_group_add'] * 3 + ['policy_route_update'] * 2
    assert routes[3]['summary'].startswith('迁移策略路由「cf-mihomo」到 IP 分组：目的地址改为引用 cf_pa_1…cf_pa_2')
    nat = _ops(plan, 'nat_bypass')
    assert nat[-1]['summary'].startswith('迁移 NAT 免转换规则「cf-mihomo」到 IP 分组') and '源地址保持不变' in nat[-1]['summary']
    assert session.writes == []       # a preview writes nothing

    agent = FakeAgent(SAFE_RETURN_STATUS)
    result = execute(plan, driver, agent)
    assert result['ok'], result['log'][-3:]
    for func, comment in (('stream_ipport', 'cf-mihomo'), ('stream_ipport', 'cf-mihomo-manual'), ('nat_rule', 'cf-mihomo')):
        assert _rule(session, func, comment)['dst_addr']['custom'] == []
    assert _resolved(session, _rule(session, 'stream_ipport', 'cf-mihomo')) == CIDRS
    assert _resolved(session, _rule(session, 'stream_ipport', 'cf-mihomo-manual')) == ['1.1.1.1/32']
    assert _resolved(session, _rule(session, 'nat_rule', 'cf-mihomo')) == CIDRS
    again = _plan(driver, status={**agent.status(), 'status': 'ok'}, desired=_desired(CIDRS))
    assert again['empty'] and not any(warning.startswith('将把') for warning in again['warnings'])
    state = driver.read_state()
    assert build_report(_desired(CIDRS), state)['ikuai_groups'] == {'migrations': [], 'orphans': [], 'marks': []}


def test_hand_made_rules_and_other_managed_nat_rules_are_left_alone():
    hand = _policy(5, 'my split', ['8.8.8.0/24'])
    session = Device([], [_nat(1, 'cf-mihomo', ['198.18.0.0/16']), _nat(2, 'cf-old', ['203.0.113.0/24']),
                          _nat(3, 'mine', ['192.0.2.0/24'])], policies=[hand])
    driver = _driver(session)
    result = execute(_plan(driver, desired=_desired(['198.18.0.0/16'])), driver, FakeAgent(SAFE_RETURN_STATUS))
    assert result['ok'], result['log'][-3:]
    assert _rule(session, 'nat_rule', 'cf-old')['dst_addr']['custom'] == ['203.0.113.0/24']
    assert _rule(session, 'nat_rule', 'mine')['dst_addr']['custom'] == ['192.0.2.0/24']
    assert _rule(session, 'stream_ipport', 'my split') == hand
    assert not any(param.get('id') in (2, 3) for func, _, param in session.writes if func == 'nat_rule')


def test_groups_of_the_user_are_never_changed_or_reused():
    mine = _ip_group(40, 'cf_pa_1', 'my own', CIDRS[:10])
    same = _ip_group(41, 'tg', '', CIDRS[:10])
    session = Device([], [], policies=[], groups=[mine, same])
    driver = _driver(session)
    plan = _plan(driver, desired=_desired(CIDRS[:10]))
    assert [op['name'] for op in _ops(plan, 'routes') if op['kind'] == 'ikuai_group_add'] == ['cf_pa_2']
    result = execute(plan, driver, FakeAgent(SAFE_RETURN_STATUS))
    assert result['ok'], result['log'][-3:]
    assert [ref['gp_name'] for ref in _rule(session, 'stream_ipport', 'cf-mihomo')['dst_addr']['object']] == ['cf_pa_2']
    assert [row for row in session.rows['route_object'] if row['id'] in (40, 41)] == [mine, same]


def test_a_managed_group_used_by_a_hand_made_rule_is_kept_with_a_warning():
    groups = [_ip_group(31, 'cf_pa_1', 'cf-mihomo', CIDRS[:5])]
    rules = [_grouped(1, 'cf-mihomo', [_ref(31, 'cf_pa_1')]), _grouped(7, 'my split', [_ref(31, 'cf_pa_1')])]
    session = Device([], [], policies=rules, groups=groups)
    driver = _driver(session)
    plan = _plan_without_return(driver, _desired(CIDRS[:5]))
    assert not any(op['kind'] == 'ikuai_group_delete' for op in _ops(plan, 'routes'))
    assert any('cf_pa_1 被手工规则端口分流「my split」引用，已保留' in warning for warning in plan['warnings'])
    assert execute(plan, driver, None)['ok']
    assert [row['group_name'] for row in session.rows['route_object']][:1] == ['cf_pa_1']


def test_unreferenced_managed_groups_are_reported_and_removed():
    groups = [_ip_group(31, 'cf_pa_1', 'cf-mihomo', CIDRS[:5]), _ip_group(32, 'cf_pm_9', 'cf-mihomo-manual', ['1.1.1.1/32'])]
    session = Device([], [], policies=[_grouped(1, 'cf-mihomo', [_ref(31, 'cf_pa_1')])], groups=groups)
    driver = _driver(session)
    state = driver.read_state()
    report = build_report(_desired(CIDRS[:5]), state)
    assert report['ikuai_groups']['orphans'] == ['cf_pm_9'] and report['in_sync'] is False
    plan = _plan(driver, desired=_desired(CIDRS[:5]))
    assert [(op['kind'], op.get('name')) for op in _ops(plan, 'routes')] == [('ikuai_group_delete', 'cf_pm_9')]


# --- later releases only edit groups -----------------------------------------------------

def test_adding_and_removing_cidrs_edits_groups_not_rules():
    cidrs = _cidrs(6000, '10.0.0.0/8', 24)
    session = Device([], [], policies=[])
    _, result, agent = _publish(session, cidrs[:5999])
    assert result['ok'], result['log'][-3:]

    _, result, _ = _publish(session, cidrs, agent)
    assert result['ok'] and _writes(session) == [('route_object', 'edit')] * 2     # cf_pa_3 and cf_nat_3

    extra = '192.0.2.0/24'
    plan, result, _ = _publish(session, cidrs + [extra], agent)
    assert result['ok'] and _writes(session) == [('route_object', 'add'), ('stream_ipport', 'edit'),
                                                 ('route_object', 'add'), ('nat_rule', 'edit')]
    assert _groups(session)['cf_pa_4'] == [extra]

    plan, result, _ = _publish(session, cidrs, agent)
    # The rule stops referencing cf_pa_4 first, then it goes; NAT bypass only ever grows.
    assert result['ok'] and _writes(session) == [('stream_ipport', 'edit'), ('route_object', 'del')]

    _, result, _ = _publish(session, cidrs[:5999], agent)
    assert result['ok'] and _writes(session) == [('route_object', 'edit')]

    _, result, _ = _publish(session, cidrs[:5999], agent)
    assert result['ok'] and _writes(session) == []

    _, result, _ = _publish(session, cidrs[:4000], agent)
    assert result['ok'] and _writes(session) == [('stream_ipport', 'edit'), ('route_object', 'del')]
    assert sorted(name for name in _groups(session) if name.startswith('cf_pa_')) == ['cf_pa_1', 'cf_pa_2']
    assert _resolved(session, _rule(session, 'stream_ipport', 'cf-mihomo')) == cidrs[:4000]


# --- failures roll back to exactly what was there ------------------------------------------

def _failed_release(session, cidrs=CIDRS, stage=None):
    driver = _driver(session)
    before = _device(session)
    plan = _plan(driver, desired=_desired(cidrs))
    assert plan['blockers'] == []
    result = execute(plan, driver, FakeAgent(SAFE_RETURN_STATUS))
    assert not result['ok']
    if stage:
        assert result['failed_stage'] == stage, result['log'][-3:]
    return before, result


@pytest.mark.parametrize('point, kind', [
    (('route_object', 'add', 2), 'refuse'),
    (('route_object', 'add', 2), 'lose'),
    (('route_object', 'add', 2), 'drop'),
    (('stream_ipport', 'edit', 1), 'refuse'),
    (('stream_ipport', 'edit', 1), 'lose'),
    (('stream_ipport', 'edit', 2), 'lose'),
    (('static_rt', 'del', 1), 'refuse'),
    (('route_object', 'add', 4), 'refuse'),      # the NAT stage's first group, after routes migrated
    (('route_object', 'add', 4), 'lose'),
    (('nat_rule', 'edit', 1), 'refuse'),
    (('nat_rule', 'edit', 1), 'lose'),
])
def test_every_failed_migration_step_restores_the_device(point, kind):
    route = _route(60, CIDRS[0], GATEWAY, 'cf-lab', _tagname('rt', CIDRS[0]))
    session = _legacy(routes=[route])
    getattr(session, kind).add(point)
    before, result = _failed_release(session)
    assert all(entry['status'] == 'ok' for entry in result['rollback']), result['rollback']
    assert _device(session) == before
    assert not [name for name in _groups(session) if name.startswith('cf_')]


def test_group_delete_failure_restores_the_rule_reference():
    groups = [_ip_group(31, 'cf_pa_1', 'cf-mihomo', CIDRS[:2000]), _ip_group(32, 'cf_pa_2', 'cf-mihomo', CIDRS[2000:])]
    session = Device([], [], policies=[_grouped(1, 'cf-mihomo', [_ref(31, 'cf_pa_1'), _ref(32, 'cf_pa_2')])],
                     groups=groups)
    session.refuse.add(('route_object', 'del', 1))
    before, result = _failed_release(session, CIDRS[:2000], 'routes')
    assert all(entry['status'] == 'ok' for entry in result['rollback'])
    assert _device(session) == before


def test_a_group_deleted_and_re_created_on_rollback_is_referenced_by_its_new_id():
    groups = [_ip_group(31, 'cf_pa_1', 'cf-mihomo', CIDRS[:2000]), _ip_group(32, 'cf_pa_2', 'cf-mihomo', CIDRS[2000:])]
    rule = _grouped(1, 'cf-mihomo', [_ref(31, 'cf_pa_1'), _ref(32, 'cf_pa_2')])
    session = Device([], [_nat(1, 'cf-mihomo', CIDRS)], policies=[rule], groups=groups)
    session.refuse.add(('nat_rule', 'edit', 1))
    before, result = _failed_release(session, CIDRS[:2000], 'nat_bypass')
    assert all(entry['status'] == 'ok' for entry in result['rollback']), result['rollback']
    assert _device(session) == before
    restored = _rule(session, 'stream_ipport', 'cf-mihomo')
    new_gid = next(row['group_id'] for row in session.rows['route_object'] if row['group_name'] == 'cf_pa_2')
    assert new_gid != 'IPGP32' and restored['dst_addr']['object'][1]['gid'] == new_gid


def test_a_new_group_changed_by_hand_during_the_release_is_not_deleted():
    session = _legacy()
    session.refuse.add(('nat_rule', 'edit', 1))

    def edit_by_hand(func, action, param, current):
        if (func, action) == ('route_object', 'add') and param['group_name'] == 'cf_nat_1':
            row = next(row for row in current.rows['route_object'] if row['group_name'] == 'cf_pa_1')
            row['group_value'] = row['group_value'][:-1]
    session.on_write = edit_by_hand
    _, result = _failed_release(session, stage='nat_bypass')
    failed = [entry for entry in result['rollback'] if entry['status'] == 'failed']
    assert len(failed) == 1 and '「cf_pa_1」已被修改，未自动删除' in failed[0]['summary']
    assert 'cf_pa_1' in _groups(session) and 'cf_pa_2' not in _groups(session)
    assert _rule(session, 'stream_ipport', 'cf-mihomo')['dst_addr']['custom'] == CIDRS


def test_a_rule_that_cannot_be_written_back_keeps_its_groups():
    session = _legacy()
    session.refuse.update({('nat_rule', 'edit', 1), ('stream_ipport', 'edit', 3)})   # 3rd: restoring cf-mihomo-manual
    _, result = _failed_release(session, stage='nat_bypass')
    failed = [entry['summary'] for entry in result['rollback'] if entry['status'] == 'failed']
    assert any(summary.startswith('撤销失败：迁移策略路由「cf-mihomo-manual」') for summary in failed)
    assert any('「cf_pm_1」仍被端口分流「cf-mihomo-manual」引用，未自动删除' in summary for summary in failed)
    # Nothing references a group that is gone.
    for func in ('stream_ipport', 'nat_rule'):
        assert all(_resolved(session, row) is not None for row in session.rows[func])
    assert _rule(session, 'stream_ipport', 'cf-mihomo')['dst_addr']['custom'] == CIDRS


def test_a_large_rule_is_written_back_with_more_time():
    session = _legacy()
    session.refuse.add(('nat_rule', 'edit', 1))
    timeouts = []
    real = session.post

    def post(url, json=None, timeout=None, **kwargs):
        if json and json.get('action') == 'edit' and json['func_name'] == 'stream_ipport':
            timeouts.append(timeout)
        return real(url, json=json, **kwargs)
    session.post = post
    _failed_release(session, stage='nat_bypass')
    # Migration edits are small; writing 2500 CIDRs back gets 10 s + 3 ms each.
    assert timeouts == [10, 10, 10, pytest.approx(10 + 0.003 * 2501)]


# --- read-back --------------------------------------------------------------------------------

def test_references_resolve_by_group_id_not_by_name():
    sets = {'cf_pa_1': ['10.9.0.0/24']}
    by_gid = {'IPGP7': ['10.1.0.0/24']}
    ref = {'custom': [], 'object': [{'type': 0, 'gid': 'IPGP7', 'gp_name': 'cf_pa_1'}]}
    assert _addresses(ref, sets, by_gid) == ['10.1.0.0/24']
    dangling = {'custom': [], 'object': [{'type': 0, 'gid': 'IPGP8', 'gp_name': 'cf_pa_1'}]}
    assert _addresses(dangling, sets, by_gid) == []


def test_a_dangling_group_reference_is_not_coverage():
    rule = _grouped(1, 'cf-mihomo', [_ref(99, 'cf_pa_1')])
    session = Device([], [_nat(1, 'cf-mihomo', [], [_ref(98, 'cf_nat_1')])], policies=[rule],
                     groups=[_ip_group(31, 'cf_pa_1', 'cf-mihomo', CIDRS[:3])])
    driver = _driver(session)
    report = build_report(_desired(CIDRS[:3]), driver.read_state())
    assert {item['status'] for item in report['routes']['items']} == {'missing'}
    assert {item['status'] for item in report['nat_bypass']['items']} == {'missing'}
    plan = _plan(driver, desired=_desired(CIDRS[:3]))
    assert execute(plan, driver, FakeAgent(SAFE_RETURN_STATUS))['ok']
    assert [ref['gid'] for ref in _rule(session, 'stream_ipport', 'cf-mihomo')['dst_addr']['object']] == ['IPGP31']


@pytest.mark.parametrize('tamper', ['custom', 'comment'])
def test_structure_read_back_catches_what_coverage_cannot(tamper):
    session = Device([], [], policies=[])

    def change(func, action, param, current):
        if (func, action) == ('stream_ipport', 'add'):
            if tamper == 'custom':
                current.rows['stream_ipport'][-1]['dst_addr']['custom'] = [CIDRS[0]]
            else:
                # The marker: the first entry's comment (iKuai keeps no group comment).
                current.rows['route_object'][0]['group_value'][0]['comment'] = 'renamed'
    session.on_write = change
    driver = _driver(session)
    result = execute(_plan(driver, desired=_desired(CIDRS[:3])), driver, FakeAgent(SAFE_RETURN_STATUS))
    assert not result['ok'] and result['failed_stage'] == 'routes'
    message = result['log'][-1]['summary']
    assert ('没有按计划只引用 ConfigFlow 的 IP 分组' if tamper == 'custom' else '「cf_pa_1」内容与计划不一致') in message


def test_static_routing_mode_keeps_only_the_nat_groups():
    cidrs = CIDRS[:10]
    session = Device([], [], policies=[])
    _, result, agent = _publish(session, cidrs)
    assert result['ok']
    driver = _driver(session)
    plan = _plan_without_return(driver, _desired(cidrs))
    assert [op['kind'] for op in _ops(plan, 'routes')] == ['route_add'] * 10 + ['policy_route_delete',
                                                                               'ikuai_group_delete']
    assert plan['checks']['ikuai_groups']['routes'] == {'groups': {}, 'rules': [], 'removed': ['cf_pa_1']}
    result = execute(plan, driver, None)
    assert result['ok'], result['log'][-3:]
    assert list(_groups(session)) == ['cf_nat_1'] and session.rows['stream_ipport'] == []
    assert _plan_without_return(driver, _desired(cidrs))['empty']


# --- shared groups and a stable NAT target ---------------------------------------------------

A, B, C = '91.108.0.0/16', '149.154.160.0/20', '8.8.8.0/24'


def _release(session, cidrs):
    """One manual release with auto return routing; the plan and its result."""
    driver = _driver(session)
    agent = FakeAgent(SAFE_RETURN_STATUS)
    plan = _plan(driver, status={**agent.status(), 'status': 'ok'}, desired=_desired(cidrs))
    assert plan['blockers'] == [], plan['blockers']
    return plan, execute(plan, driver, agent)


def _settled(session, cidrs):
    """The next preview has nothing to do and the router reads as in sync."""
    driver = _driver(session)
    plan = _plan(driver, desired=_desired(cidrs))
    assert _ops(plan, 'nat_bypass') == [] and _ops(plan, 'routes') == []
    assert build_report(_desired(cidrs), driver.read_state())['in_sync'] is True


@pytest.mark.parametrize('nat_rules, cidrs', [
    # Adoption order (report order) picks rule 2 first, not the lowest ID.
    ([_nat(1, 'mine-a', ['10.1.0.0/16']), _nat(2, 'mine-b', ['10.2.0.0/16'])],
     ['10.2.0.0/16', '10.1.0.0/16', '10.3.0.0/16']),
    ([_nat(3, 'x', [B]), _nat(5, 'y', [A])], [A, B, C]),
])
def test_adopted_nat_rules_keep_the_groups_on_the_rule_that_references_them(nat_rules, cidrs):
    session = Device([], nat_rules, policies=[])
    _, first = _release(session, cidrs)
    assert first['ok'], first['log'][-3:]
    grouped = [row for row in session.rows['nat_rule'] if row['dst_addr']['object']]
    assert len(grouped) == 1
    before = _groups(session)['cf_nat_1']
    _settled(session, cidrs)
    _, second = _release(session, cidrs)
    assert second['ok'], second['log'][-3:]
    assert _groups(session)['cf_nat_1'] == before
    assert [row for row in session.rows['nat_rule'] if row['dst_addr']['object']] == grouped


def test_adopting_a_lower_id_hand_made_rule_later_does_not_move_the_nat_groups():
    session = Device([], [_nat(3, 'office', [B])], policies=[])
    assert _release(session, [A])[1]['ok']
    assert _release(session, [A, B])[1]['ok']
    office = _rule(session, 'nat_rule', 'cf-office')
    assert office['dst_addr'] == {'custom': [B], 'object': []}
    _settled(session, [A, B])
    _, third = _release(session, [A, B])
    assert third['ok'], third['log'][-3:]
    assert _groups(session)['cf_nat_1'] == [A]
    assert _resolved(session, _rule(session, 'nat_rule', 'cf-mihomo')) == [A]


def test_legacy_nat_rules_migrate_the_one_releases_added_to():
    # Before IP groups, new CIDRs went to the lowest-ID managed rule; adopting a
    # hand-made lower-ID rule later left the big list on cf-mihomo.
    session = Device([], [_nat(3, 'cf-office', [B]), _nat(9, 'cf-mihomo', [A, C])], policies=[])
    plan, result = _release(session, [A, B, C])
    assert result['ok'], result['log'][-3:]
    assert [(op['kind'], op.get('id')) for op in _ops(plan, 'nat_bypass')] == [
        ('ikuai_group_add', None), ('nat_update', '9')]
    assert _rule(session, 'nat_rule', 'cf-office')['dst_addr'] == {'custom': [B], 'object': []}
    _settled(session, [A, B, C])


def test_a_group_another_rule_also_uses_is_never_edited():
    groups = [_ip_group(31, 'cf_pa_1', 'cf-mihomo', [A, B])]
    rules = [_grouped(1, 'cf-mihomo', [_ref(31, 'cf_pa_1')]), _grouped(7, 'my split', [_ref(31, 'cf_pa_1')])]
    session = Device([], [], policies=rules, groups=groups)
    plan, result = _release(session, [A])
    assert result['ok'], result['log'][-3:]
    assert [(op['kind'], op.get('name')) for op in _ops(plan, 'routes')] == [
        ('ikuai_group_add', 'cf_pa_2'), ('policy_route_update', None)]
    assert any('cf_pa_1 被手工规则端口分流「my split」引用，已保留' in warning for warning in plan['warnings'])
    assert _groups(session)['cf_pa_1'] == [A, B]
    assert _resolved(session, _rule(session, 'stream_ipport', 'my split')) == [A, B]
    assert _resolved(session, _rule(session, 'stream_ipport', 'cf-mihomo')) == [A]


def test_a_shared_group_holding_only_wanted_entries_is_referenced_as_it_is():
    existing = [{'name': 'cf_nat_1', 'index': 1, 'values': [A], 'frozen': True},
                {'name': 'cf_nat_2', 'index': 2, 'values': [A, C], 'frozen': True}]
    result = layout('nat', existing, [A, B], set())
    assert result['final'] == [('cf_nat_1', [A]), ('cf_nat_3', [B])]
    assert (result['updates'], result['deletes']) == ([], [])


class BareIPDevice(Device):
    """Reads an IP group's x/32 entry back as x, as canon() allows for."""

    def post(self, url, json=None, **kwargs):
        response = super().post(url, json=json, **kwargs)
        if json and json.get('func_name') == 'route_object' and json.get('action') == 'show':
            for row in response._data['results']['data']:
                for item in row.get('group_value') or []:
                    for suffix in ('%2F32', '/32'):
                        if isinstance(item.get('ip'), str) and item['ip'].endswith(suffix):
                            item['ip'] = item['ip'][:-len(suffix)]
        return response


def test_a_host_read_back_without_its_prefix_is_not_an_extra_destination():
    cidrs = [A, '1.1.1.1/32']
    session = BareIPDevice([], [], policies=[])
    assert _release(session, cidrs)[1]['ok']
    report = build_report(_desired(cidrs), _driver(session).read_state())
    assert report['routes']['extra'] == [] and report['in_sync'] is True


def test_a_rollback_rebinds_a_reused_group_id_by_name():
    nat = _ip_group(7, 'cf_nat_1', 'cf-mihomo-nat', ['9.9.9.0/24'])
    pa = _ip_group(9, 'cf_pa_2', 'cf-mihomo', ['1.1.1.0/24'])
    session = Device([], [], policies=[], groups=[nat, pa])
    driver = _driver(session)
    record = {'comment': 'cf-mihomo', 'src_addr': {'custom': ['10.0.0.2'], 'object': []},
              'dst_addr': {'custom': [], 'object': [{'type': 0, 'gid': 'IPGP7', 'gp_name': 'cf_pa_2'}]}}
    assert driver._rebind(record)['dst_addr']['object'] == [{'type': 0, 'gid': 'IPGP9', 'gp_name': 'cf_pa_2'}]
    # A reference to the user's group is written back exactly.
    own = {'dst_addr': {'custom': [], 'object': [{'type': 0, 'gid': 'IPGP7', 'gp_name': 'tg'}]}}
    assert driver._rebind(own) == own


# --- what the user does to our groups in the iKuai UI -------------------------------------

def _group_row(session, name):
    [row] = [row for row in session.rows['route_object'] if row['group_name'] == name]
    return row


def _notes(session, name):
    return [(decode_value(item['ip']), decode_value(item['comment'], 'comment'))
            for item in _group_row(session, name)['group_value']]


def test_entry_notes_survive_updates_and_rollbacks():
    session = Device([], [], policies=[])
    _, result, agent = _publish(session, MANY[:10])
    assert result['ok'], result['log'][-3:]
    _group_row(session, 'cf_nat_1')['group_value'][3]['comment'] = 'office'
    before = _device(session)
    # The cf_nat_1 edit is made but its reply is lost: the release rolls back.
    session.lose.add(('route_object', 'edit', 2))
    _, failed, _ = _publish(session, MANY[:11], agent)
    assert not failed['ok'] and all(entry['status'] != 'failed' for entry in failed['rollback'])
    assert _device(session) == before
    _, result, _ = _publish(session, MANY[:11], agent)
    assert result['ok'], result['log'][-3:]
    notes = _notes(session, 'cf_nat_1')
    assert notes[0][1] == 'cf-mihomo-nat' and (MANY[3], 'office') in notes
    assert sorted(value for value, _ in notes) == sorted(MANY[:11])


def test_entry_notes_count_toward_the_request_size():
    note = '办公室-香港-专线' * 4
    notes = {canon(value): note for value in MANY[:GROUP_MAX_ENTRIES]}
    result = layout('pm', [], MANY[:GROUP_MAX_ENTRIES], set(), notes)
    assert len(result['final']) > 1
    for name, values in result['final']:
        assert group_bytes(name, 'pm', values, notes) <= GROUP_MAX_BYTES
    # The marker goes on an entry without a note, moved to the front; no note is lost.
    entries = payload('cf_pm_1', 'pm', [A, B, C], notes={canon(A): 'tg', canon(C): 'dns'})['group_value']
    assert entries == [{'ip': B, 'comment': 'cf-mihomo-manual'}, {'ip': A, 'comment': 'tg'},
                       {'ip': C, 'comment': 'dns'}]


def test_a_group_referenced_where_configflow_cannot_see_is_never_deleted():
    # ACL, 流控 and other features can reference an IP group; iKuai counts them in
    # ref_count and refuses (2021) to delete the group.
    session = Device([], [], policies=[])
    assert _publish(session, CIDRS[:10])[1]['ok']
    _group_row(session, 'cf_pa_1')['hidden_refs'] = 1
    for _ in range(2):
        driver = _driver(session)
        plan = _plan_without_return(driver, _desired(CIDRS[:10]))
        assert ('ikuai_group_delete', 'cf_pa_1') not in [(op['kind'], op.get('name')) for op in _ops(plan, 'routes')]
        assert any('cf_pa_1 还被 iKuai 中 ConfigFlow 读不到的配置（如 ACL、流控）引用 1 处，已保留' in warning
                   for warning in plan['warnings'])
        result = execute(plan, driver, None)
        assert result['ok'], result['log'][-3:]
    assert not [row for row in session.rows['stream_ipport'] if decode_value(row['comment']) == 'cf-mihomo']
    assert _groups(session)['cf_pa_1'] == CIDRS[:10]
    assert build_report(_desired(CIDRS[:10]), _driver(session).read_state())['ikuai_groups']['orphans'] == []


@pytest.mark.parametrize('extra', ['src_addr', 'dst_addr'])
def test_ref_count_is_one_per_rule_so_a_rule_naming_a_group_twice_hides_no_acl(extra):
    # iKuai 4.0.314 counts a rule once however many times it names the group (src
    # and dst, or the same reference twice) and counts disabled rules too. A rule
    # naming cf_pa_1 twice must not cancel out the ACL reference: the group is still
    # referenced after the rule is deleted, and a delete would hit 2021 every release.
    session = Device([], [], policies=[])
    assert _publish(session, CIDRS[:10])[1]['ok']
    rule = _rule(session, 'stream_ipport', 'cf-mihomo')
    group = _group_row(session, 'cf_pa_1')
    rule[extra] = {'custom': list(rule[extra].get('custom') or []) if extra == 'src_addr' else [],
                   'object': [*(rule[extra].get('object') or []), _ref(group['id'], 'cf_pa_1')]}
    rule['enabled'] = 'no'
    group['hidden_refs'] = 1
    driver = _driver(session)
    plan = _plan_without_return(driver, _desired(CIDRS[:10]))
    assert ('ikuai_group_delete', 'cf_pa_1') not in [(op['kind'], op.get('name')) for op in _ops(plan, 'routes')]
    assert any('cf_pa_1 还被 iKuai 中 ConfigFlow 读不到的配置（如 ACL、流控）引用 1 处，已保留' in warning
               for warning in plan['warnings'])
    result = execute(plan, driver, None)
    assert result['ok'], result['log'][-3:]
    assert _groups(session)['cf_pa_1'] == CIDRS[:10]


def test_a_group_referenced_where_configflow_cannot_see_is_frozen():
    session = Device([], [], policies=[])
    _, result, agent = _publish(session, CIDRS)
    assert result['ok'] and len(_groups(session)['cf_pa_2']) == 500
    _group_row(session, 'cf_pa_2')['hidden_refs'] = 1
    # Fewer CIDRs: cf_pa_2 keeps what the other reference sees, the rule stops using it.
    _, result, _ = _publish(session, CIDRS[:1500], agent)
    assert result['ok'], result['log'][-3:]
    assert _groups(session)['cf_pa_2'] == CIDRS[2000:]
    assert sorted(_resolved(session, _rule(session, 'stream_ipport', 'cf-mihomo'))) == sorted(CIDRS[:1500])
    # More CIDRs never go into it either.
    _group_row(session, 'cf_pa_1')['hidden_refs'] = 1
    plan, result, _ = _publish(session, CIDRS[:1501], agent)
    assert result['ok'], result['log'][-3:]
    assert ('ikuai_group_update', 'cf_pa_1') not in [(op['kind'], op.get('name')) for op in _ops(plan, 'routes')]
    assert _groups(session)['cf_pa_1'] == CIDRS[:1500] and _groups(session)['cf_pa_2'] == CIDRS[2000:]
    assert sorted(_resolved(session, _rule(session, 'stream_ipport', 'cf-mihomo'))) == sorted(CIDRS[:1501])


@pytest.mark.parametrize('note', ['telegram', ''])
def test_a_lost_marker_on_a_group_our_rule_references_is_written_back(note):
    session = Device([], [], policies=[])
    assert _publish(session, CIDRS[:10])[1]['ok']
    _group_row(session, 'cf_pa_1')['group_value'][0]['comment'] = note
    report = build_report(_desired(CIDRS[:10]), _driver(session).read_state())
    assert report['in_sync'] is False and report['ikuai_groups']['marks'] == ['cf_pa_1']
    plan, result = _release(session, CIDRS[:10])
    assert result['ok'], result['log'][-3:]
    [op] = _ops(plan, 'routes')
    assert (op['kind'], op['name'], op.get('mark')) == ('ikuai_group_update', 'cf_pa_1', True)
    assert '恢复 IP 分组「cf_pa_1」的 ConfigFlow 归属标记' in op['summary']
    assert sorted(_groups(session)) == ['cf_nat_1', 'cf_pa_1']
    notes = _notes(session, 'cf_pa_1')
    assert notes[0][1] == 'cf-mihomo' and sorted(value for value, _ in notes) == sorted(CIDRS[:10])
    assert ((CIDRS[0], 'telegram') in notes) is bool(note)
    _settled(session, CIDRS[:10])


def test_a_lost_marker_does_not_move_the_nat_groups_to_another_rule():
    session = Device([], [_nat(1, 'cf-mihomo', [])], policies=[])
    assert _publish(session, CIDRS[:10])[1]['ok']
    session.rows['nat_rule'].append(_nat(50, 'cf-extra', ['203.0.113.0/24']))
    _group_row(session, 'cf_nat_1')['group_value'][0]['comment'] = ''
    plan, result = _release(session, CIDRS[:11])
    assert result['ok'], result['log'][-3:]
    assert [(op['kind'], op.get('name'), op.get('mark')) for op in _ops(plan, 'nat_bypass')] == [
        ('ikuai_group_update', 'cf_nat_1', True), ('ikuai_group_update', 'cf_nat_1', None)]
    assert sorted(_groups(session)) == ['cf_nat_1', 'cf_pa_1']
    assert _rule(session, 'nat_rule', 'cf-extra')['dst_addr'] == {'custom': ['203.0.113.0/24'], 'object': []}
    assert sorted(_resolved(session, _rule(session, 'nat_rule', 'cf-mihomo'))) == sorted(CIDRS[:11])
    _settled(session, CIDRS[:11])


def test_an_unmarked_group_with_our_name_is_the_users_but_is_pointed_out():
    mine = _ip_group(40, 'cf_pa_1', 'my own', CIDRS[:3])
    split = _ip_group(41, 'cf_pa_2', '', CIDRS[3:6])
    session = Device([], [], policies=[_grouped(7, 'my split', [_ref(41, 'cf_pa_2')])], groups=[mine, split])
    plan, result = _release(session, CIDRS[:6])
    assert result['ok'], result['log'][-3:]
    assert [row for row in session.rows['route_object'] if row['id'] in (40, 41)] == [mine, split]
    assert any('IP 分组「cf_pa_1」名称是 ConfigFlow 的格式' in warning for warning in plan['warnings'])
    # One a hand-made rule uses is plainly the user's: no hint.
    assert not any('「cf_pa_2」名称是' in warning for warning in plan['warnings'])


def test_a_managed_nat_rule_disabled_for_a_while_keeps_its_groups_quietly():
    session = Device([], [], policies=[])
    _, result, agent = _publish(session, CIDRS[:10])
    assert result['ok']
    holder = _rule(session, 'nat_rule', 'cf-mihomo')
    holder['enabled'] = 'no'
    _, result, _ = _publish(session, CIDRS[:10] + [A], agent)
    assert result['ok'], result['log'][-3:]
    holder['enabled'] = 'yes'
    plan = _plan(_driver(session), status={**agent.status(), 'status': 'ok'}, desired=_desired(CIDRS[:10] + [A]))
    assert plan['empty'] and not any('cf_nat' in warning for warning in plan['warnings']), plan['warnings']
