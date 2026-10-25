"""Inspect editable providers and normalize safe Classical delivery syntax."""
from bisect import bisect_left
from collections import OrderedDict
from copy import deepcopy
import hashlib
import ipaddress
import json
import re
import struct
import threading

import yaml

from backend.common.config_repository import ProfileValidationError
from backend.utils.rule_syntax import RULE_TYPES, cidr_error, looks_like_ip, rule_value_error, split_rule_payload


# libyaml parses several times faster, but on unusual YAML its marks and even
# what it accepts differ from PyYAML (BOM offsets, tabs, tag syntax, a missing
# final newline, indentless lists). Use it only for an indented block payload
# list of plain or simply quoted entries, the usual shape of large rule files,
# where differential tests show the same nodes, tokens and values. Anything
# else, and any libyaml failure, goes through PyYAML for identical diagnostics.
_LIBYAML = getattr(yaml, 'CSafeLoader', None)
_YAML_COMMENT = '#[\x20-\x7e\xa0-\u2027\u202a-\ud7ff\ue000-\ufefe\uff00-\ufffd\U00010000-\U0010ffff]*'
_YAML_SCALAR = ("(?:[A-Za-z0-9._+/(][A-Za-z0-9._+/,=:@~^$()|?*\\\\-]*(?<!:)"
                '|"[ !#-\\[\\]-~]*"'
                "|'[ -&(-~]*')")
_YAML_SPACE = f'[ ]*(?:{_YAML_COMMENT})?\\r?\\n'
_YAML_ITEM = f'-[ ]+{_YAML_SCALAR}(?:[ ]+{_YAML_COMMENT})?[ ]*\\r?\\n'
_SIMPLE_PAYLOAD = re.compile(
    f'(?:{_YAML_SPACE})*payload:(?:[ ]+{_YAML_COMMENT})?[ ]*\\r?\\n(?:{_YAML_SPACE})*'
    f'(?:(?P<indent>[ ]+){_YAML_ITEM}(?:{_YAML_SPACE}|(?P=indent){_YAML_ITEM})*)?')


def _libyaml(content):
    return _LIBYAML is not None and isinstance(content, str) and _SIMPLE_PAYLOAD.fullmatch(content) is not None


def yaml_compose(content, fast=None):
    if _libyaml(content) if fast is None else fast:
        try:
            return yaml.compose(content, Loader=_LIBYAML)
        except Exception:
            pass
    return yaml.compose(content)


def yaml_scan(content, fast=None):
    if _libyaml(content) if fast is None else fast:
        try:
            return iter(list(yaml.scan(content, Loader=_LIBYAML)))
        except Exception:
            pass
    return yaml.scan(content)


def yaml_safe_load(content, fast=None):
    if _libyaml(content) if fast is None else fast:
        try:
            return yaml.load(content, Loader=_LIBYAML)
        except Exception:
            pass
    return yaml.safe_load(content)


class ResultCache:
    """Thread-safe LRU for pure results keyed by content digests.

    Every publication, and each Agent sharing a profile, re-reads the same rule
    bytes; parsing them again yields nothing new. Bounded by entries and size.
    """

    instances = []

    def __init__(self, max_entries, max_size=0):
        ResultCache.instances.append(self)
        self._entries = OrderedDict()
        self._lock = threading.Lock()
        self._max_entries = max_entries
        self._max_size = max_size
        self._size = 0

    def get(self, key, default=None):
        with self._lock:
            if key not in self._entries:
                return default
            self._entries.move_to_end(key)
            return self._entries[key][0]

    def put(self, key, value, size=0):
        if self._max_size and size > self._max_size:
            return
        with self._lock:
            previous = self._entries.pop(key, None)
            self._size -= previous[1] if previous else 0
            self._entries[key] = (value, size)
            self._size += size
            while len(self._entries) > self._max_entries or (self._max_size and self._size > self._max_size):
                self._size -= self._entries.popitem(last=False)[1][1]

    def clear(self):
        with self._lock:
            self._entries.clear()
            self._size = 0


def content_digest(content):
    """Digest identifying exactly these bytes or text, or None for other values."""
    if isinstance(content, bytes):
        return 'b' + hashlib.sha256(content).hexdigest()
    if isinstance(content, str):
        return 's' + hashlib.sha256(content.encode('utf-8', 'surrogatepass')).hexdigest()
    return None


# Process memory only: a code change restarts the process and empties them.
_validated = ResultCache(1024)
_normalized = ResultCache(256, 16 * 1024 * 1024)


def clear_rule_content_caches():
    for cache in ResultCache.instances:
        cache.clear()


def _valid_pattern(value):
    if not value or re.search(r'[^\w.*+\-]', value):
        return False
    hostname = value[2:] if value.startswith(('+.', '*.')) else value.lstrip('.')
    if looks_like_ip(hostname):
        return False
    try:
        ipaddress.ip_address(value)
        return False
    except ValueError:
        pass
    if value == '*':
        return True
    parts = value.split('.')
    if any(not part for part in parts[1:]) or value == '.':
        return False
    return all(('+' not in part or (index == 0 and part == '+' and len(parts) > 1))
               and ('*' not in part or part == '*') for index, part in enumerate(parts))


def _domain_line(value):
    if ',' in value:
        raise ValueError('Domain 不接受 Classical 规则行；精确域名请填写 example.com，域名及其子域名请填写 +.example.com。')
    if not _valid_pattern(value):
        raise ValueError('请填写域名或通配符，例如 example.com、+.example.com。')
    return value


def _valid_classical_domain(value):
    # Domain patterns permit wildcards and leading dots; an exact Classical
    # DOMAIN needs a complete hostname. Keep ambiguous/foreign syntax untouched.
    return (len(value) <= 253 and _valid_pattern(value)
            and not value.rsplit('.', 1)[-1].isdigit()
            and all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', part)
                    for part in value.split('.')))


def _classical_line(value):
    stripped = value.strip()
    if not stripped or stripped.startswith(('#', '//')) or ',' in stripped:
        return value
    if stripped.startswith('+.') and _valid_classical_domain(stripped[2:]):
        return 'DOMAIN-SUFFIX,' + stripped[2:]
    try:
        if '%' in stripped or ('/' in stripped and cidr_error(stripped)):
            raise ValueError()
        network = ipaddress.ip_network(stripped, strict=False)
        prefix = 'IP-CIDR' if network.version == 4 else 'IP-CIDR6'
        address = stripped if '/' in stripped else f'{stripped}/{network.max_prefixlen}'
        return f'{prefix},{address},no-resolve'
    except ValueError:
        pass
    if _valid_classical_domain(stripped):
        return 'DOMAIN,' + stripped
    raise ValueError('无法确定此行的 Classical 语义，已保留原文；请改为明确的规则类型和值。')


def classical_line_error(value):
    """Validate explicit provider rules; bare compatibility lines are separate."""
    kind, payload, params = split_rule_payload(value)
    if kind in ('MATCH', 'RULE-SET', 'SUB-RULE'):
        return f'Classical 规则集不支持 {kind}；请放在策略规则中。'
    error = rule_value_error(kind, payload)
    if error:
        return error
    if any(not param for param in params):
        return '规则包含空参数，请检查多余的逗号。'
    return None


def _metadata_error(behavior, rule_format, source_type='content'):
    if source_type not in ('content', 'url'):
        return '规则来源必须是 content 或 url。'
    if behavior not in ('domain', 'ipcidr', 'classical'):
        return '规则集类型必须是 domain、ipcidr 或 classical。'
    if rule_format not in (None, '', 'text', 'yaml', 'mrs'):
        return '规则集格式必须是 text、yaml 或 mrs。'
    if rule_format == 'mrs' and source_type == 'content':
        return 'MRS 是二进制规则格式，不能作为手写文本保存；请使用 text/yaml 或订阅 MRS 文件。'
    if rule_format == 'mrs' and behavior == 'classical':
        return 'MRS 只支持 domain 或 ipcidr，不支持 classical。'
    return None


def normalize_classical_lines(content):
    """Return delivery-only normalization and warnings, preserving other bytes.

    The proposal is idempotent. Unsupported entries (or an invalid/ambiguous
    YAML document) stay intact, so serving a provider never modifies its source.
    """
    checked = inspect_domain_content(content, 'classical')
    return checked['content'], checked['warnings'] + checked['errors']


def normalize_mihomo_yaml(content):
    """Expand flow payloads for Mihomo's line-oriented YAML provider reader.

    Keep ordinary block documents byte-for-byte. Flow mappings need to be
    serialized as block mappings; preserve their other keys and comments, but
    leave aliases, anchors and multiline entries untouched with a warning.
    """
    try:
        fast = _libyaml(content)
        root = yaml_compose(content, fast)
        if not isinstance(root, yaml.MappingNode):
            raise ValueError('YAML 规则需要 payload 列表。')
        pairs = [(key, value) for key, value in root.value if key.value == 'payload']
        if len(pairs) != 1 or not isinstance(pairs[0][1], yaml.SequenceNode):
            raise ValueError('YAML 规则需要唯一的 payload 列表。')
        key, payload = pairs[0]
        tokens = list(yaml_scan(content, fast))
        if any(isinstance(token, (yaml.tokens.AnchorToken, yaml.tokens.AliasToken)) for token in tokens):
            raise ValueError('请先展开 YAML 锚点或别名；Mihomo 下发保留了原文。')
        if any(not isinstance(node, yaml.ScalarNode) or node.tag != 'tag:yaml.org,2002:str'
               or node.style in ('|', '>') or node.start_mark.line != node.end_mark.line
               or '\n' in node.value or '\r' in node.value for node in payload.value):
            raise ValueError('payload 每项应为一行文本；复杂 YAML 内容已保留原文。')
        if not payload.value or not (root.flow_style or payload.flow_style):
            return content, []

        # Index real comments once. Re-scanning all tokens for each entry would
        # make large flow lists quadratic; scalar spans also protect literal '#'.
        comment_positions, comment_texts, cursor = [], [], 0
        scalar_spans = [(token.start_mark.index, token.end_mark.index) for token in tokens
                        if isinstance(token, yaml.tokens.ScalarToken)]
        for start, end in [*scalar_spans, (len(content), len(content))]:
            for match in re.finditer(r'#[^\r\n]*', content[cursor:start]):
                comment_positions.append(cursor + match.start())
                comment_texts.append(match.group())
            cursor = end

        def comments(start, end):
            return comment_texts[bisect_left(comment_positions, start):bisect_left(comment_positions, end)]

        if root.flow_style:
            document = yaml_safe_load(content, fast)
            # Loading duplicate keys would silently discard data when writing
            # the flow mapping back out, so refuse that ambiguous structure.
            def unique_mappings(node):
                if isinstance(node, yaml.MappingNode):
                    keys = [key.value for key, _ in node.value if isinstance(key, yaml.ScalarNode)]
                    if len(keys) != len(node.value) or len(set(keys)) != len(keys):
                        raise ValueError('YAML 映射键重复或过于复杂，已保留原文。')
                    for _, value in node.value:
                        unique_mappings(value)
                elif isinstance(node, yaml.SequenceNode):
                    for value in node.value:
                        unique_mappings(value)
                elif node.style in ('|', '>') or node.start_mark.line != node.end_mark.line:
                    raise ValueError('多行 YAML 内容已保留原文，请展开为普通 payload 列表。')
            unique_mappings(root)
            rendered = yaml.safe_dump(document, allow_unicode=True, sort_keys=False,
                                      default_flow_style=False, width=2**31 - 1).rstrip('\n')
            notes = comments(root.start_mark.index, root.end_mark.index)
            if notes:
                rendered = '\n'.join(notes) + '\n' + rendered
            start, end = root.start_mark.index, root.end_mark.index
            prefix = content[:start]
            if prefix.strip('\ufeff \t') and not prefix.endswith('\n'):
                prefix = prefix.rstrip(' \t') + '\n'
        else:
            indent = ' ' * (key.start_mark.column + 2)
            lines, cursor = [], payload.start_mark.index
            for node in payload.value:
                lines.extend(indent + note for note in comments(cursor, node.start_mark.index))
                lines.append(indent + '- ' + json.dumps(node.value, ensure_ascii=False))
                cursor = node.end_mark.index
            lines.extend(indent + note for note in comments(cursor, payload.end_mark.index))
            rendered = '\n'.join(lines)
            start, end = payload.start_mark.index, payload.end_mark.index
            prefix = content[:start].rstrip(' \t')
            if not prefix.endswith('\n'):
                prefix += '\n'
        return prefix + rendered + content[end:], []
    except (yaml.YAMLError, ValueError, RecursionError) as error:
        return content, [{'line': 0, 'message': str(error)}]


def normalize_mihomo_ruleset(content, behavior, rule_format):
    """Compose delivery-only syntax and YAML-layout compatibility."""
    if rule_format == 'mrs':
        return content, []
    # A pure function of its arguments; each publication repeats it.
    key = (content_digest(content), behavior, rule_format)
    cached = _normalized.get(key) if key[0] else None
    if cached is not None:
        return cached[0], deepcopy(cached[1])
    normalized, warnings = _normalize_mihomo_ruleset(content, behavior, rule_format)
    if key[0]:
        _normalized.put(key, (normalized, deepcopy(warnings)), len(normalized))
    return normalized, warnings


def _normalize_mihomo_ruleset(content, behavior, rule_format):
    # Mihomo splits on LF and treats a text BOM as part of the first domain.
    # Normalize transport framing only; the stored source remains untouched.
    line_break = (r'\r(?!\n)|[\v\f\x1c-\x1e\x85\u2028\u2029]'
                  if rule_format == 'text' else r'\r(?!\n)')
    content = re.sub(line_break, '\n', content.removeprefix('\ufeff'))
    warnings = []
    if behavior == 'classical':
        content, warnings = normalize_classical_lines(content)
    if rule_format == 'yaml':
        content, yaml_warnings = normalize_mihomo_yaml(content)
        warnings.extend(yaml_warnings)
    return content, warnings


def _has_yaml_payload(content):
    probe = content[1:] if content.startswith('\ufeff') else content
    return re.search(r"^\s*(?:payload|\"payload\"|'payload')\s*:", probe, re.M) or probe.lstrip().startswith('{')


def inspect_domain_content(content, behavior, rule_format=None):
    """Return a lossless, explicit repair proposal; never write or drop entries."""
    if not isinstance(content, str):
        raise ProfileValidationError('规则内容必须是文本。')
    result = {'content': content, 'changes': [], 'errors': []}
    if behavior == 'classical':
        result['warnings'] = []
    metadata_error = _metadata_error(behavior, rule_format)
    if metadata_error:
        result['errors'].append({'line': 0, 'message': metadata_error})
        return result
    yaml_payload = _has_yaml_payload(content)
    if rule_format == 'text' and yaml_payload:
        result['errors'].append({'line': 0, 'message': '内容是 YAML payload，请选择 YAML 格式后保存。'})
        return result
    entries = []
    try:
        if rule_format == 'yaml' or yaml_payload:
            fast = _libyaml(content)
            root = yaml_compose(content, fast)
            if not isinstance(root, yaml.MappingNode):
                raise ValueError('YAML 规则需要 payload 列表。')
            pairs = [(key, value) for key, value in root.value if key.value == 'payload']
            if len(pairs) != 1 or not isinstance(pairs[0][1], yaml.SequenceNode):
                raise ValueError('YAML 规则需要唯一的 payload 列表。')
            key, payload = pairs[0]
            if payload.start_mark.index < key.end_mark.index:
                raise ValueError('请将 payload 的 YAML 别名展开后再编辑。')
            # A tag may precede an anchor (e.g. !!str &host). Replacing that
            # scalar would remove the anchor and break aliases elsewhere.
            anchors = [token.start_mark.index for token in yaml_scan(content, fast)
                       if isinstance(token, yaml.tokens.AnchorToken)] if '&' in content else []
            seen = set()
            for node in payload.value:
                anchor_index = bisect_left(anchors, node.start_mark.index)
                has_anchor = anchor_index < len(anchors) and anchors[anchor_index] < node.end_mark.index
                if (not isinstance(node, yaml.ScalarNode) or node.tag != 'tag:yaml.org,2002:str'
                        or node.start_mark.index < payload.start_mark.index or id(node) in seen
                        or node.style in ('|', '>') or has_anchor):
                    raise ValueError('payload 每项应为一行文本；请先展开 YAML 锚点、别名或多行内容。')
                seen.add(id(node))
                entries.append((node.start_mark.line + 1, node.value, node.start_mark.index, node.end_mark.index, True))
        else:
            offset = 0
            for number, raw in enumerate(content.splitlines(keepends=True), 1):
                line = raw[1:] if offset == 0 and raw.startswith('\ufeff') else raw
                value = line.strip()
                if value and not value.startswith(('#', '//')):
                    start = offset + len(raw) - len(line.lstrip())
                    entries.append((number, value, start, start + len(value), False))
                offset += len(raw)
    except (yaml.YAMLError, ValueError) as error:
        result['errors'].append({'line': 0, 'message': str(error)})
        return result
    replacements = []
    for number, value, start, end, is_yaml in entries:
        if not value.strip() or value.strip().startswith(('#', '//')):
            continue
        if behavior == 'ipcidr':
            error = cidr_error(value)
            if error:
                result['errors'].append({'line': number, 'before': value, 'message': error})
            continue
        if behavior == 'classical' and (',' in value or value.strip().upper() in RULE_TYPES):
            error = classical_line_error(value.strip())
            if error:
                result['errors'].append({'line': number, 'before': value, 'message': error})
            continue
        try:
            updated = _classical_line(value) if behavior == 'classical' else _domain_line(value)
        except ValueError as error:
            if behavior == 'classical':
                stripped = value.strip()
                foreign = re.match(r'^(?:full|domain|keyword|regexp|regex|ip):', stripped, re.I)
                address = stripped[2:] if stripped.startswith('+.') else stripped
                if not foreign and looks_like_ip(address):
                    result['errors'].append({'line': number, 'before': value,
                                             'message': '不是有效 IP 地址或 CIDR；请检查地址与前缀长度。'})
                else:
                    result['warnings'].append({'line': number, 'before': value, 'message': str(error)})
            else:
                result['errors'].append({'line': number, 'message': str(error)})
            continue
        if updated != value:
            result['changes'].append({'line': number, 'before': value, 'after': updated})
            replacements.append((start, end, json.dumps(updated, ensure_ascii=False) if is_yaml else updated))
    if replacements:
        pieces = []
        cursor = 0
        for start, end, value in replacements:
            pieces.extend((content[cursor:start], value))
            cursor = end
        pieces.append(content[cursor:])
        result['content'] = ''.join(pieces)
    return result


def validate_domain_content(rule, previous=None):
    # Renaming, disabling and reordering old sources must remain possible.
    if previous and all(rule.get(key) == previous.get(key) for key in ('source_type', 'behavior', 'content', 'format')):
        return
    behavior = rule.get('behavior', 'domain')
    error = _metadata_error(behavior, rule.get('format'), rule.get('source_type', 'url'))
    if error:
        raise ProfileValidationError(error)
    if rule.get('source_type') != 'content':
        return
    checked = inspect_domain_content(rule.get('content', ''), behavior, rule.get('format'))
    if checked['errors']:
        error = checked['errors'][0]
        raise ProfileValidationError(f"规则内容第 {error['line']} 行：{error['message']}" if error['line'] else error['message'])



class RemoteRuleValidationError(ProfileValidationError):
    """Downloaded rules did not match their declared type or representation."""

    def __init__(self, rule, rule_format=None, errors=None):
        if isinstance(rule, str):
            self.errors = errors or [{'line': 0, 'before': '', 'message': rule}]
            self.behavior = None
            self.rule_format = rule_format
            super().__init__(rule)
            return
        self.errors = errors
        self.behavior = rule.get('behavior', 'domain')
        self.rule_format = rule_format
        name = rule.get('name') or rule.get('id') or '未命名规则集'
        error = errors[0]
        location = f"第 {error['line']} 行" if error.get('line') else '文件'
        original = str(error.get('before', ''))
        # A remote HTML response or oversized line should not overwhelm the UI.
        preview = original if len(original) <= 200 else original[:200] + '…'
        detail = f'，原文 {preview!r}' if original else ''
        remainder = f'（共 {len(errors)} 处错误）' if len(errors) > 1 else ''
        super().__init__(f'远端规则集「{name}」声明为 {self.behavior}/{rule_format}，'
                         f'{location}{detail}：{error["message"]}{remainder}')


def _remote_text_entries(content, rule_format):
    """Return physical source locations without rendering or modifying the source."""
    source_lines = content.splitlines()
    errors = []

    def problem(line, message, before=None):
        if before is None:
            before = source_lines[line - 1] if 0 < line <= len(source_lines) else ''
        errors.append({'line': line, 'before': before, 'message': message})

    if rule_format != 'yaml':
        if _has_yaml_payload(content):
            first = next((i for i, value in enumerate(source_lines, 1)
                          if value.lstrip('\ufeff \t') and not value.lstrip().startswith(('#', '//'))), 1)
            problem(first, '声明为 text，但内容是 YAML payload；请检查订阅链接或选择 YAML 格式。')
            return [], errors
        return [(number, value.removeprefix('\ufeff') if number == 1 else value)
                for number, value in enumerate(source_lines, 1)], errors
    try:
        fast = _libyaml(content)
        root = yaml_compose(content, fast)
        if not isinstance(root, yaml.MappingNode):
            problem(1, '声明为 YAML，但文件需要包含 payload 列表；请检查订阅链接或文件格式。')
            return [], errors
        pairs = [(key, value) for key, value in root.value
                 if isinstance(key, yaml.ScalarNode) and key.value == 'payload']
        if len(pairs) != 1:
            problem(pairs[1][0].start_mark.line + 1 if len(pairs) > 1 else 1,
                    'YAML 规则需要唯一的 payload 列表。')
            return [], errors
        _, payload = pairs[0]
        if not isinstance(payload, yaml.SequenceNode):
            problem(payload.start_mark.line + 1, 'YAML payload 必须是列表。')
            return [], errors
        for token in yaml_scan(content, fast):
            if isinstance(token, (yaml.tokens.AnchorToken, yaml.tokens.AliasToken)):
                problem(token.start_mark.line + 1, '远端 YAML 规则不支持锚点或别名；请使用展开后的 payload 列表。')
        entries = []
        for node in payload.value:
            number = node.start_mark.line + 1
            if (not isinstance(node, yaml.ScalarNode) or node.tag != 'tag:yaml.org,2002:str'
                    or node.style in ('|', '>') or node.start_mark.line != node.end_mark.line
                    or '\n' in node.value or '\r' in node.value):
                problem(number, 'payload 每项必须是单行文本；不接受数字、布尔值、嵌套结构或多行内容。')
                continue
            entries.append((number, node.value))
        return entries, errors
    except (yaml.YAMLError, ValueError, RecursionError) as error:
        mark = getattr(error, 'problem_mark', None)
        message = getattr(error, 'problem', None) or str(error)
        problem(mark.line + 1 if mark else 0, 'YAML 格式无效：' + message)
        return [], errors



def _validate_remote_mrs(content, rule):
    """Check the MRS envelope and declared behavior without decoding its rule tree.

    MRS v1 stores magic, behavior, signed count and reserved-extra length inside
    a zstd stream. The compressed stream must be complete, including its checksum.
    Trie/IP-set internals remain the destination core's responsibility.
    """
    import zstandard

    def fail(message):
        raise RemoteRuleValidationError(rule, 'mrs', [{'line': 0, 'before': '', 'message': message}])

    if not isinstance(content, bytes):
        fail('MRS 必须按原始二进制读取，不能作为文本校验。')
    header = bytearray()
    total = 0
    decoder = zstandard.ZstdDecompressor(max_window_size=128 * 1024).decompressobj()
    try:
        # Small input chunks bound each temporary decoded allocation; only the
        # fixed header is retained. Large legitimate providers can stream through.
        for offset in range(0, len(content), 1024):
            pending = content[offset:offset + 1024]
            while pending:
                if decoder.eof:
                    decoder = zstandard.ZstdDecompressor(max_window_size=128 * 1024).decompressobj()
                decoded = decoder.decompress(pending)
                total += len(decoded)
                if total > 256 * 1024 * 1024:
                    fail('MRS 解压后超过 256 MiB 校验上限。')
                header.extend(decoded[:max(0, 21 - len(header))])
                pending = decoder.unused_data
        if not decoder.eof:
            fail('MRS 的 Zstandard 压缩流不完整或已截断。')
    except zstandard.ZstdError:
        fail('MRS 的 Zstandard 压缩流无效或校验失败；请检查订阅链接和文件格式。')
    if len(header) != 21 or header[:4] != b'MRS\x01':
        fail('不是有效 MRS v1 文件：文件头缺失或版本不受支持。')
    actual = {0: 'domain', 1: 'ipcidr'}.get(header[4])
    if actual is None:
        fail(f'MRS 文件包含未知规则类型标记 {header[4]}。')
    if actual != rule.get('behavior', 'domain'):
        fail(f'MRS 文件实际类型为 {actual}，与声明的 {rule.get("behavior", "domain")} 不匹配。')
    count, extra_length = struct.unpack('>qq', header[5:21])
    if count < 0:
        fail('MRS 规则数量不能为负数。')
    if extra_length < 0 or extra_length > total - 21:
        fail('MRS 扩展字段长度无效或内容已截断。')
    if total - 21 - extra_length == 0:
        fail('MRS 规则正文缺失。')


def validate_remote_rule_content(content, rule, config_data=None, *, rule_format=None):
    """Validate a remote response without conversion; raise with source diagnostics.

    Classical bare domains/IPs remain valid under the existing delivery contract.
    This function never returns a normalized replacement for the original bytes.
    """
    from backend.converters.common import ruleset_format

    behavior = rule.get('behavior', 'domain')
    declared = rule.get('format')
    selected_format = rule_format or ruleset_format(rule, config_data or {})
    metadata_error = _metadata_error(behavior, declared, 'url')
    if not metadata_error:
        metadata_error = _metadata_error(behavior, selected_format, 'url')
    if metadata_error:
        raise RemoteRuleValidationError(rule, selected_format,
                                        [{'line': 0, 'before': '', 'message': metadata_error}])
    # The verdict depends only on these bytes, behavior and format. Cache reads,
    # each publication and every Agent sharing a profile check the same bytes;
    # remember passes only, so failures are rebuilt with their own diagnostics.
    digest = content_digest(content)
    key = (digest, behavior, selected_format)
    if digest and _validated.get(key):
        return
    _validate_remote_content(content, rule, behavior, selected_format)
    if digest:
        _validated.put(key, True)


def _validate_remote_content(content, rule, behavior, selected_format):
    if selected_format == 'mrs':
        _validate_remote_mrs(content, rule)
        return
    if isinstance(content, bytes):
        try:
            content = content.decode('utf-8')
        except UnicodeDecodeError as error:
            prefix = content[:error.start].decode('utf-8', errors='ignore')
            line_breaks = '\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029'
            line = len(prefix.splitlines()) + (0 if prefix and prefix[-1] not in line_breaks else 1)
            raise RemoteRuleValidationError(rule, selected_format, [{
                'line': max(1, line), 'before': repr(content[error.start:error.end]),
                'message': '内容不是有效 UTF-8 文本；请检查订阅链接、编码或是否误选了 MRS 格式。',
            }]) from None
    if not isinstance(content, str):
        raise RemoteRuleValidationError(rule, selected_format,
                                        [{'line': 0, 'before': '', 'message': '规则内容必须是文本或字节。'}])
    entries, errors = _remote_text_entries(content, selected_format)
    for number, original in entries:
        value = original.strip()
        if not value or value.startswith(('#', '//')):
            continue
        error = None
        if behavior == 'ipcidr':
            error = cidr_error(value)
        elif behavior == 'classical' and (',' in value or value.upper() in RULE_TYPES):
            error = classical_line_error(value)
        else:
            try:
                # Inspect the existing accepted range but discard any proposal.
                # No source/cache data is modified by this validation.
                (_classical_line if behavior == 'classical' else _domain_line)(value)
            except ValueError as failure:
                error = str(failure).replace('，已保留原文', '')
        if error:
            errors.append({'line': number, 'before': original, 'message': error})
    if errors:
        errors.sort(key=lambda item: item['line'])
        raise RemoteRuleValidationError(rule, selected_format, errors)


def read_rule_payload(content):
    """Read payload YAML as well as text; retain the document for a lossless append."""
    if _has_yaml_payload(content):
        try:
            document = yaml.safe_load(content)
            node = yaml.compose(content)
            if not isinstance(document, dict) or not isinstance(document.get('payload'), list):
                raise ValueError()
            key, payload = next((key, value) for key, value in node.value if key.value == 'payload')
            if payload.start_mark.index < key.end_mark.index:
                raise ValueError()  # Do not edit an aliased sequence elsewhere in the document.
            if not all(isinstance(line, str) for line in document['payload']):
                raise ValueError()
            return document['payload'], payload
        except (yaml.YAMLError, ValueError, StopIteration, AttributeError):
            raise ProfileValidationError('规则集的 payload 格式无法读取，请先在规则仓库检查内容。') from None
    return content.splitlines(), None


def append_rule_payload(content, line, payload):
    return append_rule_lines(content, [line], payload)


def append_rule_lines(content, lines, payload):
    if not lines:
        return content
    if payload is None:
        return content + ('' if not content or content.endswith('\n') else '\n') + '\n'.join(lines) + '\n'
    # Insert into the existing sequence, preserving comments and surrounding YAML.
    quoted = [json.dumps(line) for line in lines]
    if payload.flow_style:
        index = payload.value[-1].end_mark.index if payload.value else payload.end_mark.index - 1
        return content[:index] + (', ' if payload.value else '') + ', '.join(quoted) + content[index:]
    index = payload.end_mark.index
    prefix, suffix = content[:index], content[index:]
    indent = ' ' * payload.start_mark.column
    return prefix + ('' if prefix.endswith('\n') else '\n') + ''.join(indent + '- ' + line + '\n' for line in quoted) + suffix


def remove_rule_payload(content, matches):
    """Remove matching entries, keeping YAML valid for a later add or reload."""
    lines, payload = read_rule_payload(content)
    removed = [line for line in lines if matches(line)]
    if not removed:
        return content, []
    if payload is None:
        return ''.join(raw for raw in content.splitlines(keepends=True) if not matches(raw.strip())), removed
    selected = [index for index, line in enumerate(lines) if matches(line)]
    if len(selected) == len(lines):
        return content[:payload.start_mark.index] + '[]\n' + content[payload.end_mark.index:], removed
    if payload.flow_style:
        # Work backwards so each comma belongs to a still-present neighbour.
        for index in reversed(selected):
            _, current = read_rule_payload(content)
            node = current.value[index]
            if index + 1 < len(current.value):
                start, end = node.start_mark.index, current.value[index + 1].start_mark.index
            else:
                start, end = current.value[index - 1].end_mark.index, node.end_mark.index
            content = content[:start] + content[end:]
    else:
        for index in reversed(selected):
            node = payload.value[index]
            start = content.rfind('\n', 0, node.start_mark.index) + 1
            end = content.find('\n', node.end_mark.index)
            content = content[:start] + content[end + 1 if end >= 0 else len(content):]
    return content, removed
