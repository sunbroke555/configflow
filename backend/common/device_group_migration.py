"""Explicit, non-persistent upgrade planning for legacy OxiDNS device policies."""
from copy import deepcopy
import hashlib
import ipaddress
import json

from backend.common.device_groups import DeviceGroupsError, compile_groups, normalize_sources, source_networks
from backend.common.device_notes import normalize_notes, read_notes
from backend.common.oxidns_device_policy import normalize_policies, patch_document
from backend.common.oxidns_native import load_native_yaml
from backend.common.oxidns_settings import OxidnsSettingsError
from backend.routers.base import RouterError


def _group_id(config, rows):
    value = {'profile_id': config.get('profile_id') or config.get('id') or 'default',
             'policies': sorted((row['ip'], row['mode']) for row in rows)}
    encoded = json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
    return 'dg_' + hashlib.sha256(encoded).hexdigest()[:16]


def _inside(member, ranges):
    network = ipaddress.ip_network(member)
    return any(network.version == parent.version and network.subnet_of(parent) for parent in ranges)


def plan_migration(config):
    """Return an unpatched candidate; the repository alone applies native edits.

    Preview may patch a copy using normalize_transition(config, copy). Saving
    that patched preview would introduce a second native ownership baseline, so
    callers must persist this original candidate inside an explicit transaction.
    """
    candidate = deepcopy(config)
    result = {'config': candidate, 'groups': [], 'policy_routing_sources': None,
              'sources_before': {}, 'sources_after': {}, 'warnings': [], 'blockers': []}
    if config.get('device_groups') is not None:
        result['blockers'].append('此配置空间已经启用设备组，无需重复升级')
        return result
    settings = config.get('oxidns') or {}
    template = settings.get('template_settings') or {}
    if not settings.get('device_policies') and not (isinstance(template, dict) and template.get('device_policies')):
        result['blockers'].append('此配置空间没有需要升级的设备策略')
        return result
    try:
        rows = normalize_policies(settings.get('device_policies', []))
        # Native ownership is checked before touching either logical model.
        # An inactive template/native archive is never executed or rewritten.
        if settings.get('config_mode') == 'native':
            patch_document(load_native_yaml(settings.get('native_config', '')), rows, rows)
        direct = [row['ip'] for row in rows if row['mode'] == 'direct']
        direct_ranges = [ipaddress.ip_network(member) for member in direct]
        smart = [row['ip'] for row in rows if row['mode'] == 'smart']
        exceptions = [member for member in smart if _inside(member, direct_ranges)]
        groups = []
        if direct:
            groups.append({'id': _group_id(config, rows), 'name': '直连设备', 'note': '由设备策略迁移',
                           'members': direct, 'mode': 'direct', 'keep_blocking': False, 'dns': 'follow'})
        groups.append({'id': 'default', 'name': '默认组', 'members': exceptions})
        candidate['device_groups'] = {'version': 1, 'groups': groups}
        candidate['policy_routing_sources'] = {'include': smart,
                                               'exclude_groups': [groups[0]['id']] if direct else []}
        candidate['device_notes'] = normalize_notes(read_notes(config))
        candidate.setdefault('oxidns', {})['device_policies'] = []
        if isinstance(candidate['oxidns'].get('template_settings'), dict):
            candidate['oxidns']['template_settings']['device_policies'] = []
        compiled = compile_groups(candidate)
        candidate['device_groups']['groups'] = deepcopy(compiled['groups'])
        candidate['policy_routing_sources'] = normalize_sources(candidate['policy_routing_sources'], group_ids=compiled['by_id'])
        # Validate expansion even when no RouterOS is currently configured.
        source_networks(candidate, None, compiled=compiled)
        result['groups'] = deepcopy(compiled['groups'])
        result['policy_routing_sources'] = deepcopy(candidate['policy_routing_sources'])
        if config.get('policy_routing_sources') is not None:
            if source_networks(config, None) != source_networks(candidate, None, compiled=compiled):
                result['blockers'].append('当前独立策略路由来源与旧设备策略不同，升级会改变来源，请先在来源编辑器核对')
        result['warnings'].extend([
            '直连设备的 DNS 继续使用无缓存、无路由器联动的直连链；AdGuard 和 hosts 仍全局生效',
            'Mihomo 将新增设备组分段，直连设备进入 Mihomo 的连接会直接连接，不再执行全局规则',
            '路由器、相关 Agent 和网关地址从设备组有效网段排除，改按全局规则处理',
            '请先发布受影响的 Mihomo Agent，再发布 OxiDNS Agent；本次升级只保存草稿',
            'RouterOS 正在运行的来源列表保持不变；若已开启规则自动同步，升级后请手动预览并发布一次路由器策略，刷新同步基线后再继续自动同步',
        ])
        infrastructure = compiled['infrastructure']
        affected = [row for row in infrastructure['addresses'] if _inside(row['ip'], direct_ranges)]
        result['infrastructure_in_direct'] = deepcopy(affected)
        if infrastructure['unresolved']:
            result['warnings'].append('部分基础设施地址尚未确认；不会在生成时解析主机名，请核实实际 LAN 地址')
        agents = {agent.get('id'): agent for agent in config.get('agents') or [] if isinstance(agent, dict)}
        for router in (config.get('routers') or {}).get('items') or []:
            if not isinstance(router, dict) or router.get('type') != 'routeros':
                continue
            agent = agents.get(router.get('proxy_gateway_agent_id')) or {}
            gateway = agent.get('host') or ((router.get('ros_policy_state') or {}).get('spec') or {}).get('gateway')
            try:
                gateway = str(ipaddress.IPv4Address(gateway))
            except (ValueError, TypeError):
                result['warnings'].append(f'路由器「{router.get("name") or router.get("id") or "RouterOS"}」未关联可核实的 IPv4 网关；发布前需重新预览')
                continue
            identity = router.get('id') or ''
            before = source_networks(config, gateway)
            after = source_networks(candidate, gateway, compiled=compiled)
            result['sources_before'][identity] = before
            result['sources_after'][identity] = after
            if before != after:
                result['blockers'].append(f'路由器「{router.get("name") or identity}」迁移前后来源条目不同，不能升级')
    except (DeviceGroupsError, OxidnsSettingsError, RouterError, ValueError) as error:
        result['blockers'].append(str(error))
    return result
