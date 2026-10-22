"""Bounded, profile-scoped bridge to the Agent's opt-in Linux gateway API."""
import ipaddress
import json
import re

import requests

from backend.common.config import get_config
from .oxidns_monitor import MonitorError, _agent_origin
from .version import compare_versions

MIN_AGENT_VERSION = '1.8.25-go'
MAX_RESPONSE_BYTES = 512 * 1024
OPERATIONS = {'status': 'GET', 'preview': 'POST', 'apply': 'POST', 'remove': 'POST'}
SPEC_FIELDS = {'interface', 'address', 'lan_cidr', 'upstream_gateway'}


def supports_gateway(agent):
    version = agent.get('version', '')
    if not isinstance(version, str) or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:-go)?', version):
        return False
    try:
        return compare_versions(version, MIN_AGENT_VERSION) >= 0
    except (ValueError, TypeError, AttributeError):
        return False


def require_gateway_agent(agent):
    if not supports_gateway(agent):
        raise ValueError(f'Linux 网关需要 Mihomo Agent {MIN_AGENT_VERSION} 或更新版本，请先升级 Agent 后再发布。')


def require_gateway_bundle_agent(agent, bundle):
    """Guard upload even if a caller bypassed shared profile preparation."""
    if agent.get('service_type', 'mihomo') != 'mihomo' or supports_gateway(agent):
        return
    import io
    import tarfile
    import yaml

    with tarfile.open(fileobj=io.BytesIO(bundle.archive), mode='r:gz') as archive:
        document = yaml.safe_load(archive.extractfile('files/' + bundle.manifest['config_path']).read())
    gateway = document.get('x-configflow-linux-gateway') if isinstance(document, dict) else None
    if isinstance(gateway, dict) and gateway.get('enabled') is True:
        require_gateway_agent(agent)


def _error(code, status, message):
    return MonitorError(code, status, message)


def _spec(body):
    if not isinstance(body, dict) or set(body) != SPEC_FIELDS:
        raise _error('INVALID_REQUEST', 400, '请填写网卡、Linux IPv4、LAN 网段和主路由 IPv4。')
    if any(not isinstance(v, str) or not v or len(v) > 64 for v in body.values()):
        raise _error('INVALID_REQUEST', 400, '网关转发的网络参数必须是有效的短文本。')
    interface = body['interface']
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,14}', interface) or interface == 'lo':
        raise _error('INVALID_REQUEST', 400, '请选择实际的 LAN 网卡，不能使用回环网卡。')
    try:
        address = ipaddress.IPv4Address(body['address'])
        upstream = ipaddress.IPv4Address(body['upstream_gateway'])
        network = ipaddress.IPv4Network(body['lan_cidr'], strict=True)
    except ValueError:
        raise _error('INVALID_REQUEST', 400, '需要合法 IPv4 地址和规范 LAN 网段，例如 192.168.1.0/24。') from None
    if not 8 <= network.prefixlen <= 30 or any(
        value not in network or value in (network.network_address, network.broadcast_address)
        or value.is_multicast or value.is_unspecified or value.is_loopback or value.is_link_local
        for value in (address, upstream)
    ) or address == upstream:
        raise _error('INVALID_REQUEST', 400, 'Linux 和主路由需要不同的可用 IPv4 地址，且位于同一个 /8 至 /30 LAN 网段。')
    return {**body, 'address': str(address), 'upstream_gateway': str(upstream), 'lan_cidr': str(network)}


def call(manager, agent_id, operation, method, profile_id, body=None):
    if OPERATIONS.get(operation) != method:
        raise _error('NOT_ALLOWED', 404, '不支持这个网关操作。')
    agent = manager.get_agent_by_id(agent_id)
    if not agent:
        raise _error('AGENT_NOT_FOUND', 404, '未找到这台 Agent。')
    if not profile_id or agent.get('profile_id') != profile_id:
        raise _error('PROFILE_CHANGED', 409, '设备绑定的配置空间已变化，请重新打开网关设置。')
    if agent.get('service_type') != 'mihomo':
        raise _error('WRONG_SERVICE', 400, 'Linux 网关只能应用到 Mihomo Agent。')
    if agent.get('deployment_method') == 'docker':
        raise _error('UNSUPPORTED_ENVIRONMENT', 409, '自动网络配置适用于 Linux Shell 安装，请选择原生安装的 Mihomo Agent。')
    if not supports_gateway(agent):
        raise _error('UPGRADE_REQUIRED', 409, f'请先将 Mihomo Agent 更新到 {MIN_AGENT_VERSION} 或更新版本。')
    if operation == 'apply':
        config = get_config(profile_id)
        if (config.get('mihomo', {}).get('linux_gateway') or {}).get('enabled') is not True:
            raise _error('GATEWAY_DISABLED', 409, '请先启用并保存当前配置空间的 Linux 网关。')
    if operation in ('preview', 'apply'):
        payload = {**_spec(body), 'profile_id': profile_id}
    elif operation == 'remove':
        if body not in (None, {}):
            raise _error('INVALID_REQUEST', 400, '撤销网关转发不接受自定义命令或网络参数。')
        payload = {}
    else:
        payload = None
    try:
        with requests.Session() as session:
            session.trust_env = False
            with session.request(method, _agent_origin(agent) + '/api/linux-gateway/' + operation,
                                 json=payload, headers={'Authorization': 'Bearer ' + agent['token'],
                                                        'Accept': 'application/json'},
                                 timeout=(3, 45), allow_redirects=False, stream=True) as response:
                chunks, size = [], 0
                for chunk in response.iter_content(64 * 1024):
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise _error('INVALID_RESPONSE', 502, 'Agent 返回的网关数据过大。')
                    chunks.append(chunk)
                status = response.status_code
                raw = b''.join(chunks)
    except MonitorError:
        raise
    except (requests.RequestException, KeyError, TypeError, ValueError):
        message = ('未能确认网关操作结果，请先刷新设备网络状态，勿重复应用。'
                   if operation in ('apply', 'remove') else '暂时无法连接 Agent，请检查设备连接。')
        raise _error('AGENT_UNREACHABLE', 502, message) from None
    if status == 404:
        raise _error('UPGRADE_REQUIRED', 409, f'设备尚未提供网关接口，请更新到包含本功能的 {MIN_AGENT_VERSION} 或更新版本。')
    if status in (401, 403):
        raise _error('AGENT_AUTH_FAILED', 502, 'Agent 认证失败，请检查设备注册状态。')
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        raise _error('INVALID_RESPONSE', 502, 'Agent 返回的网关数据无法解析，请刷新设备状态。') from None
    if not isinstance(data, dict):
        raise _error('INVALID_RESPONSE', 502, 'Agent 返回的网关数据格式异常。')
    if status >= 400:
        message = data.get('message') or data.get('error')
        if not isinstance(message, str) or not message:
            message = '网关操作未完成，请刷新状态后查看检查结果。'
        raise _error('GATEWAY_ERROR', status if status in (400, 409, 422) else 502, message[:1000])
    return data
