"""Read-only source-identity diagnostics using actual ordered RouterOS NAT."""
from collections import OrderedDict
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
from threading import Lock
from time import monotonic

import yaml

from backend.agents.device_group_activity import _binding
from backend.common.device_groups import compile_groups
from backend.routers.desired import compute_desired
from backend.routers.routeros_nat import (
    RouteEgressIndex, _egress, assess_bypass, preserves_sources,
)
from backend.routers.store import routers_of, static_rule_sets


MAX_PATH_PAIRS = 16384
CACHE_SECONDS = 10
MAX_CACHE_ENTRIES = 64
_CACHE = OrderedDict()
_CACHE_LOCK = Lock()


def clear_diagnostics_cache():
    with _CACHE_LOCK:
        _CACHE.clear()


def _bindings(config):
    default = (config.get('system_config') or {}).get('default_profile_id') or 'default'
    values = {agent['id']: _binding(agent, default) for agent in config.get('agents') or []
              if isinstance(agent, dict) and agent.get('id')}
    for router in routers_of(config):
        if router.get('proxy_gateway_agent_id'):
            values.setdefault(router['proxy_gateway_agent_id'], None)
    return tuple(sorted(values.items()))


def _evidence_fingerprint(config, router_id):
    # Keep every configuration field, including router credentials/manifests,
    # groups, sources and rule selections. Agent telemetry is not proof input;
    # its endpoint, profile, token and enabled state are covered by _binding.
    value = {**config, 'agents': _bindings(config)}
    agents = {agent['id']: agent for agent in config.get('agents') or []
              if isinstance(agent, dict) and agent.get('id')}
    destinations = []
    for router in routers_of(config):
        if router_id is not None and router.get('id') != router_id:
            continue
        target = agents.get(router.get('proxy_gateway_agent_id')) or {}
        try:
            gateway = str(ipaddress.IPv4Address(target.get('host')))
        except (ValueError, TypeError):
            continue
        selected = list(dict.fromkeys(static_rule_sets(router) + list(router.get('ros_policy_rule_sets') or [])))
        desired = compute_desired(config, gateway, selected)
        # Local rule-set contents can change without changing their URL or the
        # profile revision. Re-resolve these inputs even on a cache hit.
        destinations.append((router.get('id'), desired.get('proxy_cidrs'), desired.get('blocking_warnings')))
    return hashlib.sha256(json.dumps({'configuration': value, 'destinations': destinations,
        'router_id': router_id}, sort_keys=True, ensure_ascii=False,
        separators=(',', ':')).encode()).hexdigest()


def _stale_reason(config_loader, fingerprint, bindings, manager, default, router_id):
    try:
        if _evidence_fingerprint(config_loader(), router_id) != fingerprint:
            return '检查依据中的配置已变化，请刷新后重新检查。'
        if any(_binding(manager.get_agent_by_id(identity), default) != binding for identity, binding in bindings):
            return '代理网关或相关 Agent 的连接绑定已变化，请刷新后重新检查。'
    except Exception:
        return '暂时无法核对当前配置与 Agent 绑定，已丢弃本次 NAT 检查依据，请刷新后重试。'
    return None


def _unconfirmed(response, message):
    result = deepcopy(response)
    for row in result['diagnostics']:
        row.update(state='uncertain', preserves_source_ip=None, message=message)
        row.pop('checks', None)
        row['warnings'] = [warning for warning in row['warnings'] if warning.get('code') != 'source_ip_unconfirmed']
        row['warnings'].append({'code': 'source_ip_unconfirmed', 'router_id': row.get('router_id'),
                               'agent_id': row.get('agent_id'), 'message': message})
    return result


def _deployment(config):
    settings = config.get('mihomo') or {}
    if settings.get('router_split'):
        return 'router_split'
    gateway = settings.get('linux_gateway')
    if isinstance(gateway, dict) and gateway.get('enabled') is True:
        return 'selective_gateway'
    try:
        document = yaml.safe_load(settings.get('custom_config') or '') or {}
        if isinstance(document, dict) and isinstance(document.get('tun'), dict) and document['tun'].get('enable') is True:
            return 'full_tun'
    except yaml.YAMLError:
        pass
    default = (config.get('system_config') or {}).get('default_profile_id') or 'default'
    profile = config.get('profile_id') or default
    services = {agent.get('service_type') for agent in config.get('agents') or []
                if (agent.get('profile_id') or default) == profile and agent.get('enabled', True)}
    return 'mihomo_only' if 'mihomo' in services and 'oxidns' not in services else 'unknown'


def _driver(router):
    from backend.routers.routeros import RouterOSDriver
    return RouterOSDriver(router['endpoint'], router['username'], router.get('password') or '',
                          verify_tls=router.get('verify_tls') is not False)


def diagnostics(config, manager, *, router_id=None, check=False, driver_factory=None, config_loader=None):
    """Reuse completed read-only evidence for at most ten seconds.

    A fresh loader and matching live bindings are required for every hit. The
    original check time is retained; failures and incomplete checks are never
    cached. Without a loader, keep the original uncached inspection path.
    """
    if not check or config_loader is None:
        return _diagnostics(config, manager, router_id=router_id, check=check,
                            driver_factory=driver_factory, config_loader=config_loader)
    try:
        fingerprint = _evidence_fingerprint(config, router_id)
        bindings = _bindings(config)
    except Exception:
        return _diagnostics(config, manager, router_id=router_id, check=check,
                            driver_factory=driver_factory, config_loader=config_loader)
    default = (config.get('system_config') or {}).get('default_profile_id') or 'default'
    key = hashlib.sha256(json.dumps([id(manager), id(driver_factory), fingerprint]).encode()).hexdigest()
    reason = _stale_reason(config_loader, fingerprint, bindings, manager, default, router_id)
    if reason:
        return _unconfirmed(_diagnostics(config, manager, router_id=router_id), reason)
    now = monotonic()
    with _CACHE_LOCK:
        for expired in [entry for entry, value in _CACHE.items() if value[0] <= now]:
            del _CACHE[expired]
        cached = _CACHE.get(key)
        if cached:
            _CACHE.move_to_end(key)
    # Hold the originating objects in each bounded entry so Python object-id
    # reuse cannot cross manager/driver contexts. Never store secrets in keys.
    if cached and cached[2] is manager and cached[3] is driver_factory and monotonic() < cached[0]:
        result = deepcopy(cached[1])
        if monotonic() < cached[0]:
            return result
    started = monotonic()
    response = _diagnostics(config, manager, router_id=router_id, check=True,
                            driver_factory=driver_factory, config_loader=config_loader)
    reason = _stale_reason(config_loader, fingerprint, bindings, manager, default, router_id)
    if reason:
        return _unconfirmed(response, reason)
    complete = response['diagnostics'] and all(row.get('checked_at') and row.get('checks') and
        row.get('state') in ('confirmed', 'uncertain') for row in response['diagnostics'])
    expires = started + CACHE_SECONDS
    if complete and monotonic() < expires:
        entry = (expires, deepcopy(response), manager, driver_factory)
        with _CACHE_LOCK:
            _CACHE[key] = entry
            _CACHE.move_to_end(key)
            while len(_CACHE) > MAX_CACHE_ENTRIES:
                _CACHE.popitem(last=False)
    return response


def _diagnostics(config, manager, *, router_id=None, check=False, driver_factory=None, config_loader=None):
    """Only ``check=True`` contacts routers; neither path modifies remote state.

    A proof for the router's historical /24 is insufficient. Every effective
    IPv4 group source is checked against fake-IP and selected destinations.
    ``None`` means unconfirmed, never a claim that translation definitely occurs.
    """
    compiled = compile_groups(config)
    default = (config.get('system_config') or {}).get('default_profile_id') or 'default'
    profile = config.get('profile_id') or default
    sources = [ipaddress.IPv4Network(network) for networks in compiled['effective'].values()
               for network in networks if ipaddress.ip_network(network).version == 4]
    sources = list(ipaddress.collapse_addresses(sources))
    deployment = _deployment(config)
    routers = [r for r in routers_of(config) if router_id is None or r.get('id') == router_id]
    rows = []
    for original in routers:
        router = deepcopy(original)
        identity = router.get('proxy_gateway_agent_id')
        row = {'router_id': router.get('id'), 'agent_id': identity, 'checked_at': None,
               'state': 'not_checked', 'preserves_source_ip': None, 'deployment': deployment,
               'message': '尚未检查路由器 NAT；检查前不能确认连接层保留了设备来源 IP。', 'warnings': []}
        if (router.get('dns_guard') or {}).get('enabled'):
            row['warnings'].append({'code': 'dns_guard_client_identity', 'router_id': router.get('id'),
                                    'message': '强制 DNS 的回环伪装可能让 OxiDNS 只看到路由器 IP，DNS 与连接可能归入不同设备组。'})
        rows.append(row)
        if not check:
            continue
        row['checked_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
        target = manager.get_agent_by_id(identity) if identity else None
        if (not target or target.get('service_type') != 'mihomo' or not target.get('enabled', True)
                or (target.get('profile_id') or default) != profile):
            row.update(state='uncertain', message='代理网关 Agent 不存在、已停用或不在当前配置空间，无法确认来源。')
            continue
        if router.get('type') != 'routeros':
            row.update(state='uncertain', message='当前仅能通过 RouterOS 有序 NAT 规则核实设备组来源。')
            continue
        if not sources:
            row.update(state='uncertain', message='没有可检查的设备组 IPv4 有效网段，尚未确认来源保留情况。')
            continue
        driver = None
        try:
            gateway = str(ipaddress.IPv4Address(target.get('host')))
            selected = list(dict.fromkeys(static_rule_sets(router) + list(router.get('ros_policy_rule_sets') or [])))
            desired = compute_desired(config, gateway, selected)
            if desired.get('blocking_warnings'):
                row.update(state='uncertain', message='所选目标规则集尚不完整，无法核实所有设备组路径。')
                continue
            destinations = [ipaddress.IPv4Network(item['cidr']) for item in desired['proxy_cidrs']]
            if len(sources) * len(destinations) > MAX_PATH_PAIRS:
                row.update(state='uncertain', message='来源与目标组合过多，本次未完成完整 NAT 检查，请缩小范围后重试。')
                continue
            driver = (driver_factory or _driver)(router)
            driver.ros_policy_manifest = router.get('ros_policy_state')
            driver.dns_guard_manifest = router.get('dns_guard_state')
            state = driver.read_state()
            context = {**(state.nat_context or {}), 'address_sets': state.address_sets}
            nat_rows = context.get('rules')
            if not isinstance(nat_rows, list) or any(not isinstance(item, dict) for item in nat_rows):
                row.update(state='uncertain', message='路由器未提供完整有序 NAT 依据，无法确认来源 IP 被保留。')
                continue
            index = RouteEgressIndex(context)
            checks = []
            for destination in destinations:
                cidr = str(destination)
                assessment = assess_bypass(cidr, gateway, state, route_index=index)
                interface, _ = _egress(cidr, gateway, context, route_index=index)
                proven = preserves_sources(nat_rows, sources, [destination], interface, context,
                                           selector_cache=index.nat_selectors)
                checks.append({'cidr': cidr, 'status': assessment['status'], 'preserves_source_ip': proven})
            row['checks'] = checks
            if checks and all(item['preserves_source_ip'] for item in checks):
                row.update(state='confirmed', preserves_source_ip=True,
                           message='已核实设备组 IPv4 来源到 fake-IP 与所选目标网段的流量保留源 IP。')
            else:
                row.update(state='uncertain', message='无法确认全部设备组路径均免于源 NAT，连接层可能不能按设备来源分组。')
            if _binding(manager.get_agent_by_id(identity), default) != _binding(target, default):
                row.update(state='uncertain', preserves_source_ip=None,
                           message='检查期间代理网关 Agent 已改绑，请刷新后重新检查。')
            if config_loader is not None:
                current = next((r for r in routers_of(config_loader()) if r.get('id') == router.get('id')), None)
                if current != original:
                    row.update(state='uncertain', preserves_source_ip=None,
                               message='检查期间路由器配置已变化，请刷新后重新检查。')
        except Exception:
            row.update(state='failed', preserves_source_ip=None,
                       message='只读 NAT 检查失败，请检查路由器连接与读取权限后重试。')
        finally:
            if driver is not None:
                driver.close()
    if check:
        for row in rows:
            if row['preserves_source_ip'] is not True:
                row['warnings'].append({'code': 'source_ip_unconfirmed', 'router_id': row.get('router_id'),
                                        'agent_id': row.get('agent_id'), 'message': row['message']})
    if not rows:
        rows.append({'checked_at': None, 'state': 'not_checked', 'preserves_source_ip': None,
                     'deployment': deployment, 'message': '未配置可检查的上游路由器，尚未核实客户端源 IP。',
                     'warnings': []})
    return {'success': True, 'diagnostics': rows}
