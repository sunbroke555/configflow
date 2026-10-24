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
    agents = [{'id': agent['id'], 'name': agent.get('name') or agent['id'], 'host': agent.get('host') or '',
               'status': agent.get('status') or 'offline'}
              for agent in get_agent_manager().get_all_agents()
              if agent.get('service_type') == 'oxidns' and (agent.get('profile_id') or default) == profile_id]
    return {'success': True, 'profile_id': profile_id, 'profile_name': get_repository().get_profile(profile_id).get('name') or profile_id,
            'oxidns_revision': revision(config.get('oxidns') or {}), 'agents': agents, **list_devices(config)}


def _response(payload, status=200):
    result = jsonify(payload)
    result.headers['Cache-Control'] = 'no-store'
    return result, status


@client_devices_bp.route('', methods=['GET', 'PUT'])
@require_auth
def client_devices():
    """PUT {profile_id, ip, name, mode, previous_name, previous_mode, base_url}；mode 为空表示不设 DNS 策略。"""
    from backend.common.config import update_config_transaction
    from backend.common.profile_context import resolve_profile_id
    profile_id = resolve_profile_id()
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
