"""Docker HA guide generation and narrowly scoped installer enrollment."""
import json
from flask import jsonify, request

from backend.agents import docker_ha_enrollment as enrollment
from backend.agents.docker_ha_guide import generate_guide
from backend.common.agent_manager import get_agent_manager
from backend.common.auth import parse_bearer_token, require_auth
from backend.common.config import get_repository
from backend.common.profile_context import resolve_profile_id
from backend.routes import agents_bp


def _body():
    raw = request.stream.read(16385)
    if len(raw) > 16384:
        raise enrollment.EnrollmentError('安装请求内容过大。', 413)
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ValueError('安装请求需要 JSON 对象。') from None
    if not isinstance(value, dict):
        raise ValueError('安装请求需要 JSON 对象。')
    return value


def _respond(callback):
    try:
        result, status = callback(), 200
    except enrollment.EnrollmentError as error:
        result, status = {'success': False, 'message': str(error)}, error.status
    except ValueError as error:
        result, status = {'success': False, 'message': str(error)}, 400
    response = jsonify(result)
    response.headers['Cache-Control'] = 'no-store'
    return response, status


@agents_bp.route('/docker-oxidns-ha-guide', methods=['POST'])
@require_auth
def docker_oxidns_ha_guide():
    def generate():
        body = _body()
        result = generate_guide(body)
        if body.get('action', 'generate') == 'generate' and body.get('mode', 'new') == 'new':
            profile_id = resolve_profile_id(request.headers.get('X-ConfigFlow-Profile'))
            capability = enrollment.issue(get_repository(), body, profile_id)
            result = generate_guide(body, enrollment=capability)
            result['enrollment_expires_at'] = capability['expires_at']
            result['warnings'].append('新装命令的本机安装凭据有效期为 24 小时；过期后重新生成。生成命令不会创建设备记录。')
        return result
    return _respond(generate)


@agents_bp.route('/docker-oxidns-ha-enroll/<session_id>', methods=['POST'])
def docker_oxidns_ha_enroll(session_id):
    def register():
        if _body() != {}:
            raise ValueError('安装登记无需提交设备信息，目标由生成命令时固定。')
        return {'success': True, **enrollment.enroll(get_repository(), get_agent_manager(), session_id,
                                 parse_bearer_token(request.headers.get('Authorization')))}
    return _respond(register)
