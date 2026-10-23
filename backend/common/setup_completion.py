"""Verify paired publication before routing setup; never write host/router state.

Finishing may make the setup profile ConfigFlow's default, only when asked.
"""
from hashlib import sha256
import json

from backend.common.setup_flow import SetupError


def _bound_agents(repository, profile_id):
    return repository.get_profile_agent_bindings(profile_id)['agents']


def _template_subscriptions():
    from backend.common.resource import get_backend_resource
    with open(get_backend_resource('config_template.json'), encoding='utf-8') as handle:
        return {item['id'] for item in json.load(handle).get('subscriptions', [])}


def profile_summary(repository, session):
    """Describe the default profile around this setup for the finish screen."""
    default_id = repository.default_profile_id()
    current = repository._profile_metadata(default_id)
    summary = {'is_default': default_id == session['profile_id'],
               'current': {'id': current['id'], 'name': current['name']},
               # Suggest it only when nothing depends on the present default.
               'suggested': default_id != session['profile_id'] and not _bound_agents(repository, default_id)}
    previous_id = session.get('previous_default')
    if summary['is_default'] and previous_id:
        leftovers = {'profile': None, 'subscriptions': []}
        try:
            previous = repository._profile_metadata(previous_id)
        except Exception:
            previous = None
        if previous and not _bound_agents(repository, previous_id):
            leftovers['profile'] = {'id': previous['id'], 'name': previous['name']}
        subscriptions = {item['id']: item for item in repository.get_shared()['subscriptions']}
        for subscription_id in sorted(_template_subscriptions() & set(subscriptions)):
            usages = repository.get_resource_usage('subscriptions', subscription_id)
            if all(usage['kind'] == 'profile' and usage['id'] == previous_id for usage in usages):
                leftovers['subscriptions'].append({'id': subscription_id, 'name': subscriptions[subscription_id]['name']})
        summary['leftovers'] = leftovers
    return summary


def core_fingerprint(config):
    # Router edits and release receipts advance the profile revision without
    # changing either core. Keep those outside this content comparison.
    fields = ('proxy_groups', 'rule_configs', 'mihomo', 'oxidns', 'subscriptions',
              'nodes', 'subscription_aggregations', 'rule_library', 'system_config')
    return sha256(json.dumps({key: config.get(key) for key in fields}, sort_keys=True,
                             ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def _check_full_tun(status):
    """The router split forwards into the full TUN; no gateway record may remain."""
    forwarding, tun = status.get('ipv4_forwarding'), status.get('tun')
    if not isinstance(forwarding, dict) or not isinstance(tun, dict):
        raise SetupError('请将 Mihomo Agent 更新到 1.8.40-go 或更新版本后再检查', 409)
    if forwarding.get('current') != '1':
        raise SetupError('Linux IPv4 转发未开启，请在安装与检查中点击「开启并持久化 IPv4 转发」', 409)
    if forwarding.get('persisted') is not True:
        raise SetupError('IPv4 转发尚未持久化，重启后可能失效；请在安装与检查中点击「开启并持久化 IPv4 转发」', 409)
    managed = status.get('managed') or {}
    if managed.get('active') or managed.get('pending') or managed.get('error'):
        raise SetupError('设备上仍有 Linux 网关的转发记录，请先在「上游路由」完成迁移或清理', 409)
    if status.get('enabled_in_config') or tun.get('gateway_marker'):
        raise SetupError('已发布的 Mihomo 仍是 Linux 网关配置，请重新发布', 409)
    if tun.get('configured') is not True:
        raise SetupError('已发布的 Mihomo 未启用完整 TUN，请检查基础 YAML 后重新发布', 409)
    if tun.get('present') is not True:
        raise SetupError(f"Mihomo TUN（{tun.get('device') or 'Meta'}）未运行，请检查 Mihomo 日志", 409)


def check_installation(repository, session):
    from backend.common.agent_manager import get_agent_manager
    from backend.agents import gateway_installation, linux_gateway
    from backend.agents.oxidns_monitor import MonitorError
    from backend.common.router_split import is_full_tun, route_rule_set_ids
    manager = get_agent_manager()
    gateway = gateway_installation.latest(repository, manager, session['profile_id'])['session']
    config, revision = repository.get_publish_snapshot(session['profile_id'])
    checked = session.get('installation')
    if (not gateway or gateway['phase'] != 'published' or gateway['target_changed']
            or (gateway['config_changed'] and (not checked or checked['id'] != gateway['id']
                or checked['fingerprint'] != core_fingerprint(config)))):
        raise SetupError('请先在安装向导完成 Mihomo、OxiDNS 的发布与检查；配置变化后需重新发布', 409)
    agents = gateway['agents']
    if not all(agents.get(service, {}).get('id') for service in ('mihomo', 'oxidns')):
        raise SetupError('两个核心的设备尚未确认，请继续安装向导', 409)
    if any(agents[service].get('status') != 'online' for service in ('mihomo', 'oxidns')):
        raise SetupError('安装设备离线，请检查连接后再检查', 409)
    full_tun = session['goal'] == 'router' and is_full_tun(config) and not session.get('legacy_takeover')
    try:
        status = linux_gateway.call(manager, agents['mihomo']['id'], 'status', 'GET', session['profile_id'])
    except MonitorError as error:
        raise SetupError(error.message, error.status) from None
    if full_tun:
        _check_full_tun(status)
    else:
        managed = status.get('managed') or {}
        if not managed.get('active') or managed.get('pending') or managed.get('healthy') is not True:
            raise SetupError('Linux 转发尚未启用或健康状态未确认，请继续安装向导', 409)
    session['installation'] = {'id': gateway['id'], 'fingerprint': core_fingerprint(config),
        'mihomo': {key: agents['mihomo'].get(key) for key in ('id', 'name', 'host')},
        'oxidns': {key: agents['oxidns'].get(key) for key in ('id', 'name', 'host')}}
    from backend.routers.desired import compute_desired
    selected = (route_rule_set_ids(config) if full_tun
                else config.get('mihomo', {}).get('linux_gateway', {}).get('rule_set_ids', []))
    desired = compute_desired(config, agents['mihomo'].get('host'), selected)
    for item in desired['proxy_cidrs']:
        item['requires_source_exclusion'] = item['cidr'] != desired['fake_ip_range']
    session.update(config_revision=revision, route_instructions={
        'gateway': agents['mihomo'].get('host'), 'dns': agents['oxidns'].get('host'),
        'routes': desired['proxy_cidrs'], 'warnings': desired['warnings']})
    return config


def finish(repository, session, body):
    make_default = body.get('make_default', False)
    if type(make_default) is not bool:
        raise SetupError('请选择是否设为默认配置空间')
    config = check_installation(repository, session)
    if session['goal'] == 'router':
        mode = body.get('router_mode', session.get('router_mode', 'manual'))
        if mode == 'manual':
            if body.get('ack_routes') is not True or body.get('ack_dns') is not True:
                raise SetupError('请先按说明配置主路由的静态路由与 DNS，再确认已完成', 409)
            session.update(router_mode='manual', router_verification='manual')
        elif mode == 'managed':
            from backend.routes.routers import _driver, _inspect
            from backend.routers.plan import build_plan
            from backend.routers.base import RouterError
            router_id = body.get('router_id', session.get('router_id'))
            router = next((r for r in config.get('routers', {}).get('items', []) if r['id'] == router_id), None)
            pair = session['installation']
            if (not router or router.get('proxy_gateway_agent_id') != pair['mihomo']['id']
                    or router.get('dns_agent_id') != pair['oxidns']['id']):
                raise SetupError('请关联本次安装的 Mihomo 和 OxiDNS 设备，再预览并发布路由器配置', 409)
            driver = _driver(router)
            try:
                _, desired, state, report = _inspect(config, router, driver)
                plan = build_plan(router, desired, state, report, report['return_routing'], driver)
            except RouterError as error:
                raise SetupError(str(error), 502) from None
            finally:
                driver.close()
            if plan['blockers'] or not plan['empty']:
                raise SetupError('路由器配置尚未同步，请在差异报告中预览并完成发布', 409)
            latest = repository.get_profile(session['profile_id']).get('routers', {}).get('items', [])
            if next((item for item in latest if item['id'] == router_id), None) != router:
                raise SetupError('路由器设置在检查期间已变化，请重新检查', 409)
            session.update(router_mode='managed', router_id=router_id, router_verification='verified')
        else:
            raise SetupError('请选择路由器配置方式')
    else:
        session['router_verification'] = 'client_pending'
    current, _ = repository.get_publish_snapshot(session['profile_id'])
    if core_fingerprint(current) != session['installation']['fingerprint']:
        raise SetupError('核心配置在检查期间已变化，请重新发布后检查', 409)
    session.update(completed=True, step=5, max_step=5)
    previous = repository.default_profile_id()
    if make_default and previous != session['profile_id']:
        repository.set_default_profile(session['profile_id'])
        session['previous_default'] = previous
    return session
