"""Authenticated administrator orchestration and scoped installer capabilities."""
from flask import current_app, jsonify, make_response, request

from backend.agents import gateway_installation as installations
from backend.common.agent_manager import get_agent_manager
from backend.common.auth import parse_bearer_token, require_auth
from backend.common.config import get_repository
from backend.routes import agents_bp


def _body():
    if request.content_length is not None and request.content_length > 16384:
        raise installations.InstallationError('安装请求内容过大。', 413)
    import json
    raw = request.stream.read(16385)
    if len(raw) > 16384:
        raise installations.InstallationError('安装请求内容过大。', 413)
    try:
        value = json.loads(raw or b'{}')
    except (ValueError, UnicodeError):
        raise installations.InstallationError('安装请求需要 JSON 对象。') from None
    if not isinstance(value, dict):
        raise installations.InstallationError('安装请求需要 JSON 对象。')
    return value


def _respond(callback, status=200):
    try:
        result = callback()
        response = jsonify(result)
    except installations.InstallationError as exc:
        response, status = jsonify(success=False, message=str(exc)), exc.status
    except ValueError as exc:
        response, status = jsonify(success=False, message=str(exc)), 400
    response.headers['Cache-Control'] = 'no-store'
    return response, status


@agents_bp.route('/gateway-installations', methods=['GET', 'POST'])
@require_auth
def gateway_installations():
    repository, manager = get_repository(), get_agent_manager()
    return _respond(lambda: installations.create(repository, manager, _body()) if request.method == 'POST'
                    else installations.latest(repository, manager, request.args.get('profile_id')))


@agents_bp.route('/gateway-installations/<session_id>', methods=['GET'])
@require_auth
def gateway_installation_status(session_id):
    return _respond(lambda: installations.get(get_repository(), get_agent_manager(), session_id))


@agents_bp.route('/gateway-installations/<session_id>/network', methods=['POST'])
@require_auth
def gateway_installation_network(session_id):
    return _respond(lambda: installations.update_network(get_repository(), get_agent_manager(), session_id, _body()))


@agents_bp.route('/gateway-installations/<session_id>/publish', methods=['POST'])
@require_auth
def gateway_installation_publish(session_id):
    return _respond(lambda: installations.publish(get_repository(), get_agent_manager(), session_id, _body(),
                                                 current_app._get_current_object()), 202)


@agents_bp.route('/gateway-installations/<session_id>/script', methods=['GET'])
def gateway_installation_script(session_id):
    try:
        content = installations.script(get_repository(), session_id, request.args.get('token'))
        response = make_response(content)
        response.headers['Content-Type'] = 'text/plain; charset=utf-8'
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Referrer-Policy'] = 'no-referrer'
        return response
    except installations.InstallationError as exc:
        return _respond(lambda: (_ for _ in ()).throw(exc))


@agents_bp.route('/gateway-installations/<session_id>/enroll', methods=['POST'])
def gateway_installation_enroll(session_id):
    return _respond(lambda: installations.enroll(get_repository(), get_agent_manager(), session_id,
                    parse_bearer_token(request.headers.get('Authorization', '')), _body()))
