"""Profile-scoped OxiDNS template settings."""
from copy import deepcopy
from flask import jsonify, request
from backend.routes import oxidns_bp
from backend.common.auth import require_auth
from backend.common.config import get_config, save_config_with_publish_guard
from backend.common.oxidns_settings import normalize_oxidns_settings, OxidnsSettingsError


@oxidns_bp.route('/settings', methods=['GET', 'POST'])
@require_auth
def oxidns_settings():
    config = get_config()
    try:
        if request.method == 'GET':
            # Older versions allowed incomplete plugin instances to be saved.
            # Keep them editable; new writes and every render still validate.
            return jsonify(normalize_oxidns_settings(
                config.get('oxidns', {}), validate_native_arguments=False))
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            raise OxidnsSettingsError('OxiDNS 设置必须是对象')
        # Whole-object updates are deliberate: the editor always loads defaults,
        # and callers can reset a feature without stale fields surviving.
        settings = normalize_oxidns_settings(value, validate_native_arguments=False)
        config['oxidns'] = settings
        guard = save_config_with_publish_guard(config, 'oxidns')
        return jsonify({'success': True, 'settings': settings, 'draft': True, 'publish_guard': guard})
    except OxidnsSettingsError as exc:
        return jsonify({'success': False, 'message': str(exc)}), 400


@oxidns_bp.route('/render', methods=['POST'])
@require_auth
def render_oxidns_draft():
    """Render an authenticated editor draft without persisting or redacting it."""
    from backend.converters.oxidns import generate_oxidns_config
    try:
        value = request.get_json(silent=True)
        if not isinstance(value, dict) or not isinstance(value.get('settings'), dict):
            raise OxidnsSettingsError('请提供 settings 配置对象')
        base_url = value.get('base_url', request.url_root.rstrip('/'))
        if not isinstance(base_url, str):
            raise OxidnsSettingsError('base_url 必须为文本')
        config = deepcopy(get_config())
        config['oxidns'] = normalize_oxidns_settings(value['settings'])
        response = jsonify({'config': generate_oxidns_config(config, base_url)})
        response.headers['Cache-Control'] = 'no-store'
        return response
    except OxidnsSettingsError as exc:
        return jsonify({'success': False, 'message': str(exc)}), 400


def _editor_request():
    value = request.get_json(silent=True)
    if not isinstance(value, dict) or not isinstance(value.get('settings'), dict):
        raise OxidnsSettingsError('请提供 settings 配置对象')
    base_url = value.get('base_url', request.url_root.rstrip('/'))
    if not isinstance(base_url, str):
        raise OxidnsSettingsError('base_url 必须为文本')
    return value, base_url


def _editor_response(value):
    response = jsonify(value)
    response.headers['Cache-Control'] = 'no-store'
    return response


@oxidns_bp.route('/validate', methods=['POST'])
@require_auth
def validate_oxidns_draft():
    from backend.common.oxidns_validation import validate_settings
    try:
        value, base_url = _editor_request()
        return _editor_response(validate_settings(value['settings'], get_config(), base_url))
    except OxidnsSettingsError as exc:
        return jsonify({'success': False, 'message': str(exc)}), 400


@oxidns_bp.route('/editor', methods=['POST'])
@require_auth
def prepare_oxidns_editor():
    """Prepare a unified native draft; saving remains an explicit separate call."""
    from backend.common.oxidns_editor import prepare_editor
    try:
        value, base_url = _editor_request()
        return _editor_response({'settings': prepare_editor(value['settings'], get_config(), base_url)})
    except OxidnsSettingsError as exc:
        return jsonify({'success': False, 'message': str(exc)}), 400


@oxidns_bp.route('/template-preview', methods=['POST'])
@require_auth
def preview_oxidns_template():
    """Compare the template with its prior baseline without changing saved state."""
    from backend.common.oxidns_editor import preview_template
    try:
        value, base_url = _editor_request()
        return _editor_response(preview_template(value['settings'], value.get('template_settings'),
                                                 get_config(), base_url, value.get('resolutions')))
    except OxidnsSettingsError as exc:
        return jsonify({'success': False, 'message': str(exc)}), 400


@oxidns_bp.route('/common-settings', methods=['POST'])
@require_auth
def read_oxidns_common_settings():
    """Read common controls from the native draft without persisting anything."""
    from backend.common.oxidns_common import read_common_settings
    try:
        value, base_url = _editor_request()
        return _editor_response(read_common_settings(value['settings'], get_config(), base_url))
    except OxidnsSettingsError as exc:
        return jsonify({'success': False, 'message': str(exc)}), 400


@oxidns_bp.route('/common-settings/apply', methods=['POST'])
@require_auth
def apply_oxidns_common_settings():
    """Patch requested native draft fields, retaining every unrelated value."""
    from backend.common.oxidns_common import apply_common_settings
    try:
        value, base_url = _editor_request()
        return _editor_response(apply_common_settings(value['settings'], value.get('changes'), get_config(), base_url))
    except OxidnsSettingsError as exc:
        return jsonify({'success': False, 'message': str(exc)}), 400


@oxidns_bp.route('/native-settings/apply', methods=['POST'])
@require_auth
def apply_oxidns_native_settings():
    """Reconcile an explicit complete-form edit without saving the draft."""
    from backend.common.oxidns_native_apply import apply_native_settings
    try:
        value, base_url = _editor_request()
        return _editor_response(apply_native_settings(value['settings'], value.get('native_config'), get_config(), base_url, value.get('renames'), value.get('standard_add')))
    except OxidnsSettingsError as exc:
        payload = {'success': False, 'message': str(exc)}
        if getattr(exc, 'references', None):
            payload['references'] = exc.references
        return _editor_response(payload), 400
