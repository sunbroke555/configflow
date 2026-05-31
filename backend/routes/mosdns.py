"""MosDNS 配置路由模块"""
import logging
import os
from urllib.parse import urlparse

from flask import request, jsonify

from backend.converters.mihomo import apply_github_proxy_domain
from backend.routes import mosdns_bp as bp
from backend.common.auth import require_auth
from backend.common.config import config_data, save_config
from backend.utils.rule_utils import get_rules_dir, sanitize_rule_name

logger = logging.getLogger(__name__)


def _load_cached_rule_content_for_url(original_url: str) -> str:
    """尝试从本地规则缓存中读取与 URL 对应的规则内容。"""
    if not original_url:
        return ''

    candidate_names = []

    # 1. 优先从 rule_configs / rule_library 中按 URL 反查规则名称
    for rule_item in config_data.get('rule_configs', []):
        if rule_item.get('itemType') == 'ruleset' and rule_item.get('url') == original_url:
            name = rule_item.get('name', '')
            if name:
                candidate_names.append(name)

    for library_item in config_data.get('rule_library', []):
        if library_item.get('url') == original_url:
            name = library_item.get('name', '')
            if name:
                candidate_names.append(name)

    # 2. 如果是 rule-library/content/<id> 形式，按 id 找规则库名称
    parsed = urlparse(original_url)
    path = parsed.path or ''
    marker = '/api/rule-library/content/'
    if marker in path:
        rule_id = path.split(marker, 1)[1].strip('/').split('/', 1)[0]
        if rule_id:
            library_item = next(
                (r for r in config_data.get('rule_library', []) if r.get('id') == rule_id),
                None
            )
            if library_item and library_item.get('name'):
                candidate_names.append(library_item['name'])

    # 去重并尝试读取缓存文件
    seen = set()
    for name in candidate_names:
        if not name or name in seen:
            continue
        seen.add(name)

        filename = f"{sanitize_rule_name(name)}.list"
        filepath = os.path.join(get_rules_dir(), filename)
        if os.path.exists(filepath):
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    cached_content = f.read()
                logger.info(f"使用本地规则缓存兜底: {filepath}")
                return cached_content
            except Exception as e:
                logger.warning(f"读取本地规则缓存失败 {filepath}: {e}")

    return ''


@bp.route('/rulesets', methods=['GET', 'POST'])
@require_auth
def handle_mosdns_rulesets():
    """MosDNS 规则集管理"""
    # 确保 mosdns 字段存在
    if 'mosdns' not in config_data:
        config_data['mosdns'] = {
            'direct_rulesets': [],
            'proxy_rulesets': [],
            'direct_rules': [],
            'proxy_rules': [],
            'local_dns': '',
            'remote_dns': '',
            'fallback_dns': '',
            'default_forward': 'forward_remote',
            'custom_hosts': '',
            'custom_config': ''
        }

    mosdns_config = config_data['mosdns']

    if request.method == 'GET':
        return jsonify({
            'direct_rulesets': mosdns_config.get('direct_rulesets', []),
            'proxy_rulesets': mosdns_config.get('proxy_rulesets', []),
            'direct_rules': mosdns_config.get('direct_rules', []),
            'proxy_rules': mosdns_config.get('proxy_rules', [])
        })

    elif request.method == 'POST':
        try:
            data = request.json
            mosdns_config['direct_rulesets'] = data.get('direct_rulesets', [])
            mosdns_config['proxy_rulesets'] = data.get('proxy_rulesets', [])
            mosdns_config['direct_rules'] = data.get('direct_rules', [])
            mosdns_config['proxy_rules'] = data.get('proxy_rules', [])
            save_config()
            return jsonify({'success': True})
        except Exception as e:
            return jsonify({'success': False, 'message': str(e)}), 500


@bp.route('/custom-matches', methods=['GET', 'POST'])
@require_auth
def handle_mosdns_custom_matches():
    """MosDNS 自定义匹配规则管理"""
    # 确保 mosdns 字段存在
    if 'mosdns' not in config_data:
        config_data['mosdns'] = {
            'direct_rulesets': [],
            'proxy_rulesets': [],
            'direct_rules': [],
            'proxy_rules': [],
            'local_dns': '',
            'remote_dns': '',
            'fallback_dns': '',
            'default_forward': 'forward_remote',
            'custom_hosts': '',
            'custom_config': '',
            'custom_matches': [],
            'custom_match_position': 'tail'
        }

    mosdns_config = config_data['mosdns']

    if request.method == 'GET':
        return jsonify({
            'custom_matches': mosdns_config.get('custom_matches', []),
            'position': mosdns_config.get('custom_match_position', 'tail')
        })

    elif request.method == 'POST':
        try:
            data = request.json
            mosdns_config['custom_matches'] = data.get('custom_matches', [])
            mosdns_config['custom_match_position'] = data.get('position', 'tail')
            save_config()
            return jsonify({'success': True})
        except Exception as e:
            return jsonify({'success': False, 'message': str(e)}), 500


@bp.route('/dns-servers', methods=['GET', 'POST'])
@require_auth
def handle_mosdns_dns_servers():
    """MosDNS DNS 服务器配置"""
    # 确保 mosdns 字段存在
    if 'mosdns' not in config_data:
        config_data['mosdns'] = {
            'direct_rulesets': [],
            'proxy_rulesets': [],
            'direct_rules': [],
            'proxy_rules': [],
            'local_dns': '',
            'remote_dns': '',
            'fallback_dns': '',
            'default_forward': 'forward_remote',
            'custom_hosts': '',
            'custom_config': ''
        }

    mosdns_config = config_data['mosdns']

    if request.method == 'GET':
        return jsonify({
            'local_dns': mosdns_config.get('local_dns', ''),
            'remote_dns': mosdns_config.get('remote_dns', ''),
            'fallback_dns': mosdns_config.get('fallback_dns', ''),
            'default_forward': mosdns_config.get('default_forward', 'forward_remote'),
            'custom_hosts': mosdns_config.get('custom_hosts', '')
        })

    elif request.method == 'POST':
        try:
            data = request.json
            mosdns_config['local_dns'] = data.get('local_dns', '')
            mosdns_config['remote_dns'] = data.get('remote_dns', '')
            mosdns_config['fallback_dns'] = data.get('fallback_dns', '')
            mosdns_config['default_forward'] = data.get('default_forward', 'forward_remote')
            mosdns_config['custom_hosts'] = data.get('custom_hosts', '')
            save_config()
            return jsonify({'success': True})
        except Exception as e:
            return jsonify({'success': False, 'message': str(e)}), 500


@bp.route('/log-settings', methods=['GET', 'POST'])
@require_auth
def handle_mosdns_log_settings():
    """MosDNS 日志设置"""
    # 确保 mosdns 字段存在
    if 'mosdns' not in config_data:
        config_data['mosdns'] = {
            'direct_rulesets': [],
            'proxy_rulesets': [],
            'direct_rules': [],
            'proxy_rules': [],
            'local_dns': '',
            'remote_dns': '',
            'fallback_dns': '',
            'default_forward': 'forward_remote',
            'custom_hosts': '',
            'custom_config': '',
            'log_enabled': True,
            'log_level': 'info',
            'log_file': ''
        }

    mosdns_config = config_data['mosdns']

    if request.method == 'GET':
        return jsonify({
            'log_enabled': mosdns_config.get('log_enabled', True),
            'log_level': mosdns_config.get('log_level', 'info'),
            'log_file': mosdns_config.get('log_file', '')
        })

    elif request.method == 'POST':
        try:
            data = request.json
            mosdns_config['log_enabled'] = data.get('log_enabled', True)
            mosdns_config['log_level'] = data.get('log_level', 'info')
            mosdns_config['log_file'] = data.get('log_file', '')
            save_config()
            return jsonify({'success': True})
        except Exception as e:
            return jsonify({'success': False, 'message': str(e)}), 500


@bp.route('/api-settings', methods=['GET', 'POST'])
@require_auth
def handle_mosdns_api_settings():
    """MosDNS API 设置"""
    # 确保 mosdns 字段存在
    if 'mosdns' not in config_data:
        config_data['mosdns'] = {
            'direct_rulesets': [],
            'proxy_rulesets': [],
            'direct_rules': [],
            'proxy_rules': [],
            'local_dns': '',
            'remote_dns': '',
            'fallback_dns': '',
            'default_forward': 'forward_remote',
            'custom_hosts': '',
            'custom_config': '',
            'api_enabled': True,
            'api_address': ':8080'
        }

    mosdns_config = config_data['mosdns']

    if request.method == 'GET':
        return jsonify({
            'api_enabled': mosdns_config.get('api_enabled', True),
            'api_address': mosdns_config.get('api_address', mosdns_config.get('api_addr', ':8080'))  # 兼容旧字段
        })

    elif request.method == 'POST':
        try:
            data = request.json
            mosdns_config['api_enabled'] = data.get('api_enabled', True)
            mosdns_config['api_address'] = data.get('api_address', ':8080')
            # 清理旧字段
            if 'api_addr' in mosdns_config:
                del mosdns_config['api_addr']
            save_config()
            return jsonify({'success': True})
        except Exception as e:
            return jsonify({'success': False, 'message': str(e)}), 500


@bp.route('/cache-settings', methods=['GET', 'POST'])
@require_auth
def handle_mosdns_cache_settings():
    """MosDNS 缓存设置"""
    # 确保 mosdns 字段存在
    if 'mosdns' not in config_data:
        config_data['mosdns'] = {
            'direct_rulesets': [],
            'proxy_rulesets': [],
            'direct_rules': [],
            'proxy_rules': [],
            'local_dns': '',
            'remote_dns': '',
            'fallback_dns': '',
            'default_forward': 'forward_remote',
            'custom_hosts': '',
            'custom_config': '',
            'cache_enabled': True,
            'cache_size': 10240,
            'cache_lazy_ttl': 21600,
            'cache_dump_file': './cache.dump',
            'cache_dump_interval': 300
        }

    mosdns_config = config_data['mosdns']

    # 确保字段存在（兼容老配置）
    if 'cache_enabled' not in mosdns_config:
        mosdns_config['cache_enabled'] = True
    if 'cache_size' not in mosdns_config:
        mosdns_config['cache_size'] = 10240
    if 'cache_lazy_ttl' not in mosdns_config:
        mosdns_config['cache_lazy_ttl'] = 21600
    if 'cache_dump_file' not in mosdns_config:
        mosdns_config['cache_dump_file'] = './cache.dump'
    if 'cache_dump_interval' not in mosdns_config:
        mosdns_config['cache_dump_interval'] = 300

    if request.method == 'GET':
        return jsonify({
            'cache_enabled': mosdns_config.get('cache_enabled', True),
            'cache_size': mosdns_config.get('cache_size', 10240),
            'cache_lazy_ttl': mosdns_config.get('cache_lazy_ttl', 21600),
            'cache_dump_file': mosdns_config.get('cache_dump_file', './cache.dump'),
            'cache_dump_interval': mosdns_config.get('cache_dump_interval', 300)
        })

    elif request.method == 'POST':
        try:
            data = request.json or {}

            mosdns_config['cache_enabled'] = bool(data.get('cache_enabled', True))

            # 数字字段做简单容错
            def _to_int(value, default: int) -> int:
                try:
                    return int(value)
                except Exception:
                    return default

            mosdns_config['cache_size'] = _to_int(data.get('cache_size', 10240), 10240)
            mosdns_config['cache_lazy_ttl'] = _to_int(data.get('cache_lazy_ttl', 21600), 21600)
            mosdns_config['cache_dump_interval'] = _to_int(data.get('cache_dump_interval', 300), 300)

            dump_file = data.get('cache_dump_file', './cache.dump')
            mosdns_config['cache_dump_file'] = str(dump_file) if dump_file is not None else './cache.dump'

            save_config()
            return jsonify({'success': True})
        except Exception as e:
            return jsonify({'success': False, 'message': str(e)}), 500


@bp.route('/rule-proxy', methods=['GET'])
def mosdns_rule_proxy():
    """
    MosDNS 规则代理接口
    拉取原始规则文件，转换格式后返回

    支持的格式转换：
    1. Clash 格式:
       - DOMAIN-SUFFIX,example.com → domain:example.com
       - DOMAIN,example.com → full:example.com
       - DOMAIN-KEYWORD,example → keyword:example
       - DOMAIN-REGEX,^.*example.*$ → regexp:^.*example.*$

    2. List 格式:
       - +.example.com → domain:example.com (匹配域名及所有子域名)
       - .example.com → regexp:.+\.example\.com$ (仅匹配子域名)
       - *.example.com → regexp:^[^.]+\.example\.com$ (仅匹配直接子域名)
       - example.com → full:example.com (精确匹配)
       - ip -> ip

    注意：如果内容已经是 mosdns 格式，则直接返回，不进行转换
    """
    try:
        import re
        import requests

        # 获取原始 URL
        original_url = request.args.get('url')
        if not original_url:
            return jsonify({'success': False, 'message': 'URL parameter is required'}), 400

        # 应用 GitHub 代理域名替换
        fetch_url = apply_github_proxy_domain(original_url, config_data)

        # 拉取原始规则文件
        original_content = ''
        try:
            response = requests.get(fetch_url, timeout=10)
            response.raise_for_status()
            original_content = response.text
        except requests.exceptions.RequestException as e:
            logger.warning(f"远程拉取规则失败，尝试使用本地缓存兜底: {original_url}, 错误: {e}")
            original_content = _load_cached_rule_content_for_url(original_url)
            if not original_content:
                return jsonify({'success': False, 'message': f'Failed to fetch original URL: {str(e)}'}), 500

        # 检测内容格式
        # 如果内容已经是 mosdns 格式，则直接返回
        # mosdns 格式特征：domain:xxx / full:xxx / keyword:xxx / regexp:xxx
        is_mosdns_format = False
        is_yaml_format = False
        sample_lines = []

        # 检查是否是 YAML 格式
        if 'payload:' in original_content:
            is_yaml_format = True
        else:
            for line in original_content.split('\n')[:20]:  # 检查前20行
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                sample_lines.append(line)

                # 检查是否是 mosdns 格式（使用冒号分隔）
                if ':' in line:
                    parts = line.split(':', 1)
                    if len(parts) == 2:
                        rule_type = parts[0].strip().lower()
                        # mosdns 支持的规则类型
                        if rule_type in ['domain', 'full', 'keyword', 'regexp', 'ip']:
                            is_mosdns_format = True
                            break

        # 如果已经是 mosdns 格式，直接返回原内容
        if is_mosdns_format:
            return original_content, 200, {'Content-Type': 'text/plain; charset=utf-8'}

        # 准备规则行列表
        rule_lines = []

        # 如果是 YAML 格式，解析 payload
        if is_yaml_format:
            import yaml
            try:
                data = yaml.safe_load(original_content)
                if data and 'payload' in data:
                    rule_lines = data['payload']
            except Exception as e:
                # YAML 解析失败，尝试按文本方式处理
                logger.warning(f"Failed to parse YAML, falling back to text mode: {str(e)}")
                is_yaml_format = False

        # 如果不是 YAML 格式或 YAML 解析失败，按文本行处理
        if not is_yaml_format:
            rule_lines = original_content.split('\n')

        # 进行格式转换（Clash/List -> mosdns）
        converted_lines = []
        for line in rule_lines:
            # 如果是字符串，去除空白；如果不是，跳过
            if isinstance(line, str):
                line = line.strip()
            else:
                continue

            # 跳过空行和注释
            if not line or line.startswith('#'):
                continue

            # 检测并转换 Clash 格式（包含逗号）
            if ',' in line:
                parts = line.split(',', 1)
                if len(parts) == 2:
                    rule_type = parts[0].strip()
                    value = parts[1].strip()

                    # 转换规则类型（Clash -> mosdns，mosdns 用冒号）
                    if rule_type == 'DOMAIN-SUFFIX':
                        converted_lines.append(f"domain:{value}")
                    elif rule_type == 'DOMAIN':
                        converted_lines.append(f"full:{value}")
                    elif rule_type == 'DOMAIN-KEYWORD':
                        converted_lines.append(f"keyword:{value}")
                    elif rule_type == 'DOMAIN-REGEX':
                        converted_lines.append(f"regexp:{value}")
                    # 其他类型的规则被移除（不添加到结果中）

            # 检测并转换 List 格式（通配符格式）
            else:
                # +.example.com → domain:example.com (匹配域名及所有子域名)
                if line.startswith('+.'):
                    domain = line[2:]  # 移除 +. 前缀
                    converted_lines.append(f"domain:{domain}")

                # .example.com → regexp:.+\.example\.com$ (仅匹配子域名，不匹配域名本身)
                elif line.startswith('.') and not line.startswith('..'):
                    domain = line[1:]  # 移除 . 前缀
                    # 转义域名中的点号，构造正则表达式
                    escaped_domain = re.escape(domain)
                    converted_lines.append(f"regexp:.+\\.{escaped_domain}$")

                # *.example.com → regexp:^[^.]+\.example\.com$ (仅匹配直接子域名)
                elif line.startswith('*.'):
                    domain = line[2:]  # 移除 *. 前缀
                    # 转义域名中的点号，构造正则表达式
                    escaped_domain = re.escape(domain)
                    converted_lines.append(f"regexp:^[^.]+\\.{escaped_domain}$")

                # example.com → full:example.com (精确匹配)
                # 但如果是 IP 地址，则保持原样
                else:
                    # 检查是否是 IP 地址（支持 IPv4、IPv6 和 CIDR）
                    try:
                        import ipaddress
                        # 尝试解析为 IP 地址或 CIDR 网段
                        ipaddress.ip_network(line, strict=False)
                        # 如果是 IP 地址
                        converted_lines.append(line)
                    except ValueError:
                        # 不是有效的 IP 地址，当作域名处理
                        # 验证是否是有效域名（简单检查）
                        if '.' in line and not line.startswith('.') and not line.endswith('.'):
                            converted_lines.append(f"full:{line}")

        # 返回转换后的内容
        converted_content = '\n'.join(converted_lines)
        return converted_content, 200, {'Content-Type': 'text/plain; charset=utf-8'}

    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500
