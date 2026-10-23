"""Explicit, profile-scoped preparation for the Linux gateway install wizard."""
from flask import jsonify, request

from backend.common.auth import require_auth
from backend.common.config import get_repository, reset_config_context
from backend.common.config_repository import ProfileInUse
from backend.common.gateway_onboarding import (
    GatewayOnboardingError, GatewayOnboardingBlocked,
    gateway_onboarding_state, onboarding_takeover, prepare_gateway_onboarding,
)
from backend.common.linux_gateway import gateway_settings
from backend.common.router_split import read_marker
from backend.routes import profiles_bp


def _response(value, status=200):
    response = jsonify(value)
    response.headers['Cache-Control'] = 'no-store'
    return response, status


def _body():
    if request.content_length and request.content_length > 262144:
        raise GatewayOnboardingError('安装设置超过 256 KiB 限制')
    import json
    try:
        data = request.stream.read(262145)
        if len(data) > 262144:
            raise GatewayOnboardingError('安装设置超过 256 KiB 限制')
        return json.loads(data)
    except (ValueError, UnicodeDecodeError):
        raise GatewayOnboardingError('安装设置必须是有效 JSON') from None


@profiles_bp.route('/<profile_id>/gateway-onboarding', methods=['GET', 'POST'])
@profiles_bp.route('/<profile_id>/gateway-onboarding/preview', methods=['POST'])
@require_auth
def gateway_onboarding(profile_id):
    repository = get_repository()
    base_url = request.url_root.rstrip('/')
    try:
        if request.method == 'GET':
            config, revision = repository.get_publish_snapshot(profile_id)
            takeover = onboarding_takeover(repository, profile_id, config)
            return _response(gateway_onboarding_state(config, revision, base_url, takeover=takeover))
        body = _body()
        if request.path.endswith('/preview'):
            config, revision = repository.get_publish_snapshot(profile_id)
            takeover = onboarding_takeover(repository, profile_id, config)
            _, result = prepare_gateway_onboarding(config, body, base_url, takeover=takeover)
            result.update(profile_id=profile_id, config_revision=revision)
            return _response(result)
        prepared = {}
        before = repository.get_profile(profile_id)
        # Setup sessions and Agent bindings are read outside the profile lock.
        takeover = onboarding_takeover(repository, profile_id, before)

        def update(current):
            # Validation and both service edits share the same locked snapshot.
            # A stale browser cannot silently overwrite intervening edits.
            expected = body.get('expected_config_revision') if isinstance(body, dict) else None
            if expected is not None and expected != repository.config_revision(profile_id):
                raise ProfileInUse('配置或共享资源已更新，请刷新安装向导并重新预览后保存')
            marker = read_marker(current)[0]
            # The takeover was decided outside the lock. A gateway enabled since
            # may already forward on the host; a marker change is another plan.
            if (marker != read_marker(before)[0]
                    or takeover == 'full_tun' and marker == 'absent' and gateway_settings(current)['enabled']):
                raise ProfileInUse('配置已更新，请刷新安装向导并重新预览后保存')
            candidate, result = prepare_gateway_onboarding(current, body, base_url, takeover=takeover)
            if not result['ready']:
                raise GatewayOnboardingBlocked(result)
            current['mihomo'] = candidate['mihomo']
            current['oxidns'] = candidate['oxidns']
            prepared.update(result)

        saved, revision = repository.update_profile_transaction(profile_id, update, include_config_revision=True)
        if saved['_revision'] == before['_revision'] + 1:
            # The first-use wizard embeds this step; its own save is not an
            # edit made on another page.
            from backend.common.setup_flow import adopt_owned_write
            adopt_owned_write(repository, profile_id, before, saved)
        reset_config_context()
        guards = {kind: {'expected_profile_id': profile_id, 'expected_service_type': kind,
                         'expected_config_revision': revision} for kind in ('mihomo', 'oxidns')}
        prepared.update(profile_id=profile_id, config_revision=revision, draft=True, publish_guards=guards)
        return _response(prepared)
    except GatewayOnboardingBlocked as exc:
        return _response({**exc.result, 'success': False, 'message': str(exc)}, 400)
    except GatewayOnboardingError as exc:
        return _response({'success': False, 'message': str(exc)}, 400)
