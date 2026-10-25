"""Profile-scoped OxiDNS template settings."""
from copy import deepcopy
from hashlib import sha256
import json
from flask import jsonify, request
from backend.routes import oxidns_bp
from backend.common.auth import require_auth
from backend.common.config import get_config
from backend.common.oxidns_settings import normalize_oxidns_settings, OxidnsSettingsError


_CHANGED_ELSEWHERE = 'OxiDNS 设置已在其他页面修改（例如策略规则的 DNS），请重新加载后再保存'


def _etag(settings):
    """Strong validator of the stored OxiDNS object.

    The rules page writes per-rule DNS into this object directly (and, in
    template mode, never touches native_config), so the editor guards its
    whole-object save with it.
    """
    text = json.dumps(settings if isinstance(settings, dict) else {}, sort_keys=True,
                      ensure_ascii=False, separators=(',', ':'))
    return '"' + sha256(text.encode()).hexdigest() + '"'


class _ChangedElsewhere(Exception):
    """If-Match no longer names the stored settings; nothing is saved."""


@oxidns_bp.route('/settings', methods=['GET', 'POST'])
@require_auth
def oxidns_settings():
    from backend.common.config import get_repository, reset_config_context
    from backend.common.profile_context import resolve_profile_id
    from backend.common.rule_dns import invalidate_follow
    try:
        if request.method == 'GET':
            config = get_config()
            # Older versions allowed incomplete plugin instances to be saved.
            # Keep them editable; new writes and every render still validate.
            response = jsonify(normalize_oxidns_settings(
                config.get('oxidns', {}), validate_native_arguments=False))
            response.headers['ETag'] = _etag(config.get('oxidns', {}))
            return response
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            raise OxidnsSettingsError('OxiDNS 设置必须是对象')
        # Reverse proxies that compress the response (the Docker image's nginx gzips
        # JSON) turn the strong ETag into W/"…"; the browser echoes it back as is.
        expected = (request.headers.get('If-Match') or '').strip().removeprefix('W/')
        # Whole-object updates are deliberate: the editor always loads defaults,
        # and callers can reset a feature without stale fields surviving.
        settings = normalize_oxidns_settings(value, validate_native_arguments=False)

        def update(current):
            # Checked under the write lock, so a rules-page DNS write can never
            # land between the check and the save. Requests without If-Match
            # (older clients, MCP) keep saving as before.
            if expected and expected != '*' and expected != _etag(current.get('oxidns', {})):
                raise _ChangedElsewhere()
            # A route changed here by hand is an explicit choice from now on.
            current['oxidns'] = invalidate_follow(current, settings)

        profile_id = resolve_profile_id()
        try:
            saved, revision = get_repository().update_profile_transaction(
                profile_id, update, include_config_revision=True)
        except _ChangedElsewhere:
            return jsonify({'success': False, 'code': 'oxidns_changed_elsewhere', 'message': _CHANGED_ELSEWHERE}), 409
        finally:
            reset_config_context()
        guard = {'expected_profile_id': profile_id, 'expected_service_type': 'oxidns',
                 'expected_config_revision': revision}
        response = jsonify({'success': True, 'settings': saved['oxidns'], 'draft': True, 'publish_guard': guard})
        response.headers['ETag'] = _etag(saved['oxidns'])
        return response
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
