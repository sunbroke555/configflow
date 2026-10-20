"""First-use setup endpoints; installation and publishing retain existing APIs."""
from flask import jsonify, request

from backend.common.auth import require_auth
from backend.common.config import get_repository
from backend.common.config_repository import ProfileNotFound, ProfileInUse, ProfileValidationError
from backend.common import setup_flow as flow
from backend.common.setup_presets import domain_rule_options, recommended_domain_rules
from backend.routes import settings_bp


def _body():
    if request.content_length is not None and request.content_length > 32768:
        raise flow.SetupError('向导请求过大', 413)
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise flow.SetupError('请提交有效 JSON 对象')
    return value


def _respond(action):
    try:
        result, status = action(), 200
    except flow.SetupError as error:
        result, status = {'success': False, 'message': str(error)}, error.status
    except ProfileNotFound:
        result, status = {'success': False, 'message': '该配置空间已删除，请重新开始向导'}, 404
    except (ProfileInUse, ProfileValidationError) as error:
        result, status = {'success': False, 'message': str(error)}, 409
    response = jsonify(result)
    response.headers['Cache-Control'] = 'no-store'
    return response, status


@settings_bp.route('/setup', methods=['GET', 'POST'])
@require_auth
def setup_sessions():
    def action():
        repository = get_repository()
        if request.method == 'POST':
            return flow.public(flow.create(repository, _body()))
        shared = repository.get_shared()
        sessions = flow.list_sessions(repository)
        return {'sessions': [flow.public(session) for session in sessions],
                'suggested': not sessions and not any(p.get('proxy_groups') for p in repository.export_all()['profiles'].values()),
                'subscriptions': [{'id': s['id'], 'name': s['name']} for s in shared['subscriptions'] if s.get('enabled', True)],
                'nodes': [{'id': n['id'], 'name': n['name']} for n in shared['nodes'] if n.get('enabled', True)],
                'domain_rules': domain_rule_options(repository), 'recommended_domain_rules': recommended_domain_rules(repository)}
    return _respond(action)


@settings_bp.route('/setup/<session_id>', methods=['GET', 'PATCH'])
@require_auth
def setup_session(session_id):
    def action():
        repository = get_repository()
        with flow.locked(repository, session_id):
            session = flow.read(repository, session_id)
            repository.get_profile(session['profile_id'])
            if request.method == 'PATCH':
                flow.update_progress(repository, session, _body())
                flow.write(repository, session)
            return flow.public(session)
    return _respond(action)


@settings_bp.route('/setup/<session_id>/<operation>', methods=['POST'])
@require_auth
def setup_action(session_id, operation):
    def action():
        repository = get_repository()
        with flow.locked(repository, session_id):
            session = flow.read(repository, session_id)
            repository.get_profile(session['profile_id'])
            body = _body()
            if operation == 'source':
                flow.source(repository, session, body)
            elif operation == 'prepare':
                flow.prepare(repository, session, body, request.url_root.rstrip('/'))
            elif operation == 'router-draft':
                flow.save_router_draft(session, body)
            elif operation in ('check', 'finish'):
                from backend.common import setup_completion
                if not session['prepared']:
                    raise flow.SetupError('请先生成配置方案', 409)
                if operation == 'check':
                    setup_completion.check_installation(repository, session)
                    if session.get('router_draft'):
                        # Persist the checked core fingerprint before the router
                        # commit advances the revision; retries can then resume.
                        flow.write(repository, session)
                    flow.connect_router_draft(repository, session)
                    session.update(step=4, max_step=max(session['max_step'], 4))
                else:
                    setup_completion.finish(repository, session, body)
            else:
                raise flow.SetupError('未知向导操作', 404)
            flow.write(repository, session)
            return flow.public(session)
    return _respond(action)
