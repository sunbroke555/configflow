"""One profile draft for OxiDNS source devices and a RouterOS policy bundle."""
from backend.common.oxidns_device_policy import apply_policies, read_policies, revision
from backend.common.device_notes import prepare_policy_notes
from backend.routers.ros_policy import digest
from backend.routers.store import find_router, normalize, public_view, routers_of
from backend.routers.base import RouterConflictError
from backend.common.device_groups import DeviceGroupsError, compile_groups, normalize_sources, source_revision

FIELDS = ('ip_rule_sets', 'ros_policy_rule_sets', 'proxy_gateway_agent_id')


def draft_revision(config, router):
    # Do not invalidate a draft on a probe/heartbeat, but detect edits from the
    # ordinary router form, another device editor or another publication.
    result = {'router': {k: v for k, v in router.items() if k != 'last_probe'},
              'oxidns': revision(config.get('oxidns') or {})}
    if config.get('policy_routing_sources') is not None:
        result.pop('oxidns')
        result['policy_routing_sources'] = source_revision(config)
        gateway = next((agent for agent in config.get('agents') or []
                        if agent.get('id') == router.get('proxy_gateway_agent_id')), {})
        result['gateway'] = {key: gateway.get(key) for key in ('id', 'host', 'profile_id', 'service_type')}
    return digest(result)


def read_workspace(config, router, profile_id, profile_name, agents, default_profile):
    if config.get('device_groups') is not None or config.get('policy_routing_sources') is not None:
        compiled = compile_groups(config)
        policies = {'kind': 'device_groups', 'policies': [], 'editable': True, 'reason': '',
                    'policy_routing_sources': normalize_sources(config.get('policy_routing_sources'), group_ids=compiled['by_id'])
                    or {'include': [], 'exclude_groups': []},
                    'groups': [{'id': group['id'], 'name': group['name']} for group in compiled['groups']]}
    else:
        policies = read_policies(config.get('oxidns') or {}, config)
    sources = [{'id': a['id'], 'name': a.get('name') or a['id'],
                'host': a.get('host') or '', 'status': a.get('status') or 'offline'}
               for a in agents if a.get('service_type') == 'oxidns'
               and (a.get('profile_id') or default_profile) == profile_id]
    gateways = [{'id': a['id'], 'name': a.get('name') or a['id'], 'host': a.get('host') or ''}
                for a in agents if a.get('service_type') == 'mihomo']
    rules = [{'id': r['id'], 'name': r.get('name') or r['id'], 'policy': r.get('policy') or '',
              'library_rule_id': r.get('library_rule_id') or ''}
             for r in config.get('rule_configs', []) if r.get('itemType') == 'ruleset']
    return {'profile_id': profile_id, 'profile_name': profile_name,
            'revision': draft_revision(config, router), 'router': public_view(router),
            'sources': policies, 'source_agents': sources, 'gateways': gateways, 'rule_sets': rules,
            'oxidns_revision': revision(config.get('oxidns') or {})}


def save_workspace(config, router_id, body, base_url):
    router = find_router(config, router_id)
    if not router or router.get('type') != 'routeros' or body.get('revision') != draft_revision(config, router):
        raise RouterConflictError('配置已在其他页面更新，请重新载入后再保存。')
    updated = normalize(body['router'], router)
    settings = config.get('oxidns') or {}
    modern = config.get('device_groups') is not None or config.get('policy_routing_sources') is not None
    if 'policy_routing_sources' in body:
        if not isinstance(body['policy_routing_sources'], dict):
            raise DeviceGroupsError('策略路由来源必须是包含 include 和 exclude_groups 的对象')
        compiled = compile_groups(config)
        config['policy_routing_sources'] = normalize_sources(body['policy_routing_sources'], group_ids=compiled['by_id'])
    elif modern:
        if body.get('policies') != []:
            raise RouterConflictError('此配置空间已使用独立策略路由来源，请前往设备组或来源编辑器修改')
    elif body['policies'] != read_policies(settings, config)['policies']:
        # Validation happens before either section is changed. The repository
        # commits the two sections together, never a half-saved workflow.
        rows, notes = prepare_policy_notes(config, body['policies'])
        settings = apply_policies(settings, config, rows, base_url)
        config['device_notes'] = notes
    items = routers_of(config)
    items[next(i for i, row in enumerate(items) if row['id'] == router_id)] = updated
    config['routers'] = {**config.get('routers', {}), 'items': items}
    config['oxidns'] = settings
