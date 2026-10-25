"""「客户端设备」页：当前配置空间的设备名称与 OxiDNS DNS 策略，逐行保存为草稿。"""
from flask import Blueprint, jsonify, request

from backend.common.auth import require_auth
from backend.common.client_devices import DeviceChanged, list_devices, normalize_change, update_device
from backend.common.oxidns_device_policy import revision
from backend.common.oxidns_settings import OxidnsSettingsError

client_devices_bp = Blueprint('client_devices', __name__, url_prefix='/api/client-devices')


def _state(profile_id):
    from backend.common.agent_manager import get_agent_manager
    from backend.common.config import get_config, get_default_profile_id, get_repository
    config = get_config(profile_id)
    default = get_default_profile_id()
    from backend.routes.agents import _public_deployment
    agents = [{'id': agent['id'], 'name': agent.get('name') or agent['id'], 'host': agent.get('host') or '',
               'status': agent.get('status') or 'offline',
               'deployment': _public_deployment(agent['latest_deployment']) if agent.get('latest_deployment') else None}
              for agent in get_agent_manager().get_all_agents()
              if agent.get('service_type') == 'oxidns' and (agent.get('profile_id') or default) == profile_id]
    return {'success': True, 'profile_id': profile_id, 'profile_name': get_repository().get_profile(profile_id).get('name') or profile_id,
            'oxidns_revision': revision(config.get('oxidns') or {}), 'agents': agents, **list_devices(config)}


def _response(payload, status=200):
    result = jsonify(payload)
    result.headers['Cache-Control'] = 'no-store'
    return result, status


def _profile_id(by_agent):
    """(profile_id, None) or (None, error response). ?agent=<OxiDNS Agent> pins that device's profile."""
    from backend.common.agent_manager import get_agent_manager
    from backend.common.config import get_default_profile_id
    from backend.common.profile_context import resolve_profile_id
    agent_id = request.args.get('agent')
    if agent_id is not None and by_agent:
        agent = get_agent_manager().get_agent_by_id(agent_id)
        if not agent or agent.get('service_type') != 'oxidns':
            return None, _response({'success': False, 'message': '这台 OxiDNS Agent 已不存在，请返回 Agent 页刷新。'}, 404)
        return agent.get('profile_id') or get_default_profile_id(), None
    return resolve_profile_id(), None


@client_devices_bp.route('', methods=['GET', 'PUT'])
@require_auth
def client_devices():
    """GET ?agent=<OxiDNS Agent> 读取该设备绑定的配置空间，不随全局切换。

    PUT {profile_id, ip, name, mode, previous_name, previous_mode, base_url}；mode 为空表示不设 DNS 策略。
    """
    from backend.common.config import update_config_transaction
    profile_id, error = _profile_id(request.method == 'GET')
    if error:
        return error
    try:
        if request.method == 'PUT':
            body = request.get_json(silent=True)
            keys = {'profile_id', 'ip', 'name', 'mode', 'previous_name', 'previous_mode', 'base_url'}
            if not isinstance(body, dict) or set(body) != keys or not isinstance(body['base_url'], str):
                return _response({'success': False, 'message': '客户端设备请求格式无效。'}, 400)
            if body['profile_id'] != profile_id:
                return _response({'success': False, 'message': '配置空间已切换，请重新载入。'}, 409)
            change = normalize_change(body)
            update_config_transaction(lambda config: update_device(config, change, body['base_url']), profile_id)
        return _response(_state(profile_id))
    except DeviceChanged as error:
        return _response({'success': False, 'message': str(error)}, 409)
    except OxidnsSettingsError as error:
        return _response({'success': False, 'message': str(error)}, 400)


@client_devices_bp.route('/discovery', methods=['GET'])
@require_auth
def client_device_discovery():
    """实时读取配置空间内 RouterOS / iKuai 主路由看到的局域网设备，不保存结果。

    配置空间解析同 GET /api/client-devices（?profile= 或 ?agent=）。返回格式见 doc/oxidns-device-policies.md。
    """
    from backend.common.agent_manager import get_agent_manager
    from backend.common.client_discovery import discover
    from backend.common.config import get_config, get_default_profile_id
    profile_id, error = _profile_id(True)
    if error:
        return error
    default = get_default_profile_id()
    # Agent hosts are infrastructure, not client devices to add.
    hosts = [agent.get('host') or '' for agent in get_agent_manager().get_all_agents()
             if (agent.get('profile_id') or default) == profile_id]
    return _response({'success': True, 'profile_id': profile_id, **discover(get_config(profile_id), hosts)})
