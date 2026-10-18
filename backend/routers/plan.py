"""Release plan: turn a drift report into ordered, reviewable operations.

Stages run in this order (see docs/ROUTER_INTEGRATION_DESIGN.md §6.2):

1. ``return_routing`` - when explicitly enabled, the mihomo Agent marks replies
   so they go back through the router. Disabled by default; existing rules stay.
2. ``routes``         - router static routes for proxy CIDRs to the gateway.
3. ``nat_bypass``     - exempt proxy CIDRs from source NAT so mihomo sees client IPs.
4. ``dhcp_dns``       - optionally replace DNS in the gateway's DHCP network.

Manual objects that already do the right thing are adopted (re-commented with the
managed prefix), never recreated. Conflicting routes are skipped and reported.
"""
import hashlib
import ipaddress
import json
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from backend.routers.base import (
    POLICY_AUTO_COMMENT, POLICY_MANUAL_COMMENT, RouterState, as_dict, is_managed, managed_comment, policy_route,
    policy_route_ok, stale_route_warning, stale_routes, StaticRouteIndex,
)
from backend.routers.dhcp_dns import build_dns_report
from backend.routers.report import _networks

MANAGED_RETURN_TABLE = 'configflow_return'
RETURN_SAFETY_VERSION = 1
RETURN_ALLOCATION_VERSION = 1
RETURN_ALLOCATION_FIELDS = ('table', 'priority', 'mark')

STAGE_TITLES = {
    'return_routing': 'mihomo 回流规则',
    'routes': '主路由代理路由',
    'nat_bypass': '主路由 NAT 免转换',
    'dhcp_dns': 'DHCP DNS',
}


def router_ip(endpoint: str) -> Optional[str]:
    host = urlparse(endpoint or '').hostname or ''
    try:
        return str(ipaddress.IPv4Address(host))
    except ValueError:
        return None


def _collapse(values: List[str]) -> List[str]:
    return [str(net) for net in ipaddress.collapse_addresses(_networks(values))]


def return_allocation(status: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Validate the Agent-selected resources without choosing a local fallback."""
    safety = status.get('safety') if isinstance(status, dict) else None
    allocation = safety.get('allocation') if isinstance(safety, dict) else None
    if (not isinstance(allocation, dict)
            or type(allocation.get('version')) is not int
            or allocation['version'] != RETURN_ALLOCATION_VERSION
            or type(allocation.get('reused')) is not bool
            or type(allocation.get('table')) is not int or not 1 <= allocation['table'] <= 252
            or type(allocation.get('priority')) is not int or not 1 <= allocation['priority'] <= 32765):
        return None
    mark = allocation.get('mark')
    if not isinstance(mark, str) or not re.fullmatch(r'0[xX][0-9a-fA-F]{1,8}', mark):
        return None
    number = int(mark, 16)
    if number == 0 or number & (number - 1):
        return None
    return {**allocation, 'mark': f'0x{number:x}'}


def return_safety_blockers(status: Dict[str, Any], *, require_safe: bool = True) -> List[str]:
    """Require the Agent's ownership-aware safety contract, never infer it by version text."""
    safety = status.get('safety') if isinstance(status, dict) else None
    if (not isinstance(safety, dict) or type(safety.get('version')) is not int
            or safety['version'] != RETURN_SAFETY_VERSION):
        return ['mihomo Agent 不支持回流占用安全检查，请先升级 Agent，或关闭自动配置回流后继续发布']
    if (type(safety.get('ok')) is not bool or not isinstance(safety.get('blockers'), list)
            or any(not isinstance(item, str) or not item.strip() for item in safety['blockers'])):
        return ['mihomo Agent 的回流安全检查结果不完整，无法确认规则归属']
    if require_safe and (not safety['ok'] or safety['blockers']):
        return safety['blockers'] or ['mihomo Agent 未确认回流配置可安全使用，停止发布']
    if require_safe and return_allocation(status) is None:
        return ['mihomo Agent 未提供有效的回流资源自动分配结果，请先升级 Agent，或关闭自动配置回流后继续发布']
    return []


def _lan_except(gateway: str) -> List[str]:
    """Source ranges for a new NAT bypass rule: the gateway's /24 without the gateway itself.

    The gateway's own traffic (mihomo DIRECT connections) must keep being NATed.
    """
    address = ipaddress.IPv4Address(gateway)
    network = ipaddress.IPv4Network(f'{gateway}/24', strict=False)
    first, last = network.network_address + 1, network.broadcast_address - 1
    ranges = []
    if address > first:
        ranges.append(f'{first}-{address - 1}')
    if address < last:
        ranges.append(f'{address + 1}-{last}')
    return ranges


def _route_ops(report: Dict[str, Any], state: RouterState, gateway: str, warnings: List[str]):
    ops, skipped = [], set()
    route_index = StaticRouteIndex(state.routes)
    for item in report['routes']['items']:
        cidr = item['cidr']
        if item['status'] == 'missing':
            comment = managed_comment(','.join(item['sources']))
            ops.append({'kind': 'route_add', 'cidr': cidr, 'gateway': gateway, 'comment': comment,
                        'summary': f'新增路由 {cidr} → {gateway}（备注 {comment}）'})
        elif item['status'] == 'adoptable':
            for route in route_index.select(cidr, gateway).candidates:
                if is_managed(route.comment):
                    continue
                comment = managed_comment(route.comment)
                ops.append({'kind': 'route_comment', 'id': route.id, 'cidr': route.dst, 'comment': comment,
                            'summary': f'接管路由 {route.dst}（备注「{route.comment or "无"}」→「{comment}」）'})
        elif item['status'] == 'conflict':
            skipped.add(cidr)
            warnings.append(f'{cidr}：{item["detail"]}。本次跳过该网段（路由、NAT、回流都不处理），请先手动处理')
    for extra in report['routes']['extra']:
        ops.append({'kind': 'route_delete', 'id': extra['route_id'], 'cidr': extra['cidr'],
                    'summary': f'删除不再需要的受管路由 {extra["cidr"]} → {extra["gateway"]}'})
    return ops, skipped


def _policy_op(rule, gateway: str, dst: List[str], comment: str):
    scope = f'除 {gateway} 自身外的设备访问 {", ".join(dst)} → {gateway}'
    if rule is None:
        return {'kind': 'policy_route_add', 'gateway': gateway, 'dst': dst, 'comment': comment,
                'summary': f'新增策略路由「{comment}」：{scope}'}
    if policy_route_ok(rule, gateway) and set(rule.dst) == set(dst):
        return None
    return {'kind': 'policy_route_update', 'id': rule.id, 'gateway': gateway, 'dst': dst, 'comment': comment,
            'summary': f'更新策略路由「{comment}」：{scope}'}


def _policy_route_ops(report: Dict[str, Any], state: RouterState, gateway: str, warnings: List[str]):
    """Route proxy CIDRs by policy rules excluding the gateway, then drop the static routes.

    The rules are written first; static routes take precedence over them, so
    traffic keeps flowing to the gateway until each static route is deleted.
    """
    ops, skipped = [], set()
    for item in report['routes']['items']:
        if item['status'] == 'conflict':
            skipped.add(item['cidr'])
            warnings.append(f'{item["cidr"]}：{item["detail"]}。本次跳过该网段（路由、NAT、回流都不处理），请先手动处理')
    target = [item['cidr'] for item in report['routes']['items'] if item['cidr'] not in skipped]
    covered = _networks(target)
    auto = policy_route(state, POLICY_AUTO_COMMENT)
    checks = []
    if target:
        op = _policy_op(auto, gateway, target, POLICY_AUTO_COMMENT)
        ops.extend([op] if op else [])
        checks.append({'comment': POLICY_AUTO_COMMENT, 'gateway': gateway, 'dst': target})
    elif auto is not None:
        ops.append({'kind': 'policy_route_delete', 'id': auto.id,
                    'summary': f'删除不再需要的策略路由「{POLICY_AUTO_COMMENT}」'})

    def inside(cidr):
        return any(network.subnet_of(outer) for network in _networks([cidr]) for outer in covered)

    # Hand-made routes keep routing to the gateway, minus the loop. Subnets of a
    # derived CIDR need no rule of their own.
    migrate = [item['cidr'] for item in report['routes']['unmanaged_other'] if item.get('migrate') and not inside(item['cidr'])]
    manual = policy_route(state, POLICY_MANUAL_COMMENT)
    # An existing manual rule is repaired too (e.g. a lower priority), keeping its CIDRs.
    if migrate or (manual is not None and not policy_route_ok(manual, gateway)):
        current = list(manual.dst) if manual else []
        dst = current + [cidr for cidr in migrate if cidr not in current]
        op = _policy_op(manual, gateway, dst, POLICY_MANUAL_COMMENT)
        ops.extend([op] if op else [])
        checks.append({'comment': POLICY_MANUAL_COMMENT, 'gateway': gateway, 'dst': dst})

    for route in state.routes:
        if route.enabled and route.gateway == gateway and (route.dst in migrate or inside(route.dst)):
            ops.append({'kind': 'route_delete', 'id': route.id, 'cidr': route.dst,
                        'summary': f'删除静态路由 {route.dst} → {gateway}（{route.comment or "无备注"}），'
                                   '改由策略路由转发，避免 mihomo 自身流量回环'})
    deleting = {op['id'] for op in ops if op['kind'] == 'route_delete'}
    for extra in report['routes']['extra']:
        if extra['route_id'].startswith('policy:') or extra['route_id'] in deleting:
            continue
        ops.append({'kind': 'route_delete', 'id': extra['route_id'], 'cidr': extra['cidr'],
                    'summary': f'删除不再需要的受管路由 {extra["cidr"]} → {extra["gateway"]}'})
    return ops, skipped, checks


def _nat_ops(report: Dict[str, Any], state: RouterState, gateway: str, skipped: set, vendor: str = ''):
    items = [item for item in report['nat_bypass']['items'] if item['cidr'] not in skipped]
    check = (state.ros_policy_plan or {}).get('check') or {}
    migrating = {item['id'] for item in check.get('migrations', [])
                 if item['path'] == '/ip/firewall/nat'}
    rules = {rule.id: rule for rule in state.nat_bypass if rule.enabled and rule.id not in migrating}
    missing = [item['cidr'] for item in items if item['status'] == 'missing']
    adopt_ids: List[str] = []
    for item in items:
        if item['status'] == 'adoptable':
            adopt_ids.extend(rule_id for rule_id in item['rule_ids']
                             if rule_id not in adopt_ids and not is_managed(rules[rule_id].comment))
    managed = [rule for rule in rules.values() if is_managed(rule.comment)]
    target = managed[0] if managed else (rules[adopt_ids[0]] if adopt_ids else None)
    targets = [target] if target else []
    if target and target.attributes and vendor == 'routeros':
        # ROS has one destination/source selector per rule. A single logical
        # bypass can therefore span multiple source fragments; expand them all,
        # preserving the other matchers and the original destination grouping.
        def group(rule):
            return {key: value for key, value in rule.attributes.items()
                    if key != 'src-address'}

        candidates = managed if managed else [rules[rule_id] for rule_id in adopt_ids]
        targets = [rule for rule in candidates if group(rule) == group(target)]
    target_ids = {rule.id for rule in targets}

    ops = []
    for rule_id in adopt_ids:
        rule = rules[rule_id]
        add = missing if rule.id in target_ids else []
        comment = managed_comment(rule.comment)
        extra = f'，并加入 {", ".join(add)}' if add else ''
        ops.append({'kind': 'nat_update', 'id': rule.id, 'add_dst': add, 'comment': comment,
                    'summary': f'接管 NAT 免转换规则「{rule.comment or rule.id}」（备注 → {comment}）{extra}'})
    for target_rule in targets:
        if is_managed(target_rule.comment) and missing:
            ops.append({'kind': 'nat_update', 'id': target_rule.id, 'add_dst': missing, 'comment': target_rule.comment,
                        'summary': f'受管 NAT 免转换规则「{target_rule.comment}」加入 {", ".join(missing)}'
                                   + (f'（源 {", ".join(target_rule.src)}）' if len(targets) > 1 else '')})
    if target is None and missing:
        src = _lan_except(gateway)
        ops.append({'kind': 'nat_add', 'src': src, 'dst': missing, 'comment': 'cf-mihomo',
                    'summary': f'新增 NAT 免转换规则：源 {", ".join(src)}，目的 {", ".join(missing)}'})

    if vendor == 'routeros':
        for op in ops:
            if op['kind'] == 'nat_update':
                # Include the actual matchers in the preview fingerprint, not
                # just the destination-only normalized coverage report.
                op['expected'] = rules[op['id']].attributes

    # When return routing is enabled, include the existing exemption scope too.
    exempt = [cidr for rule in rules.values() for cidr in rule.dst]
    exempt += [item['values']['dst-address'] for item in check.get('replacements', [])
               if item['path'] == '/ip/firewall/nat']
    exempt += [cidr for op in ops for cidr in op.get('add_dst') or op.get('dst') or []]
    return ops, exempt


def _return_op(return_routing: Dict[str, Any], spec: Dict[str, Any]):
    managed = return_routing.get('managed') or {}
    current = (managed.get('spec') or None) if managed.get('table_present') else None
    same = (current and managed.get('table_present')
            and set(current.get('proxy_dst') or []) == set(spec['proxy_dst'])
            and all(current.get(key) == spec[key]
                    for key in ('router_ip', 'mark', 'table', 'priority', 'safety_version')))
    if same:
        return []
    added = sorted(set(spec['proxy_dst']) - set((current or {}).get('proxy_dst') or []))
    parts = [f'应用回流规则（表 inet {MANAGED_RETURN_TABLE}，mark {spec["mark"]}，'
             f'路由表 {spec["table"]} → {spec["router_ip"]}，优先级 {spec["priority"]}）',
             f'标记 {len(spec["proxy_dst"])} 个网段']
    if current and added:
        parts.append(f'新增 {", ".join(added)}')
    return [{'kind': 'return_routing_apply', 'spec': spec, 'previous': current,
             'summary': '；'.join(parts)}]


def fingerprint(stages: List[Dict[str, Any]], nat_evidence=None, dhcp_evidence=None, return_evidence=None) -> str:
    payload = json.dumps({'stages': stages, 'nat_evidence': nat_evidence, 'dhcp_evidence': dhcp_evidence,
                          'return_evidence': return_evidence},
                         sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def build_plan(router: Dict[str, Any], desired: Dict[str, Any], state: RouterState,
               report: Dict[str, Any], return_routing: Dict[str, Any], driver=None) -> Dict[str, Any]:
    warnings = list(desired.get('warnings') or [])
    blockers = []
    manage_return = router.get('manage_return_routing') is True
    gateway = desired.get('proxy_gateway') or ''
    if not gateway:
        blockers.append('未关联 mihomo Agent，无法确定代理路由的下一跳')
    else:
        try:
            ipaddress.IPv4Address(gateway)
        except (ValueError, TypeError):
            blockers.append('代理路由的下一跳不是有效 IPv4 地址，请检查关联 Agent 的主机地址')
    upstream = router_ip(router.get('endpoint')) if manage_return else None
    if manage_return:
        if not upstream:
            blockers.append('路由器地址不是 IPv4，无法确定回流规则的下一跳')
        if return_routing.get('status') != 'ok':
            blockers.append(f'无法读取 mihomo Agent 的回流规则状态：{return_routing.get("message") or "未知错误"}')
        else:
            blockers.extend(return_safety_blockers(return_routing))
    if blockers:
        return {'stages': [], 'blockers': blockers, 'warnings': warnings, 'script': '', 'fingerprint': '',
                'empty': True}

    policy = state.policy_routes is not None
    if policy:
        route_ops, skipped, policy_checks = _policy_route_ops(report, state, gateway, warnings)
    else:
        route_ops, skipped = _route_ops(report, state, gateway, warnings)
    ros_policy = state.ros_policy_plan
    if ros_policy is not None:
        blockers.extend(ros_policy['blockers'])
        warnings.extend(ros_policy['warnings'])
        route_ops.extend(ros_policy['ops'])
        previous = (desired.get('ros_policy', {}).get('previous') or {}).get('spec') or {}
        from backend.routers.ros_policy import subtract
        withdrawn = subtract(_networks(previous.get('destinations', [])),
                             _networks([item['cidr'] for item in desired['ros_policy']['cidrs']]))
        replacements = withdrawn + _networks([
            item['config']['dst-address'] for item in (ros_policy.get('check') or {}).get('migrations', [])
            if item['path'] == '/ip/route'])
        if any(net.overlaps(old) for net in _networks(skipped) for old in replacements):
            blockers.append('替代静态路由存在冲突，不能撤销仍在生效的来源策略，请先处理冲突')
        for op in ros_policy['ops']:
            op['static_checks'] = [{'cidr': item['cidr'], 'gateway': gateway}
                                   for item in desired['proxy_cidrs']
                                   if item['cidr'] not in skipped
                                   and any(net.overlaps(old) for net in _networks([item['cidr']])
                                           for old in replacements)]
    stale = stale_routes(state, gateway)
    if stale:
        warnings.append(stale_route_warning(stale, gateway))
    manage_dns = bool(router.get('dns_agent_id'))
    dns = build_dns_report({**desired, 'dns_agent_id': router.get('dns_agent_id') or ''}, state.dhcp_dns)
    dns_ops = []
    if manage_dns:
        blockers.extend(dns['blockers'])
        if skipped:
            blockers.append('存在代理路由冲突，请先处理冲突再下发 DHCP DNS，避免客户端获取 DNS 后无法访问代理网段')
        if dns['status'] == 'pending':
            item = dns['items'][0]
            entry = next(entry for entry in state.dhcp_dns.entries if entry.id == item['id'])
            before = ', '.join(entry.dns) or '自动/未指定'
            replacement = ('主、备用 DNS 均设为所选服务器' if router.get('type') == 'ikuai'
                           else '替换原 DNS 列表')
            dns_ops.append({'kind': 'dhcp_dns_update', 'id': entry.id, 'dns': item['desired_dns'],
                            'expected': entry.attributes,
                            'scope': {'id': entry.id, 'network': entry.network, 'gateway': gateway},
                            'summary': f'DHCP 网段 {entry.network} DNS：{before} → {dns["target_dns"]}（{replacement}）'})
    nat_items = [item for item in report['nat_bypass']['items'] if item['cidr'] not in skipped]
    blockers.extend(f'{item["cidr"]}：无法确认是否需要 NAT 免转换。{item["detail"]}'
                    for item in nat_items if item['status'] == 'unknown')
    nat_ops, exempt = _nat_ops(report, state, gateway, skipped, router.get('type', ''))
    if router.get('type') == 'routeros' and state.nat_context is not None:
        from backend.routers.routeros_nat import planned_bypass_conflicts
        blockers.extend(planned_bypass_conflicts(nat_ops, state, gateway))
    proxy_cidrs = [entry['cidr'] for entry in desired['proxy_cidrs'] if entry['cidr'] not in skipped]
    # A skipped destination is unchanged, not removed from the Agent's
    # replacement set. Retain exactly its previously protected intersection.
    current_return = (return_routing.get('managed') or {}).get('spec') or {}
    retained_return = [
        str(old if old.subnet_of(target) else target)
        for old in _networks(current_return.get('proxy_dst', []))
        for target in _networks(skipped) if old.overlaps(target)]
    if ros_policy is not None:
        for op in ros_policy['ops']:
            if op.get('static_checks'):
                if nat_ops:
                    from backend.routers.routeros_nat import _egress
                    interface, reason = _egress(op['static_checks'][0]['cidr'], gateway,
                                                state.nat_context or {}, policy_routing=True)
                    if interface is None:
                        blockers.append('无法安全准备替代路径的 LAN NAT 保护：' + reason)
                    else:
                        for preparation in nat_ops:
                            preparation['out_interface'] = interface
                # NAT and return protection must precede activation of the
                # replacement routes, not just withdrawal of the old policy.
                op['summary'] += '；先准备 NAT 保护，再建立并回读替代静态路径'
                op['prepare_static'] = nat_ops + [entry for entry in route_ops if entry is not op]
                route_ops = [op]
                op['static_nat_checks'] = {'gateway': gateway,
                    'nat_bypass': [item['cidr'] for item in nat_items
                                   if item['status'] not in {'not_required', 'unknown'}],
                    'nat_not_required': [{'cidr': item['cidr'], 'gateway': gateway}
                                         for item in nat_items if item['status'] == 'not_required']}
                nat_ops = []
    return_ops = []
    if manage_return:
        allocation = return_allocation(return_routing)
        policy_cidrs = [item['cidr'] for item in desired.get('ros_policy', {}).get('cidrs', [])]
        spec = {'router_ip': upstream, 'proxy_dst': _collapse(proxy_cidrs + policy_cidrs + exempt + retained_return),
                **{key: allocation[key] for key in RETURN_ALLOCATION_FIELDS},
                'safety_version': RETURN_SAFETY_VERSION}
        return_ops = _return_op(return_routing, spec)

    manual = driver is not None and not driver.supports_write
    stages = [
        {'key': 'return_routing', 'title': STAGE_TITLES['return_routing'], 'manual': False, 'ops': return_ops,
         'enabled': manage_return,
         'note': ('已启用自动配置回流；自动选择安全可用的编号和标记，不接管已有手工规则。' if manage_return
                  else '未启用自动配置回流：本次跳过，已有回流规则保留，不新增、修改或删除。')},
        {'key': 'routes', 'title': STAGE_TITLES['routes'], 'manual': manual, 'ops': route_ops,
         **({'note': '代理网段使用排除 mihomo 自身的端口分流转发：静态路由只看目的地址，'
                     '会把 mihomo 自己的直连流量送回 mihomo 形成回环。先写入分流规则，再删除静态路由。'}
            if policy else {})},
        {'key': 'nat_bypass', 'title': STAGE_TITLES['nat_bypass'], 'manual': manual, 'ops': nat_ops},
        {'key': 'dhcp_dns', 'title': STAGE_TITLES['dhcp_dns'], 'manual': manual, 'ops': dns_ops,
         'enabled': manage_dns,
         'note': ('仅更新所选 mihomo 网关所在的唯一 DHCP 网段；替换原 DNS 列表，客户端续租或重新连接后生效。'
                  if manage_dns else '未选择 DHCP DNS 服务器：本次保留现有 DNS 配置。')},
    ]
    checks = {
        'gateway': gateway,
        'routes': [] if policy else [{'cidr': cidr, 'gateway': gateway} for cidr in proxy_cidrs],
        'removed_routes': [entry['id'] for op in route_ops for entry in op.get('prepare_static', [op])
                           if entry['kind'] == 'route_delete'],
        'nat_bypass': [item['cidr'] for item in nat_items if item['status'] not in {'not_required', 'unknown'}],
        'nat_not_required': [{'cidr': item['cidr'], 'gateway': gateway}
                             for item in nat_items if item['status'] == 'not_required'],
    }
    if policy:
        checks['policy_routes'] = policy_checks
    if ros_policy is not None:
        checks['ros_policy'] = ros_policy['check']
    if manage_return:
        checks['return_routing'] = dict(spec)
    if manage_dns and dns['status'] in {'synced', 'pending'}:
        item = dns['items'][0]
        checks['dhcp_dns'] = {'id': item['id'], 'network': item['network'], 'gateway': gateway,
                              'dns': item['desired_dns']}
    script = driver.render_script(route_ops + nat_ops + dns_ops) if manual else ''
    evidence = {'context': state.nat_context, 'address_sets': state.address_sets} if state.nat_context is not None else None
    if ros_policy is not None:
        evidence = {'nat': evidence, 'ros_policy': ros_policy.get('evidence'), 'request': ros_policy.get('request')}
    dns_evidence = ({'agent_id': router['dns_agent_id'], 'target_dns': dns['target_dns'], 'gateway': gateway,
                     'state': as_dict(state.dhcp_dns)} if manage_dns else None)
    return {'stages': stages, 'checks': checks, 'blockers': blockers, 'warnings': warnings, 'script': script,
            'fingerprint': fingerprint(stages, evidence, dns_evidence,
                                       return_routing['safety'] if manage_return else None),
            'empty': not any(stage['ops'] for stage in stages)}
