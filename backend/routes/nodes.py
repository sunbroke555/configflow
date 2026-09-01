"""节点管理路由"""
from flask import request, jsonify, current_app
import uuid

from backend.routes import nodes_bp
from backend.common.auth import require_auth
from backend.common.config import get_config, save_config, update_config_transaction
from backend.utils.reorder import resolve_new_order


def clean_aggregations_node(node_id):
    """从所有聚合中移除指定的节点，如果聚合变空则禁用并从策略组中移除"""
    config_data = get_config()
    aggregations = config_data.get('subscription_aggregations', [])
    modified = False

    for agg in aggregations:
        if node_id in agg.get('nodes', []):
            agg['nodes'] = [n for n in agg['nodes'] if n != node_id]
            modified = True
            current_app.logger.info(f"从聚合 '{agg.get('name')}' 中移除节点 {node_id}")

            # 检查聚合是否变空（没有订阅也没有节点）
            if not agg.get('subscriptions') and not agg.get('nodes'):
                current_app.logger.info(f"聚合 '{agg.get('name')}' 已变空，自动禁用")
                agg['enabled'] = False
                # 从所有策略组中移除此聚合
                clean_proxy_groups_aggregation(agg['id'])

    if modified:
        save_config()
    return modified


def clean_proxy_groups_aggregation(agg_id):
    """从所有策略组中移除指定的聚合引用"""
    config_data = get_config()
    proxy_groups = config_data.get('proxy_groups', [])
    modified = False

    for group in proxy_groups:
        aggregations = group.get('aggregations', [])
        if agg_id in aggregations:
            group['aggregations'] = [a for a in aggregations if a != agg_id]
            modified = True
            current_app.logger.info(f"从策略组 '{group.get('name')}' 中移除聚合 {agg_id}")

    if modified:
        save_config()
    return modified


@nodes_bp.route('', methods=['GET', 'POST'])
@require_auth
def handle_nodes():
    """节点管理"""
    config_data = get_config()

    if request.method == 'GET':
        return jsonify(config_data['nodes'])

    elif request.method == 'POST':
        node = request.json
        # 如果没有 ID，生成一个唯一 ID
        if 'id' not in node:
            node['id'] = f"node_{uuid.uuid4().hex[:8]}"
        update_config_transaction(
            lambda profile: profile.setdefault('nodes', []).append(node)
        )
        return jsonify({'success': True, 'data': node})


@nodes_bp.route('/<node_id>', methods=['DELETE', 'PUT'])
@require_auth
def handle_node(node_id):
    """单个节点操作"""
    config_data = get_config()
    nodes = config_data['nodes']

    if request.method == 'DELETE':
        config_data['nodes'] = [n for n in nodes if n['id'] != node_id]
        # 从所有聚合中移除此节点
        clean_aggregations_node(node_id)
        save_config()
        return jsonify({'success': True})

    elif request.method == 'PUT':
        for i, n in enumerate(nodes):
            if n['id'] == node_id:
                old_enabled = n.get('enabled', True)
                new_data = request.json
                new_enabled = new_data.get('enabled', True)

                config_data['nodes'][i] = new_data

                # 如果节点被禁用，从所有聚合中移除
                if old_enabled and not new_enabled:
                    clean_aggregations_node(node_id)

                save_config()
                return jsonify({'success': True, 'data': new_data})
        return jsonify({'success': False, 'message': 'Node not found'}), 404


@nodes_bp.route('/reorder', methods=['POST'])
@require_auth
def reorder_nodes():
    """批量更新节点顺序"""
    try:
        config_data = get_config()
        body = request.json or {}
        new_order, missing = resolve_new_order(config_data.get('nodes', []), body, 'nodes')
        if missing:
            return jsonify({'success': False, 'message': f'以下节点 id 不存在: {missing}'}), 404
        config_data['nodes'] = new_order
        save_config()
        return jsonify({'success': True, 'order': [n.get('id') for n in new_order]})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500
