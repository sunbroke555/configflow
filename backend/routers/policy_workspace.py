"""One profile draft for OxiDNS source devices and a RouterOS policy bundle."""
from backend.common.oxidns_device_policy import read_policies, revision
from backend.common.device_notes import policy_names_revision, save_policy_rows
from backend.routers.ros_policy import digest
from backend.routers.store import find_router, normalize, public_view, routers_of
from backend.routers.base import RouterConflictError

FIELDS = ('ip_rule_sets', 'ros_policy_rule_sets', 'proxy_gateway_agent_id')


def draft_revision(config, router):
    # Do not invalidate a draft on a probe/heartbeat, but detect edits from the
    # ordinary router form, another device editor or another publication.
    return digest({'router': {k: v for k, v in router.items() if k != 'last_probe'},
                   'oxidns': revision(config.get('oxidns') or {})})


def read_workspace(config, router, profile_id, profile_name, agents, default_profile):
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
            'revision': draft_revision(config, router), 'names_revision': policy_names_revision(config),
            'router': public_view(router),
            'sources': policies, 'source_agents': sources, 'gateways': gateways, 'rule_sets': rules,
            'oxidns_revision': revision(config.get('oxidns') or {})}


def save_workspace(config, router_id, body, base_url):
    router = find_router(config, router_id)
    # Source names are guarded only when sources are sent: renaming changes neither DNS nor router output.
    if not router or router.get('type') != 'routeros' or body.get('revision') != draft_revision(config, router) or \
            ('policies' in body and body.get('names_revision') != policy_names_revision(config)):
        raise RouterConflictError('配置已在其他页面更新，请重新载入后再保存。')
    updated = normalize(body['router'], router)
    existing = read_policies(config.get('oxidns') or {}, config)
    settings = config.get('oxidns') or {}
    if 'policies' in body and body['policies'] != existing['policies']:
        # Validation happens before either section is changed. The repository
        # commits the sections together, never a half-saved workflow.
        settings = save_policy_rows(config, settings, body['policies'], base_url)
    items = routers_of(config)
    items[next(i for i, row in enumerate(items) if row['id'] == router_id)] = updated
    config['routers'] = {**config.get('routers', {}), 'items': items}
    config['oxidns'] = settings
