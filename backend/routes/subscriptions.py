"""订阅管理路由"""
from flask import request, jsonify, current_app, Response
from datetime import datetime
import uuid
import yaml

from backend.routes import subscriptions_bp
from backend.common.auth import require_auth, validate_token_or_jwt
from backend.common.config import get_config, save_config, update_config_transaction
from backend.utils.reorder import resolve_new_order
from backend.utils.subscription_cache import (
    load_subscription_cache,
    save_subscription_nodes,
)
from backend.utils.sub_store_client import (
    get_subscription_proxies_yaml,
    parse_proxies_from_yaml,
    proxies_to_nodes,
)
from backend.utils.url_utils import safe_exception_details


def clean_aggregations_subscription(sub_id):
    """从所有聚合中移除指定的订阅，如果聚合变空则禁用并从策略组中移除"""
    config_data = get_config()
    aggregations = config_data.get('subscription_aggregations', [])
    modified = False

    for agg in aggregations:
        if sub_id in agg.get('subscriptions', []):
            agg['subscriptions'] = [s for s in agg['subscriptions'] if s != sub_id]
            modified = True
            current_app.logger.info(f"从聚合 '{agg.get('name')}' 中移除订阅 {sub_id}")

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


@subscriptions_bp.route('', methods=['GET', 'POST'])
@require_auth
def handle_subscriptions():
    """订阅管理"""
    config_data = get_config()

    if request.method == 'GET':
        subscriptions_with_cache = []
        for sub in config_data['subscriptions']:
            sub_copy = dict(sub)
            cache = load_subscription_cache(sub.get('id', ''))
            if cache:
                sub_copy['cached_node_count'] = cache.get('count')
                sub_copy['cached_updated_at'] = cache.get('updated_at')
            else:
                sub_copy['cached_node_count'] = None
                sub_copy['cached_updated_at'] = None
            subscriptions_with_cache.append(sub_copy)
        return jsonify(subscriptions_with_cache)

    elif request.method == 'POST':
        sub = request.json
        update_config_transaction(
            lambda profile: profile.setdefault('subscriptions', []).append(sub)
        )
        return jsonify({'success': True, 'data': sub})


@subscriptions_bp.route('/<sub_id>', methods=['DELETE', 'PUT'])
@require_auth
def handle_subscription(sub_id):
    """单个订阅操作"""
    config_data = get_config()
    subs = config_data['subscriptions']

    if request.method == 'DELETE':
        config_data['subscriptions'] = [s for s in subs if s['id'] != sub_id]
        # 从所有聚合中移除此订阅
        clean_aggregations_subscription(sub_id)
        save_config()
        return jsonify({'success': True})

    elif request.method == 'PUT':
        for i, s in enumerate(subs):
            if s['id'] == sub_id:
                old_enabled = s.get('enabled', True)
                new_data = request.json
                new_enabled = new_data.get('enabled', True)

                config_data['subscriptions'][i] = new_data

                # 如果订阅被禁用，从所有聚合中移除
                if old_enabled and not new_enabled:
                    clean_aggregations_subscription(sub_id)

                save_config()
                return jsonify({'success': True, 'data': new_data})
        return jsonify({'success': False, 'message': 'Subscription not found'}), 404


@subscriptions_bp.route('/reorder', methods=['POST'])
@require_auth
def reorder_subscriptions():
    """批量更新订阅顺序"""
    try:
        config_data = get_config()
        body = request.json or {}
        new_order, missing = resolve_new_order(config_data.get('subscriptions', []), body, 'subscriptions')
        if missing:
            return jsonify({'success': False, 'message': f'以下订阅 id 不存在: {missing}'}), 404
        config_data['subscriptions'] = new_order
        save_config()
        return jsonify({'success': True, 'order': [s.get('id') for s in new_order]})
    except Exception as e:
        current_app.logger.error("订阅操作失败: %s", safe_exception_details(e))
        return jsonify({'success': False, 'message': '订阅操作失败'}), 500


@subscriptions_bp.route('/<sub_id>/nodes', methods=['GET'])
@require_auth
def get_subscription_nodes(sub_id):
    """获取订阅下的所有节点"""
    config_data = get_config()
    subs = config_data['subscriptions']
    sub = next((s for s in subs if s['id'] == sub_id), None)

    if not sub:
        return jsonify({'success': False, 'message': 'Subscription not found'}), 404

    # 从全局节点列表中过滤出属于该订阅的节点
    nodes = [n for n in config_data.get('nodes', []) if n.get('subscription_id') == sub_id]

    return jsonify(nodes)


@subscriptions_bp.route('/<sub_id>/fetch', methods=['POST'])
@require_auth
def fetch_subscription(sub_id):
    """获取订阅节点，优先从URL获取并缓存，失败则读取本地缓存"""
    config_data = get_config()
    subs = config_data['subscriptions']
    sub = next((s for s in subs if s['id'] == sub_id), None)

    if not sub:
        return jsonify({'success': False, 'message': 'Subscription not found'}), 404

    # 获取请求参数，判断是否是预览模式
    data = request.get_json() or {}
    preview_mode = data.get('preview', False)

    nodes = None
    cache_payload = None
    fetch_error = None
    from_cache = False

    # 优先尝试从 Sub-Store 获取
    try:
        current_app.logger.info(f"尝试通过 Sub-Store 获取订阅: {sub['name']} (id: {sub_id})")
        yaml_text, source = get_subscription_proxies_yaml(sub_id, sub['url'])
        proxies = parse_proxies_from_yaml(yaml_text)
        nodes = proxies_to_nodes(proxies)

        # 为节点添加订阅来源信息和ID
        for node in nodes:
            node['subscription_id'] = sub_id
            node['subscription_name'] = sub['name']
            if 'id' not in node:
                node['id'] = f"node_{uuid.uuid4().hex[:8]}"

        # 写入缓存
        cache_payload = save_subscription_nodes(
            sub_id,
            nodes,
            {
                'subscription_name': sub.get('name'),
                'url': sub.get('url')
            }
        )
        if source == 'rendered_yaml':
            current_app.logger.info(f"成功直接复用订阅 URL 返回的 Sub-Store YAML 并写入缓存: {sub['name']}, 节点数: {len(nodes)}")
        elif source == 'sub_store':
            current_app.logger.info(f"成功通过 Sub-Store 转换订阅并写入缓存: {sub['name']}, 节点数: {len(nodes)}")
        elif source == 'direct_url_fallback':
            current_app.logger.info(f"Sub-Store 获取失败后，成功直接拉取原始订阅并写入缓存: {sub['name']}, 节点数: {len(nodes)}")
        else:
            current_app.logger.info(f"成功获取订阅并写入缓存: {sub['name']}, 节点数: {len(nodes)}")

    except Exception as e:
        fetch_error = f"request_failed ({safe_exception_details(e)})"
        current_app.logger.warning("从URL获取订阅失败: %s, 尝试读取本地缓存: %s", sub['name'], safe_exception_details(e))

        # 从URL获取失败，尝试读取本地缓存
        cache = load_subscription_cache(sub_id)
        if cache:
            nodes = cache.get('nodes', [])
            cache_payload = cache
            from_cache = True
            current_app.logger.info(f"使用本地缓存数据: {sub['name']}, 节点数: {len(nodes)}")
        else:
            # 既没有从URL获取成功，也没有本地缓存
            return jsonify({
                'success': False,
                'message': f'从订阅URL获取失败: {fetch_error}，且本地无缓存数据'
            }), 500

    # 如果是预览模式，只返回节点列表，不添加到配置中
    if preview_mode:
        return jsonify({
            'success': True,
            'count': len(nodes),
            'nodes': nodes,
            'preview': True,
            'from_cache': from_cache,
            'fetch_error': fetch_error,
            'cached_count': cache_payload.get('count') if cache_payload else 0,
            'cached_updated_at': cache_payload.get('updated_at') if cache_payload else None
        })

    # 非预览模式：添加到节点列表
    try:
        added_count = 0
        for node in nodes:
            # 检查是否已存在（根据名称判断）
            existing = next((n for n in config_data['nodes'] if n['name'] == node['name']), None)
            if not existing:
                config_data['nodes'].append(node)
                added_count += 1

        save_config()
        return jsonify({
            'success': True,
            'count': len(nodes),
            'added': added_count,
            'nodes': nodes,
            'from_cache': from_cache,
            'fetch_error': fetch_error,
            'cached_count': cache_payload.get('count') if cache_payload else 0,
            'cached_updated_at': cache_payload.get('updated_at') if cache_payload else None
        })
    except Exception as e:
        current_app.logger.error("订阅操作失败: %s", safe_exception_details(e))
        return jsonify({'success': False, 'message': '订阅操作失败'}), 500


@subscriptions_bp.route('/test', methods=['GET'])
def test_subscription_route():
    """测试订阅路由是否正常工作"""
    return {'success': True, 'message': 'Subscription route is working!', 'timestamp': datetime.now().isoformat()}


@subscriptions_bp.route('/proxies', methods=['GET'])
def get_all_subscription_proxies():
    """获取所有订阅的代理列表（YAML proxies 格式）

    支持两种授权方式：
    1. Authorization header: Bearer <JWT_TOKEN>
    2. URL query 参数: ?token=<CONFIG_TOKEN>

    返回格式为 YAML proxies 列表
    """
    # 验证授权
    auth_result = validate_token_or_jwt(request)
    if not auth_result.get('valid'):
        return jsonify({
            'success': False,
            'message': auth_result.get('message', 'Unauthorized')
        }), 401

    try:
        config_data = get_config()
        subscriptions = config_data.get('subscriptions', [])

        # 收集所有订阅的代理列表
        all_proxies = []
        subscription_info = []

        for sub in subscriptions:
            if not sub.get('enabled', True):
                continue

            sub_id = sub.get('id')
            sub_name = sub.get('name', 'Unknown')
            sub_url = sub.get('url')

            # 通过 Sub-Store 获取 proxies
            proxies = []
            try:
                yaml_text, _source = get_subscription_proxies_yaml(sub_id, sub_url)
                proxies = parse_proxies_from_yaml(yaml_text)
            except Exception as e:
                current_app.logger.warning("通过 Sub-Store 获取订阅 '%s' 失败，尝试本地缓存: %s", sub_name, safe_exception_details(e))
                # 降级：从本地缓存加载并转换
                cache = load_subscription_cache(sub_id)
                if cache:
                    from backend.converters.mihomo import convert_node_to_mihomo
                    for node in cache.get('nodes', []):
                        try:
                            proxy = convert_node_to_mihomo(node)
                            if proxy:
                                proxies.append(proxy)
                        except Exception:
                            continue

            if not proxies:
                current_app.logger.warning(f"订阅 '{sub_name}' (id: {sub_id}) 没有可用节点")
                continue

            all_proxies.extend(proxies)
            subscription_info.append({
                'name': sub_name,
                'id': sub_id,
                'proxy_count': len(proxies),
                'total_nodes': len(proxies),
                'updated_at': datetime.now().isoformat()
            })

        # 构建 YAML 响应
        yaml_data = {
            'proxies': all_proxies
        }

        # 添加元数据注释（英文，避免编码问题）
        yaml_output = f"""# ConfigFlow Subscription Proxies
# Generated: {datetime.now().isoformat()}Z
# Subscriptions: {len(subscription_info)}
# Total Proxies: {len(all_proxies)}
#
# Subscription Details:
"""
        for info in subscription_info:
            yaml_output += f"#   - {info['name']}: {info['proxy_count']} proxies (updated: {info.get('updated_at', 'N/A')})\n"

        yaml_output += "\n"
        # 使用 IndentDumper 确保正确的缩进格式
        from backend.converters.mihomo import IndentDumper
        yaml_output += yaml.dump(
            yaml_data,
            Dumper=IndentDumper,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
            indent=2
        )

        return Response(
            yaml_output,
            mimetype='text/yaml; charset=utf-8',
            headers={
                'Content-Disposition': f'inline; filename="proxies_{datetime.now().strftime("%Y%m%d_%H%M%S")}.yaml"'
            }
        )

    except Exception as e:
        current_app.logger.error("获取订阅代理列表失败: %s", safe_exception_details(e))
        return jsonify({'success': False, 'message': '获取订阅代理列表失败'}), 500


@subscriptions_bp.route('/<sub_id>/proxies', methods=['GET'])
def get_subscription_proxies(sub_id):
    """获取单个订阅的代理列表（YAML proxies 格式）

    支持两种授权方式：
    1. Authorization header: Bearer <JWT_TOKEN>
    2. URL query 参数: ?token=<CONFIG_TOKEN>

    优先从原订阅URL获取新配置并更新缓存，如果失败则返回本地缓存
    返回格式为 YAML proxies 列表
    """
    # 验证授权
    auth_result = validate_token_or_jwt(request)
    if not auth_result.get('valid'):
        return jsonify({
            'success': False,
            'message': auth_result.get('message', 'Unauthorized')
        }), 401

    try:
        config_data = get_config()
        subscriptions = config_data.get('subscriptions', [])
        sub = next((s for s in subscriptions if s['id'] == sub_id), None)

        if not sub:
            return jsonify({'success': False, 'message': 'Subscription not found'}), 404

        sub_name = sub.get('name', 'Unknown')
        sub_url = sub.get('url')
        proxies = None
        cache_updated = False
        fetch_error = None

        # 优先通过 Sub-Store 获取
        if sub_url:
            try:
                current_app.logger.info(f"尝试获取最新配置: {sub_name} (id: {sub_id})")
                yaml_text, source = get_subscription_proxies_yaml(sub_id, sub_url)
                proxies = parse_proxies_from_yaml(yaml_text)

                if proxies:
                    # 更新本地缓存（转换为 node 格式存储）
                    nodes = proxies_to_nodes(proxies)
                    for node in nodes:
                        node['subscription_id'] = sub_id
                        node['subscription_name'] = sub_name
                        if 'id' not in node:
                            node['id'] = f"node_{uuid.uuid4().hex[:8]}"
                    save_subscription_nodes(
                        sub_id,
                        nodes,
                        {
                            'subscription_name': sub_name,
                            'url': sub_url
                        }
                    )
                    cache_updated = True
                    if source == 'rendered_yaml':
                        current_app.logger.info(f"成功直接复用订阅 URL 返回的 Sub-Store YAML 并更新缓存: {sub_name}, 节点数: {len(proxies)}")
                    elif source == 'sub_store':
                        current_app.logger.info(f"成功通过 Sub-Store 转换订阅并更新缓存: {sub_name}, 节点数: {len(proxies)}")
                    elif source == 'direct_url_fallback':
                        current_app.logger.info(f"Sub-Store 获取失败后，成功直接拉取原始订阅并更新缓存: {sub_name}, 节点数: {len(proxies)}")
                    else:
                        current_app.logger.info(f"成功获取订阅并更新缓存: {sub_name}, 节点数: {len(proxies)}")
            except Exception as e:
                fetch_error = f"request_failed ({safe_exception_details(e)})"
                current_app.logger.warning("通过 Sub-Store 获取配置失败: %s, 将使用本地缓存: %s", sub_name, safe_exception_details(e))

        # 如果从 Sub-Store 获取失败或没有URL，则从本地缓存加载并转换
        if proxies is None:
            cache = load_subscription_cache(sub_id)
            if not cache:
                return jsonify({
                    'success': False,
                    'message': f"订阅 '{sub_name}' 没有缓存数据，且从 Sub-Store 获取失败: {fetch_error or '未知错误'}"
                }), 404

            nodes = cache.get('nodes', [])
            if not nodes:
                return jsonify({
                    'success': False,
                    'message': f"订阅 '{sub_name}' 缓存中没有节点"
                }), 404

            current_app.logger.info(f"使用本地缓存数据: {sub_name}, 节点数: {len(nodes)}")
            # 降级：从缓存节点转换为 proxies
            from backend.converters.mihomo import convert_node_to_mihomo
            proxies = []
            for node in nodes:
                try:
                    proxy = convert_node_to_mihomo(node)
                    if proxy:
                        proxies.append(proxy)
                except Exception as e:
                    current_app.logger.error("转换节点失败: %s", safe_exception_details(e))
                    continue

        # 如果请求 Surge 格式，转换为 Surge 纯文本
        if request.args.get('format') == 'surge':
            from backend.converters.surge import convert_proxies_to_surge_text
            surge_text = convert_proxies_to_surge_text(proxies)
            return Response(surge_text, mimetype='text/plain')

        # 构建 YAML 响应
        yaml_data = {
            'proxies': proxies
        }

        # 添加元数据注释（英文，避免编码问题）
        source_info = "from Sub-Store (cache updated)" if cache_updated else "from local cache"
        if fetch_error and not cache_updated:
            source_info += f" (fetch error: {fetch_error})"

        yaml_output = f"""# ConfigFlow Subscription Proxies
# Subscription: {sub_name}
# ID: {sub_id}
# Generated: {datetime.now().isoformat()}Z
# Proxies: {len(proxies)}
# Source: {source_info}

"""
        # 使用 IndentDumper 确保正确的缩进格式
        from backend.converters.mihomo import IndentDumper
        yaml_output += yaml.dump(
            yaml_data,
            Dumper=IndentDumper,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
            indent=2
        )

        # 使用 ASCII 安全的文件名
        safe_filename = f"proxies_{sub_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.yaml"

        return Response(
            yaml_output,
            mimetype='text/yaml; charset=utf-8',
            headers={
                'Content-Disposition': f'inline; filename="{safe_filename}"'
            }
        )

    except Exception as e:
        current_app.logger.error("获取订阅代理列表失败: %s", safe_exception_details(e))
        return jsonify({'success': False, 'message': '获取订阅代理列表失败'}), 500
