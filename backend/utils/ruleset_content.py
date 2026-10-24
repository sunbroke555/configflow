"""Inspect editable providers and normalize safe Classical delivery syntax."""
from bisect import bisect_left
import ipaddress
import json
import re

import yaml

from backend.common.config_repository import ProfileValidationError
from backend.utils.rule_syntax import RULE_TYPES, cidr_error, looks_like_ip, rule_value_error, split_rule_payload


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
    if ',' not in value:
        if not _valid_pattern(value):
            raise ValueError('请填写域名或通配符，例如 example.com、+.example.com。')
        return value
    fields = [part.strip() for part in value.split(',')]
    kind = fields[0].upper()
    if kind not in ('DOMAIN', 'DOMAIN-SUFFIX'):
        raise ValueError(f'Domain 无法表达 {fields[0]}；需要这类规则时请使用 Classical。')
    if len(fields) != 2 or not _valid_pattern(fields[1]) or any(char in fields[1] for char in '*+') or fields[1].startswith('.'):
        raise ValueError('请只填写规则类型和完整域名，例如 DOMAIN-SUFFIX,example.com。')
    return ('+.' if kind == 'DOMAIN-SUFFIX' else '') + fields[1]


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
        root = yaml.compose(content)
        if not isinstance(root, yaml.MappingNode):
            raise ValueError('YAML 规则需要 payload 列表。')
        pairs = [(key, value) for key, value in root.value if key.value == 'payload']
        if len(pairs) != 1 or not isinstance(pairs[0][1], yaml.SequenceNode):
            raise ValueError('YAML 规则需要唯一的 payload 列表。')
        key, payload = pairs[0]
        tokens = list(yaml.scan(content))
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
            document = yaml.safe_load(content)
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
            root = yaml.compose(content)
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
            anchors = [token.start_mark.index for token in yaml.scan(content)
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
    # Classical compatibility is applied only when delivering to Mihomo.
    # Saving a draft preserves the source text, including safe bare entries.
    if behavior == 'domain' and checked['changes']:
        raise ProfileValidationError('Domain 内容包含 Classical 前缀，请在规则库编辑并确认转换格式后保存。')


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
