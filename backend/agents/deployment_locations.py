"""Source locations for preparation errors; never alter validation decisions."""
import re

import yaml

from backend.agents.diagnostics import _SECRET, _SECRET_KEY, failure_text
from backend.agents.deployment_messages import deployment_message


class DeploymentPreparationError(ValueError):
    """An artifact cannot be published as a complete deployment."""


_SECRET_LINE = re.compile(rf'''(?ix)^(?P<indent>[ \t]*)(?P<item>-\s+)?["']?(?:{_SECRET_KEY}|uuid)["']?\s*:''')
_UUID = re.compile(r'''(?ix)(["']?\buuid["']?\s*[:=][ \t]*)(?:"(?:\\.|[^"\\])*"|'(?:''|\\.|[^'\\])*'|[^\s,;}]+)''')
_QUOTED_SECRET = re.compile(
    rf'''(?isx)["']?\b(?:{_SECRET_KEY}|uuid)["']?\s*[:=][ \t]*
    (?:"(?:\\.|[^"\\])*(?:"|\Z)|'(?:''|[^'])*(?:'|\Z))''')


def source_excerpt(text, line):
    """Keep source indentation, hiding credentials even on continuation lines."""
    lines = text.splitlines()
    if line == len(lines) + 1:
        return '出错内容：[文件末尾]'
    if not 0 < line <= len(lines):
        return ''
    # Match the whole quoted value before slicing a line, including YAML flow
    # mappings whose credential begins on the previous line.
    start = sum(len(value) for value in text.splitlines(keepends=True)[:line - 1])
    for pattern in (_SECRET, _UUID, _QUOTED_SECRET):
        if any(match.start() < start and match.end() > start for match in pattern.finditer(text)):
            return '出错内容：[敏感内容已隐藏]'
    secret_indent = None
    private_key = False
    for index, value in enumerate(lines[:line], 1):
        indent = len(value) - len(value.lstrip(' \t'))
        if secret_indent is not None and value.strip() and indent <= secret_indent:
            secret_indent = None
        private_key = private_key or bool(re.search(r'-----BEGIN .*PRIVATE KEY-----', value))
        match = _SECRET_LINE.match(value)
        if match:
            secret_indent = len(match['indent']) + len(match['item'] or '')
        if index == line:
            if secret_indent is not None or private_key:
                return '出错内容：[敏感内容已隐藏]'
            return failure_text('出错内容：' + _UUID.sub(r'\1[REDACTED]', value), limit=2048)
        if re.search(r'-----END .*PRIVATE KEY-----', value):
            private_key = False
    return ''


def source_error(message, text, *, line=None, column=None, payload_index=None, cause=None, source_label=''):
    """Attach real source coordinates, parsing YAML marks only on failure."""
    if payload_index is not None:
        try:
            document = yaml.compose(text, Loader=yaml.SafeLoader)
            if isinstance(document, yaml.MappingNode):
                # Match SafeLoader's last duplicate-key value, without searching
                # for repeated rule text and accidentally choosing another line.
                payload = next((value for key, value in reversed(document.value)
                                if key.value == 'payload'), None)
                if payload_index == -1 and payload is not None:
                    mark = payload.start_mark
                    line, column = mark.line + 1, mark.column + 1
                elif isinstance(payload, yaml.SequenceNode) and 0 <= payload_index < len(payload.value):
                    mark = payload.value[payload_index].start_mark
                    line, column = mark.line + 1, mark.column + 1
        except yaml.YAMLError:
            pass
    if line is None and getattr(cause, 'problem_mark', None) is not None:
        mark = cause.problem_mark
        line, column = mark.line + 1, mark.column + 1
    parts = [message]
    if line is not None:
        if source_label:
            parts.append('内容来源：' + source_label)
        parts.append(f'位置：第 {line} 行' + (f'，第 {column} 列' if column is not None else ''))
        excerpt = source_excerpt(text, line)
        if excerpt:
            parts.append(excerpt)
    if cause is not None:
        # A parser's full exception also contains neighbouring source lines,
        # potentially a secret without its key. Keep its actual diagnostic.
        diagnostic = ': '.join(str(part) for part in (getattr(cause, 'context', None),
                              getattr(cause, 'problem', None)) if part) if isinstance(cause, yaml.YAMLError) else str(cause)
        if diagnostic:
            parts.append('原始诊断：' + failure_text(diagnostic))
    return DeploymentPreparationError('\n'.join(parts))


def preparation_context(exc, *, name='', path='', kind='规则集'):
    """Add the owning artifact without modifying a shared download exception."""
    original, _, location = str(exc).partition('\n')
    explanation, _, raw = deployment_message(original).partition('\n')
    name = failure_text(str(name), limit=512).replace('\n', '\\n') if name else ''
    path = failure_text(str(path), limit=1024).replace('\n', '\\n') if path else ''
    label = f'{kind}「{name}」' if name else kind
    parts = [f'{label}准备失败：{explanation}']
    if path:
        parts.append('文件：' + path)
    if location:
        parts.append(location)
    if raw:
        parts.append(raw)
    return DeploymentPreparationError(failure_text('\n'.join(parts)))
