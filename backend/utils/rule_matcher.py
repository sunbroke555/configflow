"""规则匹配工具模块

提供域名和IP的规则匹配功能
"""
import re
import ipaddress
from array import array
from struct import calcsize
from sys import getsizeof
from threading import RLock
from typing import Any, Callable, Dict, Hashable, Optional, Tuple

import yaml

from backend.converters.common import ruleset_format
from backend.utils.logger import get_logger
from backend.utils.rule_syntax import RULE_TYPES, cidr_error
from backend.utils.ruleset_content import _classical_line, _valid_pattern, classical_line_error
from backend.utils.url_utils import safe_exception_details

logger = get_logger(__name__)


def is_valid_domain(query: str, *, allow_dns_labels: bool = False) -> bool:
    """验证输入是否为有效域名

    Args:
        query: 待验证的字符串
        allow_dns_labels: 规则测试允许 DNS 标签中的下划线和首尾连字符；域名发现仍使用严格主机名。

    Returns:
        是否为有效域名
    """
    if not query or len(query) > 253:
        return False
    if '.' in query and all(part.isdigit() for part in query.split('.')):
        # Invalid dotted IPs must not re-enter discovery as hostnames after
        # ip_address rejects them; ordinary numeric labels like 3com remain valid.
        return False

    # 域名正则表达式
    domain_pattern = r'^(?:[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?\.)*[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?$'
    if allow_dns_labels:
        domain_pattern = r'^[a-zA-Z0-9_\-]{1,63}(?:\.[a-zA-Z0-9_\-]{1,63})*$'
    return bool(re.fullmatch(domain_pattern, query))


def is_valid_ip(query: str) -> bool:
    """验证输入是否为有效IP地址（IPv4或IPv6）

    Args:
        query: 待验证的字符串

    Returns:
        是否为有效IP地址
    """
    try:
        ipaddress.ip_address(query)
        return True
    except ValueError:
        return False


def normalize_match_query(query: str) -> str:
    """Ignore one DNS root dot without turning a dotted IP input into an address."""
    if query.endswith('.') and not is_valid_ip(query[:-1]):
        return query[:-1]
    return query


def parse_rule_line(line: str) -> Optional[Tuple[str, str]]:
    """解析规则行，提取规则类型和值（支持 Clash、MosDNS 及 List 格式）

    Args:
        line: 规则行，如 "DOMAIN-SUFFIX,google.com"、"domain:google.com" 或 "+.google.com"

    Returns:
        (规则类型, 规则值) 元组，如果解析失败返回 None
    """
    line = line.strip()

    # 跳过注释和空行
    if not line or line.startswith('#') or line.startswith('!') or line.startswith('//'):
        return None

    # 处理 YAML 列表前缀 (- value)
    if line.startswith('-'):
        line = line[1:].strip()
        if not line:
            return None

    # 去掉行内注释（# 之后内容）
    if '#' in line:
        line = line.split('#', 1)[0].strip()
        if not line:
            return None

    # 去掉包裹引号
    line = line.strip('\'"')

    # 1. Clash/YAML 逗号分隔格式
    if ',' in line:
        parts = line.split(',', 1)
        rule_type = parts[0].strip().upper()
        rule_value = parts[1].strip()
        # 剥离尾部的 no-resolve 参数，它不属于规则值（否则 IP 匹配和查重都会失效）
        if rule_value.lower().endswith(',no-resolve'):
            rule_value = rule_value[:-len(',no-resolve')].strip()
        return (rule_type, rule_value)

    # 2. MosDNS 格式（domain: / full: / keyword: / regexp: / ip:）
    if ':' in line:
        prefix, value = line.split(':', 1)
        prefix = prefix.strip().lower()
        value = value.strip()

        # YAML 规则集的 payload: 头行不是规则
        if prefix == 'payload' and not value:
            return None

        if prefix == 'domain':
            return ('DOMAIN-SUFFIX', value)
        if prefix == 'full':
            return ('DOMAIN', value)
        if prefix == 'keyword':
            return ('DOMAIN-KEYWORD', value)
        if prefix in ('regexp', 'regex'):
            return ('REGEX', value)
        if prefix == 'ip':
            return ('IP', value)

    # 3. List 格式（+.example.com / .example.com / *.example.com / example.com / IP）
    if line.startswith('+.'):
        return ('DOMAIN-SUFFIX', line[2:].strip())
    if line.startswith('*.'):
        return ('DIRECT-SUBDOMAIN', line[2:].strip())
    if line.startswith('.') and not line.startswith('..'):
        return ('SUBDOMAIN-ONLY', line[1:].strip())

    # 4. 纯 IP 或 CIDR
    try:
        ipaddress.ip_address(line)
        return ('IP', line)
    except ValueError:
        try:
            ipaddress.ip_network(line, strict=False)
            return ('IP-CIDR', line)
        except ValueError:
            pass

    # 5. 默认当作精确域名
    return ('DOMAIN', line)


def match_domain(domain: str, rule_type: str, rule_value: str) -> bool:
    """匹配域名规则

    Args:
        domain: 待匹配的域名
        rule_type: 规则类型（DOMAIN, DOMAIN-SUFFIX, DOMAIN-KEYWORD等）
        rule_value: 规则值

    Returns:
        是否匹配
    """
    domain = domain.lower()
    if rule_type == 'DOMAIN-REGEX':
        try:
            return bool(re.search(rule_value, domain, re.IGNORECASE))
        except re.error:
            return False
    rule_value = rule_value.lower()

    if rule_type == 'DOMAIN':
        # 完全匹配
        return domain == rule_value

    elif rule_type == 'DOMAIN-SUFFIX':
        # 后缀匹配
        if domain == rule_value:
            return True
        return domain.endswith('.' + rule_value)

    elif rule_type == 'DOMAIN-KEYWORD':
        # 关键字匹配
        return rule_value in domain
    elif rule_type == 'SUBDOMAIN-ONLY':
        suffix = rule_value
        if not suffix or not domain.endswith('.' + suffix):
            return False
        return domain != suffix
    elif rule_type == 'DIRECT-SUBDOMAIN':
        suffix = rule_value
        if not suffix or not domain.endswith('.' + suffix):
            return False
        prefix = domain[:-(len(suffix) + 1)]
        return prefix != '' and '.' not in prefix
    elif rule_type in ('REGEX', 'REGEXP'):
        try:
            return bool(re.search(rule_value, domain))
        except re.error:
            return False

    return False


def match_ip(ip_str: str, rule_type: str, rule_value: str) -> bool:
    """匹配IP规则

    Args:
        ip_str: 待匹配的IP地址字符串
        rule_type: 规则类型（IP-CIDR, IP-CIDR6等）
        rule_value: 规则值（CIDR格式）

    Returns:
        是否匹配
    """
    try:
        ip = ipaddress.ip_address(ip_str)

        if rule_type in ('IP-CIDR', 'IP-CIDR6'):
            # CIDR匹配
            network = ipaddress.ip_network(rule_value, strict=False)
            return ip in network
        if rule_type == 'IP':
            return ip == ipaddress.ip_address(rule_value)

        return False
    except (ValueError, TypeError):
        return False


def match_query(query: str, rule_type: str, rule_value: str) -> bool:
    """根据查询内容自动判断并匹配规则

    Args:
        query: 待匹配的查询（域名或IP）
        rule_type: 规则类型
        rule_value: 规则值

    Returns:
        是否匹配
    """
    query = normalize_match_query(query)
    # 判断查询类型
    if is_valid_ip(query):
        # IP匹配
        return match_ip(query, rule_type, rule_value)
    elif is_valid_domain(query, allow_dns_labels=True):
        # 域名匹配
        return match_domain(query, rule_type, rule_value)

    return False


_LINE_END = re.compile(r'\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]')


def _iter_rule_lines(content):
    """Yield splitlines-compatible text without allocating every line at once."""
    start = 0
    for ending in _LINE_END.finditer(content):
        yield content[start:ending.start()]
        start = ending.end()
    if start < len(content):
        yield content[start:]


def _iter_ruleset_lines(content, rule_format):
    """Yield decoded rules and original display lines without splitting large text."""
    if rule_format == 'text':
        first = True
        for raw in _iter_rule_lines(content):
            value = raw.removeprefix('\ufeff') if first else raw
            first = False
            yield value.strip(), raw.strip()
        return
    if rule_format != 'yaml':
        return
    try:
        root = yaml.compose(content, Loader=getattr(yaml, 'CSafeLoader', yaml.SafeLoader))
        if not isinstance(root, yaml.MappingNode):
            return
        payloads = [value for key, value in root.value if key.value == 'payload']
        if len(payloads) != 1 or not isinstance(payloads[0], yaml.SequenceNode):
            return
        payload = payloads[0]
        if any(not isinstance(node, yaml.ScalarNode) or node.tag != 'tag:yaml.org,2002:str'
               or '\n' in node.value or '\r' in node.value for node in payload.value):
            return
        for node in payload.value:
            if payload.flow_style:
                source = node.value
            else:
                start = content.rfind('\n', 0, node.start_mark.index) + 1
                end = content.find('\n', node.end_mark.index)
                source = content[start:end if end >= 0 else len(content)].strip()
            yield node.value.strip(), source
    except (yaml.YAMLError, RecursionError):
        return


def _parse_ruleset_line(value, behavior):
    """Interpret provider entries according to the selected Mihomo behavior.

    The generic parse_rule_line remains available to DNS import/conversion
    consumers. Its cross-format fallbacks must not create routing test hits.
    """
    if not value or value.startswith(('#', '//', '!')):
        return None
    if behavior == 'ipcidr':
        return None if cidr_error(value) else ('IP-CIDR', value)
    if behavior == 'domain':
        if not _valid_pattern(value):
            return None
        value = value.lower()
        if '*' in value and not (value.startswith('*.') and '*' not in value[2:]):
            prefix = ''
            if value.startswith('+.'):
                prefix, value = r'(?:[^.]+\.)*', value[2:]
            elif value.startswith('.'):
                prefix, value = r'(?:[^.]+\.)+', value[1:]
            pattern = r'\.'.join(r'[^.]+' if part == '*' else re.escape(part) for part in value.split('.'))
            return ('DOMAIN-REGEX', '^' + prefix + pattern + '$')
        if value.startswith('+.'):
            return ('DOMAIN-SUFFIX', value[2:])
        if value.startswith('*.'):
            return ('DIRECT-SUBDOMAIN', value[2:])
        if value.startswith('.'):
            return ('SUBDOMAIN-ONLY', value[1:])
        return ('DOMAIN', value)
    if behavior != 'classical':
        return None
    if ',' not in value and value.upper() in RULE_TYPES:
        return None
    try:
        value = _classical_line(value)
    except ValueError:
        return None
    if classical_line_error(value):
        return None
    kind, separator, payload = value.partition(',')
    kind = kind.strip().upper()
    if not separator:
        return None
    if kind == 'DOMAIN-REGEX':
        return kind, payload.strip()
    if kind in ('DOMAIN', 'DOMAIN-SUFFIX', 'DOMAIN-KEYWORD', 'IP-CIDR', 'IP-CIDR6'):
        return kind, payload.split(',', 1)[0].strip()
    return None


_SOURCE_OFFSET_MAX = (1 << (array('I').itemsize * 8)) - 1
_SOURCE_ORDER_MAX = (1 << (array('i').itemsize * 8 - 1)) - 1
_MAX_PACKED_LINE_BYTES = 512


class _PackedSourceLines:
    """Original rule text, decoded only for a successful match.

    Offsets address packed lines. Mixed sources additionally map source order
    to packed offsets or to strings already shared with a lookup key. Neither
    representation changes rule positions or normalizes the displayed text.
    """

    __slots__ = ('_blob', '_offsets', '_order', '_references')

    def __init__(self, blob, offsets, order, references):
        self._blob = blob
        self._offsets = offsets
        self._order = order
        self._references = references

    def __len__(self):
        return len(self._order) if self._order is not None else len(self._offsets) - 1

    def __getitem__(self, position):
        if position < 0:
            position += len(self)
            if position < 0:
                raise IndexError(position)
        if self._order is not None:
            position = self._order[position]
            if position < 0:
                return self._references[-position - 1]
        start, end = self._offsets[position], self._offsets[position + 1]
        # surrogatepass preserves even lone surrogates in malformed source
        # comments, just as retaining the original Python string did.
        return self._blob[start:end].decode('utf-8', errors='surrogatepass')


class _SourceLinesBuilder:
    """Stream independent source strings into a compact, bounded-width buffer."""

    __slots__ = ('_blob', '_offsets', '_order', '_references',
                 '_owned_bytes', '_owned_reference_bytes')

    def __init__(self):
        self._blob = bytearray()
        self._offsets = array('I', [0])
        self._order = array('i')
        self._references = []
        self._owned_bytes = 0
        self._owned_reference_bytes = 0

    def __len__(self):
        return len(self._order)

    def append(self, line, *, shared=False):
        owned_bytes = 0 if shared else getsizeof(line)
        self._owned_bytes += owned_bytes
        if shared or owned_bytes > _MAX_PACKED_LINE_BYTES:
            # Sharing a plain domain with its lookup key is already cheaper
            # than another byte copy. Retain larger strings too: repeated
            # matching hosts can share them instead of caching decoded copies.
            self._references.append(line)
            self._owned_reference_bytes += owned_bytes
            position = -len(self._references)
        else:
            encoded = line.encode('utf-8', errors='surrogatepass')
            end = len(self._blob) + len(encoded)
            if end > _SOURCE_OFFSET_MAX and self._offsets.typecode == 'I':
                # Large sources must not truncate offsets or stop matching
                # at 4 GiB; pay for wider offsets only when they are needed.
                self._offsets = array('Q', self._offsets)
            position = len(self._offsets) - 1
            self._blob.extend(encoded)
            self._offsets.append(end)
        if not -_SOURCE_ORDER_MAX - 1 <= position <= _SOURCE_ORDER_MAX and self._order.typecode == 'i':
            self._order = array('q', self._order)
        self._order.append(position)

    def finish(self):
        if len(self._offsets) == 1:
            # Includes empty indexes and plain-domain-only sources. Return
            # the original list and string identities, with no extra buffers.
            return self._references
        order = self._order if self._references else None
        references = self._references if self._references else None
        lines = _PackedSourceLines(self._blob, self._offsets, order, references)
        compact_bytes = getsizeof(lines) + getsizeof(self._blob) + getsizeof(self._offsets)
        if references is not None:
            compact_bytes += getsizeof(order) + getsizeof(references) + self._owned_reference_bytes
        # A list may reserve more than this lower bound. Compare conservatively
        # so small, mostly shared, or unusually wide Unicode sources can keep
        # their cheaper list representation. Shared keys are excluded on both
        # sides because the lookup dictionaries already own those strings.
        list_bytes = getsizeof([]) + calcsize('P') * len(self) + self._owned_bytes
        return lines if compact_bytes < list_bytes else list(lines)


class _RuleIndex:
    """Index common rule types while retaining the first matching source line.

    Each dictionary stores an integer into one source-line array rather than a
    separate (position, line) tuple. Retained entries stay in source order, so
    skipping duplicates and unsupported rules cannot change the earliest match.
    """

    def __init__(self, entries):
        self._exact = {}
        self._suffix = {}
        self._subdomain = {}
        self._direct_subdomain = {}
        self._domain_patterns = []
        self._ips = {}
        self._networks = {4: {}, 6: {}}
        self._source_lines = _SourceLinesBuilder()
        domain_indexes = {
            'DOMAIN': self._exact,
            'DOMAIN-SUFFIX': self._suffix,
            'SUBDOMAIN-ONLY': self._subdomain,
            'DIRECT-SUBDOMAIN': self._direct_subdomain,
        }
        for rule_type, value, line in entries:
            target = None
            shared_line = False
            if rule_type in domain_indexes:
                target = domain_indexes[rule_type]
                key = value.lower()
                # Plain lowercase domain lists need only one string for both
                # the lookup key and the original displayed source line.
                if key == line:
                    key = line
                    shared_line = True
            elif rule_type == 'DOMAIN-KEYWORD':
                self._domain_patterns.append((len(self._source_lines), value.lower(), None))
                self._source_lines.append(line)
            elif rule_type in ('REGEX', 'REGEXP', 'DOMAIN-REGEX'):
                try:
                    # match_domain has always lowercased both pattern and query.
                    # Mihomo DOMAIN-REGEX ignores case without rewriting escapes.
                    pattern = (re.compile(value, re.IGNORECASE) if rule_type == 'DOMAIN-REGEX'
                               else re.compile(value.lower()))
                except re.error:
                    continue
                self._domain_patterns.append((len(self._source_lines), None, pattern))
                self._source_lines.append(line)
            elif rule_type == 'IP':
                try:
                    key = ipaddress.ip_address(value)
                except (ValueError, TypeError):
                    continue
                target = self._ips
            elif rule_type in ('IP-CIDR', 'IP-CIDR6'):
                try:
                    network = ipaddress.ip_network(value, strict=False)
                except (ValueError, TypeError):
                    continue
                target = self._networks[network.version].setdefault(network.prefixlen, {})
                key = int(network.network_address)
            if target is not None and key not in target:
                target[key] = len(self._source_lines)
                self._source_lines.append(line, shared=shared_line)
        self._source_lines = self._source_lines.finish()

    @staticmethod
    def _earlier(current, candidate):
        if candidate is not None and (current is None or candidate < current):
            return candidate
        return current

    def match_domain(self, domain: str) -> Optional[str]:
        best = self._exact.get(domain)
        suffix = domain
        is_subdomain = False
        while suffix:
            best = self._earlier(best, self._suffix.get(suffix))
            if is_subdomain:
                best = self._earlier(best, self._subdomain.get(suffix))
            _, separator, suffix = suffix.partition('.')
            if not separator:
                break
            is_subdomain = True
        _, separator, parent = domain.partition('.')
        if separator:
            best = self._earlier(best, self._direct_subdomain.get(parent))
        for position, keyword, pattern in self._domain_patterns:
            if best is not None and position >= best:
                break
            if (keyword is not None and keyword in domain) or (pattern is not None and pattern.search(domain)):
                best = position
                break
        return self._source_lines[best] if best is not None else None

    def match_ip(self, ip) -> Optional[str]:
        best = self._ips.get(ip)
        address = int(ip)
        bits = ip.max_prefixlen
        for prefix, networks in self._networks[ip.version].items():
            # At most 33 IPv4 / 129 IPv6 prefix lengths, regardless of rule count.
            host_bits = bits - prefix
            network = (address >> host_bits) << host_bits
            best = self._earlier(best, networks.get(network))
        return self._source_lines[best] if best is not None else None


class RuleConfigMatcher:
    """按 rule_configs 顺序匹配域名/IP，返回第一条命中的规则。

    每个规则集首次使用时加载并索引；域名按标签查找，IP 按前缀长度查找，
    同一实例后续查询无需逐条扫描常见规则，也不会重复解析查询的类型。
    """

    def __init__(self, rule_configs, rule_library, load_content: Callable[[dict, Optional[dict]], str],
                 *, index_key: Optional[Callable[[dict, Optional[dict]], Optional[Hashable]]] = None):
        # 序号按全部规则计（含禁用的），与规则列表上显示的行号一致
        self._items = [(index, item) for index, item in enumerate(rule_configs, start=1)
                       if item.get('enabled', True) and item.get('library_enabled', True)]
        self._library = {item.get('id'): item for item in rule_library if item.get('id')}
        # Match the format emitted by the converter. A declared text provider
        # must not accidentally gain YAML rules just because its body has payload.
        self._formats = {index: ruleset_format(item, {'rule_library': rule_library})
                         for index, item in self._items if item.get('itemType') == 'ruleset'}
        self._load_content = load_content
        # Reuse is opt-in: a custom loader may depend on the individual rule
        # item even when several items reference the same library entry.
        self._index_key = index_key
        self._indexes = {}
        self._index_lock = RLock()

    def _ruleset_key(self, index, rule_item, library_rule):
        source = self._index_key(rule_item, library_rule) if self._index_key is not None else None
        behavior = self._ruleset_behavior(rule_item, library_rule)
        return (('source', source, behavior, self._formats[index]) if source is not None
                else ('item', index, behavior, self._formats[index]))

    @staticmethod
    def _ruleset_behavior(rule_item, library_rule):
        return rule_item.get('behavior', (library_rule or {}).get('behavior', 'domain'))

    def invalidate_rulesets(self, predicate: Callable[[dict, Optional[dict]], bool]) -> int:
        """Discard selected source indexes, including all references sharing one.

        Callers own retry policy and any cached host results. Unselected indexes
        remain intact; no source is loaded until the next matching request.
        """
        with self._index_lock:
            keys = set()
            for index, item in self._items:
                if item.get('itemType', 'rule') != 'ruleset':
                    continue
                library = self._library.get(item.get('library_rule_id', ''))
                if predicate(item, library):
                    keys.add(self._ruleset_key(index, item, library))
            return sum(self._indexes.pop(key, None) is not None for key in keys)

    def _ruleset_index(self, index: int, rule_item: dict, library_rule: Optional[dict]) -> _RuleIndex:
        with self._index_lock:
            key = self._ruleset_key(index, rule_item, library_rule)
            if key not in self._indexes:
                try:
                    content = self._load_content(rule_item, library_rule) or ''
                except Exception as error:
                    logger.error('Error processing ruleset "%s": %s', rule_item.get('name', '规则集'), safe_exception_details(error))
                    content = ''
                if not content:
                    logger.warning('Skipping ruleset "%s": unable to fetch content', rule_item.get('name', '规则集'))

                def entries():
                    behavior = self._ruleset_behavior(rule_item, library_rule)
                    for value, source in _iter_ruleset_lines(content, self._formats[index]):
                        try:
                            parsed = _parse_ruleset_line(value, behavior)
                        except Exception:
                            continue
                        if parsed:
                            yield parsed[0], parsed[1], source

                # Publish only the complete immutable index to concurrent readers.
                self._indexes[key] = _RuleIndex(entries())
            return self._indexes[key]

    def match(self, query: str) -> Optional[Dict[str, Any]]:
        query = normalize_match_query(query)
        try:
            ip = ipaddress.ip_address(query)
        except ValueError:
            ip = None
            if not is_valid_domain(query, allow_dns_labels=True):
                return None
        domain = query.lower() if ip is None else None
        for index, rule_item in self._items:
            item_type = rule_item.get('itemType', 'rule')
            policy = rule_item.get('policy', 'DIRECT')
            if item_type == 'rule':
                rule_type = rule_item.get('rule_type', '')
                rule_value = rule_item.get('value', '')
                if ip is None:
                    matched = match_domain(domain, rule_type, rule_value)
                else:
                    # Reuse the parsed address for direct rules as well.
                    matched = False
                    try:
                        if rule_type in ('IP-CIDR', 'IP-CIDR6'):
                            matched = ip in ipaddress.ip_network(rule_value, strict=False)
                        elif rule_type == 'IP':
                            matched = ip == ipaddress.ip_address(rule_value)
                    except (ValueError, TypeError):
                        pass
                if matched:
                    return {
                        'rule_name': f'{rule_type} 规则',
                        'rule_type': 'rule',
                        'matched_line': f'{rule_type},{rule_value}',
                        'policy': policy,
                        'source': '直接配置的规则',
                        'priority': index,
                        'rule_id': rule_item.get('id'),
                        'item_type': 'rule',
                        'behavior': 'classical',
                    }
            elif item_type == 'ruleset':
                rule_set_name = rule_item.get('name', '规则集')
                library_rule = self._library.get(rule_item.get('library_rule_id', ''))
                ruleset = self._ruleset_index(index, rule_item, library_rule)
                line = ruleset.match_domain(domain) if ip is None else ruleset.match_ip(ip)
                if line is not None:
                    return {
                        'rule_name': rule_set_name,
                        'rule_type': 'ruleset',
                        'matched_line': line,
                        'policy': policy,
                        'source': f'规则集: {rule_set_name}',
                        'priority': index,
                        'rule_id': rule_item.get('id'),
                        'item_type': 'ruleset',
                        'behavior': self._ruleset_behavior(rule_item, library_rule),
                    }
        return None
