"""First-use setup endpoints; installation and publishing retain existing APIs."""
from flask import jsonify, request

from backend.common.auth import require_auth
from backend.common.config import get_repository
from backend.common.config_repository import ProfileNotFound, ProfileInUse, ProfileValidationError
from backend.common import setup_abandon, setup_flow as flow
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


def _described(repository, session):
    from backend.common.setup_completion import profile_summary
    result = {**flow.public(session), 'default_profile': profile_summary(repository, session)}
    if session.get('goal') == 'router' and session.get('prepared') and 'legacy_takeover' not in session:
        # Prepared before this server recorded the takeover; decide as prepare does.
        from backend.common.linux_gateway import LinuxGatewayError
        try:
            result['legacy_takeover'] = flow.legacy_takeover(repository.get_profile(session['profile_id']))
        except LinuxGatewayError:
            pass  # A malformed gateway setting is reported where it is edited.
    return result


@settings_bp.route('/setup', methods=['GET', 'POST'])
@require_auth
def setup_sessions():
    def action():
        repository = get_repository()
        if request.method == 'POST':
            return flow.public(flow.create(repository, _body()))
        shared = repository.get_shared()
        sessions = flow.list_sessions(repository)
        # The first-start template already has proxy groups; a device that
        # ConfigFlow manages is what shows the setup is under way.
        system = repository.get_system()
        return {'sessions': [flow.public(session) for session in sessions],
                # An explicitly abandoned setup must not turn into a new prompt.
                'suggested': not sessions and not system.get('agents')
                and not system.get('system_config', {}).get(setup_abandon.DISMISSED_KEY),
                'subscriptions': [{'id': s['id'], 'name': s['name']} for s in flow.selectable_subscriptions(shared)],
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
                session.pop('abandoned', None)  # Continuing reopens an abandoned setup.
                flow.write(repository, session)
            return _described(repository, session)
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
            if operation == 'abandon':
                if set(body) - {'dry_run'} or type(body.get('dry_run', False)) is not bool:
                    raise flow.SetupError('请提交有效的放弃请求')
                return setup_abandon.plan(repository, session) if body.get('dry_run') else setup_abandon.abandon(repository, session)
            if operation != 'check':
                # The page re-checks a gateway client on its own; only the
                # user's own edits reopen an abandoned setup.
                session.pop('abandoned', None)
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
            return _described(repository, session)
    return _respond(action)
