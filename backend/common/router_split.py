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
# ROUTER_SPLIT_RESTORE_BLOCKED for a full TUN profile, by what the restore point holds.
FULL_TUN_NO_RESTORE_POINT = '没有可恢复的试用前快照；主路由分流（完整 TUN）不使用 Linux 网关试用，无需恢复。'
FULL_TUN_RESTORE_OVERWRITES = '主路由分流的配置空间不能恢复试用前快照：恢复会用快照覆盖当前的 Mihomo 与 OxiDNS 设置，可能让分流中断。'
GENERATED_TUN_MISSING = ('主路由分流需要完整 TUN，但生成的 Mihomo 配置未启用（基础 YAML 缺少 tun 或被修改）。'
                         '请在基础 YAML 中恢复 tun 后再发布，否则主路由转来的连接会回环。')
GENERATED_TUN_FILTERED = ('主路由分流的完整 TUN 不能带 tun.{keys} 过滤：未进入 TUN 的连接会在主路由与 Linux 之间回环。'
                          '请在 Mihomo 基础 YAML 中移除后再发布。')
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


def tun_filters(tun):
    """The TUN filter keys in use; each lets some forwarded connections bypass the TUN."""
    return [key for key in TUN_FILTER_KEYS if tun.get(key)] if isinstance(tun, dict) else []


def full_tun_document(document):
    """Return the base document with the full TUN; user TUN keys are kept."""
    document = deepcopy(document)
    tun = document.get('tun')
    if tun is None:
        tun = {}
    if not isinstance(tun, dict):
        raise LinuxGatewayError('Mihomo tun 必须是对象')
    for key in tun_filters(tun):
        raise LinuxGatewayError(f'主路由分流的完整 TUN 与现有 tun.{key} 过滤冲突：未进入 TUN 的连接会在主路由与 Linux 之间回环。'
                                '请先在 Mihomo 基础 YAML 中移除该过滤')
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


def _full_tun_shape(doc):
    # Captures every connection, apart from what a TUN filter excludes.
    if not isinstance(doc, dict) or GATEWAY_MARKER in doc:
        return False
    tun = doc.get('tun')
    if not isinstance(tun, dict):
        return False
    hijack = tun.get('dns-hijack')
    return (all(tun.get(key) is True for key in ('enable', 'auto-route', 'auto-redirect'))
            and isinstance(hijack, list) and 'any:53' in hijack
            and not any(key in tun for key in ROUTE_KEYS_REMOVED))


def _document_filters(doc):
    return tun_filters(doc.get('tun')) if isinstance(doc, dict) else []


def is_full_tun_document(doc):
    """A TUN the main router can forward to: a filtered connection would loop back."""
    return _full_tun_shape(doc) and not _document_filters(doc)


def check_generation(config):
    """Stop Mihomo generation for an invalid marker or a conflicting gateway."""
    state, _ = read_marker(config)
    if state == 'invalid':
        raise RouterSplitError(MESSAGES['ROUTER_SPLIT_INVALID'], 'ROUTER_SPLIT_INVALID')
    if state == 'valid' and _gateway_enabled(config):
        raise RouterSplitError(MESSAGES['ROUTER_SPLIT_FULL_TUN'], 'ROUTER_SPLIT_FULL_TUN')


def require_full_tun_generated(main):
    if not _full_tun_shape(main):
        raise ValueError(GENERATED_TUN_MISSING)
    filters = _document_filters(main)
    if filters:
        raise ValueError(GENERATED_TUN_FILTERED.format(keys='、tun.'.join(filters)))


def _session_context(repository, profile_id):
    # An abandoned session is no longer the profile's purpose, as on the Setup page.
    from backend.common.setup_flow import profile_sessions
    sessions = [item for item in profile_sessions(repository, profile_id) if not item.get('abandoned')]
    latest = sessions[0] if sessions else None  # Sorted newest first.
    return {'session_goal': latest.get('goal') if latest else None,
            'session_id': latest.get('id') if latest else None,
            'session_completed': bool(latest.get('completed')) if latest else False,
            'has_gateway_session': any(item.get('goal') == 'gateway' for item in sessions)}


def legacy_context(repository, profile_id):
    """Read the setup sessions and bound Agents; call outside profile transactions."""
    agents = repository.get_profile_agent_bindings(profile_id)['agents']
    return {**_session_context(repository, profile_id),
            'mihomo_agent_ids': [agent['id'] for agent in agents if agent.get('service_type') == 'mihomo']}


def needs_context(config):
    """Only an unmarked profile with the gateway on can be a legacy router split."""
    return read_marker(config)[0] == 'absent' and _gateway_enabled(config)


def base_is_full_tun(config):
    """Whether the Mihomo base YAML itself carries the full TUN, without the gateway."""
    return is_full_tun_document(_base_document(config))


def _base_document(config):
    try:
        return yaml.safe_load((config.get('mihomo') or {}).get('custom_config') or '')
    except (yaml.YAMLError, RecursionError):
        return None


def _router_ids(config, agent_ids):
    routers = (config.get('routers') or {}).get('items') or []
    return [router.get('id') for router in routers if isinstance(router, dict)
            and router.get('proxy_gateway_agent_id') in agent_ids]


def legacy_source(config, context):
    """How an unmarked profile matches the router split set up with the gateway:
    'setup', 'topology' or None, whatever its gateway switch and base TUN."""
    if read_marker(config)[0] != 'absent':
        return None
    if context.get('session_goal') == 'router':
        return 'setup'
    if context.get('has_gateway_session'):
        return None  # A gateway-goal profile may use upstream routers without the split.
    return 'topology' if _router_ids(config, list(context.get('mihomo_agent_ids') or [])) else None


def publish_legacy_source(repository, profile_id, config, agent_id):
    """legacy_source for a publish to one Agent; reads the setup sessions only if needed."""
    if read_marker(config)[0] != 'absent' or not profile_id:
        return None
    return legacy_source(config, {**_session_context(repository, profile_id), 'mihomo_agent_ids': [agent_id]})


def legacy_gateway_agent_ids(config, context):
    """The bound Mihomo Agents an old router split's gateway record may be on:
    the routers' proxy gateways for a topology match, every one for 'setup'."""
    agent_ids = list(context.get('mihomo_agent_ids') or [])
    source = legacy_source(config, context)
    if source == 'topology':
        routers = (config.get('routers') or {}).get('items') or []
        proxies = {router.get('proxy_gateway_agent_id') for router in routers if isinstance(router, dict)}
        return [agent_id for agent_id in agent_ids if agent_id in proxies]
    return agent_ids if source else []


def gateway_was_used(config, agent, source):
    """Whether a publish may meet a gateway record left by this profile's old router split.

    The old wizard ('setup') always turned the gateway on. Otherwise the
    editor keeps a restore point once it changes the gateway, and the
    installation wizard marks each Agent it installed.
    """
    from backend.common.linux_gateway_draft import RESTORE_KEY
    return (source == 'setup' or RESTORE_KEY in (config.get('mihomo') or {})
            or bool(agent.get('gateway_installation_id')))


def _state(config, context):
    agent_ids = list(context.get('mihomo_agent_ids') or [])
    result = {'mode': None, 'source': None, 'mihomo_agent_ids': agent_ids,
              'router_ids': _router_ids(config, agent_ids),
              'setup_session_id': context.get('session_id'), 'issues': []}
    state, _ = read_marker(config)
    if state == 'valid':
        result.update(mode='full_tun', source='marker')
        base = _base_document(config)
        if _document_filters(base):
            result['issues'].append('tun_filtered')
        if not _full_tun_shape(base):
            result['issues'].append('tun_missing')
        if _gateway_enabled(config):
            result['issues'].append('gateway_enabled')
        return result
    if state == 'invalid':
        result.update(mode='invalid', source='marker')
        return result
    if not _gateway_enabled(config) or base_is_full_tun(config):
        # With its own full TUN, turning the gateway off keeps the takeover;
        # the editor still checks that the host keeps IPv4 forwarding then.
        return result
    source = legacy_source(config, context)
    if source:
        result.update(mode='legacy', source=source)
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
