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
from backend.routers.plan import (
    RETURN_ALLOCATION_FIELDS, RETURN_SAFETY_VERSION, return_allocation, return_safety_blockers,
)
from backend.routers.report import _networks
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


def _apply(op: Dict[str, Any], driver, agent: Optional[AgentReturnRouting]) -> Dict[str, Any]:
    from backend.routers import ros_address_lists
    # Any operation may change what earlier reads proved.
    ros_address_lists.invalidate(driver)
    kind = op['kind']
    if kind in {'return_routing_apply', 'return_routing_remove'} and agent is None:
        raise RouterError('未启用 Agent 回流配置，无法执行回流操作')
    if kind == 'return_routing_apply':
        spec = op['spec']
        if (type(spec.get('safety_version')) is not int or spec['safety_version'] != RETURN_SAFETY_VERSION
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


def _covered(cidr: str, state: RouterState) -> bool:
    target = ipaddress.IPv4Network(cidr)
    return any(rule.enabled and is_managed(rule.comment) and any(target.subnet_of(net) for net in _networks(rule.dst))
               for rule in state.nat_bypass)


def verify_nat(checks: Dict[str, Any], state: RouterState) -> None:
    """Verify the same NAT prerequisites during preparation and final readback."""
    route_index = RouteEgressIndex(state.nat_context)
    uncovered = [cidr for cidr in checks['nat_bypass']
                 if (assess_bypass(cidr, checks.get('gateway', ''), state, route_index=route_index)['status'] not in ('synced', 'not_required')
                     if state.nat_context is not None else not _covered(cidr, state))]
    if uncovered:
        raise RouterError(f'回读校验失败：{", ".join(uncovered)} 没有被受管 NAT 免转换规则覆盖')
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


def _verify(stage: str, checks: Dict[str, Any], driver, agent: AgentReturnRouting) -> None:
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
        if set(actual.get('proxy_dst') or []) != set(expected['proxy_dst']):
            raise ReleaseError('Agent 上的回流网段与计划不一致')
        if any(actual.get(key) != expected[key]
               for key in ('router_ip', 'mark', 'table', 'priority', 'safety_version')):
            raise ReleaseError('Agent 上的回流路由或安全握手与计划不一致')
        if not any(_return_rule_matches(rule, expected) for rule in status.get('fwmark_rules') or []):
            raise ReleaseError('没有找到与计划一致的回流策略路由（ip rule）')
        return
    state = driver.read_state()
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
            if rule is None or not policy_route_ok(rule, check['gateway']) or set(rule.dst) != set(check['dst']):
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
    elif stage == 'nat_bypass':
        verify_nat(checks, state)


def _batch(driver, rollback: bool = False):
    """A driver may share its reads across one stage's writes (iKuai static routes)."""
    batch = getattr(type(driver), 'batch', None)
    return driver.batch(rollback=rollback) if callable(batch) else nullcontext()


def _rollback(done: List[Tuple[str, Dict[str, Any], Dict[str, Any]]], driver, agent) -> List[Dict[str, Any]]:
    results = []
    try:
        with _batch(driver, rollback=True):
            for stage, op, undo in reversed(done):
                if not undo:
                    continue
                try:
                    _apply(undo, driver, agent)
                    results.append({'stage': stage, 'summary': f'已撤销：{op["summary"]}', 'status': 'ok'})
                except (RouterError, RuntimeError, requests.RequestException) as error:
                    results.append({'stage': stage, 'summary': f'撤销失败：{op["summary"]}（{error}）',
                                    'status': 'failed'})
    except (RouterError, RuntimeError, requests.RequestException) as error:
        # Routes put back by the rollback are read back together at its end.
        results.append({'stage': done[0][0], 'summary': f'撤销失败：{error}', 'status': 'failed'})
    return results


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
                    _verify(current, plan['checks'], driver, agent)
                    record({'stage': current, 'summary': '回读确认来源策略路由与计划一致', 'status': 'verified'})
                if current == 'return_routing' and plan.get('checks', {}).get('return_routing'):
                    _verify(current, plan['checks'], driver, agent)
                    record({'stage': current, 'summary': '回读确认受管回流配置与占用安全检查通过', 'status': 'verified'})
                elif current == 'nat_bypass' and plan.get('checks', {}).get('nat_not_required'):
                    _verify(current, plan['checks'], driver, agent)
                    record({'stage': current, 'summary': '回读确认无需额外 NAT 免转换', 'status': 'verified'})
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
            if current == 'dhcp_dns':
                # DNS can start directing clients to proxy destinations as soon
                # as a lease renews. Recheck its scope and forwarding prerequisites
                # immediately before advertising it, including unchanged routes.
                _verify('routes', plan['checks'], driver, agent)
                _verify('nat_bypass', plan['checks'], driver, agent)
                checked_dns_target(plan['checks']['dhcp_dns'], driver.read_dhcp_dns())
            if current == 'dns_guard' and plan['checks']['dns_guard']['roles']:
                # Hijacked clients receive fake-ip answers at once; the proxy
                # routes and their NAT protection must already be in place.
                _verify('routes', plan['checks'], driver, agent)
                _verify('nat_bypass', plan['checks'], driver, agent)
            with _batch(driver):
                for op in stage['ops']:
                    try:
                        undo = _apply(op, driver, agent)
                    except RouterError as error:
                        # Written before failing (e.g. a lost reply): the driver says how to undo it.
                        if getattr(error, 'undo', None):
                            done.append((current, op, error.undo))
                        raise
                    done.append((current, op, undo))
                    record({'stage': current, 'summary': op['summary'], 'status': 'ok'})
            _verify(current, plan['checks'], driver, agent)
            record({'stage': current, 'summary': '回读校验通过', 'status': 'verified'})
            if current == 'routes':
                # Config readback cannot see kernel leftovers; the active table can.
                # Rolling back would not remove them either, so report, don't fail.
                stale = stale_routes(driver.read_state(), plan['checks'].get('gateway') or '')
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
