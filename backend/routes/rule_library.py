"""规则仓库路由模块

提供规则仓库（Rule Library）的 CRUD 操作和相关功能
"""
import os
import hashlib
import json
from flask import request, jsonify
from flask import Blueprint
from backend.common.auth import require_auth
from backend.common.config import get_shared_config, save_shared_config, update_shared_config_transaction, get_config, get_repository, get_system_config, save_system_config
from backend.common.config_repository import ProfileRepositoryError, RuleLibraryCreationUncertain
from backend.utils.reorder import resolve_new_order
from backend.utils.rule_utils import sanitize_rule_name, get_rules_dir, save_rule_to_local, rule_cache_filename, read_rule_source_content
from backend.utils.logger import get_logger
from backend.utils.rule_fetch import request_rule
from backend.utils.url_utils import safe_exception_details
from backend.utils.ruleset_content import inspect_domain_content, validate_domain_content, read_rule_payload, append_rule_lines

logger = get_logger(__name__)

# 创建规则库蓝图
rule_library_bp = Blueprint('rule_library', __name__, url_prefix='/api/rule-library')


def _content_revision(rule):
    return hashlib.sha256(json.dumps(rule, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


@rule_library_bp.route('/check-content', methods=['POST'])
@require_auth
def check_rule_content():
    """Preview format repairs without changing shared sources or caches."""
    from backend.common.config_repository import ProfileValidationError
    body = request.get_json()
    if not isinstance(body, dict) or not isinstance(body.get('content'), str):
        raise ProfileValidationError('请提交规则类型和文本内容。')
    content = body['content']
    lines = body.get('append_lines', [])
    if not isinstance(lines, list) or not all(isinstance(line, str) and '\n' not in line and '\r' not in line for line in lines):
        raise ProfileValidationError('新增规则应为单行文本列表。')
    if lines:
        _, payload = read_rule_payload(content)
        content = append_rule_lines(content, lines, payload)
    return jsonify(inspect_domain_content(content, body.get('behavior'), body.get('format')))


@rule_library_bp.route('/<rule_id>/draft-content', methods=['GET', 'PUT'])
@require_auth
def edit_rule_library_content(rule_id):
    """Edit a single shared content source, without touching profile choices or ROS."""
    from werkzeug.exceptions import Conflict, NotFound
    from backend.common.config_repository import ProfileValidationError

    def find(shared):
        rule = next((item for item in shared.get('rule_library', []) if item.get('id') == rule_id), None)
        if rule is None:
            raise NotFound('规则集已不存在，请重新载入。')
        return rule

    if request.method == 'GET':
        try:
            rule = find(get_shared_config())
        except NotFound as error:
            return jsonify({'success': False, 'message': error.description}), 404
        return jsonify({'success': True, 'revision': _content_revision(rule),
                        'rule': {key: rule.get(key, '') for key in
                                 ('id', 'name', 'source_type', 'behavior', 'content', 'url')},
                        'usage_count': len(get_repository().get_resource_usage('rule_library', rule_id))}), 200, {'Cache-Control': 'no-store'}

    body = request.get_json()
    if (not isinstance(body, dict) or set(body) != {'revision', 'content'}
            or not isinstance(body['revision'], str) or not isinstance(body['content'], str)):
        raise ProfileValidationError('请提交规则内容及读取时的版本。')
    updated = {}

    def mutate(shared):
        rule = find(shared)
        if body['revision'] != _content_revision(rule):
            raise Conflict('规则集已在其他页面更新，请重新读取后再编辑。')
        if rule.get('source_type') != 'content':
            raise ProfileValidationError('订阅规则由订阅源更新，请使用内容型规则集添加手动 IP。')
        validate_domain_content({**rule, 'content': body['content']}, rule)
        rule['content'] = body['content']
        updated.update(rule)

    try:
        update_shared_config_transaction(mutate)
    except (NotFound, Conflict) as error:
        return jsonify({'success': False, 'message': error.description}), error.code
    save_rule_to_local(updated)
    return jsonify({'success': True, 'revision': _content_revision(updated)})


@rule_library_bp.route('', methods=['GET', 'POST'])
@require_auth
def handle_rule_library():
    """规则仓库管理"""
    config_data = get_shared_config()
    if request.method == 'GET':
        from backend.common.rule_dns import has_ip_part
        # has_ip_part: routing this source in OxiDNS DNS creates an IP group.
        return jsonify([{**rule, 'has_ip_part': has_ip_part({**rule, 'itemType': 'ruleset'})}
                        for rule in config_data.get('rule_library', [])])

    elif request.method == 'POST':
        rule = request.json
        from backend.common.config_repository import ProfileValidationError, ProfileExists
        if not isinstance(rule, dict):
            raise ProfileValidationError('规则库请求必须是 JSON 对象')
        if not isinstance(rule.get('id'), str) or not rule['id']:
            raise ProfileValidationError('规则库条目必须有 ID')
        if any(item['id'] == rule['id'] for item in config_data.get('rule_library', [])):
            raise ProfileExists('规则库 ID 已存在，请刷新后重试')
        rule.pop('has_ip_part', None)
        validate_domain_content(rule)

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

        import requests
        try:
            content = read_rule_source_content(rule)
        except requests.RequestException:
            return jsonify({'success': False, 'message': '规则源下载失败，未添加规则，请检查来源后重试'}), 502
        if content is None:
            raise ProfileValidationError('请提供有效的规则来源和订阅地址')
        try:
            get_repository().create_rule_library_item(rule, content)
        except RuleLibraryCreationUncertain as error:
            logger.exception('New rule source storage result requires confirmation')
            return jsonify({'success': False, 'confirmation_required': True, 'message': str(error)}), 503
        except OSError:
            logger.exception('Failed to commit new rule source and cache')
            return jsonify({'success': False, 'message': '规则保存失败，未添加规则，请稍后重试'}), 500
        return jsonify({'success': True, 'data': rule})


@rule_library_bp.route('/<rule_id>', methods=['DELETE', 'PUT'])
@require_auth
def handle_rule_library_item(rule_id):
    """Update shared sources without overwriting independent compositions."""
    from werkzeug.exceptions import NotFound
    from backend.common.config_repository import ProfileValidationError
    from backend.common.config import update_shared_and_config_transaction
    from backend.common.rule_dns import apply_rule_dns
    new_rule = request.get_json() if request.method == 'PUT' else None
    base_url = request.url_root.rstrip('/')
    previous, warnings = {}, []
    def mutate(shared):
        library = shared.setdefault('rule_library', [])
        old = next((item for item in library if item.get('id') == rule_id), None)
        if old is None:
            raise NotFound('Rule not found')
        previous.update(old)
        if request.method == 'DELETE':
            library.remove(old)
            return
        if not isinstance(new_rule, dict):
            raise ProfileValidationError('规则库请求必须是 JSON 对象')
        new_rule['id'] = rule_id
        new_rule.pop('base_url', None)
        new_rule.pop('has_ip_part', None)
        new_rule.pop('url' if new_rule.get('source_type') == 'content' else 'content', None)
        validate_domain_content(new_rule, old)
        library[library.index(old)] = new_rule

    def native_profiles(shared, profiles):
        """Native OxiDNS profiles whose routing this edit changes."""
        old = next((item for item in shared.get('rule_library', []) if item.get('id') == rule_id), None)
        if old is None or not isinstance(new_rule, dict) or all(
                new_rule.get(key, default) == old.get(key, default) for key, default in (('enabled', True), ('behavior', 'domain'))):
            return []
        return [profile_id for profile_id, profile in profiles.items()
                if (profile.get('oxidns') or {}).get('config_mode') == 'native' and
                any(item.get('library_rule_id') == rule_id for item in profile.get('rule_configs', []))]

    def rebuild(profile_id, before, after):
        # Enabling, disabling or retyping a source rebuilds native routing in the
        # same commit, parking and restoring the DNS choices of its references.
        warnings.extend({'profile': profile_id, 'message': message}
                        for message in apply_rule_dns(before, after, {}, base_url=base_url, strict=False))

    if new_rule is None:
        update_shared_config_transaction(mutate)
    else:
        update_shared_and_config_transaction(mutate, rebuild, native_profiles)
    if new_rule is None:
        try:
            os.remove(os.path.join(get_rules_dir(), rule_cache_filename(previous)))
        except FileNotFoundError:
            pass
    old_name = previous.get('name')
    if old_name and (new_rule is None or new_rule.get('name') != old_name):
        old_path = os.path.join(get_rules_dir(), f'{sanitize_rule_name(old_name)}.list')
        try:
            os.remove(old_path)
        except FileNotFoundError:
            pass
    if new_rule is not None:
        save_rule_to_local(new_rule)
        usages = get_repository().get_resource_usage('rule_library', rule_id)
        return jsonify({'success': True, 'data': new_rule, 'synced_count': len(usages), 'warnings': warnings})
    return jsonify({'success': True})


@rule_library_bp.route('/reorder', methods=['POST'])
@require_auth
def reorder_rule_library():
    """批量更新规则仓库顺序

    按 id 排序时传 {'ids': [...], 'position': 'top'|'bottom'}；
    传完整对象数组的旧格式仍然兼容。
    """
    config_data = get_shared_config()
    try:
        body = request.json or {}
        new_order, missing = resolve_new_order(config_data.get('rule_library', []), body, 'rules')
        if missing:
            return jsonify({'success': False, 'message': f'以下规则仓库 id 不存在: {missing}'}), 404
        config_data['rule_library'] = new_order
        save_shared_config(config_data)
        return jsonify({'success': True, 'order': [r.get('id') for r in new_order]})
    except ProfileRepositoryError:
        raise
    except Exception as e:
        return jsonify({'success': False, 'message': safe_exception_details(e)}), 500


@rule_library_bp.route('/content/<rule_id>', methods=['GET'])
def get_rule_library_content(rule_id):
    """获取规则库规则的内容（用于 source_type 为 'content' 的规则）"""
    config_data = get_shared_config()
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
        return jsonify({'success': False, 'message': safe_exception_details(e)}), 500


@rule_library_bp.route('/proxy-domains', methods=['GET', 'POST'])
@require_auth
def handle_proxy_domains():
    """GitHub 代理域名配置管理"""
    system_config = get_system_config()
    if request.method == 'GET':
        # 获取当前代理域名配置
        github_proxy = system_config.get('system_config', {}).get('github_proxy_domain', '')
        return jsonify({'proxy_domains': github_proxy})

    elif request.method == 'POST':
        # 更新代理域名配置
        try:
            data = request.json
            proxy_url = data.get('proxy_domains', '').strip()

            system_config.setdefault('system_config', {})['github_proxy_domain'] = proxy_url
            save_system_config(system_config)

            return jsonify({
                'success': True,
                'proxy_domains': proxy_url
            })
        except Exception as e:
            return jsonify({'success': False, 'message': safe_exception_details(e)}), 500


@rule_library_bp.route('/test-single', methods=['POST'])
@require_auth
def test_single_rule():
    """测试单个规则（检查URL是否可访问）"""
    import requests
    from backend.converters.mihomo import apply_github_proxy_domain

    try:
        url = request.json.get('url', '')
        if not url:
            return jsonify({'success': False, 'message': 'URL is required'}), 400

        # 应用 GitHub 代理域名替换
        config_data = get_system_config()
        test_url = apply_github_proxy_domain(url, config_data)

        response = request_rule(test_url, timeout=5, config_data=config_data)
        return jsonify({
            'success': True,
            'status_code': response.status_code,
            'available': response.status_code == 200
        })
    except (requests.RequestException, ValueError) as e:
        return jsonify({'success': False, 'message': safe_exception_details(e)}), 500


@rule_library_bp.route('/test', methods=['POST'])
@require_auth
def test_rules():
    """批量测试规则仓库连通性（只测试 URL 类型的规则）"""
    import requests
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from backend.converters.mihomo import apply_github_proxy_domain

    config_data = get_shared_config()
    config_data['system_config'] = get_system_config().get('system_config', {})
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
                response = request_rule(test_url, method='head', timeout=5, config_data=config_data)
                is_available = response.status_code < 400
                return {
                    'id': rule.get('id', ''),
                    'name': rule.get('name', ''),
                    'url': url,
                    'available': is_available,
                    'status_code': response.status_code
                }
            except (requests.exceptions.RequestException, ValueError) as e:
                return {
                    'id': rule.get('id', ''),
                    'name': rule.get('name', ''),
                    'url': rule.get('url', ''),
                    'available': False,
                    'error': safe_exception_details(e)
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

        # The shared enabled gate is resolved dynamically for every profile.
        config_data.pop('system_config', None)
        save_shared_config(config_data)

        return jsonify({
            'success': True,
            'results': results,
            'failed_count': len(failed_rule_ids),
            'total_count': len(url_rules),  # 只返回测试的 URL 规则数量
            'skipped_count': len(rule_library) - len(url_rules)  # 跳过的规则内容数量
        })

    except ProfileRepositoryError:
        raise
    except Exception as e:
        return jsonify({'success': False, 'message': safe_exception_details(e)}), 500


@rule_library_bp.route('/cache', methods=['POST'])
@require_auth
def cache_rules():
    """批量缓存选中的规则到本地"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from backend.utils.rule_utils import save_rule_to_local

    config_data = get_shared_config()
    try:
        data = request.get_json()
        rule_ids = data.get('rule_ids', [])

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
                local_path = save_rule_to_local(rule)
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
                    'error': safe_exception_details(e)
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

        # Preserve each profile's independent enabled preference.
        save_shared_config(config_data)

        success_count = len([r for r in results if r['success']])

        return jsonify({
            'success': True,
            'results': results,
            'success_count': success_count,
            'failed_count': len(failed_rule_ids),
            'total_count': len(rules_to_cache)
        })

    except ProfileRepositoryError:
        raise
    except Exception as e:
        return jsonify({'success': False, 'message': safe_exception_details(e)}), 500
