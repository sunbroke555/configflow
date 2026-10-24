"""Authenticated UI operations for one registered Linux gateway host."""
import json

from flask import jsonify, request

from backend.routes import agents_bp
from backend.common.auth import require_auth
from backend.common.agent_manager import get_agent_manager
from backend.common.profile_context import resolve_profile_id
from backend.agents import linux_gateway
from backend.agents.oxidns_monitor import MonitorError
from backend.utils.logger import get_logger, log_text

logger = get_logger(__name__)


def _no_store(response, status=200):
    response.headers['Cache-Control'] = 'no-store'
    return response, status


@agents_bp.route('/gateway-forwarding', methods=['GET', 'POST'])
@require_auth
def agent_gateway_forwarding():
    """IPv4 forwarding on Agents that a router in any profile uses as its proxy gateway (Agents page)."""
    linked = linux_gateway.proxy_gateways_all_profiles()
    manager = get_agent_manager()
    if request.method == 'GET':
        return _no_store(jsonify({'success': True, 'gateways': [
            linux_gateway.gateway_forwarding_entry(manager, agent_id, refs) for agent_id, refs in linked.items()]}))
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or set(body) != {'agent_id'} or not isinstance(body['agent_id'], str):
        return _no_store(jsonify({'success': False, 'code': 'INVALID_REQUEST', 'message': '请求只能包含 agent_id。'}), 400)
    agent_id = body['agent_id']
    if agent_id not in linked:
        return _no_store(jsonify({'success': False, 'code': 'NOT_LINKED',
                                  'message': '这台 Agent 不是任何路由器的代理网关。'}), 404)
    try:
        data = linux_gateway.persist_gateway_forwarding(manager, agent_id, linked[agent_id])
    except MonitorError as error:
        return _no_store(jsonify({'success': False, 'code': error.code, 'message': error.message}), error.status)
    logger.info('已为代理网关 Agent %s 开启并持久化 IPv4 转发', log_text(agent_id))
    return _no_store(jsonify(data))


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
