"""Manifest-owned IPv6 routes for the public IP-CIDR6 entries of static-route rule sets.

RouterOS only. While Mihomo uses IPv6 (top-level ``ipv6``, default on) the
public IPv6 networks of the rule sets selected for static routes are routed to
the Mihomo gateway's IPv6 next hop, chosen exactly as for IPv6 Fake-IP
(``ipv6_readiness._gateway``: a unique link-local address scoped to the LAN,
or without one a unique ULA). Turning ``ipv6`` off withdraws them.

Ownership is the manifest (``ipv6_routes_state``): device IDs plus the full
stable configuration of each route this feature created. Routes of others
are never taken over: one already pointing at the gateway is borrowed, any
other route to the same destination keeps that destination out.

Unlike a blocker, a reason this item cannot be applied (``skip``) leaves the
IPv6 routes as they are and lets the rest of the release go ahead, as before
this feature existed. Writes go in batches (``routeros_batch``) and are
compensated from their own undo records.
"""
import copy
import ipaddress

import yaml

from backend.routers.base import RouterError, RouterRollbackError
from backend.routers.ipv6_fakeip import _correct, evidence, snapshot, stable
from backend.routers.ipv6_readiness import _gateway, _lan, _network, _same_gateway, enabled, yes

ROUTES = '/ipv6/route'
FILTER = '/ipv6/firewall/filter'
ADDRESS_LISTS = '/ipv6/firewall/address-list'
PREFIX = 'cf-rule6:'
VERSION = 1
BAD_FLAGS = ('blackhole', 'unreachable', 'prohibit', 'invalid', 'dynamic', 'connect')
SKIPPED = 'IPv6 规则集路由本次跳过：'


def settings(config):
    """Mihomo's top-level ``ipv6`` (Mihomo's default: on) and whether TUN can receive it."""
    result = {'enabled': False, 'tun_enabled': False, 'error': ''}
    try:
        raw = (config.get('mihomo') or {}).get('custom_config') or ''
        document = yaml.safe_load(raw) if raw.strip() else {}
        if not isinstance(document, dict):
            raise ValueError('基础 YAML 必须是对象')
        value = document.get('ipv6', True)
        if type(value) is not bool:
            raise ValueError('ipv6 必须填写 true 或 false')
        tun = document.get('tun') or {}
        result.update(enabled=value, tun_enabled=isinstance(tun, dict) and tun.get('enable') is True)
    except (yaml.YAMLError, ValueError, TypeError, AttributeError) as error:
        result['error'] = 'Mihomo 的 IPv6 设置无法确认，请检查基础 YAML：' + str(error)
    return result


def relevant(config, router, desired):
    """Present only while there is something to route or still something owned."""
    if router.get('type') != 'routeros':
        return False
    if router.get('ipv6_routes_state'):
        return True
    setting = settings(config)
    return bool(desired.get('ipv6_cidrs')) and (setting['enabled'] or bool(setting['error']))


def needs_host(config, router, desired):
    """The Mihomo host is asked for its addresses only when routes may be added."""
    return relevant(config, router, desired) and settings(config)['enabled'] and bool(desired.get('ipv6_cidrs'))


def _key(value):
    network = _network(value)
    return str(network) if network is not None and network.version == 6 else None


def owned_entries(previous, live):
    """Manifest entries whose route still exists, each proven unchanged."""
    if not previous:
        return []
    if (not isinstance(previous, dict) or previous.get('version') != VERSION
            or not isinstance(previous.get('resources'), list) or not isinstance(previous.get('gateway'), str)):
        raise RouterError('IPv6 规则集路由的管理记录无效，保留现有路由，请核对归属')
    rows = {}
    for row in live['routes']:
        rows.setdefault(str(row.get('.id')), []).append(row)
    owned, seen = [], set()
    for entry in previous['resources']:
        if (not isinstance(entry, dict) or not isinstance(entry.get('id'), str) or entry.get('path') != ROUTES
                or not isinstance(entry.get('config'), dict) or _key(entry['config'].get('dst-address')) is None):
            raise RouterError('IPv6 规则集路由的管理记录缺少完整对象信息，保留现有路由')
        if entry['id'] in seen or len(rows.get(entry['id'], [])) > 1:
            raise RouterError('IPv6 规则集路由对象 ID 不唯一，保留现有路由')
        seen.add(entry['id'])
        if entry['id'] not in rows:
            continue  # deleted by someone else: no longer ours to remove
        row = rows[entry['id']][0]
        if stable(row) != entry['config']:
            raise RouterError(f'IPv6 规则集受管路由 {entry["config"].get("dst-address")} 被外部修改，'
                              '保留现场；请改回原值或删除这条路由后重新预览')
        if yes(row.get('dynamic')) or yes(row.get('connect')):
            raise RouterError('IPv6 规则集路由的管理记录指向动态路由，保留现有路由')
        owned.append(entry)
    return owned


def _configured(row, gateway):
    """This route sends its destination to ``gateway``; it may still be inactive."""
    return (row.get('routing-table', 'main') == 'main' and enabled(row)
            and not any(yes(row.get(key)) for key in BAD_FLAGS) and _same_gateway(row, gateway))


def _scoped(live, cidrs, ids):
    """Rows the plan depends on: its destinations and its own objects, plus the addresses."""
    return {'routes': [row for row in live['routes'] if _key(row.get('dst-address')) in cidrs
                       or str(row.get('.id')) in ids],
            'addresses': live.get('addresses', []), 'ipv4_addresses': live.get('ipv4_addresses', [])}


def _invalid_drop(row):
    states = str(row.get('connection-state') or '')
    return (row.get('chain') == 'forward' and row.get('action') == 'drop' and enabled(row)
            and 'invalid' in states.split(',') and not states.startswith('!'))


def hairpin(driver, lan, cidr):
    """Warn when the forward chain drops the hairpin's later packets as invalid.

    Clients reach Mihomo through the router's own LAN port, but Mihomo answers
    them directly. RouterOS then sees only the SYN; the client's next packets
    are ``invalid`` and a default ``drop invalid`` stalls every new connection
    (VM151, RouterOS 7.25: 6.7-7.4 s each). Read-only: returns (warning, command).
    """
    from urllib.parse import quote
    from backend.routers.ipv6_policy import evaluate_chain
    try:
        rows = driver._list(FILTER)
        snapshot = {'filter': rows, 'interface_lists': driver._list('/interface/list'),
                    'interface_members': driver._list('/interface/list/member'),
                    'interfaces': driver._list('/interface'), 'address_lists': []}
        names = {str(row[field]).lstrip('!') for row in rows if row.get('chain') == 'forward'
                 for field in ('src-address-list', 'dst-address-list') if row.get(field)}
        for name in sorted(names):
            snapshot['address_lists'].extend(driver._list(ADDRESS_LISTS + '?list=' + quote(name, safe='')))
    except RouterError:
        return '未能读取 RouterOS IPv6 防火墙，无法确认转给 Mihomo 的连接是否会被 forward 链丢弃', ''
    packet = {'in-interface': lan, 'out-interface': lan, 'dst-address': cidr, 'protocol': 'tcp',
              'connection-state': 'invalid'}
    if evaluate_chain(snapshot, 6, 'filter', 'forward', packet)['decision'] != 'drop':
        return '', ''
    drop = next((row for row in rows if _invalid_drop(row)), None)
    place = f' place-before={drop[".id"]}' if drop and drop.get('.id') else ''
    command = (f'/ipv6 firewall filter add chain=forward action=accept in-interface={lan} out-interface={lan} '
               f'comment="LAN to Mihomo via router"{place}')
    return ('IPv6 防火墙 forward 链会丢弃状态无效（invalid）的连接：设备经 ROS 同一 LAN 口转给 Mihomo 时，回程不经过 ROS，'
            '后续数据包被判为 invalid，每个新连接会卡住约 7 秒。请在「drop invalid」之前放行同口转发（下方命令），'
            '它同时覆盖 IPv6 Fake-IP；路由照常发布。'), command


def _item(cidr, sources, gateway, status, detail):
    return {'cidr': cidr, 'sources': sources, 'gateway': gateway, 'status': status, 'detail': detail}


def inspect(config, router, desired, agent, host, driver):
    setting = settings(config)
    previous = router.get('ipv6_routes_state')
    driver.ipv6_routes_manifest = copy.deepcopy(previous)
    wanted = {item['cidr']: list(item.get('sources') or []) for item in desired.get('ipv6_cidrs') or []}
    plan = {'enabled': setting['enabled'], 'status': 'disabled', 'gateway': '', 'items': [], 'skip': [],
            'warnings': [], 'ops': [], 'check': None, 'evidence': {}, 'auto': '', 'fix_command': ''}

    def skipped(reason, status='unknown'):
        plan['skip'].append(reason)
        plan['status'] = status
        plan['items'] = [_item(cidr, sources, '', 'unknown', '本次跳过，保留现状') for cidr, sources in wanted.items()]
        return plan

    if setting['error']:
        return skipped(setting['error'])
    if setting['enabled'] and wanted:
        if not setting['tun_enabled']:
            return skipped('Mihomo 未开启 TUN，无法接收主路由转来的 IPv6 连接；请在基础 YAML 中开启 tun')
        if not agent or agent.get('service_type') != 'mihomo' or agent.get('enabled') is False:
            return skipped('未关联本配置空间中启用的 Mihomo 代理网关')
        if host.get('error'):
            return skipped('无法读取 Mihomo 的 IPv6 地址：' + str(host['error']))
    try:
        live = snapshot(driver)
        owned = owned_entries(previous, live)
    except RouterError as error:
        return skipped(str(error))
    rows = {str(row.get('.id')): row for row in live['routes']}
    mine = {}
    for entry in owned:
        mine.setdefault(_key(entry['config'].get('dst-address')), []).append(entry)

    gateway = None
    if setting['enabled'] and wanted:
        lan = _lan(live, (agent or {}).get('host', ''))
        gateway = _gateway(host, live, lan, [rows[entry['id']] for entry in owned])
        if not gateway:
            return skipped('未确认 Mihomo 唯一、稳定的同网段 IPv6 下一跳（需唯一的链路本地地址，没有时为唯一的 ULA）；'
                           '请先在 Mihomo 设置中检查设备 IPv6 网络')
    plan['gateway'] = gateway or ''
    if gateway:
        warning, plan['fix_command'] = hairpin(driver, lan, next(iter(wanted)))
        plan['warnings'] += [warning] if warning else []
    adds, deletes, retained, active, borrowed, replaced, yielded = [], [], [], [], [], False, False
    own_ids = {entry['id'] for entry in owned}
    local = [network for network in (_network(row.get('address')) for row in live['addresses'])
             if network is not None and network.version == 6]
    for cidr, sources in (wanted.items() if gateway else ()):
        target = ipaddress.IPv6Network(cidr)
        ours = mine.pop(cidr, [])
        foreign = [row for row in live['routes'] if str(row.get('.id')) not in own_ids
                   and row.get('routing-table', 'main') == 'main' and _key(row.get('dst-address')) == cidr]
        if any(network.overlaps(target) for network in local):
            retained.extend(ours)
            plan['items'].append(_item(cidr, sources, gateway, 'conflict', 'ROS 自身的 IPv6 地址在此网段内，不能交给 Mihomo'))
        elif foreign:
            if len(foreign) == 1 and _correct(foreign[0], {'cidr': cidr, 'gateway': gateway}) and foreign[0].get('.id') \
                    and not any(yes(foreign[0].get(key)) for key in BAD_FLAGS):
                deletes.extend(ours)  # someone else's route already does it
                yielded = yielded or bool(ours)
                borrowed.append({'id': str(foreign[0]['.id']), 'config': stable(foreign[0])})
                plan['items'].append(_item(cidr, sources, gateway, 'borrowed', '沿用已有正确路由，保留原归属'))
            else:
                retained.extend(ours)
                plan['items'].append(_item(cidr, sources, gateway, 'conflict',
                                           '已有其他路由指向别处、停用或不止一条；不会覆盖，本网段跳过'))
        elif len(ours) == 1 and _configured(rows[ours[0]['id']], gateway):
            retained.extend(ours)
            if yes(rows[ours[0]['id']].get('active')):
                active.append(cidr)
                plan['items'].append(_item(cidr, sources, gateway, 'synced', '已指向 Mihomo，由 ConfigFlow 管理'))
            else:
                plan['items'].append(_item(cidr, sources, gateway, 'unknown', '受管路由未生效，请检查 Mihomo 的 IPv6 邻居'))
        else:
            replaced = replaced or bool(ours)
            deletes.extend(ours)
            adds.append({'kind': 'route_add', 'cidr': cidr, 'gateway': gateway, 'comment': PREFIX + router['id']})
            active.append(cidr)
            plan['items'].append(_item(cidr, sources, gateway, 'pending' if ours else 'missing',
                                       '下一跳变化，发布时替换' if ours else '发布时添加指向 Mihomo 的路由'))
    # Whatever is left in ``mine`` is no longer wanted: withdrawn, unless the
    # selected input is incomplete and its absence proves nothing.
    incomplete = setting['enabled'] and bool(desired.get('blocking_warnings'))
    for cidr, entries in mine.items():
        if incomplete:
            retained.extend(entries)
            plan['items'].append(_item(cidr, [], entries[0]['config'].get('gateway', ''), 'unknown',
                                       '规则集未完整读取，暂不撤回'))
        else:
            deletes.extend(entries)
            plan['items'].append(_item(cidr, [], entries[0]['config'].get('gateway', ''), 'pending',
                                       '已关闭 Mihomo IPv6，发布时撤回' if not setting['enabled']
                                       else '规则集已不含此网段，发布时撤回'))
    if incomplete and mine:
        plan['warnings'].append('部分规则集未完整读取，不撤回其 IPv6 路由')
    removed = [entry['id'] for entry in deletes]
    remains = retained or adds
    manifest_gateway = gateway or (previous or {}).get('gateway', '')
    scope = set(wanted) | {_key(entry['config'].get('dst-address')) for entry in owned}
    plan['evidence'] = evidence(_scoped(live, scope, own_ids))
    plan['check'] = {'gateway': gateway or '', 'active': active, 'borrowed': borrowed, 'removed': removed}
    statuses = {item['status'] for item in plan['items']}
    plan['status'] = next((status for status in ('conflict', 'unknown', 'missing', 'pending') if status in statuses),
                          'synced' if statuses else 'disabled')
    stale = previous and (len(owned) != len(previous.get('resources') or []) or not remains)
    if adds or deletes or stale or (previous and remains and previous.get('gateway') != manifest_gateway):
        # Additions and removals of an enrolled gateway may be applied by the
        # automatic rule sync; anything else is a change a person previews.
        if not previous:
            plan['auto'] = '尚未手动发布过 IPv6 规则集路由'
        elif not setting['enabled']:
            plan['auto'] = 'Mihomo IPv6 已关闭，撤回需手动预览发布'
        elif replaced or (gateway and previous.get('gateway') != gateway):
            plan['auto'] = 'Mihomo 的 IPv6 下一跳有变化'
        elif len(owned) != len(previous.get('resources') or []):
            plan['auto'] = '有受管 IPv6 路由已在路由器上被删除'
        elif yielded:
            plan['auto'] = '已有其他路由接替受管 IPv6 路由'
        summary = []
        summary += [f'新增 {len(adds)} 条'] if adds else []
        summary += [f'撤回 {len(deletes)} 条'] if deletes else []
        plan['ops'] = [{'kind': 'ipv6_routes_replace', 'previous': copy.deepcopy(previous),
                        'owned': copy.deepcopy(owned), 'adds': adds,
                        'deletes': [{'kind': 'route_delete', 'id': entry['id'],
                                     'cidr': entry['config'].get('dst-address')} for entry in deletes],
                        'retained': [entry['id'] for entry in retained], 'gateway': manifest_gateway,
                        'scope': sorted(scope), 'expected': plan['evidence'],
                        'summary': 'IPv6 规则集路由：' + ('，'.join(summary) or '更新管理记录')
                                   + (f'（下一跳 {gateway}）' if adds else '')}]
    return plan


def report(plan):
    return {key: copy.deepcopy(plan[key])
            for key in ('enabled', 'status', 'gateway', 'items', 'skip', 'warnings', 'fix_command')}


def _rebuild(driver, previous, live=None):
    """The manifest after a rollback: restored routes come back under new IDs."""
    if not previous:
        return None
    try:
        live = live or {'routes': driver._list(ROUTES)}
    except RouterError:
        return copy.deepcopy(previous)  # unknown: keep claiming what we had
    ids = {str(row.get('.id')) for row in live['routes']}
    claimed = {entry['id'] for entry in previous['resources'] if entry['id'] in ids}
    resources = []
    for entry in previous['resources']:
        if entry['id'] in ids:
            resources.append(copy.deepcopy(entry))
            continue
        matches = [row for row in live['routes'] if str(row.get('.id')) not in claimed
                   and stable(row) == entry['config']]
        if len(matches) == 1:
            claimed.add(str(matches[0]['.id']))
            resources.append({**copy.deepcopy(entry), 'id': str(matches[0]['.id'])})
    return {**copy.deepcopy(previous), 'resources': resources} if resources else None


def _rollback(driver, undos):
    from backend.routers.routeros_batch import restore_routes
    problems = [problem for problem in restore_routes(driver, undos, path=ROUTES) if problem]
    if problems:
        raise RouterRollbackError('IPv6 规则集路由回滚未完成：' + problems[0])


def _write(driver, ops, undos):
    """Batches of console adds or one remove, as the IPv4 routes; the rest one by one."""
    from backend.routers import routeros_batch

    def record(_op, undo):
        undos.append(undo)
    index = 0
    while index < len(ops):
        batch = routeros_batch._batches(ops, index)
        left = batch
        if len(batch) >= routeros_batch.BATCH_MIN:
            left = (routeros_batch._add if batch[0]['kind'] == 'route_add' else routeros_batch._delete)(
                driver, batch, record, path=ROUTES)
        for op in left:
            change = ({'method': 'PUT', 'path': ROUTES, 'values': routeros_batch._add_values(op)}
                      if op['kind'] == 'route_add' else {'method': 'DELETE', 'path': ROUTES, 'id': op['id']})
            undos.append(driver._transaction([change]))
        index += len(batch)


def apply(driver, op):
    live = snapshot(driver)
    scope = set(op['scope'])
    if evidence(_scoped(live, scope, {entry['id'] for entry in op['owned']})) != op['expected']:
        raise RouterError('IPv6 路由或内网地址在预览后变化，请重新预览')
    previous = op.get('previous')
    if owned_entries(previous, live) != op['owned']:
        raise RouterError('IPv6 规则集路由的归属已变化，请重新预览')
    undos = []
    try:
        # Additions first, so a replaced or moved destination is never left unrouted.
        _write(driver, op['adds'], undos)
        _write(driver, op['deletes'], undos)
        added = {change['id'] for undo in undos for change in undo['changes'] if change['method'] == 'DELETE'}
        after = driver._list(ROUTES)
        resources = [entry for entry in copy.deepcopy(op['owned']) if entry['id'] in set(op['retained'])]
        for row in after:
            if str(row.get('.id')) in added:
                if not _correct(row, {'cidr': _key(row.get('dst-address')), 'gateway': op['gateway']}):
                    raise RouterError(f'新建 IPv6 路由 {row.get("dst-address")} 尚未生效，已停止发布')
                resources.append({'path': ROUTES, 'id': str(row['.id']), 'config': stable(row)})
        if len(resources) != len(op['retained']) + len(op['adds']):
            raise RouterError('RouterOS 回读校验失败：新建的 IPv6 路由数量与计划不一致')
        driver.ipv6_routes_manifest = ({'version': VERSION, 'gateway': op['gateway'], 'resources': resources}
                                       if resources else None)
    except (RouterError, RuntimeError) as error:
        try:
            _rollback(driver, list(reversed(undos)))
        except RouterError as rollback_error:
            driver.ipv6_routes_manifest = _rebuild(driver, previous)
            raise RouterRollbackError(f'{error}；{rollback_error}') from error
        driver.ipv6_routes_manifest = _rebuild(driver, previous)
        raise
    return {'kind': 'ipv6_routes_restore', 'undos': list(reversed(undos)), 'previous': previous}


def restore(driver, op):
    try:
        _rollback(driver, op['undos'])
    finally:
        driver.ipv6_routes_manifest = _rebuild(driver, op.get('previous'))
    return {}


def verify(driver, check):
    live = {'routes': driver._list(ROUTES)}
    manifest = getattr(driver, 'ipv6_routes_manifest', None)
    owned = owned_entries(manifest, live)
    if manifest and len(owned) != len(manifest['resources']):
        raise RouterError('回读校验失败：IPv6 规则集受管路由缺失')
    rows = {str(row.get('.id')): row for row in live['routes']}
    by_cidr = {}
    for entry in owned:
        by_cidr.setdefault(_key(entry['config'].get('dst-address')), []).append(rows[entry['id']])
    for cidr in check['active']:
        candidates = by_cidr.get(cidr, [])
        if len(candidates) != 1 or not _correct(candidates[0], {'cidr': cidr, 'gateway': check['gateway']}):
            raise RouterError(f'回读校验失败：IPv6 路由 {cidr} 没有唯一生效的受管路由指向 {check["gateway"]}')
    for item in check['borrowed']:
        if item['id'] not in rows or stable(rows[item['id']]) != item['config']:
            raise RouterError('回读校验失败：沿用的 IPv6 路由已变化')
    if set(check['removed']) & set(rows):
        raise RouterError('回读校验失败：应撤回的 IPv6 规则集路由仍然存在')
