"""节点管理路由"""
from flask import request, jsonify
import uuid

from backend.routes import nodes_bp
from backend.common.auth import require_auth
from backend.common.config import get_shared_config, update_shared_config_transaction
from backend.common.config_repository import ProfileRepositoryError
from backend.utils.reorder import resolve_new_order


@nodes_bp.route('', methods=['GET', 'POST'])
@require_auth
def handle_nodes():
    """节点管理"""
    config_data = get_shared_config()

    if request.method == 'GET':
        return jsonify(config_data['nodes'])

    elif request.method == 'POST':
        node = request.json
        if not isinstance(node, dict):
            return jsonify({'success': False, 'message': '节点请求必须是 JSON 对象'}), 400
        # 如果没有 ID，生成一个唯一 ID
        if 'id' not in node:
            node['id'] = f"node_{uuid.uuid4().hex[:8]}"
        if 'dialer_ref' in node:
            return jsonify({'success': False, 'message': '共享节点不保存拨号引用，请在策略组中创建代理链'}), 400
        update_shared_config_transaction(
            lambda shared: shared.setdefault('nodes', []).append(node)
        )
        return jsonify({'success': True, 'data': node})


@nodes_bp.route('/<node_id>', methods=['DELETE', 'PUT'])
@require_auth
def handle_node(node_id):
    """单个节点操作"""
    from werkzeug.exceptions import Conflict, NotFound
    from backend.utils.dialer_references import DialerReferenceError
    new_data = request.get_json() if request.method == 'PUT' else None
    deleting = request.method == 'DELETE'
    if not deleting and not isinstance(new_data, dict):
        return jsonify({'success': False, 'message': '节点请求必须为非 null JSON 对象'}), 400
    if isinstance(new_data, dict) and 'dialer_ref' in new_data:
        return jsonify({'success': False, 'message': '共享节点不保存拨号引用，请在策略组中创建代理链'}), 400
    def mutate(profile):
        nodes = profile.setdefault('nodes', [])
        original = next((n for n in nodes if n.get('id') == node_id), None)
        if original is None:
            raise NotFound('Node not found')
        if not deleting:
            if new_data.get('id', node_id) != node_id:
                raise DialerReferenceError('节点 ID 不可修改')
            new_data['id'] = node_id
        if deleting:
            nodes.remove(original)
        else:
            nodes[nodes.index(original)] = new_data
    try:
        update_shared_config_transaction(mutate)
    except DialerReferenceError as exc:
        return jsonify({'success': False, 'message': str(exc)}), 400
    except (Conflict, NotFound) as exc:
        return jsonify({'success': False, 'message': exc.description}), exc.code
    return jsonify({'success': True, **({'data': new_data} if new_data is not None else {})})


@nodes_bp.route('/reorder', methods=['POST'])
@require_auth
def reorder_nodes():
    """批量更新节点顺序"""
    from backend.utils.dialer_references import DialerReferenceError
    from werkzeug.exceptions import NotFound
    order = []
    body = request.get_json() or {}
    def mutate(profile):
        new_order, missing = resolve_new_order(profile.get('nodes', []), body, 'nodes')
        if missing:
            raise NotFound(f'以下节点 id 不存在: {missing}')
        profile['nodes'] = new_order
        order.extend(item.get('id') for item in new_order)
    try:
        update_shared_config_transaction(mutate)
        return jsonify({'success': True, 'order': order})
    except DialerReferenceError as exc:
        return jsonify({'success': False, 'message': str(exc)}), 400
    except NotFound as exc:
        return jsonify({'success': False, 'message': exc.description}), 404
    except ProfileRepositoryError:
        raise
    except Exception as exc:
        return jsonify({'success': False, 'message': str(exc)}), 500
