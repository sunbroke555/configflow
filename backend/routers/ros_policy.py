"""Opt-in RouterOS source/destination policy bundles, separate from static routes.

Only explicitly selected rule sets participate. Source ranges come from the
current profile's saved OxiDNS device policies, with longest-prefix overrides.
Live objects are owned by a private release manifest, never by a name alone.
"""
import copy
from bisect import bisect_right
import hashlib
import ipaddress
import json
import re

from backend.routers.base import RouterError, is_managed

ADDRESSES = '/ip/firewall/address-list'
TABLES = '/routing/table'
MANGLE = '/ip/firewall/mangle'
ROUTES = '/ip/route'
NAT = '/ip/firewall/nat'
RULES = '/routing/rule'
FILTER = '/ip/firewall/filter'
RAW = '/ip/firewall/raw'
VRFS = '/ip/vrf'
SETTINGS = '/routing/settings'
PATHS = (ADDRESSES, TABLES, MANGLE, ROUTES, NAT, RULES, FILTER, RAW, VRFS)
PREFIX = 'cf-ros-policy-'
NAMING_VERSION = 1
POLICY_VERSION = 2
RUNTIME = {'.id', 'bytes', 'packets', 'active', 'inactive', 'immediate-gw', 'used',
           'creation-time', 'belongs-to', 'hw-offloaded', 'debug.fwp-ptr', 'actual-interface',
           'uptime', 'last-updated', 'invalid'}


def enabled(row):
    return str(row.get('disabled', 'false')).lower() not in ('true', 'yes', '1')


def config_row(row):
    """Keep unknown configuration fields: external edits must invalidate ownership."""
    return {key: value for key, value in row.items() if key not in RUNTIME}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def snapshot(driver):
    result = {path: driver._list(path) for path in PATHS}
    settings = driver._get(SETTINGS)
    if not isinstance(settings, dict):
        raise RouterError('无法读取 ROS 路由查找顺序')
    # A stable synthetic ID lets this singleton participate in the same preview
    # fingerprint as collections. It is never an editable policy resource.
    result[SETTINGS] = [{**settings, '.id': 'routing-settings'}]
    if 'policy-rules' not in settings:
        # policy-rules exists only since RouterOS 7.22; the version lets the
        # blocker say so instead of reporting an unknown priority.
        try:
            resource = driver._get('/system/resource')
        except RouterError:
            resource = None
        if isinstance(resource, dict) and resource.get('version'):
            # Drop the release channel: "7.21.2 (stable)" -> "7.21.2".
            result[SETTINGS][0]['routeros-version'] = str(resource['version']).split()[0]
    return result


def policy_rules_missing_message(version):
    match = re.match(r'(\d+)\.(\d+)', version or '')
    if match and (int(match.group(1)), int(match.group(2))) >= (7, 22):
        return f'RouterOS {version} 未返回路由查找顺序（/routing/settings policy-rules），无法确认 Mangle 优先级'
    return f'策略路由需要 RouterOS 7.22 或更高版本（当前 {version or "版本未知"}），请升级 RouterOS 后再发布'


def check_policy_order(live):
    settings = live.get(SETTINGS) or []
    if len(settings) == 1 and 'policy-rules' not in settings[0]:
        raise RouterError(policy_rules_missing_message(settings[0].get('routeros-version')))
    order = settings[0].get('policy-rules') if len(settings) == 1 else None
    if isinstance(order, str):
        order = [item.strip() for item in order.split(',')]
    if (not isinstance(order, list) or any(not isinstance(item, str) for item in order)
            or len(order) != len(set(order)) or not {'mangle', 'main'}.issubset(order)
            or set(order) - {'mangle', 'vrf-lookup', 'vrf-unreach', 'local', 'user', 'main'}):
        raise RouterError('无法确认 ROS 路由查找顺序中的 Mangle 优先级')
    if any(order.index(item) < order.index('mangle') for item in ('main', 'user') if item in order):
        raise RouterError('ROS 路由查找顺序将主表或用户路由规则置于 Mangle 之前，请先核对策略优先级')


def check_route_egress(row, gateway, interface):
    """The activated policy route must resolve to the previewed LAN next hop."""
    if (not isinstance(row, dict) or not enabled(row) or str(row.get('active', '')).lower() not in ('true', 'yes')
            or str(row.get('immediate-gw') or '') != f'{gateway}%{interface}'
            or any(str(row.get(key, '')).lower() in ('true', 'yes') for key in ('invalid', 'blackhole', 'ecmp'))):
        raise RouterError('独立路由表的实际下一跳或出口与预览不一致，停止启用策略')


def stable(snapshot):
    return {path: [{'.id': row.get('.id'), **config_row(row)} for row in rows]
            for path, rows in snapshot.items()}


def subtract(networks, exclusions):
    for excluded in exclusions:
        remaining = []
        for network in networks:
            if not network.overlaps(excluded):
                remaining.append(network)
            elif not network.subnet_of(excluded):
                remaining.extend(network.address_exclude(excluded))
        networks = remaining
    return list(ipaddress.collapse_addresses(networks))


def smart_sources(rows, gateway):
    from backend.common.oxidns_device_policy import normalize_policies
    normalized = normalize_policies(rows)
    entries = [(ipaddress.ip_network(row['ip']), row['mode']) for row in normalized]
    # Start with an explicit allowlist; unlisted devices retain ordinary routing.
    # Broader rules run first so a narrower smart exception can override direct.
    networks = []
    for network, mode in sorted(entries, key=lambda item: item[0].prefixlen):
        if network.version != 4:
            continue
        if mode == 'smart':
            networks = list(ipaddress.collapse_addresses(networks + [network]))
        else:
            networks = subtract(networks, [network])
    networks = subtract(networks, [ipaddress.IPv4Network(gateway + '/32')])
    if len(networks) > 4096:
        raise RouterError('OxiDNS 来源例外产生过多网段，请简化客户端设备的 DNS 策略')
    return [str(network) for network in networks]


def inspect_desired(config, router, desired, content_loader=None):
    """Called only for selected policy rule sets or a previously deployed bundle."""
    from backend.common.oxidns_device_policy import read_policies, revision
    from backend.common.oxidns_settings import OxidnsSettingsError
    from backend.routers.desired import compute_desired
    selected = router.get('ros_policy_rule_sets') or []
    result = {'selected': selected, 'sources': [], 'cidrs': [], 'error': '',
              'owner': PREFIX + digest(router['id'])[:12], 'previous': router.get('ros_policy_state')}
    if not selected:
        return result
    policy = compute_desired(config, desired.get('proxy_gateway'), selected,
                             **({'content_loader': content_loader} if content_loader else {}))
    result['cidrs'] = [entry for entry in policy['proxy_cidrs'] if entry['sources'] != ['fake-ip']]
    blocking_warnings = policy.get('blocking_warnings', [])
    result['warnings'] = [warning for warning in policy['warnings'] if warning not in blocking_warnings]
    # Missing/disabled rules must not silently withdraw an existing bundle.
    if blocking_warnings:
        result['error'] = '策略路由暂不能发布：' + '；'.join(blocking_warnings)
    settings = config.get('oxidns') or {}
    result['source_revision'] = revision(settings)
    try:
        policies = read_policies(settings, config)
        if not policies['editable']:
            raise RouterError(policies['reason'])
        result['sources'] = smart_sources(policies['policies'], desired.get('proxy_gateway') or '')
        if not result['sources']:
            raise RouterError('请先在「当前配置 → 客户端设备」中把至少一个 IPv4 地址或网段设为智能分流')
    except (RouterError, OxidnsSettingsError, ValueError) as error:
        result['error'] = str(error)
    return result


def owned_rows(previous, live):
    """An absent owned object can be repaired; a changed/reused ID is never touched."""
    owned = []
    indexed = {}
    for path, rows in live.items():
        by_id = indexed[path] = {}
        for row in rows:
            by_id.setdefault(row.get('.id'), []).append(row)
    for entry in (previous or {}).get('resources', []):
        matches = indexed[entry['path']].get(entry['id'], [])
        if len(matches) > 1:
            raise RouterError('策略路由对象 ID 不唯一，停止发布')
        if matches:
            if config_row(matches[0]) != config_row(entry['config']):
                raise RouterError('策略路由受管对象被外部修改，保留现场，请先核对：' + entry['path'] + '/' + entry['id'])
            owned.append(entry)
    return owned


def check_references(owned, live):
    keys = {(entry['path'], entry['id']) for entry in owned}
    names = set()
    for entry in owned:
        row = entry['config']
        if entry['path'] == TABLES:
            names.add(row['name'])
        elif entry['path'] == ADDRESSES:
            names.add(row['list'])
        elif entry['path'] == MANGLE and row.get('chain') != 'prerouting':
            names.add(row['chain'])
    for path, rows in live.items():
        for row in rows:
            if (path, row.get('.id')) in keys:
                continue
            for key, value in row.items():
                if key in {'table', 'routing-table', 'new-routing-mark', 'routing-mark',
                           'src-address-list', 'dst-address-list', 'jump-target', 'chain', 'list', 'name'}:
                    if str(value).lstrip('!') in names:
                        raise RouterError('其他配置正在使用本功能的路由表、地址列表或链，保留这些对象：' + str(value))
                if key == 'gateway' and any('@' + name in str(value) for name in names):
                    raise RouterError('其他路由引用了本功能的路由表，不能自动切换')


def _overlaps(cidr, entries):
    network = ipaddress.IPv4Network(cidr)
    return any(network.overlaps(ipaddress.IPv4Network(item['cidr'])) for item in entries)


def overlap_check(entries):
    """Parse/collapse once for large rule sets instead of comparing every pair."""
    networks = list(ipaddress.collapse_addresses(ipaddress.IPv4Network(item['cidr']) for item in entries))
    starts = [int(network.network_address) for network in networks]
    ends = [int(network.broadcast_address) for network in networks]
    def overlaps(cidr):
        network = ipaddress.IPv4Network(cidr)
        index = bisect_right(starts, int(network.broadcast_address)) - 1
        return index >= 0 and ends[index] >= int(network.network_address)
    return overlaps


def bundle_health(manifest, live):
    """Configuration equality alone does not prove route activity or rule order."""
    resources = (manifest or {}).get('resources', [])
    indexed = {path: {row['.id']: row for row in rows} for path, rows in live.items()}
    for path in (MANGLE, NAT):
        expected = [entry['id'] for entry in resources if entry['path'] == path]
        present = [row['.id'] for row in live[path] if row['.id'] in expected]
        if len(present) == len(expected) and present != expected:
            raise RouterError('受管策略规则的顺序被修改，请先核对 Mangle / NAT')
    for entry in resources:
        row = indexed[entry['path']].get(entry['id'])
        if row is None:
            continue
        if str(row.get('invalid', '')).lower() in ('true', 'yes', '1'):
            raise RouterError('受管策略对象尚未生效：' + entry['path'] + '/' + entry['id'])
        if entry['path'] == ROUTES and str(row.get('active', '')).lower() not in ('true', 'yes'):
            raise RouterError('独立路由表的下一跳未生效，请检查 Mihomo 网关和直连路由')
        spec = (manifest or {}).get('spec') or {}
        if entry['path'] == ROUTES and spec.get('egress_interface'):
            check_route_egress(row, spec['gateway'], spec['egress_interface'])
        if entry['path'] == NAT:
            before = live[NAT][:live[NAT].index(row)]
            if any(enabled(other) and other.get('chain') == 'srcnat'
                   and other.get('action') not in {'accept', 'log', 'passthrough', 'return'} for other in before):
                raise RouterError('策略 NAT 免转换规则之前出现转换规则，请先核对规则顺序')


def _nat_migrations(live, exclusions, owned_keys):
    """Subtract policy-only destinations without broadening any other NAT matcher."""
    from backend.routers.report import _networks
    migrations, replacements = [], []
    if not exclusions:
        return migrations, replacements
    for row in live[NAT]:
        if (not enabled(row) or row.get('chain') != 'srcnat' or row.get('action') != 'accept'
                or not is_managed(row.get('comment')) or str(row.get('comment', '')).startswith(PREFIX)
                or (NAT, row.get('.id')) in owned_keys):
            continue
        selectors = []
        for key in ('dst-address', 'dst-address-list'):
            value = row.get(key)
            if not value:
                continue
            if str(value).startswith('!'):
                raise RouterError('受管 NAT 的反向目的条件无法安全拆分，请先核对：' + row['.id'])
            values = ([value] if key == 'dst-address' else
                      [item.get('address', '') for item in live[ADDRESSES]
                       if enabled(item) and item.get('list') == value])
            networks = []
            for value in values:
                parsed = _networks([value])
                if not parsed:
                    raise RouterError('受管 NAT 的目的地址无法安全拆分，请先核对：' + row['.id'])
                networks.extend(parsed)
            selectors.append(networks)
        destinations = selectors[0] if selectors else [ipaddress.IPv4Network('0.0.0.0/0')]
        if len(selectors) == 2:
            destinations = [a if a.subnet_of(b) else b for a in selectors[0] for b in selectors[1]
                            if a.overlaps(b)]
        remaining = subtract(destinations, exclusions)
        if remaining == list(ipaddress.collapse_addresses(destinations)):
            continue
        if row.get('dst-address-list'):
            raise RouterError('列表型 NAT 免转换无法自动拆分并保持后续列表更新语义，请先核对：' + row['.id'])
        if str(row.get('dynamic', 'false')).lower() in ('true', 'yes', '1'):
            raise RouterError('动态 NAT 免转换规则不能迁移：' + row['.id'])
        migrations.append({'path': NAT, 'id': row['.id'], 'config': config_row(row)})
        values = {key: value for key, value in config_row(row).items() if key != 'dynamic'}
        replacements.extend({'path': NAT, 'values': {**values, 'dst-address': str(network),
                                                    'place-before': row['.id']}}
                            for network in remaining)
    return migrations, replacements


def build(desired, state):
    """Build one guarded bundle operation and human-readable policy report."""
    request = desired['ros_policy']
    live = state.ros_policy
    result = {'ops': [], 'blockers': [], 'warnings': list(request.get('warnings') or []), 'items': [], 'check': None}
    if request.get('error'):
        result['blockers'].append(request['error'])
    if live is None:
        result['blockers'].append('无法读取 ROS 策略路由配置')
        return result
    previous = request.get('previous')
    try:
        owned = owned_rows(previous, live)
        check_references(owned, live)
        if request['selected']:
            check_policy_order(live)
            bundle_health(previous, live)
    except RouterError as error:
        result['blockers'].append(str(error))
        return result
    owned_keys = {(entry['path'], entry['id']) for entry in owned}
    other_mangle = [row for row in live[MANGLE] if (MANGLE, row.get('.id')) not in owned_keys]
    if request['selected']:
        if any(enabled(row) and row.get('action') == 'fasttrack-connection' for row in live[FILTER]):
            result['blockers'].append('存在启用的 FastTrack，需先让策略流量避开 FastTrack；本功能不会修改现有防火墙规则')
        if any(enabled(row) and row.get('chain') == 'prerouting'
               and row.get('action') in {'mark-routing', 'route', 'jump', 'accept', 'return'} for row in other_mangle):
            result['blockers'].append('已有 prerouting 分流或提前终止规则，无法确认新策略的优先级；请先核对现有 Mangle')
        if any(enabled(row) and row.get('name') != 'main' for row in live[VRFS]):
            result['blockers'].append('存在非 main VRF，无法安全确定策略路由作用范围')
    static_overlap = overlap_check(desired['proxy_cidrs'])
    policy_overlap = overlap_check(request['cidrs'])
    policy_only = subtract([ipaddress.IPv4Network(entry['cidr']) for entry in request['cidrs']],
                           [ipaddress.IPv4Network(entry['cidr']) for entry in desired['proxy_cidrs']])
    if any(static_overlap(entry['cidr']) for entry in request['cidrs']):
        result['warnings'].append('静态组与策略组包含重叠目的网段；命中策略时使用独立表，其他流量仍按主表路由转发')

    migrations = []
    preserved = []
    for row in live[ROUTES]:
        if (row.get('routing-table') or 'main') != 'main' or not enabled(row):
            continue
        try:
            overlaps = policy_overlap(row['dst-address'])
        except (KeyError, ValueError):
            continue
        if not overlaps:
            continue
        # An obsolete managed aggregate must be removed too. The ordinary
        # static plan establishes all still-selected coverage before this bundle.
        if (is_managed(row.get('comment')) and not str(row.get('comment', '')).startswith(PREFIX)
                and row.get('gateway') == desired.get('proxy_gateway')
                and any(ipaddress.IPv4Network(row['dst-address']).overlaps(net) for net in policy_only)
                and str(row.get('dynamic', 'false')).lower() not in ('true', 'yes', '1')):
            from backend.routers.routeros_routes import _entry
            if _entry(row, set())['editable']:
                migrations.append({'path': ROUTES, 'id': row['.id'], 'config': config_row(row)})
                continue
            result['blockers'].append('受管主表路由无法安全迁移，请先核对：' + row['.id'])
        if ipaddress.IPv4Network(row['dst-address']).prefixlen:
            preserved.append(row)
    if preserved:
        result['warnings'].append(f'保留 {len(preserved)} 条与策略目的重叠的主表路由；未命中来源策略的流量仍按主表路由转发')
    try:
        nat_migrations, replacements = _nat_migrations(live, policy_only, owned_keys)
    except RouterError as error:
        result['blockers'].append(str(error))
        return result
    migrations.extend(nat_migrations)

    gateway = desired.get('proxy_gateway') or ''
    spec = {'sources': request['sources'], 'destinations': [item['cidr'] for item in request['cidrs']],
            'gateway': gateway, 'naming_version': NAMING_VERSION, 'policy_version': POLICY_VERSION}
    if request['selected'] and not spec['destinations']:
        result['blockers'].append('选中的策略路由规则集没有可用的真实 IPv4 网段')
    # NAT assessment ignores only manifest-verified Mangle objects. Other
    # programs' matchers remain in the evidence used by assess_nat.
    context = copy.deepcopy(state.nat_context)
    if context is not None:
        context['mangle'] = other_mangle
        route_ids = {item['id'] for item in migrations if item['path'] == ROUTES}
        context['routes'] = [row for row in context['routes'] if row.get('.id') not in route_ids]
    from backend.routers.routeros_nat import assess_nat, _egress, RouteEgressIndex
    route_index = RouteEgressIndex(context)
    proxy_state = copy.copy(state)
    proxy_state.nat_context = context
    need_nat = False
    interface = None
    if request['selected'] and spec['destinations']:
        interface, reason = _egress(spec['destinations'][0], gateway, context or {}, policy_routing=True,
                                   route_index=route_index)
        if interface is None:
            result['blockers'].append('无法确认 Mihomo 的 LAN 出口：' + reason)
        for cidr in spec['destinations']:
            assessment = assess_nat(cidr, gateway, proxy_state, policy_routing=True, route_index=route_index)
            if assessment['status'] == 'unknown':
                result['blockers'].append(f'{cidr}：{assessment["detail"]}')
                break
            need_nat |= assessment['status'] == 'missing'
    spec['nat_interface'] = interface if need_nat else ''
    spec['egress_interface'] = interface or ''
    current = previous and previous.get('spec') == spec and len(owned) == len(previous.get('resources', []))
    unchanged = bool(current and not migrations) if request['selected'] else not previous
    for entry in request['cidrs']:
        result['items'].append({**entry, 'status': 'synced' if unchanged else 'pending',
                                'detail': '来源与目的地址列表 → Mangle → 独立路由表', 'route_ids': [], 'mode': 'policy'})
    # Include complete stable evidence even on a no-op; changing the selectors
    # or external configuration must invalidate a previously shown preview.
    result['evidence'] = stable(live)
    result['request'] = {key: value for key, value in request.items() if key != 'previous'}
    result['check'] = {'spec': spec if request['selected'] else None,
                       'previous': previous, 'migrations': migrations, 'replacements': replacements}
    if unchanged or result['blockers']:
        return result
    bundle = []
    if request['selected']:
        # Reserve a whole readable group. Even references to an empty list or
        # table count as occupied, and the previous group stays until activation.
        values = {str(value).lstrip('!') for rows in live.values() for row in rows for value in row.values()}
        for group in range(1, 257):
            names = {'src': f'CF_Source_Proxy_IP_{group:02d}',
                     'dst': f'CF_Destination_Proxy_IP_{group:02d}',
                     'bypass': f'CF_Bypass_IP_{group:02d}',
                     'routing': f'CF_Proxy_Routing_{group:02d}'}
            reserved = set(names.values())
            if not values & reserved and not any('@' + name in value for name in reserved for value in values):
                break
        else:
            result['blockers'].append('没有可用的策略资源名称')
            return result
        name = names['routing']
        comment = request['owner']
        def add(path, **fields):
            bundle.append({'path': path, 'values': fields})
        for kind, addresses in (('src', spec['sources']), ('dst', spec['destinations']), ('bypass', [gateway])):
            for address in addresses:
                add(ADDRESSES, list=names[kind], address=address, comment=comment, disabled='false')
        add(TABLES, name=name, fib='true')
        add(ROUTES, **{'dst-address': '0.0.0.0/0', 'gateway': gateway + '@main', 'routing-table': name,
                      'comment': comment, 'disabled': 'false'})
        if need_nat:
            add(NAT, chain='srcnat', action='accept', **{'src-address-list': names['src'],
                'dst-address-list': names['dst'], 'out-interface': interface, 'comment': comment, 'disabled': 'false'})
        add(MANGLE, chain=name, action='return', **{'src-address-list': names['bypass'], 'comment': comment, 'disabled': 'false'})
        add(MANGLE, chain=name, action='mark-routing', **{'new-routing-mark': name, 'passthrough': 'false', 'comment': comment, 'disabled': 'false'})
        # Activating this last rule is the only step that starts using the bundle.
        # Unmarked packets do not necessarily match routing-mark=main on ROS v7.
        # Conflicting prerouting markers/jumps and non-main VRFs are rejected
        # above, so no routing-mark matcher is needed to preserve other policies.
        add(MANGLE, chain='prerouting', action='jump', **{'src-address-list': names['src'],
            'dst-address-list': names['dst'], 'dst-address-type': '!local',
            'jump-target': name, 'comment': comment, 'disabled': 'false'})
    summary = (f'发布来源策略：{len(spec["sources"])} 个来源范围、{len(spec["destinations"])} 个目的网段 → {gateway}'
               if bundle else '撤销本功能的来源策略，恢复所选网段的静态路由流程')
    if need_nat:
        summary += '；仅为这些来源和目的网段添加 LAN 出口 NAT 免转换'
    if migrations:
        summary += (f'；迁移 {sum(item["path"] == ROUTES for item in migrations)} 条受管主表路由'
                    f'，收窄 {len(nat_migrations)} 条旧 NAT 免转换')
    result['ops'] = [{'kind': 'ros_policy_replace', 'bundle': bundle, 'previous': previous,
                       'owned': owned, 'migrations': migrations, 'replacements': replacements,
                       'spec': spec if bundle else None,
                       'evidence': stable(live), 'summary': summary}]
    return result
