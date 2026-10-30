"""Profile-scoped router definitions (``profile['routers']['items']``)."""
import copy
import secrets
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

ROUTER_TYPES = ('ikuai', 'routeros')
EDITABLE_FIELDS = ('name', 'type', 'endpoint', 'username', 'verify_tls', 'proxy_gateway_agent_id', 'dns_agent_id',
                   'ip_rule_sets', 'manage_return_routing', 'ros_policy_rule_sets', 'auto_rule_sync', 'rule_sync_interval',
                   'dns_guard')


class RouterValidationError(ValueError):
    pass


def routers_of(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    routers = config.get('routers')
    if not isinstance(routers, dict) or not isinstance(routers.get('items'), list):
        return []
    return [item for item in routers['items'] if isinstance(item, dict)]


def find_router(config: Dict[str, Any], router_id: str) -> Optional[Dict[str, Any]]:
    return next((item for item in routers_of(config) if item.get('id') == router_id), None)


def static_rule_sets(router: Dict[str, Any]) -> List[str]:
    """Read legacy overlapping selections without changing their routing mode."""
    policy_sets = set(router.get('ros_policy_rule_sets') or []) if router.get('type') == 'routeros' else set()
    return [item for item in router.get('ip_rule_sets') or [] if item not in policy_sets]


def public_view(router: Dict[str, Any]) -> Dict[str, Any]:
    """Router as returned to the UI: never expose the password."""
    result = {key: router.get(key) for key in ('id', *EDITABLE_FIELDS, 'last_probe', 'last_release')}
    result['manage_return_routing'] = router.get('manage_return_routing') is True
    result['auto_rule_sync'] = router.get('auto_rule_sync') is True
    result['rule_sync_interval'] = router.get('rule_sync_interval', 60)
    result['ros_policy_rule_sets'] = list(router.get('ros_policy_rule_sets') or [])
    result['ip_rule_sets'] = static_rule_sets(router)
    result['dns_guard'] = guard_settings(router)
    result['has_password'] = bool(router.get('password'))
    return result


def guard_settings(router: Dict[str, Any]) -> Dict[str, Any]:
    from backend.routers.dns_guard import DEFAULTS
    return copy.deepcopy({**DEFAULTS, **(router.get('dns_guard') or {})})


def normalize(payload: Dict[str, Any], existing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise RouterValidationError('路由器参数必须是对象')
    router = dict(existing or {})
    if existing:
        # Old records used ip_rule_sets as a total selection. Convert that base
        # before applying edits, so clearing policy selection cannot add routes.
        router['ip_rule_sets'] = static_rule_sets(existing)
    for key in EDITABLE_FIELDS:
        if key in payload:
            router[key] = payload[key]
    password = payload.get('password')
    if isinstance(password, str) and password:
        router['password'] = password          # empty keeps the stored password
    elif not existing:
        router['password'] = ''

    router['name'] = str(router.get('name') or '').strip()
    router['type'] = str(router.get('type') or '')
    router['endpoint'] = str(router.get('endpoint') or '').strip().rstrip('/')
    router['username'] = str(router.get('username') or '').strip()
    router['verify_tls'] = router.get('verify_tls') is not False
    manage_return = router.get('manage_return_routing', False)
    if not isinstance(manage_return, bool):
        raise RouterValidationError('自动配置回流必须为布尔值')
    router['manage_return_routing'] = manage_return
    auto_sync = router.get('auto_rule_sync', False)
    interval = router.get('rule_sync_interval', 60)
    if type(auto_sync) is not bool or (auto_sync and router['type'] != 'routeros'):
        raise RouterValidationError('远程规则自动同步仅支持 RouterOS，开关必须为布尔值')
    if type(interval) is not int or interval not in (15, 30, 60, 180, 360, 720, 1440):
        raise RouterValidationError('请选择有效的规则检查间隔')
    router.update(auto_rule_sync=auto_sync, rule_sync_interval=interval)
    for key in ('proxy_gateway_agent_id', 'dns_agent_id'):
        router[key] = str(router.get(key) or '')
    rule_sets = router.get('ip_rule_sets') or []
    if not isinstance(rule_sets, list) or any(not isinstance(item, str) or not item for item in rule_sets):
        raise RouterValidationError('IP 路由规则集必须是规则集 ID 列表')
    router['ip_rule_sets'] = list(dict.fromkeys(rule_sets))
    policy_sets = router.get('ros_policy_rule_sets') or []
    if not isinstance(policy_sets, list) or any(not isinstance(item, str) or not item for item in policy_sets):
        raise RouterValidationError('策略路由规则集必须是规则集 ID 列表')
    if policy_sets and router['type'] != 'routeros':
        raise RouterValidationError('按来源策略路由仅支持 RouterOS')
    if set(policy_sets).intersection(rule_sets):
        raise RouterValidationError('同一规则集只能选择静态路由或策略路由中的一种方式')
    router['ros_policy_rule_sets'] = list(dict.fromkeys(policy_sets))
    if existing and existing.get('ros_policy_state') and any(router.get(key) != existing.get(key) for key in ('type', 'endpoint')):
        raise RouterValidationError('请先清空策略路由选择并发布撤销，再修改路由器类型或管理地址')
    from backend.routers.dns_guard import SettingsError, normalize_settings
    try:
        guard = normalize_settings(router.get('dns_guard'), router['type'], router['dns_agent_id'])
    except SettingsError as error:
        raise RouterValidationError(str(error)) from None
    if guard['enabled'] or 'dns_guard' in router:
        router['dns_guard'] = guard
    if existing and existing.get('dns_guard_state') and any(router.get(key) != existing.get(key) for key in ('type', 'endpoint')):
        raise RouterValidationError('请先关闭强制 DNS 并发布撤销，再修改路由器类型或管理地址')
    if existing and existing.get('ipv6_fakeip_state') and any(router.get(key) != existing.get(key) for key in ('type', 'endpoint')):
        raise RouterValidationError('请先关闭 IPv6 Fake-IP 并发布撤销，再修改路由器类型或管理地址')

    if not router['name']:
        raise RouterValidationError('请填写名称')
    if router['type'] not in ROUTER_TYPES:
        raise RouterValidationError('路由器类型只支持 iKuai 4.x 或 RouterOS')
    parsed = urlparse(router['endpoint'])
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.path not in ('', '/'):
        raise RouterValidationError('管理地址格式应为 http(s)://主机[:端口]')
    if not router['username']:
        raise RouterValidationError('请填写用户名')
    if not router.get('password'):
        raise RouterValidationError('请填写密码')
    router.setdefault('id', f'router_{secrets.token_hex(6)}')
    return router


def redact_routers(routers: Any) -> Any:
    """Desensitized export: drop credentials and management endpoints."""
    if not isinstance(routers, dict):
        return routers
    items = []
    for item in routers.get('items') or []:
        if isinstance(item, dict):
            clean = {key: value for key, value in item.items()
                     if key not in ('password', 'last_probe', 'last_release', 'ros_policy_state', 'dns_guard_state', 'ipv6_fakeip_state')}
            if clean.get('username'):
                clean['username'] = '[REDACTED]'
            if clean.get('endpoint'):
                clean['endpoint'] = '[REDACTED]'
            items.append(clean)
    return {**routers, 'items': items}
