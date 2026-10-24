"""Pure snapshots and reviewable diffs for the explicit device-group workflow."""
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import ipaddress
import json

import yaml

from backend.common.device_groups import (DeviceGroupsError, compile_groups, infrastructure_snapshot,
    normalize_sources, source_networks)
from backend.common.device_notes import normalize_notes, read_notes
from backend.common.oxidns_device_groups import normalize_transition

DRAFT_FIELDS = ('device_groups', 'policy_routing_sources', 'device_notes')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


def target_fingerprint(agent, default_profile='default'):
    return digest({key: agent.get(key) for key in ('id', 'host', 'port', 'token', 'service_type', 'enabled')} |
                  {'profile_id': agent.get('profile_id') or default_profile})


def targets(config):
    default = (config.get('system_config') or {}).get('default_profile_id') or 'default'
    identity = config.get('profile_id') or config.get('id') or default
    result = []
    for agent in config.get('agents') or []:
        service = agent.get('service_type', 'mihomo')
        if (agent.get('profile_id') or default) != identity or service not in ('mihomo', 'oxidns'):
            continue
        status = agent.get('status', 'offline')
        try:
            if datetime.now() - datetime.fromisoformat(agent.get('last_heartbeat', '')) > timedelta(minutes=2):
                status = 'offline'
        except (ValueError, TypeError):
            status = 'offline'
        result.append({'id': agent['id'], 'name': agent.get('name') or agent['id'], 'host': agent.get('host') or '',
                       'service_type': service, 'status': status, 'enabled': agent.get('enabled', True),
                       'target_fingerprint': target_fingerprint(agent, default),
                       'core_version': None, 'version_state': 'unknown'})
    return sorted(result, key=lambda row: (row['service_type'] != 'mihomo', row['id']))


def revision(config, config_revision):
    # Names/status/heartbeats are presentation; connection and binding changes
    # are guarded even when a caller bypasses the ordinary profile updater.
    return digest({'config_revision': config_revision,
                   'infrastructure': infrastructure_snapshot(config)['fingerprint'],
                   'targets': [(row['id'], row['target_fingerprint']) for row in targets(config)]})


def draft(config):
    return {'device_groups': deepcopy(config.get('device_groups') or
                {'version': 1, 'groups': [{'id': 'default', 'name': '默认组', 'members': []}]}),
            'policy_routing_sources': deepcopy(config.get('policy_routing_sources') or {'include': [], 'exclude_groups': []}),
            'device_notes': read_notes(config)}


def legacy_policies(config):
    settings = config.get('oxidns') or {}
    return settings.get('device_policies') or (settings.get('template_settings') or {}).get('device_policies') or []


def candidate(config, values):
    if not isinstance(values, dict) or any(key not in values for key in DRAFT_FIELDS):
        raise DeviceGroupsError('请提供设备组、策略路由来源和设备备注')
    updated = deepcopy(config)
    updated.update({key: deepcopy(values[key]) for key in DRAFT_FIELDS})
    if config.get('device_groups') is None and legacy_policies(config):
        raise DeviceGroupsError('现有设备策略必须通过「升级为设备组」迁移，请先查看升级预览')
    # Deletion clears references in the same transaction. Preview shows that
    # exact cleanup instead of rejecting a reference the repository will prune.
    old = {row['id'] for row in (config.get('device_groups') or {}).get('groups', [])}
    new = {row.get('id') for row in (updated.get('device_groups') or {}).get('groups', []) if isinstance(row, dict)}
    sources = updated.get('policy_routing_sources')
    if isinstance(sources, dict) and isinstance(sources.get('exclude_groups'), list):
        sources['exclude_groups'] = [identity for identity in sources['exclude_groups'] if identity not in old - new]
    compiled = compile_groups(updated)
    if not compiled['enabled']:
        raise DeviceGroupsError('设备组页面必须保留默认组，不能清空整个设备组模型')
    updated['device_groups'] = {'version': 1, 'groups': compiled['groups']}
    updated['policy_routing_sources'] = normalize_sources(sources, group_ids=compiled['by_id'])
    updated['device_notes'] = normalize_notes(updated['device_notes'])
    normalize_transition(config, updated)
    return updated


def warnings(config, compiled=None):
    compiled = compiled if compiled is not None else compile_groups(config)
    if not compiled['enabled']:
        return []
    result = [
        {'code': 'unsupported_formats', 'message': 'MosDNS、Surge、Loon 暂不支持设备组，生成内容仍使用全局规则。'},
        {'code': 'dns_client_identity', 'message': '设备组按服务实际看到的客户端 IP 匹配。DNS 转发或 NAT 改写来源时，请先检查客户端 IP。'},
        {'code': 'dns_ttl', 'message': '换组后已有连接和 DNS 缓存不会立即消失，可能需要等待 TTL 或重新联网。'},
    ]
    if any(':' in member for group in compiled['groups'] for member in group['members']):
        result.append({'code': 'ipv6', 'message': 'IPv6 地址可能轮换；RouterOS 来源策略当前仅处理 IPv4，请单独确认 IPv6 的接管路径。'})
    if compiled['infrastructure']['unresolved']:
        result.append({'code': 'infrastructure_unknown', 'message': '部分路由器或 Agent 未登记可核实的 IP，尚不能确认所有基础设施地址均已排除。'})
    routers = (config.get('routers') or {}).get('items') or []
    for router in routers:
        if (router.get('dns_guard') or {}).get('enabled'):
            result.append({'code': 'dns_guard', 'router_id': router['id'],
                           'message': 'DNS guard 劫持可能让 DNS 服务只看到路由器 IP；建议通过 DHCP 将 DNS 直接指向 OxiDNS。'})
    services = {row['service_type'] for row in targets(config)}
    if services == {'mihomo'}:
        result.append({'code': 'mihomo_only', 'message': '当前仅部署 Mihomo，设备组只作用于进入其规则表的连接，不能按组控制独立 DNS 服务。'})
    from backend.common.device_group_diagnostics import _deployment
    deployment = _deployment(config)
    proxy = any(group.get('mode') == 'proxy' for group in compiled['groups'])
    selective = deployment in ('router_split', 'selective_gateway') or (routers and deployment != 'full_tun')
    if proxy and selective:
        result.append({'code': 'selective_gateway', 'message': '只有进入 Mihomo 的连接才能按组处理；直接访问 IP 或未被接管的目标可能不经过 Mihomo。'})
    if proxy and routers and deployment != 'full_tun':
        includes = [ipaddress.ip_network(value) for value in source_networks(config, '0.0.0.0', compiled=compiled)]
        from backend.common.device_groups import _subtract
        for group in compiled['groups']:
            if group.get('mode') != 'proxy':
                continue
            members = [ipaddress.ip_network(value) for value in compiled['effective'][group['id']]
                       if ipaddress.ip_network(value).version == 4]
            if _subtract(members, includes):
                result.append({'code': 'proxy_outside_sources', 'group_id': group['id'],
                               'message': f'「{group["name"]}」有成员不在策略路由来源内，请确认这些连接能够进入 Mihomo。'})
    return result


def snapshot(config, config_revision):
    compiled = compile_groups(config)
    mode = (config.get('oxidns') or {}).get('config_mode', 'template')
    from backend.common.device_group_blocking import compile_blocking_rules
    blocking = compile_blocking_rules(config)
    def overlaps(group):
        result = []
        for member in group['members']:
            parent = ipaddress.ip_network(member)
            for other in compiled['groups']:
                if other['id'] == group['id']:
                    continue
                count = sum(1 for value in other['members'] if
                            (network := ipaddress.ip_network(value)).version == parent.version and
                            network.prefixlen > parent.prefixlen and network.subnet_of(parent))
                if count:
                    result.append({'member': member, 'group_id': other['id'], 'group_name': other['name'], 'count': count})
        return result

    effective = [{'id': group['id'], 'ownership_networks': compiled['ownership'].get(group['id'], []),
                  'effective_networks': compiled['effective'].get(group['id'], []),
                  'blocking_count': len(blocking['oxidns']) if group.get('keep_blocking') else 0,
                  'member_overlaps': overlaps(group)} for group in compiled['groups']]
    infrastructure = compiled['infrastructure']
    migration = bool(legacy_policies(config)) and not compiled['enabled']
    return {'success': True, 'profile_id': config.get('profile_id') or config.get('id'),
            'profile_name': config.get('name') or '', 'revision': revision(config, config_revision),
            'config_revision': config_revision, **draft(config), 'migrated': compiled['enabled'],
            'migration': {'available': migration, 'reason': '' if migration else '当前没有需要升级的旧设备策略'},
            'capabilities': {'oxidns_mode': mode, 'keep_blocking_editable': mode != 'native',
                             'keep_blocking_reason': '完整参数模式无法可靠提取拦截规则，请切换模板模式后开启' if mode == 'native' else ''},
            'policy_groups': [{'id': group['id'], 'name': group['name']} for group in config.get('proxy_groups', []) if group.get('enabled', True)],
            'effective_groups': effective, 'infrastructure': {'addresses': infrastructure['addresses'],
                'unresolved': [f'{row.get("name") or row.get("id")}：{row["reason"]}' for row in infrastructure['unresolved']]},
            'affected_agents': targets(config), 'warnings': warnings(config, compiled)}


def _difference(before, after):
    from backend.common.device_groups import _subtract
    return [str(net) for net in _subtract([ipaddress.ip_network(value) for value in after],
                                        [ipaddress.ip_network(value) for value in before])]


def preview_token(config, config_revision, proposed, base_url=''):
    return digest({'revision': revision(config, config_revision), 'draft': draft(proposed),
                   'oxidns': proposed.get('oxidns'), 'base_url': base_url})


def preview(config, config_revision, proposed, base_url=''):
    from backend.common.config_export import sanitize_external_payload
    from backend.common.device_group_blocking import compile_blocking_rules
    from backend.converters.oxidns import generate_oxidns_config
    from backend.converters.mihomo import generate_mihomo_config
    compiled = compile_groups(proposed)
    result = {'success': True, 'profile_id': config.get('profile_id') or config.get('id'),
              'revision': revision(config, config_revision), 'config_revision': config_revision,
              'preview_token': preview_token(config, config_revision, proposed, base_url), 'draft': draft(proposed),
              'generated_changes': [], 'blockers': [], 'warnings': warnings(proposed, compiled),
              'affected_agents': targets(proposed)}
    for service, generate in [('oxidns', generate_oxidns_config),
                              ('mihomo', lambda value, url: generate_mihomo_config(value, url, preflight_providers=False))]:
        texts = []
        for label, value in [('当前', config), ('保存后', proposed)]:
            try:
                text = generate(value, base_url)
                sanitized = sanitize_external_payload(yaml.safe_load(text), value.get('system_config'))
                texts.append(yaml.safe_dump(sanitized, allow_unicode=True, sort_keys=False))
            except ValueError as error:
                result['blockers'].append(f'{service} {label}生成失败：{error}')
                texts.append('')
        result['generated_changes'].append({'service_type': service, 'before': texts[0], 'after': texts[1]})
    blocking = compile_blocking_rules(proposed)
    copied = {row['item']['id']: {'id': row['item']['id'], 'name': row['item'].get('name') or row['item'].get('value') or row['item']['id'],
                                 'dns_action': row['action'], 'policy': row['item'].get('policy')} for row in blocking['oxidns']}
    for row in blocking['mihomo']:
        copied.setdefault(row['id'], {'id': row['id'], 'name': row.get('name') or row.get('value') or row['id'], 'policy': row.get('policy')})
    known = {row['id']: row for row in proposed.get('rule_configs', [])}
    result['copied_blocking_rules'] = list(copied.values()) if any(group.get('keep_blocking') for group in compiled['groups']) else []
    result['excluded_after_match_rules'] = [{'id': identity, 'name': known.get(identity, {}).get('name') or
        known.get(identity, {}).get('value') or identity, 'policy': known.get(identity, {}).get('policy')} for identity in blocking['excluded']]
    before = source_networks(config, '0.0.0.0')
    after = source_networks(proposed, '0.0.0.0', compiled=compiled)
    removed = _difference(after, before)
    result['routing_changes'] = {'added': _difference(before, after), 'removed': removed, 'affected_clients': []}
    if removed:
        result['warnings'].append({'code': 'leaving_policy_routing',
            'message': '部分设备将离开策略路由来源。重新发布路由器策略后，这些设备将恢复普通出口，请核对下方移除网段。'})
    return result
