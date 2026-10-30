"""IPv6 checks use the editor's captured profile, never the global sidebar selection."""
from flask import jsonify, request

from backend.agents import ipv6_readiness
from backend.agents.oxidns_monitor import MonitorError
from backend.common.agent_manager import get_agent_manager
from backend.common.auth import require_auth
from backend.common.profile_context import resolve_profile_id
from backend.routes import agents_bp


@agents_bp.route('/<agent_id>/ipv6-readiness', methods=['GET'])
@agents_bp.route('/<agent_id>/ipv6-readiness/<operation>', methods=['POST'])
@require_auth
def ipv6_readiness_operation(agent_id, operation='status'):
    try:
        if operation not in ('status', 'prepare', 'probe') or (operation == 'status') != (request.method == 'GET'):
            raise MonitorError('NOT_ALLOWED', 404, '不支持这个 IPv6 操作')
        if request.content_length and request.content_length > 4096:
            raise MonitorError('INVALID_REQUEST', 400, 'IPv6 请求内容过大')
        body = request.get_json(silent=True) if request.method == 'POST' else {}
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
            data = ipv6_readiness.inspect(manager, agent_id, profile_id)
        elif operation == 'prepare':
            data = ipv6_readiness.prepare(manager, agent_id, profile_id, body)
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
