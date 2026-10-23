"""Editor and stable, authenticated runtime providers for selective gateways."""
from flask import jsonify, make_response, request

from backend.common.auth import require_auth, validate_token_or_jwt
from backend.common.config import get_config, get_repository, reset_config_context
from backend.common.profile_context import resolve_profile_id
from backend.common.linux_gateway import (
    LinuxGatewayError, gateway_editor_state, gateway_provider_yaml, gateway_source_yaml, gateway_settings,
)
from backend.common.router_split import (
    LEGACY_RESTORE_BLOCKED, MESSAGES, RouterSplitError, legacy_context, mode_from_context, route_rule_set_ids, router_split_state,
)
from backend.routes import custom_config_bp, profiles_bp
from backend.common.linux_gateway_draft import (
    check_preview, check_revision, prepare_restore, prepare_settings,
    preview_draft, remember_original, restore_summary,
)


def _response(value):
    response = jsonify(value)
    response.headers['Cache-Control'] = 'no-store'
    return response


def _guard(context, current, candidate, restore):
    """Refuse a change that would end the router split's takeover."""
    mode = mode_from_context(current, context)
    after = gateway_settings(candidate)
    if mode == 'invalid':
        raise RouterSplitError(MESSAGES['ROUTER_SPLIT_INVALID'], 'ROUTER_SPLIT_INVALID')
    if mode == 'full_tun':
        if restore:
            raise RouterSplitError(MESSAGES['ROUTER_SPLIT_RESTORE_BLOCKED'], 'ROUTER_SPLIT_RESTORE_BLOCKED')
        if after['enabled']:
            raise RouterSplitError(MESSAGES['ROUTER_SPLIT_FULL_TUN'], 'ROUTER_SPLIT_FULL_TUN')
    if mode == 'legacy' and not after['enabled']:
        # Turning the gateway off here removes TUN and restores forwarding.
        if restore:
            raise RouterSplitError(LEGACY_RESTORE_BLOCKED, 'ROUTER_SPLIT_RESTORE_BLOCKED')
        raise RouterSplitError(MESSAGES['ROUTER_SPLIT_LEGACY'], 'ROUTER_SPLIT_LEGACY')


def _router_split(repository, profile_id, config):
    state = router_split_state(repository, profile_id, config)
    return {'mode': state['mode'], 'source': state['source'], 'issues': state['issues'],
            'route_rule_set_ids': route_rule_set_ids(config)}


@profiles_bp.route('/<profile_id>/custom-config/linux-gateway', methods=['GET', 'POST'])
@custom_config_bp.route('/linux-gateway', methods=['GET', 'POST'])
@profiles_bp.route('/<profile_id>/custom-config/linux-gateway/preview', methods=['POST'])
@custom_config_bp.route('/linux-gateway/preview', methods=['POST'])
@profiles_bp.route('/<profile_id>/custom-config/linux-gateway/restore/preview', methods=['POST'])
@custom_config_bp.route('/linux-gateway/restore/preview', methods=['POST'])
@profiles_bp.route('/<profile_id>/custom-config/linux-gateway/restore', methods=['POST'])
@custom_config_bp.route('/linux-gateway/restore', methods=['POST'])
@require_auth
def linux_gateway_settings(profile_id=None):
    repository = get_repository()
    profile_id = resolve_profile_id(profile_id)
    try:
        guard = None
        # Setup sessions and Agent bindings are read outside the profile lock.
        context = legacy_context(repository, profile_id)
        config, revision = repository.get_publish_snapshot(profile_id)
        if request.method == 'POST':
            value = request.get_json(silent=True)
            if not isinstance(value, dict):
                raise LinuxGatewayError('Linux 网关设置必须是 JSON 对象')
            restore = '/linux-gateway/restore' in request.path
            action = 'restore' if restore else 'save'
            def prepare(current):
                if restore:
                    # The snapshot itself is only read after the guard allows it.
                    _guard(context, current, current, True)
                    candidate = prepare_restore(current)
                else:
                    candidate = prepare_settings(current, value.get('settings', value))
                _guard(context, current, candidate, restore)
                return candidate

            if request.path.endswith('/preview'):
                check_revision(value.get('expected_config_revision'), revision)
                return _response(preview_draft(config, prepare(config), revision, action, request.url_root.rstrip('/')))

            committed = {}
            def update(current):
                current_revision = repository.config_revision(profile_id)
                # Old integrations keep their request shape. The browser always
                # supplies both guards; restore always requires a fresh preview.
                guarded = restore or 'expected_config_revision' in value or 'preview_token' in value
                if guarded:
                    check_revision(value.get('expected_config_revision'), current_revision)
                candidate = prepare(current)
                if guarded:
                    check_preview(value, preview_draft(current, candidate, current_revision, action, request.url_root.rstrip('/')))
                if not restore:
                    remember_original(current, candidate)
                for key in ('mihomo', 'oxidns'):
                    if key in candidate:
                        current[key] = candidate[key]
                committed.update(current)

            _, revision = repository.update_profile_transaction(profile_id, update, include_config_revision=True)
            reset_config_context()
            config = committed
            guard = {'expected_profile_id': profile_id, 'expected_service_type': 'mihomo',
                     'expected_config_revision': revision}
        # Static usage guidance is presented in the editor's expandable help.
        result = gateway_editor_state(config, include_guidance=False)
        result.update(config_revision=revision, restore_point=restore_summary(config),
                      router_split=_router_split(repository, profile_id, config))
        if guard is not None:
            result.update(success=True, publish_guard=guard)
        return _response(result)
    except RouterSplitError as exc:
        return jsonify(success=False, code=exc.code, message=str(exc)), exc.status
    except LinuxGatewayError as exc:
        return jsonify({'success': False, 'message': str(exc)}), 400


@profiles_bp.route('/<profile_id>/linux-gateway/rules/<path:rule_id>', methods=['GET'])
@custom_config_bp.route('/linux-gateway/rules/<path:rule_id>', methods=['GET'])
def linux_gateway_rule_provider(rule_id, profile_id=None):
    config = get_config()
    auth = validate_token_or_jwt(request, config)
    if not auth['valid']:
        return jsonify({'success': False, 'message': auth['message']}), 401
    try:
        response = make_response(gateway_provider_yaml(config, rule_id))
        response.headers['Content-Type'] = 'text/yaml; charset=utf-8'
        response.headers['Cache-Control'] = 'no-store'
        return response
    except LinuxGatewayError as exc:
        return jsonify({'success': False, 'message': str(exc)}), 503


@profiles_bp.route('/<profile_id>/linux-gateway/source/<path:rule_id>', methods=['GET'])
@custom_config_bp.route('/linux-gateway/source/<path:rule_id>', methods=['GET'])
def linux_gateway_source_provider(rule_id, profile_id=None):
    config = get_config()
    auth = validate_token_or_jwt(request, config)
    if not auth['valid']:
        return jsonify({'success': False, 'message': auth['message']}), 401
    try:
        response = make_response(gateway_source_yaml(config, rule_id))
        response.headers['Content-Type'] = 'text/yaml; charset=utf-8'
        response.headers['Cache-Control'] = 'no-store'
        return response
    except LinuxGatewayError as exc:
        return jsonify({'success': False, 'message': str(exc)}), 503
