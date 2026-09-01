"""规则仓库路由模块

提供规则仓库（Rule Library）的 CRUD 操作和相关功能
"""
import os
from flask import request, jsonify
from flask import Blueprint
from backend.common.auth import require_auth
from backend.common.config import config_data, save_config, get_config, update_config_transaction
from backend.common.profile_context import profile_api_path, resolve_profile_id
from backend.utils.reorder import resolve_new_order
from backend.utils.rule_utils import sanitize_rule_name, get_rules_dir, save_rule_to_local
from backend.utils.logger import get_logger

logger = get_logger(__name__)

# 创建规则库蓝图
rule_library_bp = Blueprint('rule_library', __name__, url_prefix='/api/rule-library')


@rule_library_bp.route('', methods=['GET', 'POST'])
@require_auth
def handle_rule_library():
    """规则仓库管理"""
    if request.method == 'GET':
        return jsonify(config_data.get('rule_library', []))

    elif request.method == 'POST':
        rule = request.json

        # 根据 source_type 清理字段
        source_type = rule.get('source_type', 'url')
        if source_type == 'content':
            # 如果是内容类型，移除 url 字段，保留 content
            if 'url' in rule:
                del rule['url']
        elif source_type == 'url':
            # 如果是 URL 类型，移除 content 字段，保留 url
            if 'content' in rule:
                del rule['content']

        # 保存规则到本地文件
        save_rule_to_local(rule)
        update_config_transaction(
            lambda profile: profile.setdefault('rule_library', []).append(rule)
        )
        return jsonify({'success': True, 'data': rule})


@rule_library_bp.route('/<rule_id>', methods=['DELETE', 'PUT'])
@require_auth
def handle_rule_library_item(rule_id):
    """单个规则仓库项操作"""
    rule_library = config_data.get('rule_library', [])

    if request.method == 'DELETE':
        # 找到要删除的规则，清理缓存文件
        rule_to_delete = next((r for r in rule_library if r['id'] == rule_id), None)
        if rule_to_delete:
            rule_name = rule_to_delete.get('name', '')
            if rule_name:
                filename = f"{sanitize_rule_name(rule_name)}.list"
                filepath = os.path.join(get_rules_dir(), filename)
                if os.path.exists(filepath):
                    try:
                        os.remove(filepath)
                        logger.info(f"Removed cached file: {filepath}")
                    except Exception as e:
                        logger.error(f"Failed to remove cached file: {str(e)}")

        config_data['rule_library'] = [r for r in rule_library if r['id'] != rule_id]
        save_config()
        return jsonify({'success': True})

    elif request.method == 'PUT':
        for i, r in enumerate(rule_library):
            if r['id'] == rule_id:
                old_rule = r
                new_rule = request.json

                # 移除 base_url 字段（不应该存储在规则库中）
                if 'base_url' in new_rule:
                    del new_rule['base_url']

                # 检查 source_type 是否发生了变化
                old_source_type = old_rule.get('source_type', 'url')
                new_source_type = new_rule.get('source_type', 'url')

                # 检查 behavior 是否发生了变化
                old_behavior = old_rule.get('behavior', 'classical')
                new_behavior = new_rule.get('behavior', 'classical')

                # 检查 name 是否发生了变化
                old_name = old_rule.get('name', '')
                new_name = new_rule.get('name', '')
                name_changed = old_name != new_name

                # 检查 enabled 是否发生了变化
                old_enabled = old_rule.get('enabled', True)
                new_enabled = new_rule.get('enabled', True)
                enabled_changed = old_enabled != new_enabled

                # 检查 URL 是否发生了变化
                old_url = old_rule.get('url', '')
                new_url = new_rule.get('url', '')
                url_changed = old_url != new_url

                # 根据 source_type 清理字段
                if new_source_type == 'content':
                    # 如果是内容类型，移除 url 字段，保留 content
                    if 'url' in new_rule:
                        del new_rule['url']
                elif new_source_type == 'url':
                    # 如果是 URL 类型，移除 content 字段，保留 url
                    if 'content' in new_rule:
                        del new_rule['content']

                # 如果规则名称发生变化，删除旧的缓存文件
                if name_changed and old_name:
                    old_filename = f"{sanitize_rule_name(old_name)}.list"
                    old_filepath = os.path.join(get_rules_dir(), old_filename)
                    if os.path.exists(old_filepath):
                        try:
                            os.remove(old_filepath)
                            logger.info(f"Removed old cached file: {old_filepath}")
                        except Exception as e:
                            logger.error(f"Failed to remove old cached file: {str(e)}")

                # 更新规则仓库
                config_data['rule_library'][i] = new_rule

                # 重新保存规则到本地文件（会使用新名称）
                save_rule_to_local(new_rule)

                # 找到所有引用这个规则的规则配置，并进行统一更新
                all_rules = config_data.get('rule_configs', [])

                # 检查是否需要更新
                source_type_changed = old_source_type != new_source_type
                behavior_changed = old_behavior != new_behavior
                name_changed = old_name != new_name

                # 如果有任何字段发生变化，遍历并更新所有引用该规则的配置
                if source_type_changed or behavior_changed or name_changed or enabled_changed or url_changed:
                    updated_count = 0
                    for rule_config in all_rules:
                        if rule_config.get('itemType') == 'ruleset' and rule_config.get('library_rule_id') == rule_id:
                            # 更新 source_type 和 URL
                            if source_type_changed:
                                if new_source_type == 'content':
                                    # 使用内容接口的相对路径
                                    rule_config['url'] = profile_api_path(
                                        config_data,
                                        f'/rule-library/content/{rule_id}',
                                    )
                                else:
                                    # 使用规则库中的 URL
                                    rule_config['url'] = new_rule.get('url', '')
                            # 如果 URL 发生变化（且 source_type 是 url），同步更新
                            elif url_changed and new_source_type == 'url':
                                rule_config['url'] = new_url

                            # 更新 behavior
                            if behavior_changed:
                                rule_config['behavior'] = new_behavior

                            # 更新 name
                            if name_changed:
                                rule_config['name'] = new_name

                            # 更新 enabled
                            if enabled_changed:
                                rule_config['enabled'] = new_enabled

                            updated_count += 1

                    if updated_count > 0:
                        changes = []
                        if source_type_changed:
                            changes.append('source_type')
                        if behavior_changed:
                            changes.append('behavior')
                        if name_changed:
                            changes.append('name')
                        if enabled_changed:
                            changes.append(f"enabled ({old_enabled} → {new_enabled})")
                        if url_changed:
                            changes.append(f"url ({old_url} → {new_url})")
                        logger.info(f"Updated {updated_count} rule configurations due to {', '.join(changes)} change(s)")

                save_config()

                # 返回同步更新的规则配置数量
                return jsonify({
                    'success': True,
                    'data': new_rule,
                    'synced_count': updated_count if (source_type_changed or behavior_changed or name_changed or enabled_changed or url_changed) else 0
                })
        return jsonify({'success': False, 'message': 'Rule not found'}), 404


@rule_library_bp.route('/reorder', methods=['POST'])
@require_auth
def reorder_rule_library():
    """批量更新规则仓库顺序

    按 id 排序时传 {'ids': [...], 'position': 'top'|'bottom'}；
    传完整对象数组的旧格式仍然兼容。
    """
    try:
        body = request.json or {}
        new_order, missing = resolve_new_order(config_data.get('rule_library', []), body, 'rules')
        if missing:
            return jsonify({'success': False, 'message': f'以下规则仓库 id 不存在: {missing}'}), 404
        config_data['rule_library'] = new_order
        save_config()
        return jsonify({'success': True, 'order': [r.get('id') for r in new_order]})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@rule_library_bp.route('/content/<rule_id>', methods=['GET'])
def get_rule_library_content(rule_id):
    """获取规则库规则的内容（用于 source_type 为 'content' 的规则）"""
    try:
        rule_library = config_data.get('rule_library', [])
        rule = next((r for r in rule_library if r['id'] == rule_id), None)

        if not rule:
            return jsonify({'success': False, 'message': 'Rule not found'}), 404

        if rule.get('source_type') != 'content':
            return jsonify({'success': False, 'message': 'This rule does not have content'}), 400

        content = rule.get('content', '')
        return content, 200, {'Content-Type': 'text/plain; charset=utf-8'}

    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@rule_library_bp.route('/proxy-domains', methods=['GET', 'POST'])
@require_auth
def handle_proxy_domains():
    """GitHub 代理域名配置管理"""
    if request.method == 'GET':
        # 获取当前代理域名配置
        github_proxy = config_data.get('system_config', {}).get('github_proxy_domain', '')
        return jsonify({'proxy_domains': github_proxy})

    elif request.method == 'POST':
        # 更新代理域名配置
        try:
            data = request.json
            proxy_url = data.get('proxy_domains', '').strip()

            # 确保 system_config 存在
            if 'system_config' not in config_data:
                config_data['system_config'] = {}

            # 保存为字符串
            config_data['system_config']['github_proxy_domain'] = proxy_url
            save_config()

            return jsonify({
                'success': True,
                'proxy_domains': proxy_url
            })
        except Exception as e:
            return jsonify({'success': False, 'message': str(e)}), 500


@rule_library_bp.route('/test-single', methods=['POST'])
@require_auth
def test_single_rule():
    """测试单个规则（检查URL是否可访问）"""
    import requests
    from backend.converters.mihomo import apply_github_proxy_domain
    from backend.common.config import load_config

    try:
        url = request.json.get('url', '')
        if not url:
            return jsonify({'success': False, 'message': 'URL is required'}), 400

        # 应用 GitHub 代理域名替换
        config_data = load_config()
        test_url = apply_github_proxy_domain(url, config_data)

        response = requests.get(test_url, timeout=5)
        return jsonify({
            'success': True,
            'status_code': response.status_code,
            'available': response.status_code == 200
        })
    except requests.RequestException as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@rule_library_bp.route('/test', methods=['POST'])
@require_auth
def test_rules():
    """批量测试规则仓库连通性（只测试 URL 类型的规则）"""
    import requests
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from backend.converters.mihomo import apply_github_proxy_domain

    try:
        rule_library = config_data.get('rule_library', [])

        # 只测试 source_type 为 'url' 的规则，跳过 'content' 类型
        # 同时确保规则有 url 字段且不为空
        url_rules = [
            rule for rule in rule_library
            if rule.get('source_type', 'url') == 'url' and rule.get('url')
        ]

        results = []
        failed_rule_ids = []

        def test_url(rule):
            """测试单个规则URL"""
            try:
                url = rule.get('url', '')
                if not url:
                    return {
                        'id': rule.get('id', ''),
                        'name': rule.get('name', ''),
                        'url': '',
                        'available': False,
                        'error': 'URL is empty'
                    }

                # 应用 GitHub 代理域名替换
                test_url = apply_github_proxy_domain(url, config_data)
                response = requests.head(test_url, timeout=5, allow_redirects=True)
                is_available = response.status_code < 400
                return {
                    'id': rule.get('id', ''),
                    'name': rule.get('name', ''),
                    'url': url,
                    'available': is_available,
                    'status_code': response.status_code
                }
            except requests.exceptions.RequestException as e:
                return {
                    'id': rule.get('id', ''),
                    'name': rule.get('name', ''),
                    'url': rule.get('url', ''),
                    'available': False,
                    'error': str(e)
                }

        # 使用线程池并发测试（只测试 URL 类型的规则）
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(test_url, rule): rule for rule in url_rules}
            for future in as_completed(futures):
                result = future.result()
                results.append(result)

                # 如果不可用，记录ID并自动关闭
                if not result['available']:
                    failed_rule_ids.append(result['id'])
                    # 在规则仓库中关闭该规则
                    for rule in config_data['rule_library']:
                        if rule['id'] == result['id']:
                            rule['enabled'] = False
                            break

        # 关闭关联的规则配置
        if failed_rule_ids:
            all_rules = config_data.get('rule_configs', [])
            for rule in all_rules:
                # 如果规则集关联了失败的规则仓库，自动关闭
                if rule.get('itemType') == 'ruleset' and rule.get('library_rule_id') in failed_rule_ids:
                    rule['enabled'] = False

        # 保存更改
        save_config()

        return jsonify({
            'success': True,
            'results': results,
            'failed_count': len(failed_rule_ids),
            'total_count': len(url_rules),  # 只返回测试的 URL 规则数量
            'skipped_count': len(rule_library) - len(url_rules)  # 跳过的规则内容数量
        })

    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@rule_library_bp.route('/cache', methods=['POST'])
@require_auth
def cache_rules():
    """批量缓存选中的规则到本地"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from backend.utils.rule_utils import save_rule_to_local

    try:
        data = request.get_json()
        rule_ids = data.get('rule_ids', [])
        profile_id = resolve_profile_id()

        if not rule_ids:
            return jsonify({'success': False, 'message': '请选择要缓存的规则'}), 400

        rule_library = config_data.get('rule_library', [])

        # 过滤出要缓存的规则
        rules_to_cache = [rule for rule in rule_library if rule.get('id') in rule_ids]

        if not rules_to_cache:
            return jsonify({'success': False, 'message': '未找到要缓存的规则'}), 404

        results = []
        failed_rule_ids = []

        def cache_single_rule(rule):
            """缓存单个规则"""
            try:
                local_path = save_rule_to_local(rule, profile_id=profile_id)
                return {
                    'id': rule.get('id', ''),
                    'name': rule.get('name', ''),
                    'success': True,
                    'local_path': local_path
                }
            except Exception as e:
                return {
                    'id': rule.get('id', ''),
                    'name': rule.get('name', ''),
                    'success': False,
                    'error': str(e)
                }

        # 使用线程池并发缓存
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(cache_single_rule, rule): rule for rule in rules_to_cache}
            for future in as_completed(futures):
                result = future.result()
                results.append(result)

                # 如果缓存失败，记录ID并自动关闭
                if not result['success']:
                    failed_rule_ids.append(result['id'])
                    # 在规则仓库中关闭该规则
                    for rule in config_data['rule_library']:
                        if rule['id'] == result['id']:
                            rule['enabled'] = False
                            break

        # 关闭关联的规则配置
        if failed_rule_ids:
            all_rules = config_data.get('rule_configs', [])
            for rule in all_rules:
                # 如果规则集关联了失败的规则仓库，自动关闭
                if rule.get('itemType') == 'ruleset' and rule.get('library_rule_id') in failed_rule_ids:
                    rule['enabled'] = False

        # 保存更改
        save_config()

        success_count = len([r for r in results if r['success']])

        return jsonify({
            'success': True,
            'results': results,
            'success_count': success_count,
            'failed_count': len(failed_rule_ids),
            'total_count': len(rules_to_cache)
        })

    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500
