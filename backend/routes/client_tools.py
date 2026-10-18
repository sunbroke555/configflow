"""Agent-bound display notes and connection rule actions; no controller writes."""
from flask import jsonify, request

from backend.routes import agents_bp as bp
from backend.common.auth import require_auth
from backend.common.agent_manager import get_agent_manager
from backend.common.config import get_config, get_default_profile_id, get_repository, update_config_transaction
from backend.common.config_repository import ProfileInUse, ProfileValidationError
from backend.common.device_notes import normalize_name, read_notes, set_note
from backend.common.oxidns_device_policy import normalize_client_range
from backend.common.oxidns_settings import OxidnsSettingsError
from backend.utils import domain_discovery_service as discovery


def bound_profile(agent_id, services):
    agent = get_agent_manager().get_agent_by_id(agent_id)
    if not agent or agent.get('service_type') not in services:
        raise ProfileValidationError('请选择有效的 Mihomo / OxiDNS 设备。')
    return agent.get('profile_id') or get_default_profile_id()


def check_binding(agent_id, profile_id, services):
    if bound_profile(agent_id, services) != profile_id:
        raise ProfileInUse('设备绑定的配置空间已改变，请重新打开后再保存。')


def profile_label(profile_id):
    return get_repository().get_profile(profile_id).get('name') or profile_id


def response(payload):
    result = jsonify({'success': True, **payload})
    result.headers['Cache-Control'] = 'no-store'
    return result


@bp.route('/<agent_id>/client-notes', methods=['GET', 'PUT'])
@require_auth
def client_notes(agent_id):
    services = ('mihomo', 'oxidns')
    profile_id = bound_profile(agent_id, services)
    try:
        if request.method == 'PUT':
            body = request.get_json(silent=True)
            if not isinstance(body, dict) or set(body) != {'profile_id', 'ip', 'name', 'previous_name'}:
                raise ProfileValidationError('设备备注请求格式无效。')
            if body['profile_id'] != profile_id:
                raise ProfileInUse('设备绑定的配置空间已改变，请重新打开后再保存。')
            ip, name = normalize_client_range(body['ip']), normalize_name(body['name'])
            previous = normalize_name(body['previous_name'])

            def update(config):
                check_binding(agent_id, profile_id, services)
                if read_notes(config).get(ip, '') != previous:
                    raise ProfileInUse('此设备的备注已在其他页面修改，请重新打开后再保存。')
                set_note(config, ip, name)

            update_config_transaction(update, profile_id)
        return response({'profile_id': profile_id, 'profile_name': profile_label(profile_id),
                         'notes': read_notes(get_config(profile_id))})
    except OxidnsSettingsError as exc:
        raise ProfileValidationError(str(exc)) from exc


@bp.route('/<agent_id>/mihomo/rule-targets', methods=['GET'])
@require_auth
def rule_targets(agent_id):
    profile_id = bound_profile(agent_id, ('mihomo',))
    config = get_config(profile_id)
    settings = discovery.read_settings(config)
    return response({'profile_id': profile_id, 'profile_name': profile_label(profile_id),
                     **{target: discovery.ruleset_info(config, settings[target + '_ruleset'])
                        for target in ('direct', 'proxy')}})


@bp.route('/<agent_id>/mihomo/rule-targets/apply', methods=['POST'])
@require_auth
def connection_rule(agent_id):
    profile_id = bound_profile(agent_id, ('mihomo',))
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or set(body) != {'profile_id', 'ruleset_id', 'target', 'value', 'rule_type'}:
        raise ProfileValidationError('连接规则请求格式无效。')
    if body['profile_id'] != profile_id:
        raise ProfileInUse('设备绑定的配置空间已改变，请重新打开后再保存。')
    if body['target'] not in ('direct', 'proxy') or not isinstance(body['ruleset_id'], str) or not body['ruleset_id']:
        raise ProfileValidationError('请选择域名发现中已设置的默认规则集。')
    entries = discovery.parse_apply_items([{key: body[key] for key in ('target', 'value', 'rule_type')}])

    def guard(infos=None):
        check_binding(agent_id, profile_id, ('mihomo',))
        # Read through the repository inside the shared write lock, bypassing request caches.
        fresh = get_repository().get_compat_config(profile_id)
        settings = discovery.read_settings(fresh)
        if settings.get(body['target'] + '_ruleset') != body['ruleset_id'] or (
                infos is not None and infos[body['target']]['id'] != body['ruleset_id']):
            raise ProfileInUse('默认规则集已改变，请重新打开后再保存。')

    guard()
    return response(discovery.apply_domains(profile_id, entries, source='manual', before_write=guard))
