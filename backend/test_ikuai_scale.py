"""iKuai at scale: every list page is read, a stage reads static_rt a constant number of times,
and every object a release may have created can be undone by its unique name."""
import copy
import ipaddress

import pytest
import requests

from backend.routers.base import RouterError, retire_policy_routes
from backend.routers.ikuai import (
    MAX_REQUEST_BYTES, IKuaiDriver, _tagname, decode_value, oversized_requests, request_bytes,
)
from backend.routers.plan import build_plan
from backend.routers.release import execute
from backend.routers.report import build_report
from backend.test_router_release import (
    DESIRED, FakeAgent, GATEWAY, HOME_NATS, MIGRATED, SAFE_RETURN_STATUS, StatefulIKuai, _driver, _plan,
    _plan_without_return,
)
from backend.test_routers import FakeResponse


class PagedIKuai(StatefulIKuai):
    """Honours ``limit=offset,size`` like 4.0.314, rejects duplicate names, and records every show."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.next_id = max([row['id'] for rows in self.rows.values() for row in rows] + [99]) + 1
        self.shows = []
        self.on_show = None      # (func, number, session) -> None; may raise or mutate
        self.on_write = None     # (func, action, param, session) -> None; may raise after the change

    def post(self, url, json=None, **kwargs):
        if url.endswith('/Action/login'):
            return super().post(url, json=json, **kwargs)
        func, action, param = json['func_name'], json['action'], json['param']
        if action == 'show':
            self.shows.append((func, param['limit']))
            if self.on_show:
                self.on_show(func, len(self.shows), self)
            response = super().post(url, json=json, **kwargs)
            offset, size = map(int, param['limit'].split(','))
            response._data['results']['data'] = response._data['results']['data'][offset:offset + size]
            return response
        if action == 'add' and any(row.get('tagname') == param.get('tagname') for row in self.rows[func]):
            self.writes.append((func, action, copy.deepcopy(param)))
            return FakeResponse({'code': 3001, 'message': '请求参数不合法',
                                 'details': [{'field': 'tagname', 'msg': '名称已存在，请重新输入'}]})
        response = super().post(url, json=json, **kwargs)
        if self.on_write:
            self.on_write(func, action, param, self)
        return response

    def static_shows(self):
        return [limit for func, limit in self.shows if func == 'static_rt']


def _cidrs(count, base='10.200.0.0/16', prefix=28):
    return [str(net) for net in list(ipaddress.IPv4Network(base).subnets(new_prefix=prefix))[:count]]


def _route(row_id, cidr, gateway='10.0.0.9', comment='lab', tagname=None):
    network = ipaddress.IPv4Network(cidr)
    return {'id': row_id, 'dst_addr': str(network.network_address), 'netmask': str(network.netmask),
            'gateway': gateway, 'enabled': 'yes', 'comment': comment, 'tagname': tagname or f'LB{row_id}',
            'ip_type': '4', 'interface': 'auto', 'prio': 1}


def _bulk(count):
    """Unrelated hand-made routes to another next hop, like a large imported table."""
    return [_route(index + 1, cidr) for index, cidr in enumerate(_cidrs(count, '10.100.0.0/16'))]


def _desired(cidrs):
    return {'fake_ip_range': '', 'proxy_gateway': GATEWAY, 'warnings': [],
            'proxy_cidrs': [{'cidr': cidr, 'sources': ['lab']} for cidr in cidrs]}


def _static_plan(driver, cidrs):
    return _plan_without_return(driver, _desired(cidrs))


def _managed(session):
    return sorted((row['dst_addr'], row['netmask'], row['gateway'], row['comment'], row['tagname'])
                  for row in map(decode_value, session.rows['static_rt']) if row['comment'].startswith('cf-'))


# --- every page ----------------------------------------------------------------

def test_read_state_reads_every_page_of_every_list():
    session = PagedIKuai(_bulk(2500), [])
    state = _driver(session).read_state()
    assert len(state.routes) == 2500 and len(state.active_routes) == 2500
    assert session.static_shows() == ['0,1000', '1000,1000', '2000,1000']
    assert [limit for func, limit in session.shows if func == 'static_rt_table'] == ['0,1000', '1000,1000',
                                                                                     '2000,1000']


def test_routes_beyond_the_first_page_are_seen_by_the_preview():
    # Managed routes past row 1000 used to be missing (re-added) and their extras never deleted.
    rows = _bulk(1500) + [_route(1501 + index, cidr, GATEWAY, 'cf-lab', _tagname('rt', cidr))
                          for index, cidr in enumerate(_cidrs(3))]
    driver = _driver(PagedIKuai(rows, []))
    state = driver.read_state()
    retire_policy_routes(state)
    report = build_report(_desired(_cidrs(2)), state)
    assert [item['status'] for item in report['routes']['items']] == ['synced', 'synced']
    assert [extra['cidr'] for extra in report['routes']['extra']] == [_cidrs(3)[2]]


def test_rows_changing_between_pages_fail_closed():
    session = PagedIKuai(_bulk(1500), [])
    session.on_show = lambda func, number, current: (current.rows['static_rt'].pop()
                                                     if func == 'static_rt' and number == 2 else None)
    with pytest.raises(RouterError, match='静态路由列表读取不完整或读取期间发生变化'):
        _driver(session).read_state()


def test_unreadable_active_table_only_disables_leftover_detection():
    session = PagedIKuai(_bulk(1500), [])
    session.on_show = lambda func, number, current: (current.stale.append(dict(current.stale[0]))
                                                     if func == 'static_rt_table' and current.stale else None)
    session.stale = [{'id': 9000, 'dst_addr': '8.8.8.8', 'netmask': '255.255.255.255', 'gateway': GATEWAY,
                      'interface': 'lan1', 'prio': 20}]
    state = _driver(session).read_state()
    assert state.active_routes is None and len(state.routes) == 1500


class DhcpSession:
    def __init__(self, bindings):
        self.bindings = bindings
        self.limits = []
        self.trust_env = True
        self.verify = True

    def close(self):
        pass

    def post(self, url, json=None, **_):
        if url.endswith('/Action/login'):
            return FakeResponse({'code': 0})
        assert (json['func_name'], json['param']['TYPE']) == ('dhcp_static', 'static_total,static_data')
        self.limits.append(json['param']['limit'])
        offset, size = map(int, json['param']['limit'].split(','))
        return FakeResponse({'code': 0, 'results': {'static_total': len(self.bindings),
                                                    'static_data': self.bindings[offset:offset + size]}})


def test_dhcp_bindings_beyond_one_page_are_read():
    driver = IKuaiDriver('http://10.0.0.1', 'admin', 'pw')
    driver.session = DhcpSession([{'id': index, 'ip_addr': f'10.1.{index // 250}.{index % 250}'}
                                  for index in range(1, 1501)])
    assert len(driver._dhcp_list('dhcp_static', 'static_')) == 1500
    assert driver.session.limits == ['0,1000', '1000,1000']


# --- one read per stage ----------------------------------------------------------

def test_adding_routes_reads_static_routes_a_constant_number_of_times():
    session = PagedIKuai(_bulk(1500), [])
    driver = _driver(session)
    cidrs = _cidrs(300)
    plan = _static_plan(driver, cidrs)
    assert [op['kind'] for op in plan['stages'][1]['ops']] == ['route_add'] * 300
    session.shows.clear()
    result = execute(plan, driver, None)
    assert result['ok'], result['log'][-3:]
    # One readback of the new routes, the stage check and the leftover check: 2 pages each,
    # the nat stage check another 2; never one per route as before.
    assert len(session.static_shows()) <= 8
    assert [action for func, action, _ in session.writes if func == 'static_rt'] == ['add'] * 300
    assert _managed(session) == sorted(
        (str(ipaddress.IPv4Network(cidr).network_address), str(ipaddress.IPv4Network(cidr).netmask), GATEWAY,
         'cf-lab', _tagname('rt', cidr)) for cidr in cidrs)
    assert _static_plan(driver, cidrs)['empty']


def test_deleting_routes_reads_static_routes_a_constant_number_of_times():
    managed = [_route(1501 + index, cidr, GATEWAY, 'cf-lab', _tagname('rt', cidr))
               for index, cidr in enumerate(_cidrs(300))]
    session = PagedIKuai(_bulk(1500) + managed, [])
    driver = _driver(session)
    plan = _static_plan(driver, [])
    assert [op['kind'] for op in plan['stages'][1]['ops']] == ['route_delete'] * 300
    session.shows.clear()
    assert execute(plan, driver, None)['ok']
    assert len(session.static_shows()) <= 6
    assert [param for func, action, param in session.writes if action == 'del'] == [
        {'id': row['id']} for row in managed]
    assert _managed(session) == [] and len(session.rows['static_rt']) == 1500


def test_adopting_routes_reads_each_row_just_before_editing_it():
    # An edit sends the whole row, so it is read fresh (as before batching), never from the snapshot.
    manual = [_route(1501 + index, cidr, GATEWAY, f'manual {index}') for index, cidr in enumerate(_cidrs(50))]
    session = PagedIKuai(_bulk(1500) + manual, [])
    driver = _driver(session)
    plan = _static_plan(driver, _cidrs(50))
    assert [op['kind'] for op in plan['stages'][1]['ops']] == ['route_comment'] * 50
    session.shows.clear()
    assert execute(plan, driver, None)['ok']
    assert len(session.static_shows()) <= 2 * 50 + 8
    edits = [param for func, action, param in session.writes if action == 'edit']
    assert edits == [{**row, 'comment': f'cf-manual%20{index}'} for index, row in enumerate(manual)]


def test_adoption_keeps_a_change_made_by_hand_during_the_stage():
    manual = [_route(11, '10.1.0.0/24', GATEWAY, 'a'), _route(12, '10.2.0.0/24', GATEWAY, 'b')]
    session = PagedIKuai(_bulk(5) + manual, [])
    driver = _driver(session)
    plan = _static_plan(driver, ['10.1.0.0/24', '10.2.0.0/24'])
    assert [op['kind'] for op in plan['stages'][1]['ops']] == ['route_comment'] * 2

    def disable_second_by_hand(func, action, param, current):
        if (func, action) == ('static_rt', 'edit') and param['id'] == 11:
            next(row for row in current.rows['static_rt'] if row['id'] == 12)['enabled'] = 'no'
    session.on_write = disable_second_by_hand
    result = execute(plan, driver, None)
    # The disabled route is edited as it now is; the stage check then fails and rolls back.
    edits = [param for func, action, param in session.writes if action == 'edit']
    assert edits[1]['id'] == 12 and edits[1]['enabled'] == 'no'
    assert not result['ok'] and result['failed_stage'] == 'routes'
    assert next(row for row in session.rows['static_rt'] if row['id'] == 12)['enabled'] == 'no'


class SwallowedRouteAdds(PagedIKuai):
    """Accepts static_rt adds with code 0 but never stores them."""

    def post(self, url, json=None, **kwargs):
        if json and (json.get('func_name'), json.get('action')) == ('static_rt', 'add'):
            self.writes.append(('static_rt', 'add', copy.deepcopy(json['param'])))
            return FakeResponse({'code': 0, 'results': {}})
        return super().post(url, json=json, **kwargs)


def test_policy_rules_are_kept_until_their_replacement_routes_exist():
    # Retiring 端口分流 re-routes each CIDR first; an add that never landed must stop the deletes.
    session = SwallowedRouteAdds([], HOME_NATS, policies=MIGRATED)
    driver = _driver(session)
    plan = _plan_without_return(driver)
    assert [op['kind'] for op in plan['stages'][1]['ops']] == ['route_add'] * 4 + ['policy_route_delete'] * 2
    before = copy.deepcopy(session.rows['stream_ipport'])
    result = execute(plan, driver, None)
    assert not result['ok'] and '已提交但读不回新对象' in result['log'][-1]['summary']
    assert [(func, action) for func, action, _ in session.writes] == [('static_rt', 'add')] * 4
    assert session.rows['stream_ipport'] == before


def test_covered_route_is_kept_until_the_wider_route_exists():
    narrow = _route(50, '91.108.4.0/22', GATEWAY, 'cf-lab', _tagname('rt', '91.108.4.0/22'))
    session = SwallowedRouteAdds(_bulk(5) + [narrow], [])
    driver = _driver(session)
    plan = _static_plan(driver, ['91.108.0.0/16'])
    assert [op['kind'] for op in plan['stages'][1]['ops']] == ['route_add', 'route_delete']
    result = execute(plan, driver, None)
    assert not result['ok']
    assert [action for func, action, _ in session.writes if func == 'static_rt'] == ['add']
    assert narrow in session.rows['static_rt']


def test_rollback_still_attempts_every_undo_after_a_re_add_is_lost():
    # As before batching, a re-added route that cannot be read back does not stop the other undos.
    managed = [_route(50 + index, cidr, GATEWAY, 'cf-mihomo', _tagname('rt', cidr))
               for index, cidr in enumerate(['91.108.0.0/16', '149.154.160.0/20', '198.18.0.0/16'])]
    session = SwallowedRouteAdds(managed, HOME_NATS)
    session.fail = ('nat_rule', 'edit')
    driver = _driver(session)
    plan = _plan(driver)
    assert [op['kind'] for op in plan['stages'][1]['ops']] == ['policy_route_add'] + ['route_delete'] * 3
    result = execute(plan, driver, FakeAgent(SAFE_RETURN_STATUS))
    assert not result['ok'] and result['failed_stage'] == 'nat_bypass'
    undone = [(func, action) for func, action, _ in session.writes][5:]
    assert undone == [('static_rt', 'add')] * 3 + [('stream_ipport', 'del')]
    assert session.rows['stream_ipport'] == []
    assert result['rollback'][-1]['status'] == 'failed'


def test_new_routes_are_read_back_once_before_the_deletes_that_follow():
    managed = [_route(1501 + index, cidr, GATEWAY, 'cf-lab', _tagname('rt', cidr))
               for index, cidr in enumerate(_cidrs(100, '10.220.0.0/16'))]
    session = PagedIKuai(_bulk(1500) + managed, [])
    driver = _driver(session)
    plan = _static_plan(driver, _cidrs(100))
    assert [op['kind'] for op in plan['stages'][1]['ops']] == ['route_add'] * 100 + ['route_delete'] * 100
    session.shows.clear()
    assert execute(plan, driver, None)['ok']
    # The readback of the adds doubles as the deletes' snapshot.
    assert len(session.static_shows()) <= 8


def test_same_writes_as_one_read_per_operation():
    # A stage mixing adds, adoptions and deletes writes exactly what reading
    # static_rt fresh before every operation would have written.
    adopt = [_route(11 + index, cidr, GATEWAY, 'hand') for index, cidr in enumerate(_cidrs(3, '10.210.0.0/16'))]
    stale = [_route(21 + index, cidr, GATEWAY, 'cf-old', _tagname('rt', cidr))
             for index, cidr in enumerate(_cidrs(3, '10.220.0.0/16'))]
    cidrs = _cidrs(4) + _cidrs(3, '10.210.0.0/16')

    def run(batched):
        session = PagedIKuai(_bulk(5) + adopt + stale, [])
        driver = _driver(session)
        ops = _static_plan(driver, cidrs)['stages'][1]['ops']
        if not batched:
            return session.writes, session.rows['static_rt'], [driver.apply(op) for op in ops]
        with driver.batch():
            undos = [driver.apply(op) for op in ops]
        return session.writes, session.rows['static_rt'], undos

    batched_writes, batched_rows, batched_undos = run(True)
    single_writes, single_rows, single_undos = run(False)
    assert batched_writes == single_writes and batched_rows == single_rows
    assert batched_undos == single_undos
    assert [action for _, action, _ in batched_writes] == ['add'] * 4 + ['edit'] * 3 + ['del'] * 3


# --- undo by name ------------------------------------------------------------------

def _failing_nat(session):
    session.fail = ('nat_rule', 'add')


def test_rollback_removes_every_added_route_with_one_snapshot():
    session = PagedIKuai(_bulk(1500), [])
    _failing_nat(session)
    driver = _driver(session)
    before = copy.deepcopy(session.rows['static_rt'])
    plan = _static_plan(driver, _cidrs(300))
    session.shows.clear()
    result = execute(plan, driver, None)
    assert not result['ok'] and result['failed_stage'] == 'nat_bypass'
    assert [entry['status'] for entry in result['rollback']] == ['ok'] * 300
    assert session.rows['static_rt'] == before
    assert len(session.static_shows()) <= 10


def test_route_added_but_not_read_back_is_still_rolled_back():
    # The readback after the adds times out: the routes exist but no ID was ever read.
    session = PagedIKuai(_bulk(1200), [])
    driver = _driver(session)
    before = copy.deepcopy(session.rows['static_rt'])
    plan = _static_plan(driver, _cidrs(3))
    added = []
    session.on_write = lambda func, action, param, current: added.append(action) if action == 'add' else None

    def timeout_after_adds(func, number, current):
        if func == 'static_rt' and len(added) == 3 and not getattr(current, 'failed', False):
            current.failed = True
            raise requests.Timeout('read timeout')
    session.on_show = timeout_after_adds
    result = execute(plan, driver, None)
    assert not result['ok'] and '等待 iKuai 响应超时' in result['log'][-1]['summary']
    assert [entry['status'] for entry in result['rollback']] == ['ok'] * 3
    assert session.rows['static_rt'] == before


def test_route_whose_reply_was_lost_is_rolled_back():
    session = PagedIKuai(_bulk(10), [])
    driver = _driver(session)
    before = copy.deepcopy(session.rows['static_rt'])
    plan = _static_plan(driver, _cidrs(3))
    count = []

    def lose_second_reply(func, action, param, current):
        count.append(action)
        if len(count) == 2:
            raise requests.Timeout('response lost after mutation')
    session.on_write = lose_second_reply
    result = execute(plan, driver, None)
    assert not result['ok']
    assert [entry['summary'].split('：')[0] for entry in result['rollback']] == ['已撤销'] * 2
    assert session.rows['static_rt'] == before


def test_refused_route_needs_no_undo():
    session = PagedIKuai(_bulk(10), [])
    session.fail = ('static_rt', 'add')
    driver = _driver(session)
    result = execute(_static_plan(driver, _cidrs(3)), driver, None)
    assert not result['ok'] and result['rollback'] == []
    assert '参数错误' in result['log'][-1]['summary']


def test_lost_reply_without_a_new_route_is_nothing_to_undo():
    session = PagedIKuai(_bulk(10), [])
    driver = _driver(session)
    plan = _static_plan(driver, _cidrs(1))
    real_post = session.post

    def drop(url, json=None, **kwargs):
        if json and json.get('action') == 'add':
            raise requests.ConnectionError('reset')
        return real_post(url, json=json, **kwargs)
    session.post = drop
    result = execute(plan, driver, None)
    assert not result['ok']
    assert [entry['status'] for entry in result['rollback']] == ['ok']
    assert len(session.rows['static_rt']) == 10


def test_route_changed_after_adding_is_not_deleted_on_rollback():
    session = PagedIKuai(_bulk(10), [])
    _failing_nat(session)
    driver = _driver(session)
    plan = _static_plan(driver, _cidrs(1))

    def edit_by_hand(func, action, param, current):
        if (func, action) == ('nat_rule', 'add'):
            current.rows['static_rt'][-1]['gateway'] = '10.0.0.99'
    session.on_write = edit_by_hand
    result = execute(plan, driver, None)
    [entry] = result['rollback']
    assert entry['status'] == 'failed' and '已被修改，未自动删除' in entry['summary']
    assert len(session.rows['static_rt']) == 11


def test_policy_rule_added_but_not_read_back_is_rolled_back():
    session = PagedIKuai([], HOME_NATS)
    driver = _driver(session)
    plan = _plan(driver, desired=DESIRED)

    def timeout_after_add(func, number, current):
        if func == 'stream_ipport' and current.rows['stream_ipport'] and not getattr(current, 'failed', False):
            current.failed = True
            raise requests.Timeout('read timeout')
    session.on_show = timeout_after_add
    result = execute(plan, driver, FakeAgent(SAFE_RETURN_STATUS))
    assert not result['ok'] and result['failed_stage'] == 'routes'
    assert result['rollback'][0]['summary'].startswith('已撤销：新增策略路由「cf-mihomo」')
    assert session.rows['stream_ipport'] == []


def test_nat_rule_whose_reply_was_lost_is_rolled_back():
    session = PagedIKuai(_bulk(5), [])
    driver = _driver(session)
    plan = _static_plan(driver, _cidrs(2))

    def lose_nat_reply(func, action, param, current):
        if (func, action) == ('nat_rule', 'add'):
            raise requests.Timeout('response lost after mutation')
    session.on_write = lose_nat_reply
    result = execute(plan, driver, None)
    assert not result['ok'] and result['failed_stage'] == 'nat_bypass'
    assert [entry['stage'] for entry in result['rollback'] if entry['status'] == 'ok'] == ['nat_bypass', 'routes',
                                                                                          'routes']
    assert session.rows['nat_rule'] == [] and len(session.rows['static_rt']) == 5


def test_rollback_reports_restored_routes_it_cannot_read_back():
    managed = [_route(11 + index, cidr, GATEWAY, 'cf-lab', _tagname('rt', cidr))
               for index, cidr in enumerate(_cidrs(2))]
    session = PagedIKuai(_bulk(10) + managed, [])
    driver = _driver(session)
    plan = _static_plan(driver, [])
    seen = {'del': 0, 'failed': False}

    def swallow_readds(func, action, param, current):
        if action == 'del':
            seen['del'] += 1
        if action == 'add':
            current.rows['static_rt'].pop()     # accepted, never stored

    def fail_stage_check(func, number, current):
        if func == 'static_rt' and seen['del'] == 2 and not seen['failed']:
            seen['failed'] = True
            raise requests.Timeout('read timeout')
    session.on_write, session.on_show = swallow_readds, fail_stage_check
    result = execute(plan, driver, None)
    assert not result['ok']
    assert [entry['status'] for entry in result['rollback']] == ['ok', 'ok', 'failed']
    assert result['rollback'][-1]['summary'].startswith('撤销失败：iKuai static_rt 已提交但读不回新对象：')


# --- sessions, replies and request size --------------------------------------------

class ExpiringSession(PagedIKuai):
    def __init__(self, expire):
        super().__init__([], [])
        self.expire = expire
        self.logins = 0

    def post(self, url, json=None, **kwargs):
        if url.endswith('/Action/login'):
            self.logins += 1
        elif json['action'] != 'show' and self.expire:
            self.expire -= 1
            return FakeResponse({'code': 1008, 'message': 'session expired'})
        return super().post(url, json=json, **kwargs)


def test_write_logs_in_again_once_after_session_expiry():
    session = ExpiringSession(expire=1)
    driver = _driver(session)
    driver._write('static_rt', 'add', _route(0, '10.9.0.0/24', tagname='T1'))
    assert session.logins == 2 and len(session.rows['static_rt']) == 1

    session = ExpiringSession(expire=2)
    driver = _driver(session)
    with pytest.raises(RouterError, match='session expired'):
        driver._write('static_rt', 'add', _route(0, '10.9.0.0/24', tagname='T1'))
    assert session.logins == 2 and session.rows['static_rt'] == []


class EmptyReply:
    status_code = 200

    def __init__(self, content):
        self.content = content

    def json(self):
        raise ValueError('no JSON')


@pytest.mark.parametrize('content, message', [(b'', '请求可能过大或被 iKuai 拒绝'),
                                              (b'<html>login</html>', '请确认地址指向管理页面')])
def test_empty_reply_points_at_request_size(content, message):
    driver = IKuaiDriver('http://10.0.0.1', 'admin', 'pw')
    driver.session.post = lambda *args, **kwargs: EmptyReply(content)
    with pytest.raises(RouterError, match=message):
        driver._post('/Action/call', {})


def test_rules_too_large_for_one_request_are_blocked_in_the_preview():
    cidrs = _cidrs(6000, '10.0.0.0/8', 24)
    blockers = oversized_requests([
        {'kind': 'policy_route_add', 'gateway': GATEWAY, 'dst': cidrs, 'comment': 'cf-mihomo'},
        {'kind': 'nat_add', 'src': ['10.0.0.1-10.0.0.3'], 'dst': cidrs, 'comment': 'cf-mihomo'},
        {'kind': 'policy_route_update', 'id': '1', 'gateway': GATEWAY, 'dst': cidrs[:100], 'comment': 'cf-mihomo'},
        {'kind': 'route_add', 'cidr': cidrs[0], 'gateway': GATEWAY, 'comment': 'cf-x'}])
    assert len(blockers) == 2
    assert blockers[0].startswith('iKuai 端口分流「cf-mihomo」需要一次写入 6000 个网段')
    assert blockers[1].startswith('iKuai NAT 免转换规则「cf-mihomo」需要一次写入 6000 个网段')


def test_request_size_limit_matches_what_ikuai_accepts():
    # 4.0.314: 5706 such CIDRs in one 端口分流 add were accepted, 5712 were refused.
    def size(count):
        return request_bytes('stream_ipport', 'add',
                             IKuaiDriver._new_policy('192.168.77.250', _cidrs(count, '10.0.0.0/8', 24), 'cf-sizeprobe'))
    assert size(5706) <= MAX_REQUEST_BYTES < size(5712)


def test_oversized_policy_rule_blocks_the_release_plan():
    desired = {**DESIRED, 'proxy_cidrs': [{'cidr': cidr, 'sources': ['lab']}
                                          for cidr in _cidrs(6000, '10.0.0.0/8', 24)]}
    driver = _driver(StatefulIKuai([], [], policies=MIGRATED))
    plan = _plan(driver, desired=desired)
    assert any(blocker.startswith('iKuai 端口分流「cf-mihomo」') for blocker in plan['blockers'])
    small = _plan(_driver(StatefulIKuai([], [], policies=MIGRATED)))
    assert small['blockers'] == []


def test_router_plan_for_routeros_is_never_size_checked():
    state = _driver(StatefulIKuai([], [], policies=MIGRATED)).read_state()
    desired = {**DESIRED, 'proxy_cidrs': [{'cidr': cidr, 'sources': ['lab']}
                                          for cidr in _cidrs(6000, '10.0.0.0/8', 24)]}
    plan = build_plan({'id': 'r1', 'type': 'routeros', 'endpoint': 'http://10.0.0.1', 'manage_return_routing': True},
                      desired, state, build_report(desired, state), SAFE_RETURN_STATUS, None)
    assert not any('单次请求上限' in blocker for blocker in plan['blockers'])
