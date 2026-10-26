"""Execute a release plan stage by stage, verify each stage, roll back in reverse on failure."""
import ipaddress
from contextlib import nullcontext
from typing import Any, Dict, List, Optional, Tuple

import requests

from backend.routers.base import (
    RouterError, RouterRollbackError, RouterState, is_managed, policy_route, policy_route_ok, stale_route_warning,
    stale_routes, StaticRouteIndex,
)
from backend.routers.dhcp_dns import checked_dns_target, verify_dns
from backend.routers.ikuai_groups import canon, verify as verify_groups
from backend.routers.plan import (
    RETURN_ALLOCATION_FIELDS, RETURN_PROTOCOLS, return_allocation, return_protocol,
    return_safety_blockers,
)
from backend.routers.report import _networks, SupernetIndex
from backend.routers.routeros_nat import assess_bypass, RouteEgressIndex


class ReleaseError(Exception):
    pass


class AgentReturnRouting:
    """Return-routing calls on the proxy gateway's Agent."""

    def __init__(self, manager, agent: Dict[str, Any]):
        self.manager = manager
        self.agent = agent

    def apply(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        return self.manager.return_routing_apply(self.agent, spec)

    def remove(self, restore_legacy: bool) -> Dict[str, Any]:
        return self.manager.return_routing_remove(self.agent, restore_legacy)

    def status(self) -> Dict[str, Any]:
        return self.manager.return_routing_status(self.agent)


def _check_return_allocation(status: Dict[str, Any], spec: Dict[str, Any]) -> None:
    blockers = return_safety_blockers(status)
    if blockers:
        raise RouterError('；'.join(blockers))
    allocation = return_allocation(status)
    if any(allocation[key] != spec.get(key) for key in RETURN_ALLOCATION_FIELDS):
        raise RouterError('回流资源分配在预览后发生变化，请重新预览后发布')
    if (spec.get('safety_version') or 0) > return_protocol(status):
        raise RouterError('mihomo Agent 不支持按来源标记的回流规则，请重新预览后发布')


def _apply(op: Dict[str, Any], driver, agent: Optional[AgentReturnRouting]) -> Dict[str, Any]:
    from backend.routers import ros_address_lists
    # Any operation may change what earlier reads proved.
    ros_address_lists.invalidate(driver)
    kind = op['kind']
    if kind in {'return_routing_apply', 'return_routing_remove'} and agent is None:
        raise RouterError('未启用 Agent 回流配置，无法执行回流操作')
    if kind == 'return_routing_apply':
        spec = op['spec']
        if (type(spec.get('safety_version')) is not int or spec['safety_version'] not in RETURN_PROTOCOLS
                or spec.get('adopt_legacy')):
            raise RouterError('回流操作缺少安全握手或包含手工规则接管，请重新预览')
        _check_return_allocation(agent.status(), spec)
        agent.apply(op['spec'])
        if op.get('previous'):
            return {'kind': 'return_routing_apply', 'spec': op['previous'], 'previous': None}
        return {'kind': 'return_routing_remove', 'restore_legacy': False}
    if kind == 'return_routing_remove':
        # Occupancy can change after a write. A new Agent can still remove only
        # its own objects; an old Agent's broad cleanup must never be invoked.
        blockers = return_safety_blockers(agent.status(), require_safe=False)
        if blockers:
            raise RouterError('；'.join(blockers))
        agent.remove(op['restore_legacy'])
        if op.get('previous'):
            # A later failure rolls the release back: rules go back with the routes they serve.
            return {'kind': 'return_routing_apply', 'spec': op['previous'], 'previous': None}
        return {}
    return driver.apply(op)


def _covered(cidr: str, state: RouterState, cache: Dict[str, SupernetIndex]) -> bool:
    target = ipaddress.IPv4Network(cidr)
    if 'index' not in cache:
        # Parse managed destinations once per readback, not once per CIDR.
        cache['index'] = SupernetIndex(net for rule in state.nat_bypass if rule.enabled and is_managed(rule.comment)
                                       for net in _networks(rule.dst))
    return cache['index'].covers(target)


def verify_nat(checks: Dict[str, Any], state: RouterState) -> None:
    """Verify the same NAT prerequisites during preparation and final readback."""
    route_index = RouteEgressIndex(state.nat_context)
    nat_index: Dict[str, SupernetIndex] = {}
    uncovered = [cidr for cidr in checks['nat_bypass']
                 if (assess_bypass(cidr, checks.get('gateway', ''), state, route_index=route_index)['status'] not in ('synced', 'not_required')
                     if state.nat_context is not None else not _covered(cidr, state, nat_index))]
    if uncovered:
        raise RouterError(f'回读校验失败：{", ".join(uncovered)} 没有被受管 NAT 免转换规则覆盖')
    if checks.get('nat_list') is not None:
        from backend.routers.routeros_nat_list import verify
        verify(checks['nat_list'], state)
    for check in checks.get('nat_not_required') or []:
        # Re-prove with the preview's assessment: the egress-only check alone
        # rejects LAN-egress rules (e.g. DNS hairpins) that the ordered-chain
        # simulation shows cannot translate client traffic to this CIDR.
        assessment = assess_bypass(check['cidr'], check['gateway'], state, route_index=route_index)
        if assessment['status'] != 'not_required':
            raise RouterError(f'回读校验失败：{check["cidr"]} 无法继续确认无需 NAT 免转换。{assessment["detail"]}')


def _return_rule_matches(rule: Dict[str, Any], expected: Dict[str, Any]) -> bool:
    """iproute2 may print marks in decimal or padded hexadecimal form."""
    def number(value):
        if type(value) is int:
            return value
        if not isinstance(value, str) or not value:
            raise ValueError('invalid policy rule number')
        return int(value, 16 if value.lower().startswith('0x') else 10)

    try:
        mark, mask = str(rule.get('mark', '')).split('/')
        return (number(rule.get('priority')) == expected['priority']
                and number(rule.get('table')) == expected['table']
                and number(mark) == number(mask) == number(expected['mark']))
    except (TypeError, ValueError, AttributeError):
        return False


def _verify(stage: str, checks: Dict[str, Any], driver, agent: AgentReturnRouting,
            state: Optional[RouterState] = None) -> Optional[RouterState]:
    """Read back one stage. ``state`` reuses a read made with no write since;
    routes and NAT return the state they checked."""
    if stage == 'dns_guard':
        from backend.routers.dns_guard import verify
        verify(driver, checks['dns_guard'])
        return
    if stage == 'dhcp_dns':
        verify_dns(checks['dhcp_dns'], driver.read_dhcp_dns())
        return
    if stage == 'return_cleanup':
        managed = agent.status().get('managed') or {}
        if managed.get('active') is True or managed.get('table_present') is True:
            raise ReleaseError('回读校验失败：Agent 上仍有受管的回流规则')
        return
    if stage == 'return_routing':
        status = agent.status()
        expected = checks['return_routing']
        _check_return_allocation(status, expected)
        managed = status.get('managed') or {}
        if not managed.get('table_present'):
            raise ReleaseError('Agent 上没有找到 configflow_return 表')
        actual = managed.get('spec') or {}
        if set(actual.get('proxy_dst') or []) != set(expected.get('proxy_dst') or []):
            raise ReleaseError('Agent 上的回流网段与计划不一致')
        if any(actual.get(key) != expected[key]
               for key in ('router_ip', 'mark', 'table', 'priority', 'safety_version')):
            raise ReleaseError('Agent 上的回流路由或安全握手与计划不一致')
        if not any(_return_rule_matches(rule, expected) for rule in status.get('fwmark_rules') or []):
            raise ReleaseError('没有找到与计划一致的回流策略路由（ip rule）')
        return
    state = driver.read_state() if state is None else state
    if stage == 'routes':
        if checks.get('ros_policy') is not None:
            from backend.routers.ros_policy_write import verify
            verify(driver, checks['ros_policy'])
        route_index = StaticRouteIndex(state.routes)
        for check in checks['routes']:
            candidates = route_index.select(check['cidr'], check['gateway']).candidates
            if not candidates or not all(is_managed(route.comment) for route in candidates):
                raise ReleaseError(f'回读校验失败：{check["cidr"]} 没有有效且无冲突的受管主路由指向 {check["gateway"]}')
        for check in checks.get('policy_routes') or []:
            rule = policy_route(state, check['comment'])
            # iKuai may read an IP group's 1.1.1.1/32 back as 1.1.1.1.
            if (rule is None or not policy_route_ok(rule, check['gateway'])
                    or {canon(cidr) for cidr in rule.dst} != {canon(cidr) for cidr in check['dst']}):
                raise ReleaseError(f'回读校验失败：策略路由「{check["comment"]}」没有按计划排除 {check["gateway"]} '
                                   f'并转发到 {check["gateway"]}')
        remaining = {route.id for route in state.routes} & set(checks['removed_routes'])
        if remaining:
            raise ReleaseError('回读校验失败：应删除的路由仍然存在')
        for check in checks.get('restored_routes') or []:
            if not any(route.dst == check['cidr'] and route.gateway == check['gateway'] and route.enabled
                       for route in state.routes):
                raise ReleaseError(f'回读校验失败：没有找到恢复的静态路由 {check["cidr"]} → {check["gateway"]}')
        if {rule.id for rule in state.policy_routes or []} & set(checks.get('removed_policy_routes') or []):
            raise ReleaseError('回读校验失败：应删除的端口分流规则仍然存在')
        if checks.get('ikuai_groups'):
            verify_groups(checks['ikuai_groups']['routes'], state)
    elif stage == 'nat_bypass':
        verify_nat(checks, state)
        if checks.get('ikuai_groups'):
            verify_groups(checks['ikuai_groups']['nat_bypass'], state)
    return state


def _batch(driver, rollback: bool = False):
    """A driver may share its reads across one stage's writes (iKuai static routes)."""
    batch = getattr(type(driver), 'batch', None)
    return driver.batch(rollback=rollback) if callable(batch) else nullcontext()


def _rollback(done: List[Tuple[str, Dict[str, Any], Dict[str, Any]]], driver, agent) -> List[Dict[str, Any]]:
    from backend.routers import ros_address_lists
    results = []
    try:
        with _batch(driver, rollback=True):
            entries = [entry for entry in reversed(done) if entry[2]]
            batchable = getattr(driver, 'batch_restorable', lambda undo: False)
            index = 0
            while index < len(entries):
                end = index
                while end < len(entries) and batchable(entries[end][2]):
                    end += 1
                if end - index > 1:
                    # Consecutive single-route undo records run in batches, each with
                    # its own result, exactly as one-by-one rollback reports them.
                    run = entries[index:end]
                    ros_address_lists.invalidate(driver)
                    try:
                        errors = driver.restore_many([undo for _, _, undo in run])
                    except (RouterError, RuntimeError, requests.RequestException) as error:
                        # restore_many reports each record; should it still fail, part
                        # of the run may be done. Running it again would report routes
                        # it recreated as foreign, so keep it for inspection instead.
                        errors = [str(error)] * len(run)
                    for (stage, op, _), error in zip(run, errors):
                        results.append({'stage': stage, 'summary': f'撤销失败：{op["summary"]}（{error}）', 'status': 'failed'}
                                       if error else {'stage': stage, 'summary': f'已撤销：{op["summary"]}', 'status': 'ok'})
                    index = end
                    continue
                end = index + 1
                for stage, op, undo in entries[index:end]:
                    try:
                        _apply(undo, driver, agent)
                        results.append({'stage': stage, 'summary': f'已撤销：{op["summary"]}', 'status': 'ok'})
                    except (RouterError, RuntimeError, requests.RequestException) as error:
                        results.append({'stage': stage, 'summary': f'撤销失败：{op["summary"]}（{error}）', 'status': 'failed'})
                index = end
    except (RouterError, RuntimeError, requests.RequestException) as error:
        # Routes put back by the rollback are read back together at its end.
        results.append({'stage': done[0][0], 'summary': f'撤销失败：{error}', 'status': 'failed'})
    return results


def _apply_stage(ops: List[Dict[str, Any]], driver, agent, finished) -> None:
    """Apply ops in order; ``finished(op, undo)`` each one as soon as it is done
    (``ok=False`` for one written before it failed)."""
    from backend.routers import ros_address_lists
    batch_kinds = getattr(driver, 'batch_kinds', frozenset())
    index = 0
    while index < len(ops):
        end = index + 1
        while ops[index]['kind'] in batch_kinds and end < len(ops) and ops[end]['kind'] == ops[index]['kind']:
            end += 1
        if end - index > 1:
            ros_address_lists.invalidate(driver)
            driver.apply_many(ops[index:end], finished)
        else:
            try:
                undo = _apply(ops[index], driver, agent)
            except RouterError as error:
                # Written before failing (e.g. a lost reply): the driver says how to undo it.
                if getattr(error, 'undo', None):
                    finished(ops[index], error.undo, ok=False)
                raise
            finished(ops[index], undo)
        index = end


def execute(plan: Dict[str, Any], driver, agent: Optional[AgentReturnRouting]) -> Dict[str, Any]:
    from backend.routers import ros_address_lists
    # Consecutive readbacks between operations share one proof of each list.
    with ros_address_lists.reuse(driver):
        return _execute(plan, driver, agent)


def _execute(plan: Dict[str, Any], driver, agent: Optional[AgentReturnRouting]) -> Dict[str, Any]:
    log: List[Dict[str, Any]] = []
    done: List[Tuple[str, Dict[str, Any], Dict[str, Any]]] = []
    record = log.append

    current = ''
    # The last routes/NAT read-back while nothing has been written since; a
    # no-op NAT check right after the routes stage needs no second table read.
    unchanged = None
    try:
        if plan.get('blockers'):
            raise ReleaseError('发布计划存在阻止项：' + '；'.join(plan['blockers']))
        for stage in plan['stages']:
            current = stage['key']
            if stage.get('enabled') is False:
                if stage['ops']:
                    raise ReleaseError('未启用的发布阶段包含写入操作，请重新预览')
                continue
            if not stage['ops']:
                if current == 'routes' and plan.get('checks', {}).get('ros_policy') is not None:
                    unchanged = _verify(current, plan['checks'], driver, agent)
                    record({'stage': current, 'summary': '回读确认来源策略路由与计划一致', 'status': 'verified'})
                if current == 'return_routing' and plan.get('checks', {}).get('return_routing'):
                    _verify(current, plan['checks'], driver, agent)
                    record({'stage': current, 'summary': '回读确认受管回流配置与占用安全检查通过', 'status': 'verified'})
                elif current == 'nat_bypass' and plan.get('checks', {}).get('nat_not_required'):
                    unchanged = _verify(current, plan['checks'], driver, agent, unchanged)
                    record({'stage': current, 'summary': '回读确认无需额外 NAT 免转换', 'status': 'verified'})
                elif current == 'nat_bypass' and plan.get('checks', {}).get('nat_list') is not None:
                    # List removals ran in the routes stage: prove the NAT stage all the same.
                    unchanged = _verify(current, plan['checks'], driver, agent, unchanged)
                    record({'stage': current, 'summary': '回读确认 NAT 免转换与计划一致', 'status': 'verified'})
                elif current == 'dhcp_dns' and plan.get('checks', {}).get('dhcp_dns'):
                    _verify(current, plan['checks'], driver, agent)
                    record({'stage': current, 'summary': '回读确认 DHCP DNS 与所选服务器一致', 'status': 'verified'})
                elif current == 'dns_guard' and plan.get('checks', {}).get('dns_guard', {}).get('roles'):
                    _verify(current, plan['checks'], driver, agent)
                    record({'stage': current, 'summary': '回读确认强制 DNS 规则与计划一致', 'status': 'verified'})
                continue
            if stage['manual']:
                record({'stage': current, 'summary': '需要在路由器上手动导入脚本', 'status': 'manual'})
                continue
            ops = stage['ops']
            if current == 'dhcp_dns':
                # DNS can start directing clients to proxy destinations as soon
                # as a lease renews. Recheck its scope and forwarding prerequisites
                # immediately before advertising it, including unchanged routes.
                # Both checks share one read: nothing is written in between.
                state = _verify('routes', plan['checks'], driver, agent)
                _verify('nat_bypass', plan['checks'], driver, agent, state)
                dhcp = driver.read_dhcp_dns()
                checked_dns_target(plan['checks']['dhcp_dns'], dhcp)
                # The update checks its target again; the read just made is current.
                if getattr(driver, 'vendor', '') == 'routeros':
                    ops = [{**op, 'checked_dhcp_dns': dhcp} if index == 0 and op['kind'] == 'dhcp_dns_update'
                           else op for index, op in enumerate(ops)]
            if current == 'dns_guard' and plan['checks']['dns_guard']['roles']:
                # Hijacked clients receive fake-ip answers at once; the proxy
                # routes and their NAT protection must already be in place.
                state = _verify('routes', plan['checks'], driver, agent)
                _verify('nat_bypass', plan['checks'], driver, agent, state)

            def finished(op, undo, ok=True):
                op = {key: value for key, value in op.items() if key != 'checked_dhcp_dns'}
                done.append((current, op, undo))
                if ok:
                    record({'stage': current, 'summary': op['summary'], 'status': 'ok'})

            unchanged = None
            with _batch(driver):
                _apply_stage(ops, driver, agent, finished)
            unchanged = state = _verify(current, plan['checks'], driver, agent)
            record({'stage': current, 'summary': '回读校验通过', 'status': 'verified'})
            if current == 'routes':
                # Config readback cannot see kernel leftovers; the active table can.
                # Rolling back would not remove them either, so report, don't fail.
                # The routes read-back above is that table, with nothing written since.
                stale = stale_routes(state, plan['checks'].get('gateway') or '')
                if stale:
                    record({'stage': current, 'summary': stale_route_warning(stale, plan['checks']['gateway']),
                            'status': 'warning'})
    except (ReleaseError, RouterError, RuntimeError, requests.RequestException) as error:
        record({'stage': current, 'summary': str(error) or error.__class__.__name__, 'status': 'failed'})
        rollback = []
        if isinstance(error, RouterRollbackError):
            rollback.append({'stage': current, 'summary': str(error), 'status': 'failed'})
        rollback.extend(_rollback(done, driver, agent))
        return {'ok': False, 'failed_stage': current, 'log': log, 'rollback': rollback}
    return {'ok': True, 'log': log, 'rollback': []}
