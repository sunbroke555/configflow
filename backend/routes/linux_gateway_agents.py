"""Authenticated UI operations for one registered Linux gateway host."""
import json

from flask import jsonify, request

from backend.routes import agents_bp
from backend.common.auth import require_auth
from backend.common.agent_manager import get_agent_manager
from backend.common.profile_context import resolve_profile_id
from backend.agents import linux_gateway
from backend.agents.oxidns_monitor import MonitorError


@agents_bp.route('/<agent_id>/linux-gateway/<operation>', methods=['GET', 'POST'])
@require_auth
def linux_gateway_operation(agent_id, operation):
    try:
        # Agent routes are global; explicitly scope this action to the editor's
        # captured profile rather than the default/global sidebar selection.
        selected = request.headers.get('X-ConfigFlow-Profile') or request.args.get('profile_id')
        if not selected:
            raise MonitorError('PROFILE_REQUIRED', 400, '请从一个明确的配置空间打开 Linux 网关设置。')
        profile_id = resolve_profile_id(explicit=selected)
        body = None
        if request.method == 'POST':
            if request.content_length is not None and request.content_length > 4096:
                raise MonitorError('INVALID_REQUEST', 400, '网关请求内容过大。')
            raw = request.stream.read(4097)
            if len(raw) > 4096:
                raise MonitorError('INVALID_REQUEST', 400, '网关请求内容过大。')
            try:
                body = json.loads(raw) if raw else {}
            except (ValueError, UnicodeError):
                raise MonitorError('INVALID_REQUEST', 400, '网关参数必须是 JSON 对象。') from None
        data = linux_gateway.call(get_agent_manager(), agent_id, operation, request.method, profile_id, body)
        response = jsonify(data)
        response.headers['Cache-Control'] = 'no-store'
        return response
    except MonitorError as error:
        response = jsonify(success=False, code=error.code, message=error.message)
        response.headers['Cache-Control'] = 'no-store'
        return response, error.status
