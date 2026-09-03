"""Profile management and explicit profile URL aliases."""

from flask import jsonify, request

from backend.common.auth import require_auth
from backend.common.config import get_repository
from backend.common.config_export import sanitize_config_for_output
from backend.routes import profiles_bp


@profiles_bp.route('', methods=['GET', 'POST'])
@require_auth
def handle_profiles():
    repository = get_repository()
    if request.method == 'GET':
        return jsonify(repository.list_profiles())

    data = request.get_json(silent=True) or {}
    clone_from = data.pop('clone_from', None)
    return jsonify(repository.create_profile(data, clone_from=clone_from)), 201


@profiles_bp.route('/<profile_id>', methods=['GET', 'PUT', 'DELETE'])
@require_auth
def handle_profile(profile_id):
    repository = get_repository()
    if request.method == 'GET':
        return jsonify(repository._profile_metadata(profile_id))
    if request.method == 'PUT':
        return jsonify(repository.update_profile(profile_id, request.get_json(silent=True) or {}))
    repository.delete_profile(profile_id)
    return '', 204


@profiles_bp.route('/<profile_id>/clone', methods=['POST'])
@require_auth
def clone_profile(profile_id):
    data = request.get_json(silent=True) or {}
    return jsonify(get_repository().clone_profile(profile_id, data)), 201


@profiles_bp.route('/<profile_id>/activate', methods=['POST'])
@require_auth
def activate_profile(profile_id):
    profile = get_repository().activate_profile(profile_id)
    return jsonify({'success': True, 'active_profile_id': profile_id, 'profile': profile})


@profiles_bp.route('/<profile_id>/export', methods=['GET'])
@require_auth
def export_profile(profile_id):
    response = jsonify(sanitize_config_for_output(get_repository().export_profile(profile_id)))
    response.headers['Content-Disposition'] = f'attachment; filename={profile_id}.json'
    return response


@profiles_bp.route('/<profile_id>/import', methods=['POST'])
@require_auth
def import_profile(profile_id):
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'success': False, 'message': '请求数据必须是 JSON 对象'}), 400
    get_repository().import_profile(profile_id, data)
    return jsonify({'success': True, 'profile_id': profile_id})


@profiles_bp.route('/<profile_id>/subscriptions', methods=['GET', 'POST'])
@require_auth
def profile_subscriptions(profile_id):
    from backend.routes.subscriptions import handle_subscriptions
    return handle_subscriptions()


@profiles_bp.route('/<profile_id>/subscriptions/<sub_id>', methods=['DELETE', 'PUT'])
@require_auth
def profile_subscription(profile_id, sub_id):
    from backend.routes.subscriptions import handle_subscription
    return handle_subscription(sub_id)


@profiles_bp.route('/<profile_id>/subscriptions/<sub_id>/nodes', methods=['GET'])
@require_auth
def profile_subscription_nodes(profile_id, sub_id):
    from backend.routes.subscriptions import get_subscription_nodes
    return get_subscription_nodes(sub_id)


@profiles_bp.route('/<profile_id>/subscriptions/<sub_id>/proxies', methods=['GET'])
def profile_subscription_proxies(profile_id, sub_id):
    from backend.routes.subscriptions import get_subscription_proxies
    return get_subscription_proxies(sub_id)


@profiles_bp.route('/<profile_id>/nodes', methods=['GET', 'POST'])
@require_auth
def profile_nodes(profile_id):
    from backend.routes.nodes import handle_nodes
    return handle_nodes()


@profiles_bp.route('/<profile_id>/rules', methods=['GET', 'POST'])
@require_auth
def profile_rules(profile_id):
    from backend.routes.rules import handle_rules
    return handle_rules()


@profiles_bp.route('/<profile_id>/rules/local/<name>', methods=['GET'])
def profile_local_rule(profile_id, name):
    from backend.routes.rules import get_local_rule
    return get_local_rule(name)


@profiles_bp.route('/<profile_id>/rule-library', methods=['GET', 'POST'])
@require_auth
def profile_rule_library(profile_id):
    from backend.routes.rule_library import handle_rule_library
    return handle_rule_library()


@profiles_bp.route('/<profile_id>/rule-library/<rule_id>', methods=['DELETE', 'PUT'])
@require_auth
def profile_rule_library_item(profile_id, rule_id):
    from backend.routes.rule_library import handle_rule_library_item
    return handle_rule_library_item(rule_id)


@profiles_bp.route('/<profile_id>/rule-library/content/<rule_id>', methods=['GET'])
def profile_rule_library_content(profile_id, rule_id):
    from backend.routes.rule_library import get_rule_library_content
    return get_rule_library_content(rule_id)


@profiles_bp.route('/<profile_id>/proxy-groups', methods=['GET', 'POST'])
@require_auth
def profile_proxy_groups(profile_id):
    from backend.routes.proxy_groups import handle_proxy_groups
    return handle_proxy_groups()


@profiles_bp.route('/<profile_id>/aggregations', methods=['GET', 'POST'])
@require_auth
def profile_aggregations(profile_id):
    from backend.routes.aggregations import handle_subscription_aggregations
    return handle_subscription_aggregations()


@profiles_bp.route('/<profile_id>/aggregations/<agg_id>/provider', methods=['GET'])
def profile_aggregation_provider(profile_id, agg_id):
    from backend.routes.aggregations import get_aggregation_provider
    return get_aggregation_provider(agg_id)


@profiles_bp.route('/<profile_id>/mosdns/rule-proxy', methods=['GET'])
def profile_mosdns_rule_proxy(profile_id):
    from backend.routes.mosdns import mosdns_rule_proxy
    return mosdns_rule_proxy()


@profiles_bp.route('/<profile_id>/generate/mihomo', methods=['POST'])
@require_auth
def profile_generate_mihomo(profile_id):
    from backend.routes.generate import generate_mihomo
    return generate_mihomo()


@profiles_bp.route('/<profile_id>/generate/surge', methods=['POST'])
@require_auth
def profile_generate_surge(profile_id):
    from backend.routes.generate import generate_surge
    return generate_surge()


@profiles_bp.route('/<profile_id>/generate/mosdns', methods=['POST'])
@require_auth
def profile_generate_mosdns(profile_id):
    from backend.routes.generate import generate_mosdns
    return generate_mosdns()


@profiles_bp.route('/<profile_id>/generate/mihomo/preview', methods=['POST'])
@require_auth
def profile_preview_mihomo(profile_id):
    from backend.routes.generate import preview_mihomo
    return preview_mihomo()


@profiles_bp.route('/<profile_id>/generate/surge/preview', methods=['POST'])
@require_auth
def profile_preview_surge(profile_id):
    from backend.routes.generate import preview_surge
    return preview_surge()


@profiles_bp.route('/<profile_id>/generate/mosdns/preview', methods=['POST'])
@require_auth
def profile_preview_mosdns(profile_id):
    from backend.routes.generate import preview_mosdns
    return preview_mosdns()
