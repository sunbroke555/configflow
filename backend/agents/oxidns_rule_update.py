"""Refresh deployed OxiDNS rule data through its registered Agent.

This bridge exposes one fixed operation. Core addresses, credentials, plugin
tags and download targets are resolved by the Agent from its deployed config.
"""
import json
import re

import requests

from .oxidns_monitor import MonitorError, _agent_origin
from .version import compare_versions

MIN_AGENT_VERSION = '1.8.30-go'
MAX_RESPONSE_BYTES = 16 * 1024

MESSAGES = {
    'UPGRADE_REQUIRED': f'请将此 Agent 升级到 {MIN_AGENT_VERSION} 或更新版本后使用更新规则。',
    'AGENT_NOT_FOUND': '未找到这台 Agent。',
    'WRONG_SERVICE': '更新规则仅适用于 OxiDNS Agent。',
    'AGENT_UNREACHABLE': '暂时无法连接 Agent，请检查网络后重新查询更新状态。',
    'AGENT_AUTH_FAILED': 'Agent 认证失败，请检查注册状态。',
    'INVALID_REQUEST': '更新规则请求无效。',
    'INVALID_RESPONSE': '规则更新状态格式异常，请检查 Agent 和 OxiDNS 版本。',
    'API_NOT_CONFIGURED': 'OxiDNS 管理 API 未启用，请在设置中启用并发布一次。',
    'TLS_UNSUPPORTED': '规则更新暂不支持当前 OxiDNS API 的 TLS 配置。',
    'CONFIG_UNSUPPORTED': '当前 OxiDNS 配置无法用于更新规则，请检查管理 API 和规则更新配置。',
    'RULE_UPDATE_NOT_CONFIGURED': '请先在 OxiDNS 设置中开启原生规则定时更新并发布一次，之后即可单独更新规则。',
    'RULE_UPDATE_AMBIGUOUS': '发现多条规则更新链，无法确定更新目标，请检查 OxiDNS 规则更新配置。',
    'CORE_UPDATE_UNSUPPORTED': '当前 OxiDNS 核心不支持原生规则更新接口，请升级核心后重试。',
    'CORE_UNREACHABLE': '暂时无法连接 OxiDNS 管理 API，请检查核心运行状态。',
    'CORE_AUTH_FAILED': 'OxiDNS 管理 API 认证失败，请检查核心配置。',
    'SERVICE_BUSY': '此设备正在发布、重启或更新，请等待当前操作结束。',
    'RULE_UPDATE_BUSY': '此设备正在更新规则，请等待当前更新完成。',
    'CORE_BUSY': 'OxiDNS 正在执行下载或重载，请等待当前操作结束。',
    'SNAPSHOT_CHANGED': '运行中的配置与已部署文件不一致，请先确认配置已应用，再更新规则。',
    'DOWNLOAD_FAILED': '规则下载未全部成功，本次未执行热重载，请检查 OxiDNS 日志。',
    'DOWNLOAD_TIMEOUT': '规则下载等待超时，核心可能仍在下载；请稍后核对状态和日志。',
    'RELOAD_FAILED': '部分规则未能热重载，已完成的更新不会自动回滚，请检查 OxiDNS 日志。',
    'RULE_UPDATE_TIMEOUT': '规则更新等待超时，请检查 OxiDNS 运行状态和日志。',
    'RULE_UPDATE_UNKNOWN': '暂时无法确认规则更新结果，请稍后核对状态和 OxiDNS 日志。',
    'RULE_UPDATE_FAILED': '规则更新失败，请检查 OxiDNS 日志后重试。',
}


def _fail(code, status=502):
    return MonitorError(code, status, MESSAGES.get(code, MESSAGES['RULE_UPDATE_FAILED']))


def _agent(manager, agent_id):
    agent = manager.get_agent_by_id(agent_id)
    if not agent:
        raise _fail('AGENT_NOT_FOUND', 404)
    if agent.get('service_type') != 'oxidns':
        raise _fail('WRONG_SERVICE', 400)
    try:
        supported = compare_versions(agent.get('version', '0'), MIN_AGENT_VERSION) >= 0
    except (ValueError, TypeError, AttributeError):
        supported = False
    if not supported:
        raise _fail('UPGRADE_REQUIRED', 409)
    return agent


def _task(value):
    if not isinstance(value, dict):
        raise _fail('INVALID_RESPONSE')
    state, phase, task_id = (value.get(key) for key in ('status', 'phase', 'id'))
    if state not in ('idle', 'running', 'succeeded', 'failed') or phase not in (
            'idle', 'downloading', 'reloading', 'done'):
        raise _fail('INVALID_RESPONSE')
    if not isinstance(task_id, str) or (state == 'idle' and task_id != '') or (
            state != 'idle' and not re.fullmatch(r'[A-Za-z0-9_-]{1,96}', task_id)):
        raise _fail('INVALID_RESPONSE')
    result = {'id': task_id, 'status': state, 'phase': phase}
    for key in ('downloaded', 'download_total', 'reloaded', 'reload_total'):
        count = value.get(key)
        if type(count) is not int or not 0 <= count <= 1_000_000:
            raise _fail('INVALID_RESPONSE')
        result[key] = count
    if result['downloaded'] > result['download_total'] or result['reloaded'] > result['reload_total']:
        raise _fail('INVALID_RESPONSE')
    if state == 'succeeded' and (phase != 'done' or result['downloaded'] != result['download_total']
                               or result['reloaded'] != result['reload_total']):
        raise _fail('INVALID_RESPONSE')
    if state == 'failed':
        code = value.get('code')
        result['code'] = code if isinstance(code, str) and code in MESSAGES else 'RULE_UPDATE_FAILED'
        result['message'] = MESSAGES[result['code']]
    return result


def call(manager, agent_id, method):
    if method not in ('GET', 'POST'):
        raise _fail('INVALID_REQUEST', 400)
    agent = _agent(manager, agent_id)
    try:
        url = _agent_origin(agent) + '/api/oxidns/rules/update'
        headers = {'Authorization': 'Bearer ' + agent['token'], 'Accept': 'application/json'}
        if method == 'POST':
            headers['Content-Type'] = 'application/json'
        with requests.Session() as session:
            session.trust_env = False
            with session.request(method, url, data='{}' if method == 'POST' else None,
                                 headers=headers, timeout=(3, 15), allow_redirects=False,
                                 stream=True) as response:
                status = response.status_code
                if status in (401, 403):
                    raise _fail('AGENT_AUTH_FAILED')
                if 300 <= status < 400:
                    raise _fail('INVALID_RESPONSE')
                chunks, size = [], 0
                for chunk in response.iter_content(4096):
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise _fail('INVALID_RESPONSE')
                    chunks.append(chunk)
                try:
                    data = json.loads(b''.join(chunks))
                except (ValueError, UnicodeError):
                    raise _fail('UPGRADE_REQUIRED' if status == 404 else 'INVALID_RESPONSE',
                                409 if status == 404 else 502) from None
    except MonitorError:
        raise
    except (requests.RequestException, KeyError, TypeError, ValueError):
        raise _fail('AGENT_UNREACHABLE') from None
    if status == 404:
        raise _fail('UPGRADE_REQUIRED', 409)
    if not isinstance(data, dict):
        raise _fail('INVALID_RESPONSE')
    if status >= 400 or data.get('success') is False:
        code = data.get('code')
        raise _fail(code if isinstance(code, str) and code in MESSAGES else 'RULE_UPDATE_FAILED',
                    status if status in (400, 409, 503, 504) else 502)
    if status not in (200, 202) or data.get('success') is not True:
        raise _fail('INVALID_RESPONSE')
    return {'success': True, 'task': _task(data.get('task'))}, status
