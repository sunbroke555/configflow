"""Editor and stable, authenticated runtime providers for selective gateways."""
from flask import jsonify, make_response, request

from backend.common.auth import require_auth, validate_token_or_jwt
from backend.common.config import get_config, save_config_with_publish_guard
from backend.common.linux_gateway import (
    LinuxGatewayError, gateway_editor_state, gateway_provider_yaml, gateway_source_yaml, validate_gateway_selection,
)
from backend.routes import custom_config_bp, profiles_bp


@profiles_bp.route('/<profile_id>/custom-config/linux-gateway', methods=['GET', 'POST'])
@custom_config_bp.route('/linux-gateway', methods=['GET', 'POST'])
@require_auth
def linux_gateway_settings(profile_id=None):
    config = get_config()
    try:
        guard = None
        if request.method == 'POST':
            value = request.get_json(silent=True)
            if not isinstance(value, dict):
                raise LinuxGatewayError('Linux 网关设置必须是 JSON 对象')
            settings = validate_gateway_selection(config, value.get('settings', value))
            config.setdefault('mihomo', {})['linux_gateway'] = settings
            guard = save_config_with_publish_guard(config, 'mihomo')
        result = gateway_editor_state(config)
        if guard is not None:
            result.update(success=True, publish_guard=guard)
        response = jsonify(result)
        response.headers['Cache-Control'] = 'no-store'
        return response
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
