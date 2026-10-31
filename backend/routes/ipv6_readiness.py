"""IPv6 checks use the editor's captured profile, never the global sidebar selection."""
from flask import jsonify, request

from backend.agents import ipv6_readiness
from backend.agents import ipv6_verification
from backend.agents.oxidns_monitor import MonitorError
from backend.common.agent_manager import get_agent_manager
from backend.common.auth import require_auth
from backend.common.profile_context import resolve_profile_id
from backend.routes import agents_bp


@agents_bp.route('/<agent_id>/ipv6-readiness', methods=['GET'])
@agents_bp.route('/<agent_id>/ipv6-readiness/verify', methods=['GET'], defaults={'operation': 'verify'})
@agents_bp.route('/<agent_id>/ipv6-readiness/verification-settings', methods=['GET', 'PUT'], defaults={'operation': 'verification-settings'})
@agents_bp.route('/<agent_id>/ipv6-readiness/<operation>', methods=['POST'])
@require_auth
def ipv6_readiness_operation(agent_id, operation='status'):
    try:
        methods = {'status': ('GET',), 'prepare': ('POST',), 'probe': ('POST',),
                   'verify': ('GET', 'POST'), 'verification-settings': ('GET', 'PUT')}
        if request.method not in methods.get(operation, ()):
            raise MonitorError('NOT_ALLOWED', 404, '不支持这个 IPv6 操作')
        if request.content_length and request.content_length > 4096:
            raise MonitorError('INVALID_REQUEST', 400, 'IPv6 请求内容过大')
        body = request.get_json(silent=True) if request.method in ('POST', 'PUT') else {}
        if not isinstance(body, dict):
            raise MonitorError('INVALID_REQUEST', 400, 'IPv6 请求必须是 JSON 对象')
        body = dict(body)
        selected = [value for value in (request.headers.get('X-ConfigFlow-Profile'),
                                       request.args.get('profile_id'), body.pop('profile_id', None)) if value]
        if not selected or any(not isinstance(value, str) or value != selected[0] for value in selected):
            raise MonitorError('PROFILE_REQUIRED', 400, '请从同一个明确的配置空间打开 IPv6 设置')
        profile_id = resolve_profile_id(explicit=selected[0])
        manager = get_agent_manager()
        if operation == 'status':
            # ?refresh=1 is the user's 「重新检查」 or a confirmed publish; otherwise the
            # last check is reused while nothing it depends on has changed.
            config, revision = manager.repository.get_publish_snapshot(profile_id)
            data = ipv6_readiness.status(manager, agent_id, profile_id, config, revision,
                                         refresh=request.args.get('refresh') in ('1', 'true'))
        elif operation == 'prepare':
            before, revision = manager.repository.get_publish_snapshot(profile_id)
            data = ipv6_readiness.prepare(manager, agent_id, profile_id, body)
            after, current_revision = manager.repository.get_publish_snapshot(profile_id)
            if before == after and revision == current_revision:
                data['config_revision'] = revision
        elif operation == 'verify':
            data = (ipv6_verification.result(manager, agent_id, profile_id, request.args.get('deployment_id'))
                    if request.method == 'GET' else ipv6_verification.start(manager, agent_id, profile_id, body))
        elif operation == 'verification-settings':
            data = ipv6_verification.settings(manager, agent_id, profile_id, body if request.method == 'PUT' else None)
        else:
            if body:
                raise MonitorError('INVALID_REQUEST', 400, '连接检查只使用当前配置空间中已保存的节点')
            data = ipv6_readiness.probe(manager, agent_id, profile_id)
        response = jsonify(data)
        status = 200
    except MonitorError as error:
        response = jsonify(success=False, code=error.code, message=error.message)
        status = error.status
    response.headers['Cache-Control'] = 'no-store'
    return response, status
