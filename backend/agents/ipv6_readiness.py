"""Profile-bound IPv6 readiness: saved YAML, host preparation and RouterOS checks."""
from contextlib import ExitStack
import hashlib
import ipaddress
import json
import threading

import requests
import yaml

from backend.common.config import get_config, reset_config_context
from backend.converters.mihomo import _default_mihomo_template
from backend.routers import ipv6_readiness as ros
from backend.routers.base import RouterError
from backend.routers.route_editor import router_operation_lock
from backend.routers.routeros import RouterOSDriver
from backend.routers.rule_sync_store import job_lock
from backend.routers.store import routers_of
from .diagnostics import failure_text
from .oxidns_monitor import MonitorError, _agent_origin

_locks_guard = threading.Lock()
_locks = {}
MAX_RESPONSE = 512 * 1024
AGENT_FIELDS = ('id', 'host', 'port', 'token', 'profile_id', 'deployment_method', 'service_type')
HOST_LABELS = {
    'platform': '安装环境', 'network': '主机网络', 'interface': '实际上联网卡',
    'address': '主机 IPv6 地址', 'default_route': 'IPv6 默认路由',
    'ipv6_enabled': '网卡 IPv6', 'forwarding_ra': '转发与 RA',
    'persistence': '开机恢复配置', 'ownership': '已有配置归属',
    'layout': '安装方式', 'tun_ipv6': '实际 TUN IPv6', 'tun_recovery': 'TUN 启动恢复',
}


def _lock(agent_id):
    with _locks_guard:
        return _locks.setdefault(agent_id, threading.RLock())


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def configuration(config):
    raw = (config.get('mihomo') or {}).get('custom_config') or ''
    errors = []
    try:
        document = yaml.safe_load(raw) if raw.strip() else _default_mihomo_template()
        if not isinstance(document, dict):
            raise ValueError('基础 YAML 必须是对象')
        dns = document.get('dns', {})
        if not isinstance(dns, dict):
            raise ValueError('dns 必须是对象')
        for key, value in (('ipv6', document.get('ipv6', True)), ('dns.ipv6', dns.get('ipv6', False))):
            if type(value) is not bool:
                errors.append(key + ' 必须填写 true 或 false，不能使用字符串或拼写错误的值')
        fake = dns.get('fake-ip-range6')
        if fake:
            network = ipaddress.IPv6Network(fake, strict=False)
            if not network.subnet_of(ipaddress.IPv6Network('fc00::/7')):
                errors.append('IPv6 Fake-IP 应使用 ULA 网段，不能覆盖公网或链路本地地址')
            fake = str(network)
        tun = document.get('tun') or {}
        result = {'ipv6': document.get('ipv6', True), 'dns_ipv6': dns.get('ipv6', False),
                  'ipv6_explicit': 'ipv6' in document, 'dns_ipv6_explicit': 'ipv6' in dns,
                  'fake_ipv6_range': fake, 'tun_enabled': isinstance(tun, dict) and tun.get('enable') is True,
                  'fake_ipv6_enabled': document.get('ipv6', True) is True and dns.get('enable') is True
                  and dns.get('ipv6') is True and dns.get('enhanced-mode') == 'fake-ip' and bool(fake),
                  'errors': errors, 'source': 'saved_draft'}
        return result, document
    except (yaml.YAMLError, ValueError, TypeError, AttributeError):
        return {'ipv6': None, 'dns_ipv6': None, 'fake_ipv6_range': None, 'fake_ipv6_enabled': False,
                'tun_enabled': False, 'errors': ['无法解析 IPv6 设置，请检查基础 YAML 的类型与 IPv6 Fake-IP 网段'],
                'source': 'saved_draft'}, {}


def _agent(manager, agent_id, profile_id):
    agent = manager.get_agent_by_id(agent_id)
    if not agent:
        raise MonitorError('AGENT_NOT_FOUND', 404, '未找到这台 Agent')
    if agent.get('profile_id') != profile_id:
        raise MonitorError('PROFILE_CHANGED', 409, '设备绑定的配置空间已变化，请重新打开设置')
    if agent.get('service_type') != 'mihomo':
        raise MonitorError('WRONG_SERVICE', 400, 'IPv6 检查仅适用于 Mihomo Agent')
    return agent


def _request(agent, operation='', payload=None):
    method = 'POST' if operation else 'GET'
    path = '/api/ipv6-readiness' + ('/' + operation if operation else '')
    try:
        with requests.Session() as session:
            session.trust_env = False
            with session.request(method, _agent_origin(agent) + path, json=payload,
                                 headers={'Authorization': 'Bearer ' + agent['token']},
                                 timeout=(3, 45), allow_redirects=False, stream=True) as response:
                parts, size = [], 0
                for part in response.iter_content(65536):
                    size += len(part)
                    if size > MAX_RESPONSE:
                        raise MonitorError('INVALID_RESPONSE', 502, 'IPv6 检查结果过大')
                    parts.append(part)
                status = response.status_code
                raw = b''.join(parts)
    except (requests.RequestException, KeyError, ValueError, TypeError):
        raise MonitorError('AGENT_UNREACHABLE', 502,
                           '未能确认 IPv6 配置操作结果，请重新检查设备状态' if operation == 'prepare'
                           else '无法连接 Agent 进行 IPv6 检查') from None
    if status == 404:
        raise MonitorError('UPGRADE_REQUIRED', 409, '请将 Mihomo Agent 更新到 1.8.50-go 或更新版本后检查 IPv6')
    if status in (401, 403):
        raise MonitorError('AGENT_AUTH_FAILED', 502, 'Agent 认证失败，请检查设备注册状态')
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        raise MonitorError('INVALID_RESPONSE', 502, 'Agent 返回的 IPv6 检查结果无法解析') from None
    if not isinstance(data, dict):
        raise MonitorError('INVALID_RESPONSE', 502, 'Agent 返回的 IPv6 检查格式不正确')
    if status >= 400 or data.get('success') is not True:
        message = failure_text(data.get('message') or data.get('error') or 'IPv6 操作未完成', (agent.get('token'),))
        raise MonitorError('IPV6_OPERATION_FAILED', status if status in (400, 409, 422) else 502, message)
    return data


def _driver(router):
    return RouterOSDriver(router['endpoint'], router['username'], router.get('password') or '',
                          verify_tls=router.get('verify_tls') is not False)


def _linked(config, agent_id):
    return [r for r in routers_of(config) if r.get('proxy_gateway_agent_id') == agent_id]


def _inspect(manager, agent, profile_id, config=None):
    config = get_config(profile_id) if config is None else config
    settings, _ = configuration(config)
    host = _request(agent)
    if not isinstance(host.get('checks'), list) or not isinstance(host.get('persistence'), dict):
        raise MonitorError('INVALID_RESPONSE', 502, 'Agent IPv6 状态缺少检查与持久化结果')
    summary = (f"Mihomo IPv6：{'开启' if settings['ipv6'] else '关闭'}；"
               f"DNS IPv6 答案：{'开启' if settings['dns_ipv6'] else '关闭'}。读取的是已保存草稿，设备配置需要另行发布。")
    checks = [ros.check('configuration', '已保存的 IPv6 设置',
                        'error' if settings['errors'] else 'ok' if settings['ipv6'] else 'warning',
                        '；'.join(settings['errors']) if settings['errors'] else summary)]
    core = host.get('core') or {}
    core_read = 'core' in host and isinstance(host['core'], dict) and not core.get('error')
    if core_read:
        core = {'ipv6': True, 'dns.ipv6': False, 'tun.enable': False, 'dns.fake-ip-range6': None, **core}
    differing = [label for key, value, label in (
        ('ipv6', settings['ipv6'], 'Mihomo IPv6'), ('dns.ipv6', settings['dns_ipv6'], 'DNS IPv6'),
        ('tun.enable', settings['tun_enabled'], 'TUN'),
        ('dns.fake-ip-range6', settings['fake_ipv6_range'], 'IPv6 Fake-IP 网段'))
        if key in core and ((str(ros._network(core[key]) or '') or None) if key == 'dns.fake-ip-range6' else core[key]) != value]
    if differing:
        checks.append(ros.check('deployed_config', '设备上的配置', 'warning',
                                '已保存草稿与设备配置不同：' + '、'.join(differing) + '。请通过保存并发布应用配置。'))
    for item in host['checks']:
        if isinstance(item, dict):
            key = str(item.get('id', ''))
            details = []
            if key == 'persistence':
                details = [f"{e.get('key')}={e.get('value')} · {e.get('file')}:{e.get('line')}"
                           for e in host['persistence'].get('evidence', []) if isinstance(e, dict)]
                details.extend(str(e) for e in host['persistence'].get('conflicts', []))
            elif key == 'forwarding_ra':
                details = [f"{name}={value}" for name, value in (host.get('runtime') or {}).items()
                           if name.startswith(('all.', str(host.get('interface')) + '.'))]
            checks.append(ros.check('host_' + key, item.get('label') or HOST_LABELS.get(key, '主机 IPv6'),
                                    {'pass': 'ok', 'warn': 'warning', 'fail': 'error', 'info': 'unknown'}.get(item.get('status'), 'unknown'),
                                    str(item.get('message') or '未提供检查结论'), details))
    results = []
    for router in _linked(config, agent['id']):
        if router.get('type') != 'routeros':
            results.append({'id': router['id'], 'name': router.get('name') or router['id'], 'can_prepare': False,
                            'status': 'warning', 'checks': [ros.check('manual', '路由器 IPv6', 'unknown', '此型号请手工核对 IPv6 路由与 DNS')]})
            continue
        driver = _driver(router)
        try:
            dns_agent = manager.get_agent_by_id(router.get('dns_agent_id')) if router.get('dns_agent_id') else None
            # A HA virtual IP may differ; it is an advisory, never a DNS rewrite.
            dns_target = (dns_agent or {}).get('host')
            results.append(ros.inspect(driver, router, agent, host, settings, dns_target))
        finally:
            driver.close()
    if not results:
        checks.append(ros.check('router', '上游路由', 'unknown', '当前空间未关联这台设备的上游路由，主路由配置需另行核对'))
    blocked = bool(settings['errors']) or any(r['status'] == 'error' for r in results)
    authorized_gateway = bool(_linked(config, agent['id'])) or ((config.get('mihomo') or {}).get('linux_gateway') or {}).get('enabled') is True
    can_prepare = (not blocked and settings['ipv6'] is True and authorized_gateway and
                   (host.get('can_prepare') is True or any(r.get('can_prepare') for r in results)))
    if not authorized_gateway:
        checks.append(ros.check('gateway_role', '网关用途', 'warning', '自动开启转发仅适用于已关联上游路由或已启用 Linux 网关的设备'))
    proof = {'agent': {key: agent.get(key) for key in AGENT_FIELDS},
             'config': config, 'interface': host.get('interface'), 'interface_index': host.get('interface_index'),
             'router_fingerprints': [r.get('fingerprint') for r in results]}
    return {'success': True, 'agent_id': agent['id'], 'profile_id': profile_id,
            'configuration': settings, 'host': host, 'checks': checks, 'routers': results,
            'can_prepare': bool(can_prepare), 'prepare_interface': host.get('interface') or '',
            'check_token': _digest(proof), 'connectivity': {'status': 'not_checked', 'message': '尚未检查节点 IPv6 连接'}}


def inspect(manager, agent_id, profile_id):
    return _inspect(manager, _agent(manager, agent_id, profile_id), profile_id)


def prepare(manager, agent_id, profile_id, body):
    if not isinstance(body, dict) or set(body) != {'interface', 'check_token'}:
        raise MonitorError('INVALID_REQUEST', 400, '请提供检查结果对应的网卡与 check_token')
    if not isinstance(body['check_token'], str) or len(body['check_token']) != 64:
        raise MonitorError('INVALID_REQUEST', 400, '请先检查 IPv6 状态')
    with _lock(agent_id), ExitStack() as stack:
        agent = _agent(manager, agent_id, profile_id)
        config = get_config(profile_id)
        linked = sorted(_linked(config, agent_id), key=lambda r: r['id'])
        for router in linked:
            acquired = stack.enter_context(job_lock(profile_id, router['id'], router))
            if not acquired:
                raise MonitorError('BUSY', 409, '路由器正在发布或同步，请稍后重试')
            stack.enter_context(router_operation_lock(router))
        report = _inspect(manager, agent, profile_id, config)
        if body['check_token'] != report['check_token'] or body['interface'] != report['prepare_interface']:
            raise MonitorError('CHECK_CHANGED', 409, '设备、草稿或路由状态已变化，请重新检查后补齐')
        if not report['can_prepare']:
            raise MonitorError('PREPARE_UNAVAILABLE', 409, '当前状态不满足自动补齐条件，请查看检查结果')
        reset_config_context()
        current_agent = _agent(manager, agent_id, profile_id)
        if get_config(profile_id) != config or any(current_agent.get(key) != agent.get(key) for key in AGENT_FIELDS):
            raise MonitorError('CHECK_CHANGED', 409, '配置或设备信息已变化，请重新检查')
        receipts = []
        prepared_host = None
        try:
            for result in report['routers']:
                if not result.get('plan'):
                    continue
                router = next(r for r in linked if r['id'] == result['id'])
                driver = _driver(router)
                stack.callback(driver.close)
                receipts.append((driver, ros.add_missing_route(driver, result['plan'])))
            if report['host'].get('can_prepare'):
                prepared_host = _request(agent, 'prepare', {'interface': body['interface'], 'interface_index': report['host'].get('interface_index')})
        except (RouterError, MonitorError) as error:
            undo_errors = []
            for driver, receipt in reversed(receipts):
                try:
                    ros.undo_route(driver, receipt)
                except RouterError as undo:
                    undo_errors.append(str(undo))
            message = str(error) + ('；撤销待核对：' + '；'.join(undo_errors) if undo_errors else '；本次新增路由已撤销，主机状态请重新检查')
            raise MonitorError('PREPARE_FAILED', 502, message) from None
        result = _inspect(manager, agent, profile_id, config)
        result['message'] = '已补齐可自动处理的配置，请查看剩余检查项并验证节点连接'
        result['changed_routes'] = len(receipts)
        if prepared_host:
            for item in prepared_host.get('checks', []):
                if item.get('id') == 'tun_recovery':
                    result['checks'].append(ros.check('prepare_tun_recovery', 'TUN 启动恢复', 'warning', item.get('message', '请重新核对 TUN IPv6')))
        return result


def _probe_targets(config, document):
    proxies = document.get('proxies')
    candidates = list(proxies) if isinstance(proxies, list) else []
    for node in config.get('nodes', []):
        if not isinstance(node, dict) or node.get('enabled') is False:
            continue
        if node.get('proxy_string'):
            try:
                proxy = yaml.safe_load(node['proxy_string'])
            except yaml.YAMLError:
                continue
            if isinstance(proxy, dict):
                candidates.append(proxy)
        else:
            params = node.get('params') or {}
            if isinstance(params, dict):
                candidates.append({**node, **params})
    targets = []
    for node in candidates:
        if not isinstance(node, dict) or str(node.get('type')).lower() not in (
                'socks5', 'http', 'ss', 'ssr', 'vmess', 'vless', 'trojan', 'anytls') or node.get('dialer-proxy'):
            continue
        host, port = node.get('server'), node.get('port')
        if not isinstance(host, str) or type(port) is not int or not 1 <= port <= 65535:
            continue
        try:
            address = ipaddress.ip_address(host.strip('[]'))
        except ValueError:
            address = None
        if address is not None and address.version != 6:
            continue
        targets.append({'host': host.strip('[]'), 'port': port, 'literal': address is not None})
    return sorted(targets, key=lambda r: not r['literal'])[:3]


def probe(manager, agent_id, profile_id):
    agent = _agent(manager, agent_id, profile_id)
    config = get_config(profile_id)
    settings, document = configuration(config)
    if settings['errors'] or settings['ipv6'] is not True:
        raise MonitorError('IPV6_DISABLED', 409, '请先保存有效的 Mihomo IPv6 配置')
    targets = _probe_targets(config, document)
    if not targets:
        return {'success': True, 'agent_id': agent_id, 'profile_id': profile_id,
                'connectivity': {'status': 'not_checked', 'message': '没有可直接检查的已保存 TCP 节点；订阅远程节点、链式和 UDP 节点需在 Mihomo 面板验证'}}
    results = []
    for target in targets:
        data = _request(agent, 'probe', {key: target[key] for key in ('host', 'port')})
        results.append(data.get('connectivity') or {'status': 'error', 'message': 'Agent 未提供连接结果'})
    passed = any(r.get('status') == 'ok' for r in results)
    details = [f"候选 {index + 1}：{item.get('message') or item.get('status')}" for index, item in enumerate(results)]
    return {'success': True, 'agent_id': agent_id, 'profile_id': profile_id, 'results': results,
            'connectivity': {'status': 'ok' if passed else 'error', 'scope': 'node_transport',
                             'details': details,
                             'message': '至少一个节点 IPv6 TCP 端口可达；尚未验证代理握手与客户端分流' if passed
                             else '本次检查的节点未能通过 IPv6 TCP 连接，请查看节点与网络状态'}}
