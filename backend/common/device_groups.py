"""Pure profile-owned device membership and policy-routing source compilation.

Only non-default groups have materialized ranges. The default group is the
complement of their ownership, evaluated inside a caller-supplied range rather
than expanding the entire IPv4/IPv6 address space.
"""
from copy import deepcopy
import hashlib
import ipaddress
import json
import re
import secrets
from urllib.parse import urlsplit

import yaml

from backend.common.oxidns_device_policy import normalize_client_range
from backend.common.oxidns_settings import OxidnsSettingsError

MAX_MEMBERS = 1024
MAX_RANGES = 4096
DEFAULT_ID = 'default'
_GROUP_ID = re.compile(r'^dg_[0-9a-f]{6,32}$')
_BUILTINS = {'GLOBAL', 'DIRECT', 'REJECT', 'REJECT-DROP', 'PASS', 'PASS-RULE', 'COMPATIBLE'}
_LOOPBACKS = (ipaddress.ip_network('127.0.0.0/8'), ipaddress.ip_network('::1/128'))


class DeviceGroupsError(ValueError):
    """An invalid or inconsistent device-group configuration."""


def new_group_id():
    return 'dg_' + secrets.token_hex(8)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


def _address(value):
    try:
        normalized = normalize_client_range(value)
        if '/' in normalized:
            return None
        return normalized
    except OxidnsSettingsError:
        return None


def _range(value):
    try:
        return normalize_client_range(value)
    except OxidnsSettingsError as error:
        raise DeviceGroupsError(str(error)) from error


def _networks(values):
    return [ipaddress.ip_network(value) for value in values]


def _collapse(networks):
    result = []
    for version in (4, 6):
        result.extend(ipaddress.collapse_addresses(net for net in networks if net.version == version))
    return result


def _subtract(networks, exclusions):
    from backend.routers.ros_policy import subtract
    result = []
    for version in (4, 6):
        result.extend(subtract([net for net in networks if net.version == version],
                               [net for net in exclusions if net.version == version]))
    return result


def _intersection(left, right):
    return _collapse([a if a.subnet_of(b) else b for a in left for b in right
                      if a.version == b.version and a.overlaps(b)])


def _text(value, label, maximum, *, empty=True):
    if (not isinstance(value, str) or len(value) > maximum or
            any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise DeviceGroupsError(f'{label}最多 {maximum} 个字符，不能包含换行或控制字符')
    value = value.strip()
    if not empty and not value:
        raise DeviceGroupsError(f'{label}不能为空')
    return value


def _fake_ip_range(config):
    source = (config.get('mihomo') or {}).get('custom_config') or ''
    try:
        document = (yaml.safe_load(source) or {}) if source.strip() else {}
    except (yaml.YAMLError, AttributeError, RecursionError) as error:
        raise DeviceGroupsError('Mihomo 基础配置无法解析，不能确定 fake-IP 地址段') from error
    if not isinstance(document, dict):
        raise DeviceGroupsError('Mihomo 基础配置必须是对象，不能确定 fake-IP 地址段')
    dns = document.get('dns') or {}
    if not isinstance(dns, dict):
        raise DeviceGroupsError('Mihomo DNS 参数必须是对象，不能确定 fake-IP 地址段')
    try:
        network = ipaddress.ip_network(dns.get('fake-ip-range', '198.18.0.1/16'), strict=False)
        if network.version != 4:
            raise ValueError()
        return network
    except (ValueError, TypeError) as error:
        raise DeviceGroupsError('Mihomo fake-IP 地址段不是有效 IPv4 网段') from error


def infrastructure_snapshot(config, *, verified_addresses=()):
    """Use registered literal addresses only; never resolve names or query agents.

    A caller may pass an immutable, already-verified network snapshot. Agent
    heartbeats/metrics do not participate in the fingerprint.
    """
    addresses, unresolved, bindings = [], [], []
    profile_id = config.get('profile_id') or config.get('id') or 'default'
    default_id = (config.get('system_config') or {}).get('default_profile_id') or 'default'
    routers = (config.get('routers') or {}).get('items') or []
    routers = [router for router in routers if isinstance(router, dict)]
    linked_agents = {router.get(key) for router in routers
                     for key in ('proxy_gateway_agent_id', 'dns_agent_id') if router.get(key)}

    def add(value, kind, identity, name=''):
        ip = _address(value)
        entry = {'kind': kind, 'id': identity, 'name': name}
        if ip:
            addresses.append({**entry, 'ip': ip})
        else:
            unresolved.append({**entry, 'value': value if isinstance(value, str) else '',
                               'reason': '未登记可核实的 IP 地址；不会在生成时解析主机名'})

    for router in routers:
        try:
            host = urlsplit(router.get('endpoint') or '').hostname or ''
        except (ValueError, AttributeError):
            host = ''
        add(host, 'router', router.get('id') or '', router.get('name') or '')
        bindings.append({'kind': 'router', 'id': router.get('id') or '', 'host': host,
                         'proxy_gateway_agent_id': router.get('proxy_gateway_agent_id') or '',
                         'dns_agent_id': router.get('dns_agent_id') or '',
                         'dns_guard_enabled': (router.get('dns_guard') or {}).get('enabled') is True})
    for agent in config.get('agents') or []:
        if not isinstance(agent, dict):
            continue
        bound = agent.get('profile_id') or default_id
        if (bound != profile_id or agent.get('service_type') not in ('oxidns', 'mihomo')) and agent.get('id') not in linked_agents:
            continue
        add(agent.get('host'), 'agent', agent.get('id') or '', agent.get('name') or '')
        bindings.append({'kind': 'agent', 'id': agent.get('id') or '', 'host': agent.get('host') or '',
                         'profile_id': bound, 'service_type': agent.get('service_type') or ''})
    for index, value in enumerate(verified_addresses):
        if isinstance(value, str):
            add(value, 'verified', str(index))
        elif isinstance(value, dict):
            add(value.get('ip'), value.get('kind') or 'verified', value.get('id') or str(index), value.get('name') or '')
        else:
            raise DeviceGroupsError('已核实的基础设施地址格式无效')
    addresses = list({(row['ip'], row['kind'], row['id']): row for row in addresses}.values())
    addresses.sort(key=lambda row: (ipaddress.ip_address(row['ip']).version,
                                   int(ipaddress.ip_address(row['ip'])), row['kind'], row['id']))
    unresolved.sort(key=lambda row: (row['kind'], row['id'], row['value']))
    bindings.sort(key=lambda row: (row['kind'], row['id']))
    fingerprint = _digest({'addresses': [{key: row[key] for key in ('ip', 'kind', 'id')} for row in addresses],
                           'unresolved': [{key: row[key] for key in ('kind', 'id', 'value')} for row in unresolved],
                           'bindings': bindings})
    return {'addresses': addresses, 'unresolved': unresolved, 'bindings': bindings, 'fingerprint': fingerprint}


def normalize_groups(value, *, proxy_groups=(), infrastructure=None, fake_ip_range=None):
    """Normalize a model without turning absent/null legacy state into a model."""
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {'version', 'groups'} or
            type(value.get('version')) is not int or value['version'] != 1 or
            not isinstance(value.get('groups'), list)):
        raise DeviceGroupsError('设备组必须是 version=1 且包含 groups 列表的对象')
    available = {group.get('name') for group in proxy_groups
                 if isinstance(group, dict) and group.get('enabled', True)}
    infrastructure = infrastructure or {'addresses': []}
    forbidden = {row['ip'] for row in infrastructure['addresses']}
    fake = ipaddress.ip_network(fake_ip_range or '198.18.0.0/16', strict=False)
    groups, ids, names, seen, count = [], set(), set(), {}, 0
    for row in value['groups']:
        if not isinstance(row, dict) or set(row) - {'id', 'name', 'note', 'members', 'mode', 'policy', 'keep_blocking', 'dns'}:
            raise DeviceGroupsError('设备组包含不支持的字段')
        identity = row.get('id')
        if not isinstance(identity, str) or (identity != DEFAULT_ID and not _GROUP_ID.fullmatch(identity)):
            raise DeviceGroupsError('设备组 ID 必须是 dg_ 加 6–32 位小写十六进制字符')
        if identity in ids:
            raise DeviceGroupsError(f'设备组 ID 重复：{identity}')
        ids.add(identity)
        name = _text(row.get('name', '默认组' if identity == DEFAULT_ID else ''), '设备组名称', 80, empty=False)
        if name in names:
            raise DeviceGroupsError(f'设备组名称重复：{name}')
        names.add(name)
        note = _text(row.get('note', ''), '设备组备注', 500)
        if not isinstance(row.get('members'), list):
            raise DeviceGroupsError(f'设备组「{name}」成员必须是 IP / CIDR 列表')
        members = []
        for raw in row['members']:
            member = _range(raw)
            if member in seen:
                raise DeviceGroupsError(f'成员 {member} 重复，已属于设备组「{seen[member]}」')
            network = ipaddress.ip_network(member)
            if network.prefixlen == 0 or network.is_loopback:
                raise DeviceGroupsError(f'设备组成员不能是默认路由或环回地址：{member}')
            if network.version == fake.version and network.overlaps(fake):
                raise DeviceGroupsError(f'设备组成员不能包含 Mihomo fake-IP 地址段：{member}')
            if network.prefixlen == network.max_prefixlen and member in forbidden:
                raise DeviceGroupsError(f'设备组成员不能是路由器、Agent 或网关自身地址：{member}')
            seen[member] = name
            members.append(member)
            count += 1
            if count > MAX_MEMBERS:
                raise DeviceGroupsError(f'设备组成员总数不能超过 {MAX_MEMBERS} 条')
        normalized = {'id': identity, 'name': name, 'members': members}
        if note:
            normalized['note'] = note
        if identity == DEFAULT_ID:
            if set(row) & {'mode', 'policy', 'keep_blocking', 'dns'}:
                raise DeviceGroupsError('默认组跟随全局，不能设置组模式或 DNS 选项')
        else:
            mode = row.get('mode', 'follow')
            if mode not in ('follow', 'direct', 'proxy'):
                raise DeviceGroupsError('设备组模式只能是跟随全局、全部直连或全部代理')
            if 'keep_blocking' in row and type(row['keep_blocking']) is not bool:
                raise DeviceGroupsError('仍执行 DNS 拦截必须为布尔值')
            if 'dns' in row and row['dns'] not in ('follow', 'direct_full'):
                raise DeviceGroupsError('直连 DNS 只能选择直连 DNS 或直连 DNS（完整）')
            normalized['mode'] = mode
            if mode in ('direct', 'proxy'):
                normalized['keep_blocking'] = row.get('keep_blocking', False)
            if mode == 'direct':
                normalized['dns'] = row.get('dns', 'follow')
            if mode == 'proxy':
                policy = row.get('policy')
                if not isinstance(policy, str) or policy in _BUILTINS or policy not in available:
                    raise DeviceGroupsError(f'设备组「{name}」的全部代理模式必须选择已启用的策略组')
                normalized['policy'] = policy
        groups.append(normalized)
    if DEFAULT_ID not in ids:
        raise DeviceGroupsError('设备组必须保留默认组')
    groups.sort(key=lambda row: row['id'] == DEFAULT_ID)
    return {'version': 1, 'groups': groups}


def normalize_sources(value, *, group_ids=()):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {'include', 'exclude_groups'}:
        raise DeviceGroupsError('策略路由来源必须包含 include 和 exclude_groups')
    if not isinstance(value['include'], list) or len(value['include']) > MAX_MEMBERS:
        raise DeviceGroupsError(f'策略路由来源最多 {MAX_MEMBERS} 条 IP / CIDR')
    include, seen = [], set()
    for raw in value['include']:
        member = _range(raw)
        if member in seen:
            raise DeviceGroupsError(f'策略路由来源重复：{member}')
        include.append(member)
        seen.add(member)
    excluded = value['exclude_groups']
    if (not isinstance(excluded, list) or any(not isinstance(identity, str) for identity in excluded) or
            len(set(excluded)) != len(excluded)):
        raise DeviceGroupsError('策略路由排除设备组必须是无重复的设备组 ID 列表')
    missing = set(excluded) - set(group_ids)
    if missing:
        raise DeviceGroupsError('策略路由仍引用不存在的设备组：' + '、'.join(sorted(missing)))
    return {'include': include, 'exclude_groups': list(excluded)}


def compile_groups(config, *, infrastructure=None):
    infrastructure = infrastructure if infrastructure is not None else infrastructure_snapshot(config)
    value = config.get('device_groups')
    model = normalize_groups(value, proxy_groups=config.get('proxy_groups') or [], infrastructure=infrastructure,
                             fake_ip_range=_fake_ip_range(config) if value is not None else None)
    result = {'enabled': model is not None, 'groups': [], 'by_id': {}, 'ownership': {},
              'effective': {}, 'infrastructure': deepcopy(infrastructure)}
    if model is None:
        return result
    result['groups'] = model['groups']
    result['by_id'] = {group['id']: group for group in result['groups']}
    entries = [(group['id'], ipaddress.ip_network(member)) for group in model['groups'] for member in group['members']]
    excluded = _networks([row['ip'] for row in infrastructure['addresses']]) + list(_LOOPBACKS)
    total = 0
    for group in model['groups']:
        identity = group['id']
        if identity == DEFAULT_ID:
            continue
        owned = []
        for owner, network in entries:
            if owner == identity:
                owned.extend(_subtract([network], [other for other_owner, other in entries
                    if other_owner != identity and other.version == network.version and
                    other.prefixlen > network.prefixlen and other.subnet_of(network)]))
        owned = _collapse(owned)
        effective = _subtract(owned, excluded)
        total += len(effective)
        if total > MAX_RANGES:
            raise DeviceGroupsError(f'设备组有效网段展开不能超过 {MAX_RANGES} 条，请简化成员和例外')
        result['ownership'][identity] = [str(network) for network in owned]
        result['effective'][identity] = [str(network) for network in effective]
    return result


def ownership_ranges(compiled, group_ids, *, within):
    """Return a disjoint union of ownership clipped to an explicit universe."""
    universe = _collapse(_networks(within))
    selected = set(group_ids)
    if selected - set(compiled['by_id']):
        raise DeviceGroupsError('归属网段引用了不存在的设备组')
    networks = []
    for identity in selected - {DEFAULT_ID}:
        networks.extend(_intersection(_networks(compiled['ownership'][identity]), universe))
    if DEFAULT_ID in selected:
        nondefault = _networks([value for values in compiled['ownership'].values() for value in values])
        networks.extend(_subtract(universe, nondefault))
    return [str(network) for network in _collapse(networks)]


def source_networks(config, gateway, *, compiled=None):
    """Compile RouterOS IPv4 sources; preserve the complete legacy path if unset."""
    if config.get('policy_routing_sources') is None:
        from backend.routers.ros_policy import smart_sources
        return smart_sources((config.get('oxidns') or {}).get('device_policies', []), gateway)
    compiled = compiled if compiled is not None else compile_groups(config)
    sources = normalize_sources(config['policy_routing_sources'], group_ids=compiled['by_id'])
    include = _collapse([net for net in _networks(sources['include']) if net.version == 4])
    excluded = ownership_ranges(compiled, sources['exclude_groups'], within=[str(net) for net in include])
    gateways = []
    if gateway is not None:
        try:
            address = ipaddress.IPv4Address(gateway)
        except (ValueError, TypeError) as error:
            raise DeviceGroupsError('策略路由网关必须是有效 IPv4 地址') from error
        gateways = [ipaddress.ip_network(str(address) + '/32')]
    networks = _subtract(include, _networks(excluded) + gateways)
    if len(networks) > MAX_RANGES:
        raise DeviceGroupsError(f'策略路由来源展开不能超过 {MAX_RANGES} 条，请简化来源和例外')
    return [str(network) for network in networks]


def source_revision(config, *, compiled=None):
    if config.get('policy_routing_sources') is None:
        from backend.common.oxidns_device_policy import revision as legacy_revision
        return legacy_revision(config.get('oxidns') or {})
    compiled = compiled if compiled is not None else compile_groups(config)
    sources = normalize_sources(config['policy_routing_sources'], group_ids=compiled['by_id'])
    include = [str(net) for net in _collapse([net for net in _networks(sources['include']) if net.version == 4])]
    excluded = ownership_ranges(compiled, sources['exclude_groups'], within=include)
    return _digest({'include': include, 'excluded_ownership': excluded})


def revision(config, *, infrastructure=None):
    compiled = compile_groups(config, infrastructure=infrastructure)
    sources = normalize_sources(config.get('policy_routing_sources'), group_ids=compiled['by_id'])
    return _digest({'device_groups': {'version': 1, 'groups': compiled['groups']} if compiled['enabled'] else None,
                    'policy_routing_sources': sources,
                    'infrastructure': compiled['infrastructure']['fingerprint']})


def lookup_ip(config, value, *, compiled=None):
    ip = _address(value)
    if ip is None:
        raise DeviceGroupsError('请输入一个有效的客户端 IP 地址')
    address = ipaddress.ip_address(ip)
    compiled = compiled if compiled is not None else compile_groups(config)
    matches = []
    for group in compiled['groups']:
        for member in group['members']:
            network = ipaddress.ip_network(member)
            if address.version == network.version and address in network:
                matches.append({'group_id': group['id'], 'member': member, 'prefix_length': network.prefixlen})
    matches.sort(key=lambda row: -row['prefix_length'])
    owner = matches[0]['group_id'] if matches else DEFAULT_ID
    infrastructure = [row for row in compiled['infrastructure']['addresses'] if row['ip'] == ip]
    if address.is_loopback:
        infrastructure.append({'ip': ip, 'kind': 'loopback', 'id': 'loopback', 'name': '环回地址'})
    effective = DEFAULT_ID if infrastructure else owner
    default = {'id': DEFAULT_ID, 'name': '默认组', 'members': []}
    group = compiled['by_id'].get(effective, default)
    return {'ip': ip, 'group_id': effective, 'owner_id': owner, 'group': deepcopy(group),
            'mode': group.get('mode', 'follow'), 'matched_member': matches[0]['member'] if matches else None,
            'matches': matches, 'infrastructure_excluded': bool(infrastructure), 'infrastructure': infrastructure}
