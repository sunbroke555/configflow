"""规则路由模块"""
import copy
import os
import requests
from urllib.parse import urlparse
from flask import request, jsonify, make_response
from backend.routes import rules_bp as bp, rule_sets_bp as rule_sets_bp
from backend.common.auth import require_auth
from backend.common.config import config_data, save_config, get_config
from backend.utils.rule_matcher import parse_rule_line, match_query, is_valid_domain, is_valid_ip
from backend.utils.rule_utils import get_rules_dir, sanitize_rule_name
from backend.utils.logger import get_logger

logger = get_logger(__name__)


def attach_full_url_to_rules(rules: list) -> list:
    """为规则集的相对路径 URL 拼接完整的 server_domain

    Args:
        rules: 规则列表（会被深拷贝，不影响原始数据）

    Returns:
        带有完整 URL 的规则列表副本
    """
    # 深拷贝，避免修改原始数据
    rules_copy = copy.deepcopy(rules)
    server_domain = config_data.get('system_config', {}).get('server_domain', '').strip()

    # 如果 server_domain 为空，不进行拼接
    if not server_domain:
        return rules_copy

    # 遍历所有规则集，拼接相对路径
    for rule in rules_copy:
        if rule.get('itemType') == 'ruleset' and 'url' in rule:
            url = rule['url']
            # 如果是相对路径（以 / 开头），则拼接 server_domain
            if url and url.startswith('/'):
                rule['url'] = f"{server_domain}{url}"

    return rules_copy


def normalize_rule_config_url(rule_item: dict) -> None:
    """Normalize stored rule URLs when linked to the rule library."""
    if not isinstance(rule_item, dict):
        return

    library_rule_id = rule_item.get('library_rule_id')
    if library_rule_id:
        rule_library = config_data.get('rule_library', [])
        library_rule = next((lr for lr in rule_library if lr.get('id') == library_rule_id), None)
        if not library_rule:
            return

        source_type = library_rule.get('source_type', 'url')
        if source_type == 'content':
            rule_item['url'] = f"/api/rule-library/content/{library_rule_id}"
        else:
            rule_item['url'] = library_rule.get('url', '')
        return

    url = rule_item.get('url', '') or ''
    if not url:
        return

    server_domain = (config_data.get('system_config', {}).get('server_domain', '') or '').strip()
    if not server_domain:
        return

    def _parse(value: str):
        prefixed = value if '://' in value else f"http://{value}"
        return urlparse(prefixed)

    parsed_server = _parse(server_domain)
    parsed_url = _parse(url)

    if parsed_url.netloc and parsed_url.netloc == parsed_server.netloc:
        server_path = parsed_server.path.rstrip('/')
        rest_path = parsed_url.path
        if server_path and rest_path.startswith(server_path):
            rest_path = rest_path[len(server_path):]
            if not rest_path.startswith('/'):
                rest_path = '/' + rest_path

        if rest_path.startswith('/api/rule-library/content/'):
            rule_item['url'] = rest_path


@bp.route('', methods=['GET', 'POST'])
@require_auth
def handle_rules():
    """规则管理（包含规则和规则集）"""
    config = get_config()

    if request.method == 'GET':
        # 获取规则并拼接完整 URL（用于前端显示）
        rule_configs = config.get('rule_configs', [])
        rules_with_full_url = attach_full_url_to_rules(rule_configs)
        return jsonify(rules_with_full_url)

    elif request.method == 'POST':
        rule = request.json
        # 确保有 itemType 字段
        if 'itemType' not in rule:
            rule['itemType'] = 'rule' if 'rule_type' in rule else 'ruleset'
        normalize_rule_config_url(rule)
        rule_configs = config_data.setdefault('rule_configs', [])
        rule_configs.insert(0, rule)  # 新规则添加到第一位
        save_config()
        return jsonify({'success': True, 'data': rule})


@bp.route('/<rule_id>', methods=['DELETE', 'PUT'])
@require_auth
def handle_rule(rule_id):
    """单个规则操作（规则或规则集）"""
    rule_configs = config_data.get('rule_configs', [])

    if request.method == 'DELETE':
        config_data['rule_configs'] = [r for r in rule_configs if r['id'] != rule_id]
        save_config()
        return jsonify({'success': True})

    elif request.method == 'PUT':
        for i, r in enumerate(rule_configs):
            if r['id'] == rule_id:
                updated_rule = request.json
                # 保留 itemType
                if 'itemType' not in updated_rule and 'itemType' in r:
                    updated_rule['itemType'] = r['itemType']
                normalize_rule_config_url(updated_rule)
                config_data['rule_configs'][i] = updated_rule
                save_config()
                return jsonify({'success': True, 'data': updated_rule})
        return jsonify({'success': False, 'message': 'Rule not found'}), 404


@bp.route('/reorder', methods=['POST'])
@require_auth
def reorder_rules():
    """批量更新规则和规则集顺序"""
    try:
        # 检查请求体是否存在
        if not request.json:
            logger.warning("Reorder request with no JSON body")
            return jsonify({'success': False, 'message': 'No request body provided'}), 400

        rule_configs = request.json.get('rule_configs')

        # 检查 rule_configs 是否存在且为列表
        if rule_configs is None:
            logger.warning("Reorder request with no rule_configs field")
            return jsonify({'success': False, 'message': 'No rule_configs provided'}), 400

        if not isinstance(rule_configs, list):
            logger.warning(f"Reorder request with invalid rule_configs type: {type(rule_configs)}")
            return jsonify({'success': False, 'message': 'rule_configs must be a list'}), 400

        config_data['rule_configs'] = rule_configs
        save_config()
        logger.info(f"Successfully reordered {len(rule_configs)} rules")
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"Error reordering rules: {e}", exc_info=True)
        return jsonify({'success': False, 'message': str(e)}), 500


@bp.route('/batch', methods=['POST'])
@require_auth
def batch_add_rules():
    """批量添加规则"""
    data = request.json
    rule_type = data.get('rule_type')
    domains = data.get('domains', [])  # 域名列表
    policy = data.get('policy')

    # 获取现有规则数量（只计算 itemType 为 'rule' 的项目）
    rule_configs = config_data.get('rule_configs', [])
    existing_rules_count = len([r for r in rule_configs if r.get('itemType') == 'rule'])

    new_rules = []
    for domain in domains:
        rule = {
            'id': f"rule_{existing_rules_count + len(new_rules) + 1}",
            'rule_type': rule_type,
            'value': domain.strip(),
            'policy': policy,
            'enabled': True,
            'itemType': 'rule'
        }
        new_rules.append(rule)

    rule_configs = config_data.setdefault('rule_configs', [])
    rule_configs[:0] = new_rules  # 新规则添加到最前面
    save_config()
    return jsonify({'success': True, 'count': len(new_rules), 'rules': new_rules})


@bp.route('/local/<name>')
def get_local_rule(name):
    """获取本地缓存的规则文件（通过规则名称）

    这个端点用于 Mihomo 配置中的 rule-providers，
    提供本地缓存的规则文件，避免外部网络请求。

    注意：此接口不需要权限校验，因为会被 Mihomo 等客户端直接访问。

    对于 URL 类型的规则，会尝试在 2 秒内拉取最新数据，
    如果超时则返回本地缓存版本。
    """
    import os
    from flask import send_file
    from backend.common.config import DATA_DIR
    from backend.utils.logger import get_logger

    logger = get_logger(__name__)

    try:
        # 对规则名称进行清理，确保与文件名匹配
        from backend.utils.rule_utils import sanitize_rule_name, get_rules_dir, save_rule_to_local

        filename = f"{sanitize_rule_name(name)}.list"
        filepath = os.path.join(get_rules_dir(), filename)

        logger.info(f"Requesting local rule: {name}, filepath: {filepath}")

        # 查找规则库中的规则（通过名称）
        rule_library = config_data.get('rule_library', [])
        rule = next((r for r in rule_library if r.get('name') == name), None)

        # 如果规则仓库中没有找到该规则
        if not rule:
            # 检查本地缓存是否存在（可能是旧数据）
            if os.path.exists(filepath):
                logger.warning(f"Rule '{name}' not found in library, but cache exists, returning cache")
                with open(filepath, 'rb') as f:
                    content = f.read()
                resp = make_response(content, 200)
                resp.headers['Content-Type'] = 'text/plain; charset=utf-8'
                resp.headers['Content-Length'] = str(len(content))
                return resp
            else:
                logger.error(f"Rule '{name}' not found in rule library and no cache exists")
                return jsonify({
                    'success': False,
                    'message': f'Rule not found in rule library: {name}. Please add it to the rule library first.'
                }), 404

        # 如果找到规则且是 URL 类型，尝试实时拉取最新数据
        if rule.get('source_type') == 'url':
            import requests
            url = rule.get('url', '')
            logger.info(f"Rule '{name}' is URL type, attempting to fetch from: {url}")

            try:
                # 尝试在 2 秒内拉取最新数据
                response = requests.get(url, timeout=2)
                if response.status_code == 200:
                    logger.info(f"Successfully fetched latest data for rule '{name}'")
                    # 更新本地缓存
                    os.makedirs(get_rules_dir(), exist_ok=True)
                    with open(filepath, 'w', encoding='utf-8') as f:
                        f.write(response.text)
                    # 返回响应并添加 Content-Length 头
                    content = response.text.encode('utf-8')
                    resp = make_response(content, 200)
                    resp.headers['Content-Type'] = 'text/plain; charset=utf-8'
                    resp.headers['Content-Length'] = str(len(content))
                    return resp
                else:
                    logger.warning(f"Failed to fetch rule '{name}', status code: {response.status_code}")
            except requests.Timeout:
                logger.warning(f"Timeout fetching rule '{name}', falling back to cache")
            except Exception as e:
                logger.error(f"Error fetching rule '{name}': {e}, falling back to cache")

        # 如果是 content 类型或拉取失败，使用本地缓存
        if os.path.exists(filepath):
            logger.info(f"Returning cached rule file: {filepath}")
            with open(filepath, 'rb') as f:
                content = f.read()
            resp = make_response(content, 200)
            resp.headers['Content-Type'] = 'text/plain; charset=utf-8'
            resp.headers['Content-Length'] = str(len(content))
            return resp
        else:
            # 缓存文件不存在，尝试重新生成
            logger.warning(f"Cache file not found for rule '{name}', regenerating...")
            try:
                save_rule_to_local(rule)
                if os.path.exists(filepath):
                    logger.info(f"Successfully regenerated cache for rule '{name}'")
                    with open(filepath, 'rb') as f:
                        content = f.read()
                    resp = make_response(content, 200)
                    resp.headers['Content-Type'] = 'text/plain; charset=utf-8'
                    resp.headers['Content-Length'] = str(len(content))
                    return resp
                else:
                    logger.error(f"Failed to regenerate cache for rule '{name}'")
                    return jsonify({
                        'success': False,
                        'message': f'Failed to generate cache for rule: {name}'
                    }), 500
            except Exception as cache_error:
                logger.error(f"Error regenerating cache for rule '{name}': {cache_error}")
                return jsonify({
                    'success': False,
                    'message': f'Error generating cache: {str(cache_error)}'
                }), 500

    except Exception as e:
        logger.error(f"Error getting local rule '{name}': {str(e)}")
        return jsonify({'success': False, 'message': str(e)}), 500


# ==================== /api/rule-sets 路由（向后兼容）====================
# 注意：规则集现在统一存储在 rule_configs 数组中，通过 itemType='ruleset' 区分
# 这些接口是为了向后兼容旧版前端代码

@rule_sets_bp.route('', methods=['GET', 'POST'])
@require_auth
def handle_rule_sets():
    """规则集管理（使用 rule_configs 数组）"""
    if request.method == 'GET':
        # 从 rule_configs 中筛选出规则集
        rule_configs = config_data.get('rule_configs', [])
        rule_sets = [r for r in rule_configs if r.get('itemType') == 'ruleset']
        # 拼接完整 URL（用于前端显示）
        rule_sets_with_full_url = attach_full_url_to_rules(rule_sets)
        return jsonify(rule_sets_with_full_url)

    elif request.method == 'POST':
        rule_set = request.json
        # 确保有 itemType 字段
        rule_set['itemType'] = 'ruleset'
        normalize_rule_config_url(rule_set)
        rule_configs = config_data.setdefault('rule_configs', [])
        rule_configs.insert(0, rule_set)  # 新规则集添加到第一位
        save_config()
        return jsonify({'success': True, 'data': rule_set})


@rule_sets_bp.route('/<rule_set_id>', methods=['DELETE', 'PUT'])
@require_auth
def handle_rule_set(rule_set_id):
    """单个规则集操作（使用 rule_configs 数组）"""
    rule_configs = config_data.get('rule_configs', [])

    if request.method == 'DELETE':
        # 从 rule_configs 中删除指定的规则集
        config_data['rule_configs'] = [r for r in rule_configs if not (r.get('id') == rule_set_id and r.get('itemType') == 'ruleset')]
        save_config()
        return jsonify({'success': True})

    elif request.method == 'PUT':
        # 在 rule_configs 中更新指定的规则集
        for i, r in enumerate(rule_configs):
            if r.get('id') == rule_set_id and r.get('itemType') == 'ruleset':
                updated_rule_set = request.json
                # 确保保留 itemType
                updated_rule_set['itemType'] = 'ruleset'
                normalize_rule_config_url(updated_rule_set)
                config_data['rule_configs'][i] = updated_rule_set
                save_config()
                return jsonify({'success': True, 'data': updated_rule_set})
        return jsonify({'success': False, 'message': 'Rule set not found'}), 404


@rule_sets_bp.route('/reorder', methods=['POST'])
@require_auth
def reorder_rule_sets():
    """批量更新规则集顺序（已废弃，请使用 /api/rules/reorder）"""
    # 注意：此接口已废弃，因为规则和规则集现在合并在 rule_configs 中统一排序
    # 保留此接口仅为向后兼容
    try:
        return jsonify({
            'success': False,
            'message': 'This endpoint is deprecated. Please use /api/rules/reorder instead.'
        }), 410
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


def get_ruleset_content(rule_item: dict, library_rule: dict = None) -> str:
    """获取规则集内容（优先使用本地缓存，从URL获取后会自动缓存）

    Args:
        rule_item: 规则集配置项
        library_rule: 规则仓库中的规则（可选）

    Returns:
        规则集内容字符串，获取失败返回空字符串
    """
    import os

    rule_content = ''
    rule_name = ''
    filepath = ''

    # 获取规则名称和缓存路径
    if library_rule:
        rule_name = library_rule.get('name', '')
    if not rule_name:
        rule_name = rule_item.get('name', '')

    if rule_name:
        filename = f"{sanitize_rule_name(rule_name)}.list"
        filepath = os.path.join(get_rules_dir(), filename)

    # 1. 优先尝试从本地缓存读取
    if filepath and os.path.exists(filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                rule_content = f.read()
            logger.info(f"Loaded rule content from cache: {filepath}")
            return rule_content
        except Exception as e:
            logger.warning(f"Failed to read cached rule file {filepath}: {e}")

    # 2. 如果缓存不存在或读取失败，从规则仓库获取内容
    if library_rule and not rule_content:
        source_type = library_rule.get('source_type', 'url')
        if source_type == 'content':
            # 直接使用内容（content类型在规则仓库保存时已经缓存）
            rule_content = library_rule.get('content', '')
            logger.info(f"Using rule content from library config")
            return rule_content
        else:
            # 从 URL 获取
            url = library_rule.get('url', '')
            if url:
                try:
                    response = requests.get(url, timeout=30)
                    if response.status_code == 200:
                        rule_content = response.text
                        logger.info(f"Fetched rule content from library URL: {url}")

                        # 保存到本地缓存
                        if filepath:
                            try:
                                os.makedirs(get_rules_dir(), exist_ok=True)
                                with open(filepath, 'w', encoding='utf-8') as f:
                                    f.write(rule_content)
                                logger.info(f"Cached rule content to: {filepath}")
                            except Exception as cache_error:
                                logger.warning(f"Failed to cache rule content to {filepath}: {cache_error}")

                        return rule_content
                except Exception as e:
                    logger.warning(f"Failed to fetch rule from library URL {url}: {e}")

    # 3. 如果规则仓库没有，尝试从规则集的 url 字段获取
    if not rule_content:
        url = rule_item.get('url', '')
        if url:
            # 如果是相对路径，拼接 server_domain
            if url.startswith('/'):
                server_domain = config_data.get('system_config', {}).get('server_domain', '')
                if server_domain:
                    url = f"{server_domain}{url}"

            try:
                response = requests.get(url, timeout=30)
                if response.status_code == 200:
                    rule_content = response.text
                    logger.info(f"Fetched rule content from item URL: {url}")

                    # 保存到本地缓存
                    if filepath:
                        try:
                            os.makedirs(get_rules_dir(), exist_ok=True)
                            with open(filepath, 'w', encoding='utf-8') as f:
                                f.write(rule_content)
                            logger.info(f"Cached rule content to: {filepath}")
                        except Exception as cache_error:
                            logger.warning(f"Failed to cache rule content to {filepath}: {cache_error}")

                    return rule_content
            except Exception as e:
                logger.warning(f"Failed to fetch rule from item URL {url}: {e}")

    return rule_content


@bp.route('/match-test', methods=['POST'])
@require_auth
def match_test_rule():
    """规则索引 - 测试域名/IP匹配哪条规则（专业版功能）"""
    import time

    # 记录开始时间
    start_time = time.time()

    try:
        data = request.json
        query = data.get('query', '').strip()

        if not query:
            return jsonify({'success': False, 'message': '请输入域名或IP地址'}), 400

        # 验证输入格式
        if not is_valid_domain(query) and not is_valid_ip(query):
            return jsonify({'success': False, 'message': '请输入有效的域名或IP地址'}), 400

        # 获取所有规则配置
        rule_configs = config_data.get('rule_configs', [])
        rule_library = config_data.get('rule_library', [])

        # 遍历规则，按顺序匹配
        for index, rule_item in enumerate(rule_configs, start=1):
            # 跳过禁用的规则
            if not rule_item.get('enabled', True):
                continue

            item_type = rule_item.get('itemType', 'rule')

            if item_type == 'rule':
                # 直接规则匹配
                rule_type = rule_item.get('rule_type', '')
                rule_value = rule_item.get('value', '')
                policy = rule_item.get('policy', 'DIRECT')

                if match_query(query, rule_type, rule_value):
                    # 计算耗时
                    elapsed_time = time.time() - start_time
                    return jsonify({
                        'success': True,
                        'matched': True,
                        'rule_name': f'{rule_type} 规则',
                        'rule_type': 'rule',
                        'matched_line': f'{rule_type},{rule_value}',
                        'policy': policy,
                        'source': '直接配置的规则',
                        'priority': index,
                        'behavior': 'classical',
                        'elapsed_time': round(elapsed_time * 1000, 2)  # 转换为毫秒，保留2位小数
                    })

            elif item_type == 'ruleset':
                # 规则集匹配
                rule_set_name = rule_item.get('name', '规则集')
                policy = rule_item.get('policy', 'DIRECT')
                behavior = rule_item.get('behavior', 'classical')
                library_rule_id = rule_item.get('library_rule_id', '')

                # 获取规则集内容（优先使用本地缓存）
                try:
                    library_rule = None
                    if library_rule_id:
                        library_rule = next((r for r in rule_library if r['id'] == library_rule_id), None)

                    rule_content = get_ruleset_content(rule_item, library_rule)

                    # 如果获取失败，记录警告并跳过该规则集
                    if not rule_content:
                        logger.warning(f'Skipping ruleset "{rule_set_name}": unable to fetch content')
                        continue

                    # 匹配规则集中的每一行
                    for line in rule_content.splitlines():
                        try:
                            parsed = parse_rule_line(line)
                            if not parsed:
                                continue

                            rule_type, rule_value = parsed
                            if match_query(query, rule_type, rule_value):
                                # 计算耗时
                                elapsed_time = time.time() - start_time
                                return jsonify({
                                    'success': True,
                                    'matched': True,
                                    'rule_name': rule_set_name,
                                    'rule_type': 'ruleset',
                                    'matched_line': line.strip(),
                                    'policy': policy,
                                    'source': f'规则集: {rule_set_name}',
                                    'priority': index,
                                    'behavior': behavior,
                                    'elapsed_time': round(elapsed_time * 1000, 2)  # 转换为毫秒，保留2位小数
                                })
                        except Exception as line_error:
                            # 单条规则解析失败，跳过该行继续处理
                            logger.debug(f'Failed to parse line in "{rule_set_name}": {line_error}')
                            continue

                except Exception as e:
                    # 规则集处理失败，记录错误并跳过该规则集
                    logger.error(f'Error processing ruleset "{rule_set_name}": {e}')
                    continue

        # 没有匹配到任何规则
        elapsed_time = time.time() - start_time
        return jsonify({
            'success': True,
            'matched': False,
            'message': '该域名/IP未匹配任何规则，将使用默认策略',
            'elapsed_time': round(elapsed_time * 1000, 2)  # 转换为毫秒，保留2位小数
        })

    except Exception as e:
        logger.error(f'Rule match test failed: {e}')
        return jsonify({'success': False, 'message': f'查询失败: {str(e)}'}), 500


@bp.route('/find-duplicates', methods=['POST'])
@require_auth
def find_duplicate_rules():
    """查找重复规则 - 检查直接规则与规则集内容中的重复条目"""
    import time

    start_time = time.time()

    try:
        rule_configs = config_data.get('rule_configs', [])
        rule_library = config_data.get('rule_library', [])

        # key: (规则类型, 归一化规则值) -> {'value': 首次出现的原始值, 'items': 出现位置列表}
        occurrences = {}
        rules_checked = 0
        rulesets_checked = 0
        failed_rulesets = []

        # 正则类规则值大小写敏感，不做小写归一
        case_sensitive_types = {'REGEX', 'REGEXP'}

        def add_occurrence(rule_type, rule_value, item):
            normalized = rule_value if rule_type in case_sensitive_types else rule_value.lower()
            entry = occurrences.setdefault((rule_type, normalized), {'value': rule_value, 'items': []})
            entry['items'].append(item)

        for index, rule_item in enumerate(rule_configs, start=1):
            # 跳过禁用的规则（与生成配置、规则索引行为一致）
            if not rule_item.get('enabled', True):
                continue

            item_type = rule_item.get('itemType', 'rule')

            if item_type == 'rule':
                rule_type = (rule_item.get('rule_type') or '').strip().upper()
                rule_value = (rule_item.get('value') or '').strip()
                if not rule_type or not rule_value:
                    continue

                rules_checked += 1
                add_occurrence(rule_type, rule_value, {
                    'source_type': 'rule',
                    'source': '直接配置的规则',
                    'rule_id': rule_item.get('id', ''),
                    'policy': rule_item.get('policy', 'DIRECT'),
                    'priority': index,
                    'line': f'{rule_type},{rule_value}'
                })

            elif item_type == 'ruleset':
                rule_set_name = rule_item.get('name', '规则集')
                policy = rule_item.get('policy', 'DIRECT')
                library_rule_id = rule_item.get('library_rule_id', '')

                library_rule = None
                if library_rule_id:
                    library_rule = next((r for r in rule_library if r.get('id') == library_rule_id), None)

                rule_content = get_ruleset_content(rule_item, library_rule)
                if not rule_content:
                    logger.warning(f'Skipping ruleset "{rule_set_name}" in duplicate check: unable to fetch content')
                    failed_rulesets.append(rule_set_name)
                    continue

                rulesets_checked += 1
                for line_no, line in enumerate(rule_content.splitlines(), start=1):
                    parsed = parse_rule_line(line)
                    if not parsed:
                        continue

                    rule_type, rule_value = parsed
                    if not rule_value:
                        continue

                    add_occurrence(rule_type, rule_value, {
                        'source_type': 'ruleset',
                        'source': rule_set_name,
                        'rule_id': rule_item.get('id', ''),
                        'policy': policy,
                        'priority': index,
                        'line': line.strip(),
                        'line_no': line_no
                    })

        # 同一规则集内部与跨来源的重复都算重复
        duplicates = []
        for (rule_type, _), entry in occurrences.items():
            items = entry['items']
            if len(items) < 2:
                continue
            duplicates.append({
                'rule_type': rule_type,
                'value': entry['value'],  # 首次出现的原始值，保留大小写用于展示
                'count': len(items),
                'policy_conflict': len({i['policy'] for i in items}) > 1,
                'occurrences': items
            })

        # 策略冲突的排前面，其余按出现次数降序、优先级升序
        duplicates.sort(key=lambda d: (not d['policy_conflict'], -d['count'], d['occurrences'][0]['priority']))

        elapsed_time = time.time() - start_time
        return jsonify({
            'success': True,
            'duplicates': duplicates,
            'stats': {
                'rules_checked': rules_checked,
                'rulesets_checked': rulesets_checked,
                'failed_rulesets': failed_rulesets,
                'duplicate_groups': len(duplicates)
            },
            'elapsed_time': round(elapsed_time * 1000, 2)  # 毫秒
        })

    except Exception as e:
        logger.error(f'Find duplicate rules failed: {e}', exc_info=True)
        return jsonify({'success': False, 'message': f'查重失败: {str(e)}'}), 500
