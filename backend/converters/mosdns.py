"""MosDNS 配置生成器"""
import yaml
from typing import Dict, Any, List
from backend.common.profile_context import append_url_query, profile_api_path
from backend.converters.common import parse_hosts_pairs


_BUILTIN_TAGS = {
    'lazy_cache', 'forward_local', 'forward_remote', 'forward_remote_fallback',
    'custom_hosts', 'remote_dns', 'remote_dns_fallback', 'proxy_dns', 'proxy_dns_seq',
    'china_dns', 'sequence_main', 'ip_requery_proxy',
}

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


def _build_ruleset_tag_map(rule_sets_map: Dict[str, Dict[str, Any]], ruleset_order: List[str],
                           reserved=()) -> Dict[str, str]:
    """Keep familiar tags where possible, including space for classical IP tags."""
    tag_map: Dict[str, str] = {}
    used = _BUILTIN_TAGS | set(reserved)

    for ruleset_id in ruleset_order:
        rule_set = rule_sets_map.get(ruleset_id, {})
        name = (rule_set.get('name') or '').strip()
        if not name:
            name = _normalize_ruleset_id(ruleset_id)

        suffixes = ('', '_ip') if rule_set.get('behavior') == 'classical' else ('',)
        tag, index = name, 1
        while any(tag + suffix in used for suffix in suffixes):
            index += 1
            tag = f'{name}_{index}'
        used.update(tag + suffix for suffix in suffixes)
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
        if not item.get('enabled', True) or not item.get('library_enabled', True):
            continue
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


def _build_rule_proxy_url(config_data: Dict[str, Any], effective_base_url: str, url: str,
                          part: str = None) -> str:
    endpoint = profile_api_path(config_data, '/mosdns/rule-proxy')
    if endpoint == '/mosdns/rule-proxy':
        endpoint = f'/api{endpoint}'
    query = {'url': url}
    if part:
        query['part'] = part
    rule_proxy_token = config_data.get('system_config', {}).get('rule_proxy_token', '')
    if not isinstance(rule_proxy_token, str) or not rule_proxy_token:
        raise ValueError('MosDNS rule proxy requires a rule proxy token')
    query['token'] = rule_proxy_token
    return append_url_query(f"{effective_base_url}{endpoint}", query)


RULE_PART_DOMAIN = 'domain'
RULE_PART_IP = 'ip'
RULE_PARTS = (RULE_PART_DOMAIN, RULE_PART_IP)

_MOSDNS_DOMAIN_PREFIXES = ('domain', 'full', 'keyword', 'regexp')
_CLASH_DOMAIN_PREFIXES = {
    'DOMAIN-SUFFIX': 'domain',
    'DOMAIN': 'full',
    'DOMAIN-KEYWORD': 'keyword',
    'DOMAIN-REGEX': 'regexp',
}
_CLASH_IP_TYPES = ('IP-CIDR', 'IP-CIDR6')
# 单条规则中目前能转换为 DNS 规则的类型；GEOIP/GEOSITE 需要下发 dat 文件，端口、逻辑规则等无对应匹配
DNS_RULE_TYPES = (*_CLASH_DOMAIN_PREFIXES, *_CLASH_IP_TYPES)


def parse_clash_rule_value(line: str):
    """Read classical provider payloads (Mihomo needTarget=false), not full rules."""
    kind, _, value = line.partition(',')
    kind, value = kind.strip().upper(), value.strip()
    # DOMAIN-REGEX has neither target nor params in classical providers.
    # Every remaining comma belongs to its payload, regardless of regex syntax.
    if kind != 'DOMAIN-REGEX':
        value = value.split(',', 1)[0].strip()
    return kind, value


def _is_ip_or_cidr(value: str) -> bool:
    import ipaddress
    try:
        ipaddress.ip_network(value, strict=False)
        return True
    except ValueError:
        return False


def _ruleset_parts(behavior: str) -> List[str]:
    """规则集在 MosDNS 中需要拆出的文件部分：classical 混合规则集拆成域名 + IP 两份"""
    if behavior == 'ipcidr':
        return [RULE_PART_IP]
    if behavior == 'classical':
        return [RULE_PART_DOMAIN, RULE_PART_IP]
    return [RULE_PART_DOMAIN]


def _single_rule_groups(config_data):
    """Only adjacent rules with the same route and matcher may share a file."""
    settings = config_data.get('mosdns', {})
    direct = set(settings.get('direct_rules', []))
    proxy = set(settings.get('proxy_rules', []))
    selected = direct | proxy
    groups, counts, previous = [], {}, None
    items = config_data.get('rule_configs') or config_data.get('rules', [])
    for item in items:
        ident, kind = item.get('id'), item.get('rule_type')
        if (item.get('itemType', 'rule') != 'rule' or not item.get('enabled', True)
                or not item.get('library_enabled', True) or ident not in selected):
            previous = None
            continue
        if kind in _CLASH_DOMAIN_PREFIXES:
            part = RULE_PART_DOMAIN
            value = f"{_CLASH_DOMAIN_PREFIXES[kind]}:{item.get('value', '')}"
        elif kind in _CLASH_IP_TYPES:
            part, value = RULE_PART_IP, item.get('value', '')
        else:
            previous = None
            continue
        is_direct = ident in direct
        base = ('direct' if is_direct else 'proxy') + ('_ip_rules' if part == RULE_PART_IP else '_rules')
        if previous != base:
            counts[base] = counts.get(base, 0) + 1
            tag = base if counts[base] == 1 else f'{base}_{counts[base]}'
            groups.append({'tag': tag, 'part': part, 'ids': [],
                           'path': f'./rules/{tag}.txt', 'lines': []})
        groups[-1]['ids'].append(ident)
        groups[-1]['lines'].append(value)
        previous = base
    return groups


def _ruleset_layout(config_data, rule_sets_map, ruleset_order):
    """Use one namespace for generated tags/files in preview, ZIP and Agent paths."""
    groups = _single_rule_groups(config_data)
    tags = _build_ruleset_tag_map(rule_sets_map, ruleset_order,
                                  reserved=[group['tag'] for group in groups])
    stems, sources = {}, {}
    for ident in ruleset_order:
        item = rule_sets_map.get(ident, {})
        # Multiple references to one library still share downloaded content.
        source = (item.get('library_rule_id'), item.get('name'),
                  item.get('behavior', 'domain'), item.get('url'))
        stems[ident] = sources.setdefault(source, tags[ident])
    return tags, stems


def _ruleset_local_path(rule_set: Dict[str, Any], part: str, file_stem=None) -> str:
    # 混合规则集的 IP 部分单独落盘，域名部分沿用原文件名保持兼容
    suffix = '_ip' if part == RULE_PART_IP and rule_set.get('behavior') == 'classical' else ''
    return f"./rules/{file_stem or rule_set['name']}{suffix}.txt"


def _ruleset_ip_tag(rule_set: Dict[str, Any], ruleset_tag: str) -> str:
    return f"{ruleset_tag}_ip" if rule_set.get('behavior') == 'classical' else ruleset_tag


def _ruleset_files(config_data: Dict[str, Any], effective_base_url: str,
                   rule_set: Dict[str, Any], file_stem=None) -> List[Dict[str, Any]]:
    """规则集需要的本地文件及其下载地址（rule-proxy 会转换为 MosDNS 文本格式）"""
    behavior = rule_set.get('behavior', 'domain')
    url = rule_set.get('url', '')
    # 如果 URL 是相对路径，动态拼接 server_domain
    if url and url.startswith('/') and effective_base_url:
        url = f"{effective_base_url}{url}"

    files = []
    for part in _ruleset_parts(behavior):
        download_url = None
        if url:
            download_url = _build_rule_proxy_url(
                config_data, effective_base_url, url,
                # 纯域名规则集保持旧 URL，不带 part 参数
                part=None if behavior not in ('ipcidr', 'classical') else part,
            )
        files.append({
            'part': part,
            'url': download_url,
            'local_path': _ruleset_local_path(rule_set, part, file_stem),
        })
    return files


def empty_rule_part(part: str) -> str:
    """rule-proxy 对空拆分部分返回的内容；发布包须与设备刷新得到的字节一致"""
    return f"# no {part} rules"


def _join_rule_lines(lines: List[str], part: str = None) -> str:
    # 拆分后某部分为空时（如混合规则集没有 IP 行）返回注释行：
    # Agent 预获取把空内容视为失败并回退到 URL 下载，非空可直接落盘；MosDNS 会忽略注释
    if not lines and part:
        return empty_rule_part(part)
    return '\n'.join(lines)


def convert_rule_content_for_mosdns(content: str, part: str = None) -> str:
    """把 Clash / List / YAML payload 规则转换为 MosDNS 文本格式

    part:
        None     兼容旧行为：域名规则 + 裸 IP 行（Clash 的 IP-CIDR 行丢弃）
        'domain' 只输出域名规则（domain_set 使用）
        'ip'     只输出 IP/CIDR（ip_set 使用），包括 Clash 的 IP-CIDR/IP-CIDR6
    """
    import re

    want_domain = part in (None, RULE_PART_DOMAIN)
    want_ip = part in (None, RULE_PART_IP)
    want_clash_ip = part == RULE_PART_IP

    # 已经是 mosdns 格式（domain:/full:/keyword:/regexp:）时只按 part 过滤
    is_mosdns_format = False
    is_yaml_format = 'payload:' in content
    if not is_yaml_format:
        sample_count = 0
        for line in content.split('\n'):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            sample_count += 1
            if sample_count > 20:
                break
            if ':' in line:
                prefix = line.split(':', 1)[0].strip().lower()
                if prefix in _MOSDNS_DOMAIN_PREFIXES or prefix == 'ip':
                    is_mosdns_format = True
                    break

    if is_mosdns_format:
        if part is None:
            return content
        kept = []
        for line in content.split('\n'):
            stripped = line.strip()
            if not stripped or stripped.startswith('#'):
                continue
            if _is_ip_or_cidr(stripped):
                if want_ip:
                    kept.append(stripped)
            elif want_domain:
                kept.append(stripped)
        return _join_rule_lines(kept, part)

    rule_lines = []
    if is_yaml_format:
        try:
            data = yaml.safe_load(content)
            if isinstance(data, dict) and 'payload' in data:
                rule_lines = data['payload'] or []
            else:
                is_yaml_format = False
        except Exception:
            # YAML 解析失败，按文本方式处理
            is_yaml_format = False
    if not is_yaml_format:
        rule_lines = content.split('\n')

    converted = []
    for line in rule_lines:
        if not isinstance(line, str):
            continue
        line = line.strip()
        if not line or line.startswith('#'):
            continue

        if ',' in line:
            # Clash 字段规则与 Agent bundle 共用，保留正则内部逗号。
            rule_type, value = parse_clash_rule_value(line)
            if not value:
                continue
            if rule_type in _CLASH_DOMAIN_PREFIXES:
                if want_domain:
                    converted.append(f"{_CLASH_DOMAIN_PREFIXES[rule_type]}:{value}")
            elif rule_type in _CLASH_IP_TYPES:
                if want_clash_ip and _is_ip_or_cidr(value):
                    converted.append(value)
            # 其他类型（GEOIP/PROCESS-NAME 等）MosDNS 无法表达，丢弃
            continue

        # List 格式
        if _is_ip_or_cidr(line):
            if want_ip:
                converted.append(line)
            continue
        if not want_domain:
            continue
        if line.startswith('+.'):
            # +.example.com → 匹配域名及所有子域名
            converted.append(f"domain:{line[2:]}")
        elif line.startswith('.') and not line.startswith('..'):
            # .example.com → 仅匹配子域名
            converted.append(f"regexp:.+\\.{re.escape(line[1:])}$")
        elif line.startswith('*.'):
            # *.example.com → 仅匹配直接子域名
            converted.append(f"regexp:^[^.]+\\.{re.escape(line[2:])}$")
        elif '.' in line and not line.endswith('.'):
            # example.com → 精确匹配
            converted.append(f"full:{line}")

    return _join_rule_lines(converted, part)


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
    _, ruleset_file_stems = _ruleset_layout(config_data, rule_sets_map, ruleset_order)

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

        # rule-proxy 会将所有格式转换为 MosDNS 文本格式，所以始终使用 .txt 扩展名
        # 混合规则集会拆成域名、IP 两个文件
        for ruleset_file in _ruleset_files(config_data, effective_base_url, rule_set, ruleset_file_stems[rule_set_id]):
            if ruleset_file['url']:
                downloads.append({
                    'name': rule_set['name'],
                    'url': ruleset_file['url'],
                    'local_path': ruleset_file['local_path'],
                    'behavior': rule_set.get('behavior', 'domain'),
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

    hosts = parse_hosts_pairs(mosdns_config_data.get('custom_hosts', ''))
    if hosts:
        custom_files.append({
            'path': './rules/custom_hosts.txt',
            'content': '\n'.join(f'{domain} {address}' for domain, address in hosts),
        })
    for group in _single_rule_groups(config_data):
        custom_files.append({'path': group['path'], 'content': '\n'.join(group['lines'])})

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
    ruleset_tag_map, ruleset_file_stems = _ruleset_layout(config_data, rule_sets_map, ruleset_order)

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

        ruleset_tag = ruleset_tag_map.get(
            rule_set_id,
            (rule_set.get('name') or _normalize_ruleset_id(rule_set_id))
        )

        # 为每个规则集创建独立的插件（Agent 会将规则文件下载到 local_path）
        # 混合规则集（classical）拆成 domain_set + ip_set，IP 部分 tag 加 _ip 后缀
        for ruleset_file in _ruleset_files(config_data, effective_base_url, rule_set, ruleset_file_stems[rule_set_id]):
            if ruleset_file['url']:
                ruleset_downloads.append({
                    'name': rule_set['name'],
                    'url': ruleset_file['url'],
                    'local_path': ruleset_file['local_path'],
                    'behavior': rule_set.get('behavior', 'domain'),
                })
            is_ip = ruleset_file['part'] == RULE_PART_IP
            plugins.append({
                'tag': _ruleset_ip_tag(rule_set, ruleset_tag) if is_ip else ruleset_tag,
                'type': 'ip_set' if is_ip else 'domain_set',
                'args': {
                    'files': [ruleset_file['local_path']]
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

    # 6. Hosts 与打包文件共用解析，避免 IPv6 被当成域名。
    custom_hosts = parse_hosts_pairs(mosdns_config_data.get('custom_hosts', ''))
    if custom_hosts:
        plugins.append({
            'tag': 'custom_hosts', 'type': 'hosts',
            'args': {'files': ['./rules/custom_hosts.txt']},
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

    # 11. 相邻同策略规则才合并；生成与附属文件使用同一份分组逻辑。
    rule_id_to_tag = {}
    for group in _single_rule_groups(config_data):
        rule_id_to_tag.update({ident: group['tag'] for ident in group['ids']})
        plugins.append({
            'tag': group['tag'],
            'type': 'ip_set' if group['part'] == RULE_PART_IP else 'domain_set',
            'args': {'files': [group['path']]},
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
    if custom_hosts:
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
    # IP 类匹配（规则集 / 混合规则集的 IP 部分 / 单条 IP 规则），按规则顺序收集，
    # 统一放到域名匹配之后、默认转发之前，基于国内 DNS 的应答 IP 判断
    ip_match_entries: List[Dict[str, Any]] = []

    def _ip_match_entry(ip_tag: str, is_direct: bool) -> Dict[str, Any]:
        return {
            'matches': [f"resp_ip ${ip_tag}"],
            # 命中直连 IP 直接采用国内应答；命中代理 IP 丢弃应答改走国外 DNS
            'exec': 'accept' if is_direct else 'goto ip_requery_proxy'
        }

    # 记录头部自定义 match 的插入位置（在 custom_hosts 之后）
    head_match_insert_index = len(sequence)

    # 追踪已添加的合并规则tag，避免重复添加
    added_merged_tags = set()

    for item in all_rules:
        if not item.get('enabled', True) or not item.get('library_enabled', True):
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
            is_direct_ruleset = item_id in direct_ruleset_ids
            if not (is_direct_ruleset or item_id in proxy_ruleset_ids):
                continue
            if RULE_PART_IP in _ruleset_parts(behavior):
                ip_match_entries.append(
                    _ip_match_entry(_ruleset_ip_tag(rule_set, tag), is_direct_ruleset)
                )
            if RULE_PART_DOMAIN not in _ruleset_parts(behavior):
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
            # 每个相邻分组在首次出现处执行，不能跨过其他规则或规则集。
            rule_tag = rule_id_to_tag.get(item_id)

            if not rule_tag:
                continue

            # 检查是否已经添加过这个合并的tag
            if rule_tag in added_merged_tags:
                continue

            # 标记为已添加
            added_merged_tags.add(rule_tag)

            # IP 类规则需要应答 IP，延后到 IP 匹配阶段
            if 'ip_rules' in rule_tag:
                ip_match_entries.append(_ip_match_entry(rule_tag, item_id in direct_rule_ids))
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

    default_forward = mosdns_config_data.get('default_forward', 'forward_remote')

    # 第五步：IP 匹配
    # 域名规则都未命中时，先用国内 DNS 解析，再按应答 IP 依次匹配 IP 规则：
    # 命中直连 IP 采用国内应答，命中代理 IP 改走国外 DNS 重新解析
    if ip_match_entries:
        plugins.append({
            'tag': 'ip_requery_proxy',
            'type': 'sequence',
            'args': [
                {'exec': 'drop_resp'},
                {'exec': 'goto proxy_dns_seq'}
            ]
        })
        sequence.append({'exec': 'query_summary forward_local'})
        sequence.append({'exec': '$forward_local'})
        sequence.extend(ip_match_entries)
        if default_forward == 'forward_local':
            # 已有国内应答，无需再查一次
            sequence.append({
                'matches': ['has_resp'],
                'exec': 'accept'
            })
        else:
            # 未命中任何 IP 规则，丢弃国内应答，交给默认的国外 DNS
            sequence.append({'exec': 'drop_resp'})

    # 第六步：默认转发
    # 如果前面的规则都不匹配，则使用此默认规则
    # 这确保所有查询都能得到响应（从嵌套结构中读取）

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
            'listen': mosdns_config_data.get('listen_address') or ':53'
        }
    })
    plugins.append({
        'type': 'tcp_server',
        'args': {
            'entry': 'sequence_main',
            'listen': mosdns_config_data.get('listen_address') or ':53'
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
