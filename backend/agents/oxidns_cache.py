"""Flush DNS caches of one OxiDNS Agent's core.

The Agent discovers ``type: cache`` plugins in its own deployed configuration
and accepts only those tags. This bridge targets exactly the registered Agent
named in the request; core addresses and credentials never leave the Agent.
"""
import json
import re

import requests

from .oxidns_monitor import MonitorError, _agent_origin
from .version import compare_versions

MIN_AGENT_VERSION = '1.8.36-go'
MAX_RESPONSE_BYTES = 16 * 1024
MAX_TAGS = 64
# Same rule as the Agent's oxMonitorSafeTag: dot-separated alphanumeric-edged
# segments of letters, digits, '_' and '-', at most 64 characters.
_SEGMENT = r'[A-Za-z0-9](?:[A-Za-z0-9_-]*[A-Za-z0-9])?'
_TAG = re.compile(rf'(?=.{{1,64}}\Z){_SEGMENT}(?:\.{_SEGMENT})*')

MESSAGES = {
    'UPGRADE_REQUIRED': f'请将此 Agent 升级到 {MIN_AGENT_VERSION} 或更新版本后清理缓存。',
    'AGENT_NOT_FOUND': '未找到这台 Agent。',
    'WRONG_SERVICE': '清理缓存仅适用于 OxiDNS Agent。',
    'AGENT_UNREACHABLE': '暂时无法连接 Agent，请检查网络后重试。',
    'AGENT_AUTH_FAILED': 'Agent 认证失败，请检查注册状态。',
    'INVALID_REQUEST': '清理缓存请求无效。',
    'INVALID_RESPONSE': '缓存操作结果格式异常，请检查 Agent 和 OxiDNS 版本。',
    'API_NOT_CONFIGURED': 'OxiDNS 管理 API 未启用，请在设置中启用并发布一次。',
    'TLS_UNSUPPORTED': '清理缓存暂不支持当前 OxiDNS API 的 TLS 配置。',
    'CONFIG_UNSUPPORTED': '当前 OxiDNS 配置无法用于清理缓存，请检查管理 API 配置。',
    'CACHE_UNKNOWN': '设备当前配置中没有这个缓存实例，请刷新面板后重试。',
    'CACHE_FLUSH_UNSUPPORTED': '当前 OxiDNS 核心不支持清理缓存接口，请升级核心后重试。',
    'CORE_UNREACHABLE': '暂时无法连接 OxiDNS 管理 API，请检查核心运行状态。',
    'CORE_AUTH_FAILED': 'OxiDNS 管理 API 认证失败，请检查核心配置。',
    'CORE_BUSY': 'OxiDNS 缓存正忙，请稍后重试。',
    'SERVICE_BUSY': '此设备正在发布、重启或更新，请等待当前操作结束。',
    'CACHE_FLUSH_FAILED': '清理缓存失败，请检查 OxiDNS 日志后重试。',
}


def _fail(code, status=502):
    return MonitorError(code, status, MESSAGES.get(code, MESSAGES['CACHE_FLUSH_FAILED']))


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


def _tags(value):
    if (not isinstance(value, list) or len(value) > MAX_TAGS
            or any(not isinstance(tag, str) or not _TAG.fullmatch(tag) for tag in value)
            or len(set(value)) != len(value)):
        raise _fail('INVALID_RESPONSE')
    return value


def valid_request_tags(value):
    return (isinstance(value, list) and 0 < len(value) <= MAX_TAGS and len(set(value)) == len(value)
            and all(isinstance(tag, str) and _TAG.fullmatch(tag) for tag in value))


def call(manager, agent_id, method, tags=None):
    """GET lists the Agent's cache plugins; POST flushes the given tags."""
    if (method, tags is None) not in (('GET', True), ('POST', False)) or (
            tags is not None and not valid_request_tags(tags)):
        raise _fail('INVALID_REQUEST', 400)
    agent = _agent(manager, agent_id)
    path = '/api/oxidns/cache' if method == 'GET' else '/api/oxidns/cache/flush'
    headers = {'Authorization': 'Bearer ' + agent['token'], 'Accept': 'application/json'}
    if method == 'POST':
        headers['Content-Type'] = 'application/json'
    try:
        with requests.Session() as session:
            session.trust_env = False
            with session.request(method, _agent_origin(agent) + path,
                                 data=json.dumps({'tags': tags}) if method == 'POST' else None,
                                 headers=headers, timeout=(3, 40), allow_redirects=False,
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
    if not isinstance(data, dict):
        raise _fail('INVALID_RESPONSE')
    if status >= 400 or data.get('success') is False:
        code = data.get('code')
        if status == 404 and code not in MESSAGES:
            raise _fail('UPGRADE_REQUIRED', 409)
        error = _fail(code if isinstance(code, str) and code in MESSAGES else 'CACHE_FLUSH_FAILED',
                      status if status in (400, 409, 503, 504) else 502)
        # A partial flush already changed the core; report what completed.
        try:
            error.flushed = _tags(data.get('flushed', []))
        except MonitorError:
            error.flushed = []
        raise error
    if status != 200 or data.get('success') is not True:
        raise _fail('INVALID_RESPONSE')
    if method == 'GET':
        return {'success': True, 'caches': _tags(data.get('caches'))}
    flushed = _tags(data.get('flushed'))
    if flushed != tags:
        raise _fail('INVALID_RESPONSE')
    return {'success': True, 'flushed': flushed}
