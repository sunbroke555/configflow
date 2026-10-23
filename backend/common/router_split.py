"""Router split ("Mihomo + OxiDNS 分流") takeover: full TUN instead of the gateway.

A profile set up for the router split keeps ``mihomo.router_split``. Its base
YAML carries a complete TUN, like an ordinary All-in-One install, and the Linux
gateway's selective takeover stays disabled. Profiles from before this marker
used the gateway; they are recognized here as ``legacy`` so that callers do not
remove a takeover the main router still forwards connections to.
"""
from copy import deepcopy

import yaml

from backend.common.linux_gateway import LinuxGatewayError, TUN_FILTER_KEYS

MARKER_KEY = 'router_split'
FULL_TUN = {'enable': True, 'stack': 'mixed', 'auto-route': True, 'auto-redirect': True,
            'auto-detect-interface': True, 'dns-hijack': ['any:53', 'tcp://any:53']}
ROUTE_KEYS_REMOVED = ('route-address', 'inet4-route-address', 'inet6-route-address',
                      'route-address-set', 'route-exclude-address-set')
GATEWAY_MARKER = 'x-configflow-linux-gateway'
MESSAGES = {
    'ROUTER_SPLIT_FULL_TUN': '此配置空间使用 Mihomo + OxiDNS 分流（完整 TUN），主路由转来的连接已全部交给 Mihomo，'
                             '不能再开启 Linux 网关的选择性接管。',
    'ROUTER_SPLIT_LEGACY': '这是旧版主路由分流：Mihomo 靠 Linux 网关接管主路由转来的连接。'
                           '关闭 Linux 网关会同时去掉 TUN 并还原转发，主路由转来的连接会中断。',
    'ROUTER_SPLIT_RESTORE_BLOCKED': '主路由分流的配置空间不能恢复试用前快照：快照中没有完整 TUN，恢复会让分流中断。',
    'ROUTER_SPLIT_LEGACY_TEARDOWN': '撤销会还原 TUN 接管与 IPv4 转发，主路由转来的连接将中断。确认后重试。',
    'ROUTER_SPLIT_INVALID': '主路由分流标记 mihomo.router_split 无效，已停止生成和网关操作。请从备份恢复，或重新运行首次使用向导。',
}
# ROUTER_SPLIT_RESTORE_BLOCKED for a legacy profile whose snapshot has the gateway off.
LEGACY_RESTORE_BLOCKED = '恢复后 Linux 网关为关闭状态，主路由转来的连接会中断，已阻止。'
GENERATED_TUN_MISSING = ('主路由分流需要完整 TUN，但生成的 Mihomo 配置未启用（基础 YAML 缺少 tun 或被修改）。'
                         '请在基础 YAML 中恢复 tun 后再发布，否则主路由转来的连接会回环。')
_KEYS = {'version', 'takeover', 'route_rule_set_ids', 'origin'}


class RouterSplitError(LinuxGatewayError):
    """A router split rule; a LinuxGatewayError so existing 400 handlers still apply."""

    def __init__(self, message, code, status=409):
        super().__init__(message)
        self.code = code
        self.status = status


def _valid_ids(ids):
    # Same bounds as normalize_gateway_settings, the gateway's own selection.
    return (isinstance(ids, list) and len(ids) <= 1024
            and all(isinstance(item, str) and item and len(item) <= 256
                    and not any(ord(c) < 32 or ord(c) == 127 for c in item) for item in ids))


def read_marker(config):
    """Return ('absent' | 'valid' | 'invalid', normalized marker or None)."""
    mihomo = config.get('mihomo') if isinstance(config, dict) else None
    if not isinstance(mihomo, dict) or MARKER_KEY not in mihomo:
        return 'absent', None
    marker = mihomo[MARKER_KEY]
    if (not isinstance(marker, dict) or set(marker) != _KEYS
            or type(marker['version']) is not int or marker['version'] != 1
            or marker['takeover'] != 'full_tun' or marker['origin'] != 'setup'
            or not _valid_ids(marker['route_rule_set_ids'])):
        return 'invalid', None
    result = deepcopy(marker)
    result['route_rule_set_ids'] = list(dict.fromkeys(marker['route_rule_set_ids']))
    return 'valid', result


def is_full_tun(config):
    return read_marker(config)[0] == 'valid'


def route_rule_set_ids(config):
    """The IP rule sets the main router forwards to Linux, besides fake-IP."""
    state, marker = read_marker(config)
    return marker['route_rule_set_ids'] if state == 'valid' else []


def _gateway_enabled(config):
    # Tolerates a malformed setting; generation itself rejects it separately.
    value = (config.get('mihomo') or {}).get('linux_gateway')
    return isinstance(value, dict) and value.get('enabled') is True


def full_tun_document(document):
    """Return the base document with the full TUN; user TUN keys are kept."""
    document = deepcopy(document)
    tun = document.get('tun')
    if tun is None:
        tun = {}
    if not isinstance(tun, dict):
        raise LinuxGatewayError('Mihomo tun 必须是对象')
    for key in TUN_FILTER_KEYS:
        if tun.get(key):
            raise LinuxGatewayError(f'Linux 网关与现有 tun.{key} 过滤冲突，请先在 Mihomo 基础 YAML 中移除该过滤，避免客户端绕过接管')
    for key in ROUTE_KEYS_REMOVED:
        tun.pop(key, None)
    for key, value in FULL_TUN.items():
        if key == 'stack':
            tun.setdefault(key, value)
        else:
            tun[key] = deepcopy(value)
    document['tun'] = tun
    return document


def _load_base(text):
    if not isinstance(text, str):
        raise LinuxGatewayError('Mihomo 基础配置必须为 YAML 文本')
    if not text.strip():
        from backend.converters.mihomo import _default_mihomo_template
        return _default_mihomo_template()
    try:
        document = yaml.safe_load(text)
    except (yaml.YAMLError, RecursionError) as exc:
        raise LinuxGatewayError('Mihomo 基础 YAML 无法解析，请先修正基础配置') from exc
    if not isinstance(document, dict):
        raise LinuxGatewayError('Mihomo 基础配置必须是 YAML 对象')
    return document


def _dump(document):
    return yaml.safe_dump(document, allow_unicode=True, sort_keys=False)


def full_tun_base_yaml(text):
    return _dump(full_tun_document(_load_base(text)))


def is_full_tun_document(doc):
    if not isinstance(doc, dict) or GATEWAY_MARKER in doc:
        return False
    tun = doc.get('tun')
    if not isinstance(tun, dict):
        return False
    hijack = tun.get('dns-hijack')
    return (all(tun.get(key) is True for key in ('enable', 'auto-route', 'auto-redirect'))
            and isinstance(hijack, list) and 'any:53' in hijack
            and not any(key in tun for key in ROUTE_KEYS_REMOVED))


def check_generation(config):
    """Stop Mihomo generation for an invalid marker or a conflicting gateway."""
    state, _ = read_marker(config)
    if state == 'invalid':
        raise RouterSplitError(MESSAGES['ROUTER_SPLIT_INVALID'], 'ROUTER_SPLIT_INVALID')
    if state == 'valid' and _gateway_enabled(config):
        raise RouterSplitError(MESSAGES['ROUTER_SPLIT_FULL_TUN'], 'ROUTER_SPLIT_FULL_TUN')


def require_full_tun_generated(main):
    if not is_full_tun_document(main):
        raise ValueError(GENERATED_TUN_MISSING)


def legacy_context(repository, profile_id):
    """Read the setup sessions and bound Agents; call outside profile transactions.

    An abandoned session is no longer the profile's purpose, as on the Setup page.
    """
    from backend.common.setup_flow import profile_sessions
    sessions = [item for item in profile_sessions(repository, profile_id) if not item.get('abandoned')]
    latest = sessions[0] if sessions else None  # Sorted newest first.
    agents = repository.get_profile_agent_bindings(profile_id)['agents']
    return {'session_goal': latest.get('goal') if latest else None,
            'session_id': latest.get('id') if latest else None,
            'session_completed': bool(latest.get('completed')) if latest else False,
            'has_gateway_session': any(item.get('goal') == 'gateway' for item in sessions),
            'mihomo_agent_ids': [agent['id'] for agent in agents if agent.get('service_type') == 'mihomo']}


def needs_context(config):
    """Only an unmarked profile with the gateway on can be a legacy router split."""
    return read_marker(config)[0] == 'absent' and _gateway_enabled(config)


def base_is_full_tun(config):
    """Whether the Mihomo base YAML itself carries the full TUN, without the gateway."""
    try:
        document = yaml.safe_load((config.get('mihomo') or {}).get('custom_config') or '')
    except (yaml.YAMLError, RecursionError):
        return False
    return is_full_tun_document(document)


def _state(config, context):
    agent_ids = list(context.get('mihomo_agent_ids') or [])
    routers = (config.get('routers') or {}).get('items') or []
    result = {'mode': None, 'source': None, 'mihomo_agent_ids': agent_ids,
              'router_ids': [router.get('id') for router in routers if isinstance(router, dict)
                             and router.get('proxy_gateway_agent_id') in agent_ids],
              'setup_session_id': context.get('session_id'), 'issues': []}
    state, _ = read_marker(config)
    if state == 'valid':
        result.update(mode='full_tun', source='marker')
        if not base_is_full_tun(config):
            result['issues'].append('tun_missing')
        if _gateway_enabled(config):
            result['issues'].append('gateway_enabled')
        return result
    if state == 'invalid':
        result.update(mode='invalid', source='marker')
        return result
    if not _gateway_enabled(config) or base_is_full_tun(config):
        # With its own full TUN, turning the gateway off keeps the takeover.
        return result
    if context.get('session_goal') == 'router':
        result.update(mode='legacy', source='setup')
    elif context.get('has_gateway_session'):
        pass  # A gateway-goal profile may use upstream routers without the split.
    elif result['router_ids']:
        result.update(mode='legacy', source='topology')
    return result


def mode_from_context(config, context):
    """Decide from the locked ``config`` and a context read before the transaction."""
    return _state(config, context)['mode']


def router_split_state(repository, profile_id, config=None, context=None):
    if config is None:
        config = repository.get_profile(profile_id)
    if context is None:
        context = legacy_context(repository, profile_id)
    return _state(config, context)
