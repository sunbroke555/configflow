"""策略组管理路由"""
import re

from flask import request, jsonify

from backend.routes import proxy_groups_bp
from backend.common.auth import require_auth
from backend.common.config import get_config, get_shared_config, update_config_transaction
from backend.common.config_repository import ProfileRepositoryError
from backend.utils.reorder import resolve_new_order
from backend.utils.subscription_cache import load_subscription_cache


def _get_subscription_nodes(config_data, sub_id):
    """Get nodes for a subscription from cache first, then imported nodes."""
    nodes = []
    cache = load_subscription_cache(sub_id)
    if cache and isinstance(cache.get('nodes'), list):
        nodes.extend(cache.get('nodes', []))

    existing_names = {node.get('name') for node in nodes if node.get('name')}
    for node in config_data.get('nodes', []):
        if node.get('subscription_id') == sub_id and node.get('enabled', True):
            if node.get('name') not in existing_names:
                nodes.append(node)
                existing_names.add(node.get('name'))

    return nodes


def _get_manual_nodes(config_data, node_ids):
    """Resolve manually selected node ids to node objects."""
    result = []
    for node_id in node_ids:
        if node_id in ['DIRECT', 'REJECT']:
            result.append({
                'id': node_id,
                'name': node_id,
                'type': 'builtin',
                'source_type': 'manual'
            })
            continue

        node = next(
            (n for n in config_data.get('nodes', []) if n.get('id') == node_id and n.get('enabled', True)),
            None
        )
        if node:
            result.append(node)

    return result


def _add_source_meta(nodes, source_type, source_id, source_name):
    """Return shallow node copies with display source metadata."""
    enriched = []
    for node in nodes:
        item = dict(node)
        item['source_type'] = source_type
        item['source_id'] = source_id
        item['source_name'] = source_name
        enriched.append(item)
    return enriched


def _clean_group_fields(group):
    if group.get('type') == 'chain':
        for field in ('subscriptions', 'regex', 'manual_nodes', 'aggregations', 'aggregation_regex',
                      'include_groups', 'follow_group', 'proxies', 'source', 'source_type',
                      'proxies_order', 'url', 'interval', 'strategy', 'lazy', 'use',
                      'include_all', 'include-all'):
            group.pop(field, None)
    else:
        group.pop('chain', None)


@proxy_groups_bp.route('', methods=['GET', 'POST'])
@require_auth
def handle_proxy_groups():
    """策略组管理"""
    config_data = get_config()

    if request.method == 'GET':
        return jsonify(config_data['proxy_groups'])

    elif request.method == 'POST':
        group = request.json
        if not isinstance(group, dict):
            return jsonify({'success': False, 'message': '策略组请求必须是 JSON 对象'}), 400
        _clean_group_fields(group)
        update_config_transaction(lambda profile: profile.setdefault('proxy_groups', []).append(group))
        return jsonify({'success': True, 'data': group})


@proxy_groups_bp.route('/<group_id>', methods=['DELETE', 'PUT'])
@require_auth
def handle_proxy_group(group_id):
    """Single-profile transaction: validate references before any write."""
    from werkzeug.exceptions import Conflict, NotFound
    from backend.utils.dialer_references import incoming_dialers, DialerReferenceError

    group_data = request.get_json() if request.method == 'PUT' else None
    if request.method == 'PUT' and not isinstance(group_data, dict):
        return jsonify({'success': False, 'message': '策略组请求必须是 JSON 对象'}), 400

    def mutate(profile):
        groups = profile.setdefault('proxy_groups', [])
        original = next((g for g in groups if g.get('id') == group_id), None)
        if original is None:
            raise NotFound('Group not found')
        if group_data is not None:
            if group_data.get('id', group_id) != group_id:
                raise Conflict('策略组 ID 不可修改，请保留原 ID')
            merged = {**original, **group_data}
            group_data.clear()
            group_data.update(merged)
            group_data['id'] = group_id
            _clean_group_fields(group_data)
            if any(g.get('id') != group_id and g.get('name') == group_data.get('name') for g in groups):
                raise Conflict('策略组名称已存在，请使用其他名称')
        removing = request.method == 'DELETE' or not group_data.get('enabled', True)
        if removing:
            rules = [r for r in profile.get('rule_configs', [])
                     if r.get('enabled', True) and r.get('policy') == original.get('name')]
            dependents = [g for g in groups if g.get('id') != group_id and
                          (group_id in g.get('include_groups', []) or g.get('follow_group') == group_id)]
            if rules or dependents or incoming_dialers(profile, 'group', group_id):
                raise Conflict('策略组仍被规则或其他策略组引用，请先修改引用后再删除或禁用')
        if request.method == 'DELETE':
            groups.remove(original)
        else:
            # Names are rule-policy keys; group-to-group references use stable IDs.
            old_name = original.get('name')
            new_name = group_data.get('name')
            if old_name != new_name:
                for rule in profile.get('rule_configs', []):
                    if rule.get('policy') == old_name:
                        rule['policy'] = new_name
            groups[groups.index(original)] = group_data

    try:
        update_config_transaction(mutate)
    except DialerReferenceError as exc:
        return jsonify({'success': False, 'message': str(exc)}), 400
    except (Conflict, NotFound) as exc:
        return jsonify({'success': False, 'message': exc.description}), exc.code
    return jsonify({'success': True, **({'data': group_data} if group_data is not None else {})})


@proxy_groups_bp.route('/preview-regex', methods=['POST'])
@require_auth
def preview_proxy_group_regex():
    """Preview nodes matched by a proxy-group regex without saving config."""
    try:
        config_data = get_shared_config()
        payload = request.get_json() or {}
        source = payload.get('source')
        regex_text = (payload.get('regex') or '').strip()

        if source not in ['subscription', 'aggregation']:
            return jsonify({'success': False, 'message': 'Invalid preview source'}), 400

        if not regex_text:
            return jsonify({'success': False, 'message': '请输入正则表达式'}), 400

        try:
            regex = re.compile(regex_text)
        except re.error as exc:
            return jsonify({'success': False, 'message': f'正则表达式无效: {exc}'}), 400

        candidates = []

        if source == 'subscription':
            subscription_ids = payload.get('subscriptions') or []
            subscriptions = config_data.get('subscriptions', [])
            for sub_id in subscription_ids:
                sub = next((s for s in subscriptions if s.get('id') == sub_id and s.get('enabled', True)), None)
                if not sub:
                    continue
                nodes = _get_subscription_nodes(config_data, sub_id)
                candidates.extend(_add_source_meta(nodes, 'subscription', sub_id, sub.get('name', sub_id)))

        if source == 'aggregation':
            aggregation_ids = payload.get('aggregations') or []
            aggregations = config_data.get('subscription_aggregations', [])
            subscriptions = config_data.get('subscriptions', [])

            for agg_id in aggregation_ids:
                agg = next((a for a in aggregations if a.get('id') == agg_id and a.get('enabled', True)), None)
                if not agg:
                    continue

                agg_name = agg.get('name', agg_id)
                for sub_id in agg.get('subscriptions', []):
                    sub = next((s for s in subscriptions if s.get('id') == sub_id and s.get('enabled', True)), None)
                    if not sub:
                        continue
                    nodes = _get_subscription_nodes(config_data, sub_id)
                    candidates.extend(_add_source_meta(nodes, 'aggregation', agg_id, agg_name))

                manual_nodes = _get_manual_nodes(config_data, agg.get('nodes', []))
                candidates.extend(_add_source_meta(manual_nodes, 'aggregation', agg_id, agg_name))

        matched = []
        seen_names = set()
        for node in candidates:
            name = node.get('name', '')
            if not name or not regex.search(name):
                continue
            if name in seen_names:
                continue
            seen_names.add(name)
            matched.append({
                'id': node.get('id', name),
                'name': name,
                'type': node.get('type') or node.get('protocol') or 'unknown',
                'subscription_id': node.get('subscription_id'),
                'subscription_name': node.get('subscription_name'),
                'source_type': node.get('source_type'),
                'source_id': node.get('source_id'),
                'source_name': node.get('source_name')
            })

        return jsonify({
            'success': True,
            'count': len(matched),
            'total_candidates': len(candidates),
            'nodes': matched
        })

    except Exception as exc:
        return jsonify({'success': False, 'message': str(exc)}), 500


@proxy_groups_bp.route('/reorder', methods=['POST'])
@require_auth
def reorder_proxy_groups():
    """批量更新策略组顺序

    按 id 排序时传 {'ids': [...], 'position': 'top'|'bottom'}；
    传完整对象数组的旧格式仍然兼容。
    """
    from backend.utils.dialer_references import validate_dialers, DialerReferenceError
    from werkzeug.exceptions import NotFound
    order = []
    body = request.get_json() or {}
    def mutate(profile):
        new_order, missing = resolve_new_order(profile.get('proxy_groups', []), body, 'groups')
        if missing:
            raise NotFound(f'以下策略组 id 不存在: {missing}')
        profile['proxy_groups'] = new_order
        validate_dialers(profile)
        order.extend(item.get('id') for item in new_order)
    try:
        update_config_transaction(mutate)
        return jsonify({'success': True, 'order': order})
    except DialerReferenceError as exc:
        return jsonify({'success': False, 'message': str(exc)}), 400
    except NotFound as exc:
        return jsonify({'success': False, 'message': exc.description}), 404
    except ProfileRepositoryError:
        raise
    except Exception as exc:
        return jsonify({'success': False, 'message': str(exc)}), 500
