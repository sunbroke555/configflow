"""Reminder data for pages that edit Mihomo: upstream routers with changes to publish."""
from concurrent.futures import TimeoutError as FutureTimeout

from flask import jsonify, request

from backend.common.auth import require_auth
from backend.routers import pending
from backend.routes import agents_bp


@agents_bp.route('/<agent_id>/upstream-routers', methods=['GET'])
@require_auth
def upstream_router_pending(agent_id):
    """Routers in any profile that use this Agent as proxy gateway, with their live release state.

    Read-only: each router gets the 上游路由 page's report and release preview.
    ``?refresh=1`` skips the short reuse window after the user saved or checked.
    """
    refresh = request.args.get('refresh') in ('1', 'true')
    try:
        data = pending.agent_routers(agent_id, refresh=refresh)
        response, status = jsonify({'success': True, **data}), 200
    except FutureTimeout:
        response, status = jsonify({'success': False, 'message': '读取上级路由状态超时，请稍后重试。'}), 504
    response.headers['Cache-Control'] = 'no-store'
    return response, status
