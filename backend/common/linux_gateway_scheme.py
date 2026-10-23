"""How a profile uses its Linux gateway: behind the main router, or as the
clients' own gateway.

Only an explicitly stored ``router`` scheme lets router saves rewrite the
takeover selection. Inference is for display and never writes anything.
"""
from backend.common.linux_gateway import (
    LinuxGatewayError, _library, _rule, extract_gateway_cidrs, gateway_settings,
    reason_code, validate_gateway_selection,
)
from backend.routers.store import routers_of, static_rule_sets

SCHEME_LABELS = {'router': '主路由分流', 'gateway': 'Linux 网关'}
SKIP_REASONS = {
    'missing': '规则集不存在',
    'library_missing': '规则集不存在',
    'behavior': '域名规则集不能用于 IP 接管',
    'mrs': 'MRS 格式不支持',
    'disabled': '规则集已停用',
    'library_disabled': '规则集已停用',
}


class _Unchanged(Exception):
    """Abort a sync transaction without bumping the profile revision."""


def _default_profile(default_profile):
    if default_profile is not None:
        return default_profile
    from backend.common.config import get_default_profile_id
    return get_default_profile_id()


def _bound_mihomo_ids(agents, profile_id, default_profile):
    return {agent.get('id') for agent in agents or ()
            if agent.get('service_type') == 'mihomo'
            and (agent.get('profile_id') or default_profile) == profile_id}


def takeover_routers(config, agents, profile_id, default_profile=None):
    """Routers of this profile whose proxy gateway is this profile's Mihomo."""
    bound = _bound_mihomo_ids(agents, profile_id, _default_profile(default_profile))
    return [router for router in routers_of(config)
            if router.get('proxy_gateway_agent_id') and router['proxy_gateway_agent_id'] in bound]


def effective_scheme(config, agents, profile_id, default_profile=None):
    stored = gateway_settings(config).get('scheme')
    if stored:
        return {'scheme': stored, 'inferred': False}
    routed = takeover_routers(config, agents, profile_id, default_profile)
    return {'scheme': 'router' if routed else 'gateway', 'inferred': True}


def _skip_reason(config, rule_id):
    item = _rule(config, rule_id)
    code = reason_code(config, item)
    if code:
        return SKIP_REASONS[code]
    source = _library(config, item) or item
    if source.get('source_type') == 'content':
        try:
            extract_gateway_cidrs(source.get('content', ''), source.get('behavior', item.get('behavior')))
        except LinuxGatewayError:
            return 'IP 规则内容无效'
    return ''


def _rule_name(config, rule_id):
    item = _rule(config, rule_id)
    if item is None:
        return rule_id
    return (_library(config, item) or item).get('name') or item.get('name') or rule_id


def router_takeover(config, agents, profile_id, default_profile=None):
    """Router IP rule sets the Linux gateway can mirror, plus what it cannot."""
    ids = []
    for router in takeover_routers(config, agents, profile_id, default_profile):
        ids += static_rule_sets(router)
        if router.get('type') == 'routeros':
            ids += router.get('ros_policy_rule_sets') or []
    usable, skipped = [], []
    for rule_id in dict.fromkeys(item for item in ids if isinstance(item, str) and item):
        reason = _skip_reason(config, rule_id)
        if reason:
            skipped.append({'id': rule_id, 'name': _rule_name(config, rule_id), 'reason': reason})
        else:
            usable.append(rule_id)
    return {'rule_set_ids': usable[:1024], 'skipped': skipped}


def scheme_state(config, agents, profile_id, default_profile=None):
    """Editor fields describing the scheme and the router-driven takeover."""
    effective = effective_scheme(config, agents, profile_id, default_profile)
    return {'scheme': gateway_settings(config).get('scheme'),
            'effective_scheme': effective['scheme'], 'scheme_inferred': effective['inferred'],
            'router_takeover': router_takeover(config, agents, profile_id, default_profile)}


def sync_router_takeover(repository, profile_id, agents=None, default_profile=None):
    """Mirror router IP rule sets into an explicitly router-scheme profile.

    Only ``rule_set_ids`` is ever written. A selection the gateway cannot
    accept is reported rather than failing the router save that triggered it.
    """
    if agents is None:
        from backend.common.agent_manager import get_agent_manager
        agents = get_agent_manager().get_all_agents()
    if default_profile is None:
        default_profile = repository.default_profile_id()
    result = {'synced': False, 'changed': False, 'skipped': []}

    def update(current):
        settings = gateway_settings(current)
        if settings.get('scheme') != 'router':
            raise _Unchanged
        takeover = router_takeover(current, agents, profile_id, default_profile)
        result.update(synced=True, skipped=takeover['skipped'])
        if takeover['rule_set_ids'] == settings['rule_set_ids']:
            raise _Unchanged
        try:
            validate_gateway_selection(current, {**settings, 'rule_set_ids': takeover['rule_set_ids']})
        except LinuxGatewayError as exc:
            result.update(synced=False, error=str(exc))
            raise _Unchanged from exc
        mihomo = current.setdefault('mihomo', {})
        mihomo['linux_gateway'] = {**mihomo['linux_gateway'], 'rule_set_ids': takeover['rule_set_ids']}
        result['changed'] = True

    try:
        repository.update_profile_transaction(profile_id, update)
    except _Unchanged:
        pass
    return result
