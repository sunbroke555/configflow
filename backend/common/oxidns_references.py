"""Conservative reference checks shared by both OxiDNS form editors.

Native YAML itself is never normalized here. Unknown literal references and
unexpanded reference expressions prevent automatic deletion, rather than
guessing what the core will execute after environment expansion.
"""
import re

from backend.common.oxidns_settings import OxidnsSettingsError

_REFERENCE_FIELDS = {'exec', 'matches', 'entry', 'primary', 'secondary', 'executors', 'provider'}
_ENV = re.compile(r'(?<!\$)\$\{[^}]+\}')


class OxidnsReferenceError(OxidnsSettingsError):
    def __init__(self, message, references):
        self.references = references
        locations = '、'.join(_path(item['path']) for item in references[:4])
        super().__init__(message + ('（' + locations + '）' if locations else ''))


def _path(parts):
    return ''.join(f'[{part}]' if isinstance(part, int) else ('.' if index else '') + part
                   for index, part in enumerate(parts))


def reference_locations(document, tags=(), *, dynamic=True):
    """Find remaining edges after the caller removes its precisely owned edges.

Plugin tag/type declarations are not references. Dynamic plugin tags are still
unsafe: they may expand to one of the names the form intends to remove.
Addresses and credentials containing environment variables are not edges.
"""
    patterns = {tag: re.compile(r'(?<![\w.\-])\$?' + re.escape(tag) + r'(?![\w.\-])') for tag in tags}
    result = []

    def walk(value, path=(), reference=False):
        if isinstance(value, dict):
            for key, item in value.items():
                definition = len(path) == 2 and path[0] == 'plugins' and key in ('tag', 'type')
                if definition:
                    if dynamic and key == 'tag' and isinstance(item, str) and _ENV.search(item):
                        result.append({'path': [*path, key], 'tag': None, 'reason': '未展开的环境变量引用'})
                    continue
                is_reference = key in _REFERENCE_FIELDS or (key == 'args' and isinstance(item, (str, list)))
                walk(item, (*path, key), is_reference)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, (*path, index), reference)
        elif isinstance(value, str):
            if dynamic and reference and _ENV.search(value):
                result.append({'path': list(path), 'tag': None, 'reason': '未展开的环境变量引用'})
            for tag, pattern in patterns.items():
                if pattern.search(value):
                    result.append({'path': list(path), 'tag': tag, 'reason': '仍有插件引用'})

    walk(document)
    return result


def reference_reason(references, prefix='功能仍被自定义执行链引用，请在完整参数中调整'):
    if any(item['reason'] == '未展开的环境变量引用' for item in references):
        prefix = '执行链含未展开的环境变量引用，请在完整参数中确认后调整'
    return prefix + '（' + '、'.join(dict.fromkeys(_path(item['path']) for item in references[:4])) + '）'
