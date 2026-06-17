"""策略组管理路由"""
import re

from flask import request, jsonify

from backend.routes import proxy_groups_bp
from backend.common.auth import require_auth
from backend.common.config import get_config, save_config
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


@proxy_groups_bp.route('', methods=['GET', 'POST'])
@require_auth
def handle_proxy_groups():
    """策略组管理"""
    config_data = get_config()

    if request.method == 'GET':
        return jsonify(config_data['proxy_groups'])

    elif request.method == 'POST':
        group = request.json
        config_data['proxy_groups'].append(group)
        save_config()
        return jsonify({'success': True, 'data': group})


@proxy_groups_bp.route('/<group_id>', methods=['DELETE', 'PUT'])
@require_auth
def handle_proxy_group(group_id):
    """单个策略组操作"""
    config_data = get_config()
    groups = config_data['proxy_groups']

    if request.method == 'DELETE':
        # 删除策略组
        config_data['proxy_groups'] = [g for g in groups if g['id'] != group_id]

        # 清理其他策略组中对被删除策略组的引用
        for group in config_data['proxy_groups']:
            if 'include_groups' in group and group_id in group['include_groups']:
                group['include_groups'].remove(group_id)

        save_config()
        return jsonify({'success': True})

    elif request.method == 'PUT':
        for i, g in enumerate(groups):
            if g['id'] == group_id:
                # 清理策略组数据:如果选择了聚合,清空subscriptions和manual_nodes字段
                group_data = request.json
                if group_data.get('aggregations') and len(group_data.get('aggregations', [])) > 0:
                    # 只保留聚合ID,清空策略组自身的subscriptions和manual_nodes
                    # 注意:只有当subscriptions/manual_nodes为空时才清理,如果用户同时选择了聚合和订阅/节点,则保留
                    pass  # 暂时不做强制清理,因为用户可能同时选择聚合和订阅/节点

                config_data['proxy_groups'][i] = group_data
                save_config()
                return jsonify({'success': True, 'data': group_data})
        return jsonify({'success': False, 'message': 'Group not found'}), 404


@proxy_groups_bp.route('/preview-regex', methods=['POST'])
@require_auth
def preview_proxy_group_regex():
    """Preview nodes matched by a proxy-group regex without saving config."""
    try:
        config_data = get_config()
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
    """批量更新策略组顺序"""
    try:
        config_data = get_config()
        new_order = request.json.get('groups', [])
        config_data['proxy_groups'] = new_order
        save_config()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500
