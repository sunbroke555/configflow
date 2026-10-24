"""Shared checks for explicit Mihomo rule types and clearly invalid values.

Keep the type list aligned with Mihomo rules/parser.go. These checks do not
replace the core: geo data/provider references and complex regular-expression
semantics are validated by the destination core.
"""
import ipaddress
import re
from difflib import get_close_matches


RULE_TYPES = frozenset({
    'DOMAIN', 'DOMAIN-SUFFIX', 'DOMAIN-KEYWORD', 'DOMAIN-REGEX', 'DOMAIN-WILDCARD',
    'GEOSITE', 'GEOIP', 'SRC-GEOIP', 'IP-ASN', 'SRC-IP-ASN',
    'IP-CIDR', 'IP-CIDR6', 'SRC-IP-CIDR', 'IP-SUFFIX', 'SRC-IP-SUFFIX',
    'SRC-PORT', 'DST-PORT', 'IN-PORT', 'DSCP',
    'PROCESS-NAME', 'PROCESS-PATH', 'PROCESS-NAME-REGEX', 'PROCESS-PATH-REGEX',
    'PROCESS-NAME-WILDCARD', 'PROCESS-PATH-WILDCARD',
    'NETWORK', 'UID', 'IN-TYPE', 'IN-USER', 'IN-NAME', 'REMATCH-NAME',
    'SUB-RULE', 'AND', 'OR', 'NOT', 'RULE-SET', 'MATCH',
})
COMMA_PAYLOAD_TYPES = frozenset({
    'AND', 'OR', 'NOT', 'SUB-RULE', 'DOMAIN-REGEX',
    'PROCESS-NAME-REGEX', 'PROCESS-PATH-REGEX',
})


def looks_like_ip(value):
    """Do not reinterpret a malformed address/network as a DNS hostname."""
    return ':' in value or '/' in value or bool(re.fullmatch(r'[0-9.]+', value))


def domain_value_error(value):
    if not isinstance(value, str) or not value or looks_like_ip(value):
        return '不是有效域名；IP 地址或网段请使用 IP-CIDR。'
    try:
        ascii_value = value.encode('idna').decode('ascii')
    except UnicodeError:
        return '不是有效域名。'
    if (len(ascii_value) > 253 or ascii_value.rsplit('.', 1)[-1].isdigit()
            or any(not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', part)
                   for part in ascii_value.split('.'))):
        return '不是有效域名。'
    return None


def cidr_error(value):
    """Accept numeric prefix lengths, as netip.ParsePrefix does (either family)."""
    if not isinstance(value, str) or not value:
        return '请填写有效 CIDR，例如 192.0.2.0/24 或 2001:db8::/32。'
    if '/' not in value:
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            return '不是有效 IP 网段；IPCIDR 只接受带前缀长度的 CIDR。'
        return f'单个 IP 也需要前缀长度，请填写 {value}/{address.max_prefixlen}。'
    address, _, prefix = value.rpartition('/')
    if not re.fullmatch(r'(?:0|[1-9][0-9]*)', prefix) or '%' in address:
        return '不是有效 IP 网段；请使用数字前缀长度。'
    try:
        ipaddress.ip_network(value, strict=False)
    except ValueError:
        return '不是有效 IP 网段；请检查地址与前缀长度。'
    return None


def split_rule_payload(line):
    """Match ParseRulePayload(..., false), including regex/logic commas."""
    fields = [field.strip() for field in line.split(',')]
    kind = fields[0].upper()
    if kind in COMMA_PAYLOAD_TYPES:
        return kind, ','.join(fields[1:]), []
    return kind, fields[1] if len(fields) > 1 else '', fields[2:]


def _ranges_error(value, maximum, label, *, wildcard=False):
    if value == '*' and wildcard:
        return None
    segments = re.split(r'[/,]', value)
    if len(segments) > 28:
        return f'{label}最多支持 28 组范围。'
    for segment in segments:
        match = re.fullmatch(r'([0-9]+)(?:-([0-9]+))?', segment)
        if not match:
            return f'{label}请填写数字或范围，以 / 分隔，例如 80/443/8000-9000。'
        first_text, last_text = match[1].lstrip('0') or '0', (match[2] or match[1]).lstrip('0') or '0'
        if max(len(first_text), len(last_text)) > len(str(maximum)):
            return f'{label}范围必须在 0–{maximum} 内。'
        first, last = int(first_text), int(last_text)
        if first > last or last > maximum:
            return f'{label}范围必须在 0–{maximum} 内，且起点不大于终点。'
    return None


def _regex_error(value):
    # Python and destination cores do not share a regex dialect (Mihomo uses
    # regexp2 for domain regexes). Check clear structural errors only; advanced
    # constructs remain core-validated. In verbose mode and inline comments,
    # apparent delimiters can be comment text. Leave these dialect-specific
    # expressions to the core instead of guessing which characters are syntax.
    if '(?#' in value or re.search(r'\(\?[A-Za-z-]*x[A-Za-z-]*(?::|\))', value):
        return None
    # Mask literal quoting before inspecting the simpler expressions.
    probe = re.sub(r'\\Q(.*?)(?:\\E|$)', lambda match: re.escape(match[1]), value, flags=re.S)
    probe = re.sub(r'\\c.', 'a', probe)  # Control-character escapes consume the following character.
    probe = re.sub(r'\[:\^?[A-Za-z]+:\]', 'a', probe)  # POSIX classes can contain literal brackets.
    depth, in_class, class_items, escaped = 0, False, 0, False
    for char in probe:
        if escaped:
            escaped = False
            if in_class:
                class_items += 1
            continue
        if char == '\\':
            escaped = True
            continue
        if in_class:
            if char == ']' and class_items:
                in_class = False
            elif char != '^' or class_items:
                class_items += 1
            continue
        if char == '[':
            in_class, class_items = True, 0
        elif char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
            if depth < 0:
                return '正则表达式的括号不匹配。'
    if escaped or in_class or depth:
        return '正则表达式含未闭合括号、字符组或末尾转义。'
    try:
        re.compile(probe)
    except RecursionError:
        return '正则表达式嵌套过深，请简化表达式。'
    except re.error as error:
        if any(message in str(error) for message in (
                'unterminated character set', 'unbalanced parenthesis',
                'missing ),', 'bad character range', 'min repeat greater than max repeat',
                'multiple repeat', 'nothing to repeat', 'bad escape (end of pattern)')):
            return '正则表达式格式无效：' + str(error)
    return None


def _logic_error(kind, value, depth):
    if depth > 32:
        return '逻辑规则嵌套过深，请拆分规则。'
    if not value.startswith('(') or not value.endswith(')'):
        return '逻辑规则应使用括号包裹条件，例如 ((DOMAIN,example.com),(NETWORK,tcp))。'
    # Match the core's literal parenthesis nesting. Do not reinterpret commas
    # inside a regex or a nested logical condition as separate outer rules.
    nesting, start, children = 0, 0, []
    for index, char in enumerate(value):
        if char == '(':
            nesting += 1
            if nesting == 2:
                start = index + 1
        elif char == ')':
            if nesting == 2:
                children.append(value[start:index])
            nesting -= 1
            if nesting < 0:
                return '逻辑规则的括号不匹配。'
    if nesting:
        return '逻辑规则的括号不匹配。'
    if not children or (kind == 'NOT' and len(children) != 1):
        return '逻辑规则需要有效的子条件；NOT 必须恰好包含一个条件。'
    for child in children:
        child_kind, payload, _ = split_rule_payload(child)
        if child_kind in ('MATCH', 'SUB-RULE'):
            return f'逻辑规则不能包含 {child_kind}。'
        error = _rule_value_error(child_kind, payload, depth + 1)
        if error:
            return error
    return None


def _rule_value_error(kind, value, depth):
    if not isinstance(kind, str) or kind not in RULE_TYPES:
        suggestion = get_close_matches(kind, sorted(RULE_TYPES), n=1, cutoff=0.75) if isinstance(kind, str) else []
        return f'未知规则类型 {kind}。' + (f'是否要填写 {suggestion[0]}？' if suggestion else '')
    if not isinstance(value, str) or '\n' in value or '\r' in value:
        return '规则值必须是单行文本。'
    if kind == 'MATCH':
        return None if not value.strip() else 'MATCH 不接受规则值。'
    if not value.strip():
        return '规则值为空。'
    if ',' in value and kind not in COMMA_PAYLOAD_TYPES:
        return '此规则值不能包含逗号；端口列表请使用 / 分隔，策略与参数应单独填写。'
    if kind in ('DOMAIN', 'DOMAIN-SUFFIX'):
        return domain_value_error(value)
    if kind in ('IP-CIDR', 'IP-CIDR6', 'SRC-IP-CIDR', 'IP-SUFFIX', 'SRC-IP-SUFFIX'):
        return cidr_error(value)
    if kind.endswith('-REGEX'):
        return _regex_error(value)
    if kind in ('SRC-PORT', 'DST-PORT', 'IN-PORT'):
        return _ranges_error(value, 65535, '端口')
    if kind == 'DSCP':
        return _ranges_error(value, 63, 'DSCP', wildcard=True)
    if kind == 'UID':
        return _ranges_error(value, 2**32 - 1, 'UID')
    if kind in ('IP-ASN', 'SRC-IP-ASN') and not re.fullmatch(r'[0-9]+', value):
        return 'ASN 应为非负整数。'
    if kind == 'NETWORK' and value.lower() not in ('tcp', 'udp'):
        return 'NETWORK 只接受 tcp 或 udp。'
    if kind in ('AND', 'OR', 'NOT'):
        return _logic_error(kind, value, depth)
    # SUB-RULE and RULE-SET reference other configuration. Validate their names
    # and availability in the owning configuration/core rather than guessing here.
    return None


def rule_value_error(kind, value):
    """Return a human-readable error or None, without rewriting either field."""
    return _rule_value_error(kind, value, 0)
