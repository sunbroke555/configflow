"""MosDNS 配置生成器"""
import yaml
from typing import Dict, Any, List

# 统一处理规则集 ID，避免重复的前缀
def _normalize_ruleset_id(rule_set_id: str) -> str:
    if not rule_set_id:
        return ''
    prefix = 'ruleset_'
    return rule_set_id[len(prefix):] if rule_set_id.startswith(prefix) else rule_set_id


def _ruleset_execution_order(config_data: Dict[str, Any], rule_sets_list: List[Dict[str, Any]]) -> List[str]:
    """Derive ruleset order based on exec sequence, falling back to declaration order"""
    all_rules = config_data.get('rule_configs', [])
    ordered_ids = [
        item.get('id')
        for item in all_rules
        if item.get('itemType') == 'ruleset' and item.get('id')
    ]

    if not ordered_ids:
        return [rule_set.get('id') for rule_set in rule_sets_list if rule_set.get('id')]

    known = set(ordered_ids)
    for rule_set in rule_sets_list:
        rule_set_id = rule_set.get('id')
        if rule_set_id and rule_set_id not in known:
            ordered_ids.append(rule_set_id)
            known.add(rule_set_id)

    return ordered_ids


def _build_ruleset_tag_map(rule_sets_map: Dict[str, Dict[str, Any]], ruleset_order: List[str]) -> Dict[str, str]:
    """Map each ruleset id to a tag derived from its name, keeping tags unique"""
    tag_map: Dict[str, str] = {}
    name_usage: Dict[str, int] = {}

    for ruleset_id in ruleset_order:
        rule_set = rule_sets_map.get(ruleset_id, {})
        name = (rule_set.get('name') or '').strip()
        if not name:
            name = _normalize_ruleset_id(ruleset_id)

        count = name_usage.get(name, 0)
        tag = f"{name}_{count + 1}" if count else name
        name_usage[name] = count + 1
        tag_map[ruleset_id] = tag

    return tag_map


def _sanitize_rule_value_for_tag(rule_type: str, value: str, rule_id: str) -> str:
    """Generate a meaningful tag for individual rules based on domain/IP value.

    Args:
        rule_type: Type of rule (DOMAIN, DOMAIN-SUFFIX, IP-CIDR, etc.)
        value: The actual value (domain or IP)
        rule_id: Original rule ID as fallback

    Returns:
        A sanitized tag name suitable for MosDNS configuration
    """
    if not value or not value.strip():
        # Fallback to ID-based approach for empty values
        return f"match_rule_{rule_id}"

    # Clean and prepare the value
    tag_base = value.strip().lower()

    # Replace problematic characters with underscores
    # These characters are common in domains/IPs but not ideal for tags
    replacements = {
        '.': '_',    # Common in domains (e.g., has.ninja -> has_ninja)
        '/': '_',    # Common in CIDR notation (e.g., 192.168.1.0/24)
        ':': '_',    # Common in IPv6 addresses
        '-': '_',    # Keep consistent (some domains have hyphens)
        '*': '_',    # Wildcard domains
        '?': '_',    # Special characters
        '[': '_',    # IPv6 brackets
        ']': '_',    # IPv6 brackets
        ' ': '_',    # Spaces
    }

    for old_char, new_char in replacements.items():
        tag_base = tag_base.replace(old_char, new_char)

    # Remove multiple consecutive underscores
    while '__' in tag_base:
        tag_base = tag_base.replace('__', '_')

    # Strip leading/trailing underscores
    tag_base = tag_base.strip('_')

    # Limit length to keep tags manageable
    if len(tag_base) > 40:
        tag_base = tag_base[:40].rstrip('_')

    # Add rule type prefix for clarity and uniqueness
    rule_type_prefix = rule_type.lower().replace('-', '_')
    tag = f"{rule_type_prefix}_{tag_base}" if tag_base else f"match_rule_{rule_id}"

    return tag


def _parse_custom_matches(custom_matches: Any) -> List[Dict[str, Any]]:
    """Normalize custom match definitions into MosDNS sequence entries"""
    parsed: List[Dict[str, Any]] = []

    if not isinstance(custom_matches, list):
        return parsed

    for entry in custom_matches:
        if not isinstance(entry, dict):
            continue

        if not entry.get('enabled', True):
            continue

        exec_action = (entry.get('exec') or '').strip()
        matches_raw = entry.get('matches', [])

        matches: List[str] = []
        if isinstance(matches_raw, str):
            matches = [line.strip() for line in matches_raw.splitlines() if line.strip()]
        elif isinstance(matches_raw, list):
            for match in matches_raw:
                match_str = str(match).strip()
                if match_str:
                    matches.append(match_str)

        if not exec_action or not matches:
            continue

        parsed.append({
            'matches': matches,
            'exec': exec_action
        })

    return parsed


class IndentDumper(yaml.Dumper):
    """自定义 YAML Dumper，增加列表项缩进"""
    def increase_indent(self, flow=False, indentless=False):
        return super(IndentDumper, self).increase_indent(flow, False)


def split_rules_and_rulesets(config_data: Dict[str, Any]) -> tuple:
    """从 rule_configs 中分离规则和规则集"""
    all_rules = config_data.get('rule_configs', [])
    rules = []
    rule_sets = []

    for item in all_rules:
        item_type = item.get('itemType', '')
        if item_type == 'rule':
            rules.append(item)
        elif item_type == 'ruleset':
            rule_sets.append(item)

    # 兼容旧格式
    if not all_rules:
        rules = config_data.get('rules', [])
        rule_sets = config_data.get('rule_sets', [])

    return rules, rule_sets


def parse_dns_upstreams(dns_config: str) -> List[Dict[str, Any]]:
    """
    解析 DNS 服务器配置，支持多种格式

    支持的格式：
    1. YAML 格式（推荐）：
       - addr: 192.168.200.2:1053
         enable_pipeline: false
       - addr: https://1.1.1.1/dns-query
         bootstrap: 223.5.5.5

    2. 简单格式（每行一个）：
       https://1.1.1.1/dns-query bootstrap=223.5.5.5
       tls://8.8.8.8 223.5.5.5
       223.5.5.5

    Args:
        dns_config: DNS 配置字符串

    Returns:
        List[Dict[str, Any]]: 解析后的上游服务器列表
    """
    if not dns_config or not dns_config.strip():
        return []

    upstreams = []

    # 尝试作为 YAML 解析
    try:
        parsed = yaml.safe_load(dns_config.strip())
        if isinstance(parsed, list):
            # 成功解析为 YAML 列表
            for item in parsed:
                if isinstance(item, dict):
                    # 确保至少有 addr 字段
                    if 'addr' in item:
                        upstreams.append(item)
                elif isinstance(item, str):
                    # YAML 列表中的简单字符串
                    upstreams.append({'addr': item})

            if upstreams:
                return upstreams
        elif isinstance(parsed, dict):
            # 单个 YAML 对象（没有 - 列表标记）
            if 'addr' in parsed:
                return [parsed]
    except Exception:
        # 不是有效的 YAML，继续使用简单格式解析
        pass

    # 按行解析简单格式
    for line in dns_config.strip().split('\n'):
        # 跳过看起来像 YAML 格式但解析失败的行
        # 检查原始行（未 strip）是否以空格/制表符开头（YAML 缩进）
        if line and (line[0] in ' \t'):
            continue

        line = line.strip()
        if not line or line.startswith('#'):
            continue

        # 跳过以 - 开头的行（YAML 列表项）
        if line.startswith('-'):
            continue

        upstream = {}

        # 分割参数
        parts = line.split()
        if not parts:
            continue

        # 第一个部分总是地址
        upstream['addr'] = parts[0]

        # 解析其余参数
        for i in range(1, len(parts)):
            part = parts[i]

            if '=' in part:
                # 参数格式: key=value
                key, value = part.split('=', 1)

                if key == 'bootstrap':
                    # bootstrap=223.5.5.5 格式
                    upstream['bootstrap'] = value
                elif key == 'enable_pipeline':
                    # enable_pipeline=true/false 格式
                    value_lower = value.lower()
                    if value_lower in ['true', '1', 'yes']:
                        upstream['enable_pipeline'] = True
                    elif value_lower in ['false', '0', 'no']:
                        upstream['enable_pipeline'] = False
            elif i == 1:
                # 第二个参数如果没有等号，作为 bootstrap（兼容旧格式）
                upstream['bootstrap'] = part

        upstreams.append(upstream)

    return upstreams


def get_mosdns_ruleset_downloads(config_data: Dict[str, Any], base_url: str = '') -> List[Dict[str, str]]:
    """
    获取 MosDNS 需要下载的规则集文件列表

    Args:
        config_data: 包含配置的字典
        base_url: 服务器 base URL

    Returns:
        List[Dict[str, str]]: 规则集下载信息列表
            [
                {
                    'name': '规则集名称',
                    'url': '下载 URL',
                    'local_path': '本地保存路径'
                },
                ...
            ]
    """
    import urllib.parse

    # 从合并数组中分离规则和规则集
    rules_list, rule_sets_list = split_rules_and_rulesets(config_data)

    # 获取 MosDNS 配置
    mosdns_config_data = config_data.get('mosdns', {})
    direct_ruleset_ids = mosdns_config_data.get('direct_rulesets', [])
    proxy_ruleset_ids = mosdns_config_data.get('proxy_rulesets', [])

    # 合并直连和代理规则集 ID
    configured_ruleset_ids = set(direct_ruleset_ids + proxy_ruleset_ids)

    rule_sets_map = {
        rule_set.get('id'): rule_set for rule_set in rule_sets_list if rule_set.get('id')
    }
    ruleset_order = _ruleset_execution_order(config_data, rule_sets_list)

    # 获取 server_domain
    server_domain = config_data.get('system_config', {}).get('server_domain', '').strip()
    effective_base_url = server_domain or base_url

    downloads = []

    for rule_set_id in ruleset_order:
        rule_set = rule_sets_map.get(rule_set_id)
        if not rule_set or not rule_set.get('enabled', True):
            continue

        # 只处理在配置中的规则集
        if rule_set_id not in configured_ruleset_ids:
            continue

        url = rule_set.get('url', '')

        # 如果 URL 是相对路径，动态拼接 server_domain
        if url and url.startswith('/') and effective_base_url:
            url = f"{effective_base_url}{url}"

        # 构建下载 URL（使用代理接口）
        # rule-proxy 会将所有格式转换为 MosDNS 文本格式，所以始终使用 .txt 扩展名
        if url:
            if effective_base_url:
                download_url = f"{effective_base_url}/api/mosdns/rule-proxy?url={urllib.parse.quote(url)}"
            else:
                download_url = f"/api/mosdns/rule-proxy?url={urllib.parse.quote(url)}"

            downloads.append({
                'name': rule_set['name'],
                'url': download_url,
                'local_path': f"./rules/{rule_set['name']}.txt"
            })

    return downloads


def get_mosdns_custom_files(config_data: Dict[str, Any]) -> List[Dict[str, str]]:
    """
    获取 MosDNS 需要写入的自定义文件列表（hosts 和单个规则）

    Returns:
        [
            {
                'path': '文件路径（相对于配置目录）',
                'content': '文件内容'
            },
            ...
        ]
    """
    custom_files = []

    # 从嵌套结构中获取 MosDNS 配置
    mosdns_config_data = config_data.get('mosdns', {})

    # 1. 处理自定义 Hosts
    custom_hosts_config = mosdns_config_data.get('custom_hosts', '')
    if custom_hosts_config and custom_hosts_config.strip():
        hosts_entries = []
        for line in custom_hosts_config.strip().split('\n'):
            line = line.strip()
            if line and not line.startswith('#'):
                parts = line.split()
                if len(parts) >= 2:
                    first = parts[0]
                    second = parts[1]

                    import re
                    if re.match(r'^[\d\.]+$', second):
                        domain = first
                        ip = second
                    else:
                        ip = first
                        domain = second

                    hosts_entries.append(f"{domain} {ip}")

        if hosts_entries:
            custom_files.append({
                'path': './rules/custom_hosts.txt',
                'content': '\n'.join(hosts_entries)
            })

    # 2. 处理单个规则（按照直连/代理分组合并到同一文件）
    # 获取配置的直连和代理规则
    direct_rule_ids = mosdns_config_data.get('direct_rules', [])
    proxy_rule_ids = mosdns_config_data.get('proxy_rules', [])

    # 获取规则列表
    rules_list = config_data.get('rule_configs', [])
    if not rules_list:
        rules_list = config_data.get('rules', [])

    # 规则类型映射
    rule_type_mapping = {
        'DOMAIN': 'full',
        'DOMAIN-SUFFIX': 'domain',
        'DOMAIN-KEYWORD': 'keyword'
    }

    # 按照规则类型（直连/代理）分组收集规则内容
    direct_domain_rules = []  # 直连域名规则
    proxy_domain_rules = []   # 代理域名规则
    direct_ip_rules = []      # 直连IP规则
    proxy_ip_rules = []       # 代理IP规则

    for rule in rules_list:
        if not rule.get('enabled', True):
            continue

        rule_id = rule.get('id')
        rule_type = rule.get('rule_type', '')
        value = rule.get('value', '')

        # 判断规则是直连还是代理
        is_direct = rule_id in direct_rule_ids
        is_proxy = rule_id in proxy_rule_ids

        if not (is_direct or is_proxy):
            continue

        # 根据规则类型处理
        if rule_type in ['DOMAIN', 'DOMAIN-SUFFIX', 'DOMAIN-KEYWORD']:
            prefix = rule_type_mapping.get(rule_type, 'full')
            rule_content = f"{prefix}:{value}"

            if is_direct:
                direct_domain_rules.append(rule_content)
            elif is_proxy:
                proxy_domain_rules.append(rule_content)

        elif rule_type in ['IP-CIDR', 'IP-CIDR6']:
            if is_direct:
                direct_ip_rules.append(value)
            elif is_proxy:
                proxy_ip_rules.append(value)

    # 生成合并后的文件
    if direct_domain_rules:
        custom_files.append({
            'path': './rules/direct_rules.txt',
            'content': '\n'.join(direct_domain_rules)
        })

    if proxy_domain_rules:
        custom_files.append({
            'path': './rules/proxy_rules.txt',
            'content': '\n'.join(proxy_domain_rules)
        })

    if direct_ip_rules:
        custom_files.append({
            'path': './rules/direct_ip_rules.txt',
            'content': '\n'.join(direct_ip_rules)
        })

    if proxy_ip_rules:
        custom_files.append({
            'path': './rules/proxy_ip_rules.txt',
            'content': '\n'.join(proxy_ip_rules)
        })

    return custom_files


def generate_mosdns_config(config_data: Dict[str, Any], base_url: str = '') -> str:
    """
    生成 MosDNS YAML 配置

    MosDNS 是一个插件化的 DNS 转发器，配置文件包含：
    - log: 日志配置
    - api: API 配置（监控和管理）
    - plugins: 插件列表（缓存、转发、匹配、规则集等）

    Args:
        config_data: 包含节点、策略组、规则等的配置字典
            - mosdns: MosDNS 配置对象（嵌套结构）
                - direct_rulesets: 直连规则集 ID 列表（可选）
                - proxy_rulesets: 代理规则集 ID 列表（可选）
                - local_dns: 国内 DNS 服务器配置
                - remote_dns: 国外 DNS 服务器配置
                - fallback_dns: Fallback DNS 服务器配置
                - default_forward: 默认转发配置
                - custom_hosts: 自定义 Hosts 配置
        base_url: 前端页面的 base URL（协议 + 主机 + 端口），用于构建完整的规则代理 URL

    Returns:
        str: YAML 格式的 MosDNS 配置字符串
    """

    # 从合并数组中分离规则和规则集
    rules_list, rule_sets_list = split_rules_and_rulesets(config_data)

    rule_sets_map = {
        rule_set.get('id'): rule_set for rule_set in rule_sets_list if rule_set.get('id')
    }
    ruleset_order = _ruleset_execution_order(config_data, rule_sets_list)
    ruleset_tag_map = _build_ruleset_tag_map(rule_sets_map, ruleset_order)

    # 获取 MosDNS 配置（从嵌套结构中读取）
    mosdns_config_data = config_data.get('mosdns', {})

    custom_matches_config = mosdns_config_data.get('custom_matches', [])
    custom_match_position = mosdns_config_data.get('custom_match_position', 'tail')
    parsed_custom_matches = _parse_custom_matches(custom_matches_config)
    if custom_match_position not in ['head', 'tail']:
        custom_match_position = 'tail'

    # 获取 MosDNS 规则集配置
    direct_ruleset_ids = mosdns_config_data.get('direct_rulesets', [])
    proxy_ruleset_ids = mosdns_config_data.get('proxy_rulesets', [])

    # 获取 MosDNS 单条规则配置
    direct_rule_ids = mosdns_config_data.get('direct_rules', [])
    proxy_rule_ids = mosdns_config_data.get('proxy_rules', [])

    # 检查是否有自定义配置（从嵌套结构中读取）
    custom_mosdns_config = mosdns_config_data.get('custom_config', '')

    if custom_mosdns_config and custom_mosdns_config.strip():
        # 使用自定义配置作为基础
        try:
            mosdns_config = yaml.safe_load(custom_mosdns_config)
            if not isinstance(mosdns_config, dict):
                mosdns_config = {}
        except Exception:
            # 如果解析失败，使用默认配置
            mosdns_config = {}
    else:
        # 使用默认基础配置
        mosdns_config = {}

    # 如果没有基础配置或基础配置为空，使用默认配置
    if not mosdns_config:
        mosdns_config = {
            'api': {
                'http': '0.0.0.0:8338'
            },
            'plugins': []
        }

    # 确保必需的字段存在
    if 'plugins' not in mosdns_config:
        mosdns_config['plugins'] = []

    # 处理日志配置（从嵌套结构中读取）
    log_enabled = mosdns_config_data.get('log_enabled', True)
    log_level = mosdns_config_data.get('log_level', 'info')
    log_file = mosdns_config_data.get('log_file', './mosdns.log')

    # 只有在启用日志时才添加 log 配置
    if log_enabled:
        mosdns_config['log'] = {
            'level': log_level,
            'file': log_file
        }
    else:
        # 如果禁用日志，删除已有的 log 配置
        if 'log' in mosdns_config:
            del mosdns_config['log']

    # 处理 API 配置（从嵌套结构中读取）
    api_enabled = mosdns_config_data.get('api_enabled', True)
    api_address = mosdns_config_data.get('api_address', '0.0.0.0:8338')

    # 只有在启用 API 时才添加 api 配置
    if api_enabled:
        mosdns_config['api'] = {
            'http': api_address
        }
    else:
        # 如果禁用 API，删除已有的 api 配置
        if 'api' in mosdns_config:
            del mosdns_config['api']

    # 添加插件配置
    # MosDNS 使用插件来实现各种功能，插件会在 sequence 中被调用
    plugins = []

    # 合并直连和代理规则集 ID
    configured_ruleset_ids = set(direct_ruleset_ids + proxy_ruleset_ids)

    # 获取 server_domain（优先使用配置的域名）
    server_domain = config_data.get('system_config', {}).get('server_domain', '').strip()
    # 如果没有配置 server_domain，则使用 base_url
    effective_base_url = server_domain or base_url

    # 1. 规则集加载插件 - 为每个规则集创建独立的 tag
    # 新版 MosDNS 的 domain_set/ip_set 插件只支持本地文件路径
    # Agent 需要负责下载规则文件到本地

    # 用于存储规则集下载信息（供后续使用）
    ruleset_downloads = []

    # 为每个规则集创建独立的插件
    for rule_set_id in ruleset_order:
        if rule_set_id not in configured_ruleset_ids:
            continue

        rule_set = rule_sets_map.get(rule_set_id)
        if not rule_set or not rule_set.get('enabled', True):
            continue

        behavior = rule_set.get('behavior', 'domain')
        url = rule_set.get('url', '')

        # 如果 URL 是相对路径，动态拼接 server_domain
        if url and url.startswith('/') and effective_base_url:
            url = f"{effective_base_url}{url}"

        # 使用代理接口转换规则格式
        # rule-proxy 会将所有格式转换为 MosDNS 文本格式，所以始终使用 .txt 扩展名
        import urllib.parse
        if url:
            # 构建代理 URL - 用于下载规则文件
            if effective_base_url:
                download_url = f"{effective_base_url}/api/mosdns/rule-proxy?url={urllib.parse.quote(url)}"
            else:
                download_url = f"/api/mosdns/rule-proxy?url={urllib.parse.quote(url)}"
        else:
            download_url = None

        # 使用本地文件路径
        # Agent 会将规则文件下载到这个路径
        local_path = f"./rules/{rule_set['name']}.txt"

        # 保存下载信息（可以在返回值中包含这些信息，供 agent 使用）
        if download_url:
            ruleset_downloads.append({
                'name': rule_set['name'],
                'url': download_url,
                'local_path': local_path
            })

        ruleset_tag = ruleset_tag_map.get(
            rule_set_id,
            (rule_set.get('name') or _normalize_ruleset_id(rule_set_id))
        )

        # 为每个规则集创建独立的插件
        if behavior == 'ipcidr':
            # IP 类型规则集
            plugins.append({
                'tag': ruleset_tag,
                'type': 'ip_set',
                'args': {
                    'files': [local_path]
                }
            })
        else:
            # 域名类型规则集
            plugins.append({
                'tag': ruleset_tag,
                'type': 'domain_set',
                'args': {
                    'files': [local_path]
                }
            })

    # 2. 缓存插件 - 缓存 DNS 查询结果，加速后续查询
    # size: 缓存条目数量
    # lazy_cache_ttl: 缓存过期后的延迟删除时间（秒）
    # dump_file: 缓存持久化文件
    # dump_interval: 缓存保存间隔（秒）
    cache_enabled = mosdns_config_data.get('cache_enabled', True)
    cache_size = mosdns_config_data.get('cache_size', 10240)
    cache_lazy_ttl = mosdns_config_data.get('cache_lazy_ttl', 21600)
    cache_dump_enabled = mosdns_config_data.get('cache_dump_enabled', True)
    cache_dump_file = mosdns_config_data.get('cache_dump_file', './cache.dump')
    cache_dump_interval = mosdns_config_data.get('cache_dump_interval', 300)

    if cache_enabled:
        cache_args = {
            'size': int(cache_size) if cache_size is not None else 10240,
            'lazy_cache_ttl': int(cache_lazy_ttl) if cache_lazy_ttl is not None else 21600,
        }
        if cache_dump_enabled:
            try:
                normalized_dump_interval = int(cache_dump_interval)
            except (TypeError, ValueError):
                normalized_dump_interval = 300
            if normalized_dump_interval <= 0:
                normalized_dump_interval = 300

            cache_args.update({
                'dump_file': str(cache_dump_file).strip() if cache_dump_file else './cache.dump',
                'dump_interval': normalized_dump_interval
            })

        plugins.append({
            'tag': 'lazy_cache',
            'type': 'cache',
            'args': cache_args
        })

    # 3. 转发插件 - 国内 DNS 服务器
    # 用于转发需要直连的域名查询，使用国内 DNS 可获得更快的解析速度
    # concurrent: 并发查询数量
    # upstreams: 上游 DNS 服务器列表

    # 解析国内 DNS 配置（从嵌套结构中读取）
    local_dns_config = mosdns_config_data.get('local_dns', '')
    local_dns_upstreams = parse_dns_upstreams(local_dns_config)

    # 如果没有配置，使用默认值
    if not local_dns_upstreams:
        local_dns_upstreams = [
            {'addr': '223.5.5.5'},      # 阿里 DNS
            {'addr': '119.29.29.29'}    # 腾讯 DNSPod
        ]

    plugins.append({
        'tag': 'forward_local',
        'type': 'forward',
        'args': {
            'concurrent': 10,
            'upstreams': local_dns_upstreams
        }
    })

    # 4. 转发插件 - 远程 DNS 服务器（通过代理）
    # 用于转发需要代理的域名查询，使用支持 DoH 的国外 DNS
    # DoH (DNS over HTTPS) 可以加密 DNS 查询，防止污染

    # 解析国外 DNS 配置（从嵌套结构中读取）
    remote_dns_config = mosdns_config_data.get('remote_dns', '')
    remote_dns_upstreams = parse_dns_upstreams(remote_dns_config)

    # 如果没有配置，使用默认值
    if not remote_dns_upstreams:
        remote_dns_upstreams = [
            {'addr': 'https://1.1.1.1/dns-query'},      # Cloudflare DoH
            {'addr': 'https://dns.google/dns-query'}    # Google DoH
        ]

    plugins.append({
        'tag': 'forward_remote',
        'type': 'forward',
        'args': {
            'concurrent': 5,
            'upstreams': remote_dns_upstreams
        }
    })

    # 5. 转发插件 - 远程 Fallback DNS 服务器
    # 解析国外 Fallback DNS 配置（从嵌套结构中读取）
    fallback_dns_config = mosdns_config_data.get('fallback_dns', '')
    fallback_dns_upstreams = parse_dns_upstreams(fallback_dns_config)

    # 如果没有配置 fallback DNS，使用国内 DNS 配置的副本
    if not fallback_dns_upstreams:
        # 创建副本而不是引用，避免 YAML 生成锚点引用
        import copy
        fallback_dns_upstreams = copy.deepcopy(local_dns_upstreams)

    plugins.append({
        'tag': 'forward_remote_fallback',
        'type': 'forward',
        'args': {
            'concurrent': 10,
            'upstreams': fallback_dns_upstreams
        }
    })

    # 6. Hosts 插件 - 自定义域名解析
    # 用于配置自定义的域名到 IP 映射，优先级最高（从嵌套结构中读取）
    custom_hosts_config = mosdns_config_data.get('custom_hosts', '')
    custom_hosts_content = None  # 用于收集需要写入文件的内容

    if custom_hosts_config and custom_hosts_config.strip():
        # 解析 hosts 配置
        # 支持两种格式：
        # 1. 域名在前，IP在后：example.com 192.168.1.1（推荐格式）
        # 2. IP在前，域名在后：192.168.1.1 example.com（传统 hosts 格式）
        hosts_entries = []
        for line in custom_hosts_config.strip().split('\n'):
            line = line.strip()
            if line and not line.startswith('#'):  # 忽略空行和注释
                # 分割并获取前两个部分
                parts = line.split()
                if len(parts) >= 2:
                    first = parts[0]
                    second = parts[1]

                    # 简单判断：如果第一个部分包含字母且第二个部分全是数字和点，认为是"域名 IP"格式
                    # 否则认为是"IP 域名"格式
                    import re
                    if re.match(r'^[\d\.]+$', second):
                        # 第二个参数是 IP（只包含数字和点），所以第一个是域名
                        domain = first
                        ip = second
                    else:
                        # 第一个参数是 IP，第二个是域名（标准格式）
                        ip = first
                        domain = second

                    # MosDNS hosts 格式：domain ip
                    hosts_entries.append(f"{domain} {ip}")

        if hosts_entries:
            # 保存 hosts 内容用于写入文件
            custom_hosts_content = '\n'.join(hosts_entries)

            # 使用文件引用方式
            plugins.append({
                'tag': 'custom_hosts',
                'type': 'hosts',
                'args': {
                    'files': ['./rules/custom_hosts.txt']
                }
            })

    # 7. 远程 DNS 序列插件
    # 主远程 DNS 查询序列，带错误处理和 TTL 设置
    plugins.append({
        'tag': 'remote_dns',
        'type': 'sequence',
        'args': [
            {'exec': 'query_summary forward_remote'},
            {'exec': '$forward_remote'},
            {
                'matches': ['!has_resp'],
                'exec': 'reject 3'  # 如果没有响应，返回 REFUSED
            },
            {'exec': 'ttl 1'}
        ]
    })

    # 8. 远程 Fallback DNS 序列插件
    # Fallback 远程 DNS 查询序列
    plugins.append({
        'tag': 'remote_dns_fallback',
        'type': 'sequence',
        'args': [
            {'exec': 'query_summary forward_remote_fallback'},
            {'exec': '$forward_remote_fallback'},
            {
                'matches': ['!has_resp'],
                'exec': 'reject 3'
            }
        ]
    })

    # 9. 代理 DNS Fallback 插件
    # 主备自动切换，超时阈值 500ms
    plugins.append({
        'tag': 'proxy_dns',
        'type': 'fallback',
        'args': {
            'primary': 'remote_dns',
            'secondary': 'remote_dns_fallback',
            'threshold': 500,
            'always_standby': False
        }
    })

    # 9.1 代理 DNS 执行序列
    # fallback 插件通过 exec 执行后不会终止主序列，会导致主序列继续
    # 往下匹配（甚至在尾部默认转发处重复查询一次）
    # 包装一层 sequence：执行 $proxy_dns 后 accept 终止，
    # 代理规则统一通过 goto 跳入本序列
    plugins.append({
        'tag': 'proxy_dns_seq',
        'type': 'sequence',
        'args': [
            {'exec': '$proxy_dns'},
            {'exec': 'accept'}
        ]
    })

    # 10. 国内 DNS 序列插件
    # 先查缓存，未命中再转发到国内 DNS
    china_dns_args: List[Dict[str, Any]] = []
    if cache_enabled:
        china_dns_args.append({'exec': '$lazy_cache'})
        china_dns_args.append({
            'matches': ['has_resp'],
            'exec': 'accept'
        })
    china_dns_args.extend([
        {'exec': 'query_summary forward_local'},
        {'exec': '$forward_local'},
        {
            'matches': ['!has_resp'],
            'exec': 'reject 3'
        }
    ])
    plugins.append({
        'tag': 'china_dns',
        'type': 'sequence',
        'args': china_dns_args
    })

    # 11. 匹配插件 - 为配置的单条规则创建匹配器
    # 收集需要写入文件的规则内容
    rule_files_content = {}  # {file_path: content}

    # 按照规则类型（直连/代理）分组收集规则内容
    direct_domain_rules = []  # 直连域名规则
    proxy_domain_rules = []   # 代理域名规则
    direct_ip_rules = []      # 直连IP规则
    proxy_ip_rules = []       # 代理IP规则

    # 规则ID到tag的映射（用于后续sequence中引用）
    rule_id_to_tag = {}  # rule_id -> tag (direct_rules 或 proxy_rules)

    # 遍历所有规则，按照直连/代理分组
    for rule in rules_list:
        if not rule.get('enabled', True):
            continue

        rule_id = rule.get('id')
        rule_type = rule.get('rule_type', '')
        value = rule.get('value', '')

        # 判断规则是直连还是代理
        is_direct = rule_id in direct_rule_ids
        is_proxy = rule_id in proxy_rule_ids

        if not (is_direct or is_proxy):
            continue

        # 根据规则类型处理
        if rule_type in ['DOMAIN', 'DOMAIN-SUFFIX', 'DOMAIN-KEYWORD']:
            # 域名类规则：根据规则类型确定 MosDNS 格式前缀
            rule_type_mapping = {
                'DOMAIN': 'full',          # 完整域名
                'DOMAIN-SUFFIX': 'domain', # 域名后缀
                'DOMAIN-KEYWORD': 'keyword' # 域名关键字
            }
            prefix = rule_type_mapping.get(rule_type, 'full')
            rule_content = f"{prefix}:{value}"

            if is_direct:
                direct_domain_rules.append(rule_content)
                rule_id_to_tag[rule_id] = 'direct_rules'
            elif is_proxy:
                proxy_domain_rules.append(rule_content)
                rule_id_to_tag[rule_id] = 'proxy_rules'

        elif rule_type in ['IP-CIDR', 'IP-CIDR6']:
            # IP类规则：直接使用CIDR格式
            if is_direct:
                direct_ip_rules.append(value)
                rule_id_to_tag[rule_id] = 'direct_ip_rules'
            elif is_proxy:
                proxy_ip_rules.append(value)
                rule_id_to_tag[rule_id] = 'proxy_ip_rules'

    # 创建直连域名规则插件和文件
    if direct_domain_rules:
        file_path = './rules/direct_rules.txt'
        rule_files_content[file_path] = '\n'.join(direct_domain_rules)
        plugins.append({
            'tag': 'direct_rules',
            'type': 'domain_set',
            'args': {
                'files': [file_path]
            }
        })

    # 创建代理域名规则插件和文件
    if proxy_domain_rules:
        file_path = './rules/proxy_rules.txt'
        rule_files_content[file_path] = '\n'.join(proxy_domain_rules)
        plugins.append({
            'tag': 'proxy_rules',
            'type': 'domain_set',
            'args': {
                'files': [file_path]
            }
        })

    # 创建直连IP规则插件和文件
    if direct_ip_rules:
        file_path = './rules/direct_ip_rules.txt'
        rule_files_content[file_path] = '\n'.join(direct_ip_rules)
        plugins.append({
            'tag': 'direct_ip_rules',
            'type': 'ip_set',
            'args': {
                'files': [file_path]
            }
        })

    # 创建代理IP规则插件和文件
    if proxy_ip_rules:
        file_path = './rules/proxy_ip_rules.txt'
        rule_files_content[file_path] = '\n'.join(proxy_ip_rules)
        plugins.append({
            'tag': 'proxy_ip_rules',
            'type': 'ip_set',
            'args': {
                'files': [file_path]
            }
        })

    # 12. 主要执行序列插件
    # sequence 插件定义了 DNS 查询的处理流程
    # MosDNS 会按顺序执行 sequence 中的每个步骤
    sequence = []

    # 第一步：记录查询指标
    sequence.append({'exec': 'metrics_collector metrics'})

    # 第二步：优先 IPv4
    # 在某些网络环境中，IPv6 连接可能不稳定，优先使用 IPv4 可以提高成功率
    sequence.append({'exec': 'prefer_ipv4'})

    # 第三步：自定义 Hosts（如果有配置）
    # 优先级最高，在所有规则之前匹配
    if custom_hosts_config and custom_hosts_config.strip():
        sequence.append({'exec': '$custom_hosts'})
        sequence.append({
            'matches': ['has_resp'],
            'exec': 'accept'
        })

    # 第四步：按照规则配置的顺序添加规则
    # 从 rule_configs 中按顺序处理规则和规则集
    all_rules = config_data.get('rule_configs', [])
    rule_sets_map = {rs.get('id'): rs for rs in rule_sets_list}

    rule_match_entries: List[Dict[str, Any]] = []

    # 记录头部自定义 match 的插入位置（在 custom_hosts 之后）
    head_match_insert_index = len(sequence)

    # 追踪已添加的合并规则tag，避免重复添加
    added_merged_tags = set()

    for item in all_rules:
        if not item.get('enabled', True):
            continue

        item_type = item.get('itemType', '')
        item_id = item.get('id')
        if not item_id:
            continue

        if item_type == 'ruleset':
            # 规则集
            # 检查是在 direct 还是 proxy 配置中
            rule_set = rule_sets_map.get(item_id, {})
            behavior = rule_set.get('behavior', 'domain')
            tag = ruleset_tag_map.get(
                item_id,
                (rule_set.get('name') or _normalize_ruleset_id(item_id))
            )
            if behavior == 'ipcidr':
                # IP 类规则集在主序列中无效：此处尚未向上游查询，
                # 没有响应可供 resp_ip 匹配，规则永远不会命中。
                # IP 维度的分流由 Mihomo 的 IP 规则负责，此处跳过。
                continue
            match_expr = f"qname ${tag}"

            if item_id in direct_ruleset_ids:
                # 直连规则集，使用国内 DNS
                rule_match_entries.append({
                    'matches': [match_expr],
                    'exec': 'goto china_dns'
                })
            elif item_id in proxy_ruleset_ids:
                # 代理规则集，使用国外 DNS（goto 跳转后终止主序列）
                rule_match_entries.append({
                    'matches': [match_expr],
                    'exec': 'goto proxy_dns_seq'
                })
        elif item_type == 'rule':
            # 单条规则
            # 所有直连域名规则合并到 direct_rules tag
            # 所有代理域名规则合并到 proxy_rules tag
            # IP规则同理
            rule_tag = rule_id_to_tag.get(item_id)

            if not rule_tag:
                continue

            # 检查是否已经添加过这个合并的tag
            if rule_tag in added_merged_tags:
                continue

            # 标记为已添加
            added_merged_tags.add(rule_tag)

            # IP 类规则在主序列中无效：此处尚未向上游查询，
            # 没有响应可供 resp_ip 匹配，规则永远不会命中。
            # IP 维度的分流由 Mihomo 的 IP 规则负责，此处跳过。
            if 'ip_rules' in rule_tag:
                continue

            # 域名规则使用 qname
            match_expr = f"qname ${rule_tag}"

            if item_id in direct_rule_ids:
                # 直连规则，使用国内 DNS
                rule_match_entries.append({
                    'matches': [match_expr],
                    'exec': 'goto china_dns'
                })
            elif item_id in proxy_rule_ids:
                # 代理规则，使用国外 DNS（goto 跳转后终止主序列）
                rule_match_entries.append({
                    'matches': [match_expr],
                    'exec': 'goto proxy_dns_seq'
                })

    # 根据配置决定自定义 match 的插入位置
    if custom_match_position == 'head':
        if parsed_custom_matches:
            sequence[head_match_insert_index:head_match_insert_index] = parsed_custom_matches
        sequence.extend(rule_match_entries)
    else:
        sequence.extend(rule_match_entries)
        sequence.extend(parsed_custom_matches)

    # 第五步：默认转发
    # 如果前面的规则都不匹配，则使用此默认规则
    # 这确保所有查询都能得到响应（从嵌套结构中读取）
    default_forward = mosdns_config_data.get('default_forward', 'forward_remote')

    # 根据配置使用不同的默认转发策略
    if default_forward == 'forward_local':
        # 使用国内 DNS 序列（带缓存）
        sequence.append({'exec': 'goto china_dns'})
    else:
        # 使用国外 DNS（带 fallback）
        sequence.append({'exec': 'goto proxy_dns_seq'})

    plugins.append({
        'tag': 'sequence_main',
        'type': 'sequence',
        'args': sequence
    })

    mosdns_config['plugins'] = plugins

    # 添加服务器配置
    # 使用简化的服务器配置格式
    # udp_server 仅监听 UDP，TCP 需单独配置 tcp_server（截断回退场景需要）
    # args.entry: 入口插件标签
    # args.listen: 监听地址和端口
    plugins.append({
        'type': 'udp_server',
        'args': {
            'entry': 'sequence_main',
            'listen': ':53'
        }
    })
    plugins.append({
        'type': 'tcp_server',
        'args': {
            'entry': 'sequence_main',
            'listen': ':53'
        }
    })

    # 重新组织配置，确保字段顺序正确
    # Python 3.7+ 字典保持插入顺序，所以创建一个新字典按正确顺序插入
    ordered_config = {}

    # 按照标准顺序添加字段（移除 data_providers，新版 MosDNS 不再使用）
    for key in ['log', 'api', 'plugins']:
        if key in mosdns_config:
            ordered_config[key] = mosdns_config[key]

    # 添加任何其他自定义字段（除了 servers 和 data_providers）
    for key, value in mosdns_config.items():
        if key not in ordered_config and key not in ['servers', 'data_providers']:
            ordered_config[key] = value

    # 转换为 YAML
    return yaml.dump(
        ordered_config,
        Dumper=IndentDumper,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        indent=2
    )
