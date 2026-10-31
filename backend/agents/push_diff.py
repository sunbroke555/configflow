"""What a push would change: the last applied bundle against the one to send now.

The baseline is the private receipt kept for the applied deployment
(``rule_sync_store``); the candidate is the bundle the content comparison
prepares (``content_sync``). Nothing here builds bundles or contacts devices.

Files are compared by the same per-file digests as the "changed" status.
Changed provider, rule and main configuration files are then summarised from
their bytes; the main YAML is compared key by key. Every value leaves this
module redacted: credential fields, URL credentials and long opaque URL parts
never reach a response, also not through the unified diff. Domains, addresses
and ports are kept. All lists and strings are bounded so a large rule set or a
big configuration yields a small response.
"""
from collections import Counter
import difflib
import hashlib
import hmac
import io
import json
import re
import secrets
import tarfile
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit

import yaml

from .deployment_bundle import deployment_content_digests

MAX_CONFIG_ROWS = 200
MAX_LIST_ENTRIES = 60
MAX_ENTRY_FIELDS = 20
MAX_FIELD_ITEMS = 10
MAX_VALUE_CHARS = 240
MAX_RAW_LINES = 1500
MAX_RAW_BYTES = 160 * 1024
MAX_RAW_INPUT_LINES = 60000
MAX_LISTED_FILES = 500
CONFIG_ROLE = 'config'

# A field whose value is a credential, matched on the normalized key name.
_SECRET_KEYS = frozenset({
    'password', 'passwd', 'uuid', 'private-key', 'privatekey', 'pre-shared-key', 'secret', 'token', 'auth',
    'auth-str', 'authentication', 'authorization', 'proxy-authorization', 'psk', 'obfs-password', 'short-id',
    'api-key', 'apikey', 'access-token', 'client-secret', 'cookie', 'private-key-passphrase',
})
_SECRET_SUFFIXES = ('-password', '-passwd', '-passphrase', '-secret', '-token', '-psk', '-private-key')
# URL query parameters carrying a credential (lowercase, '_' read as '-').
_SECRET_PARAMS = frozenset({
    'token', 'access-token', 'auth', 'auth-token', 'key', 'api-key', 'apikey', 'password', 'passwd', 'pass',
    'pwd', 'secret', 'sign', 'signature', 'sig', 'uuid', 'code', 'ticket', 'credential', 'session', 'sid',
})
# Keys of these mappings are data (domains, provider names), never field names.
_DATA_KEYED = frozenset({
    ('hosts',), ('dns', 'hosts'), ('dns', 'nameserver-policy'), ('dns', 'proxy-server-nameserver-policy'),
    ('rule-providers',), ('proxy-providers',), ('sub-rules',),
})
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://[^\s'\"<>]+")
_OPAQUE = re.compile(r'(?=[A-Za-z0-9_\-=~]*[0-9])(?=[A-Za-z0-9_\-=~]*[A-Za-z])[A-Za-z0-9_\-=~]{24,}')
_USERINFO = re.compile(r'^([^\s/:@]+):([^\s/@]+)@(\S+)$')
_YAML_ITEM = re.compile(r'^\s*-\s+(.*?)\s*$')
_MISSING = object()
# Masked values carry a short keyed digest, so the raw diff still shows that a
# credential changed. The key lives only in this process; tags reveal nothing.
_TAG_KEY = secrets.token_bytes(32)


class _Budget:
    """Rows kept in a response; the rest are only counted."""

    def __init__(self, limit):
        self.limit, self.rows, self.omitted = limit, [], 0

    def add(self, row):
        if len(self.rows) < self.limit:
            self.rows.append(row)
        else:
            self.omitted += 1


def _json(value, **options):
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str, **options)
    except TypeError:  # YAML allows mixed key types, which cannot be sorted
        return json.dumps(value, ensure_ascii=False, default=str, **options)


def _tag(value):
    return hmac.new(_TAG_KEY, _json(value).encode(), hashlib.sha256).hexdigest()[:6]


def _placeholder(value):
    return f'<已隐藏 {_tag(value)}>'


def is_secret_key(key):
    name = str(key).strip().lower().replace('_', '-')
    if '.' in name or ' ' in name:
        return False  # a domain or other data key, not a field name
    return name in _SECRET_KEYS or name.endswith(_SECRET_SUFFIXES)


def _secret_param(name):
    name = name.lower().replace('_', '-')
    return name in _SECRET_PARAMS or name.endswith(('-token', '-key', '-secret', '-password'))


def _redact_url(url):
    try:
        parts = urlsplit(url)
    except ValueError:
        return _placeholder(url)
    netloc = parts.netloc
    if '@' in netloc:  # user[:password]@host:port keeps only the host and port
        userinfo, _, hostport = netloc.rpartition('@')
        netloc = _placeholder(userinfo) + '@' + hostport
    path = '/'.join(_placeholder(segment) if _OPAQUE.fullmatch(segment) else segment
                    for segment in parts.path.split('/'))
    query = parts.query
    if query:
        pairs = parse_qsl(query, keep_blank_values=True)
        if pairs:
            query = '&'.join(
                quote(name, safe='') + '=' + (_placeholder(value) if value and (_secret_param(name) or _OPAQUE.fullmatch(value))
                                              else quote(value, safe='/:,+'))
                for name, value in pairs)
        elif _OPAQUE.search(query):
            query = _placeholder(query)
    return urlunsplit((parts.scheme, netloc, path, query, parts.fragment))


def _redact_word(word):
    if '://' in word:
        return _URL.sub(lambda match: _redact_url(match.group(0)), word)
    match = _USERINFO.match(word)
    if match:  # user:password@host:port, e.g. a SOCKS5 upstream
        return _placeholder(match.group(1) + ':' + match.group(2)) + '@' + match.group(3)
    return word


def redact_text(text):
    """Credentials inside one string value, keeping hosts, addresses and ports."""
    if not isinstance(text, str) or not text or ('://' not in text and '@' not in text):
        return text
    return re.sub(r'\S+', lambda match: _redact_word(match.group(0)), text)


def _redacted(value, path=(), depth=0):
    """A deep copy with every credential replaced by a tagged placeholder."""
    if depth > 64:
        return '<已隐藏>'
    if isinstance(value, dict):
        data_keys = tuple(path) in _DATA_KEYED
        return {key: (_placeholder(item) if not data_keys and is_secret_key(key)
                      else _redacted(item, (*path, str(key)), depth + 1))
                for key, item in value.items()}
    if isinstance(value, list):
        return [_redacted(item, path, depth + 1) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def _clip(text):
    if len(text) <= MAX_VALUE_CHARS:
        return text
    return text[:MAX_VALUE_CHARS] + f'…（共 {len(text)} 字符）'


def _scalar(value):
    if value is None:
        return 'null'
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return repr(value)
    return str(value)


def _show(value, path=()):
    """Display text for one (already non-secret) value."""
    if isinstance(value, (dict, list)):
        return _clip(json.dumps(_redacted(value, path), ensure_ascii=False, default=str, separators=(', ', ': ')))
    return _clip(_scalar(redact_text(value) if isinstance(value, str) else value))


def _same(left, right):
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_same(left[key], right[key]) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right))
    return type(left) is type(right) and left == right


def _identity(items):
    """The key naming each entry of a list of mappings (name / tag), if unique."""
    if not items or not all(isinstance(item, dict) for item in items):
        return None
    for key in ('name', 'tag'):
        names = [item.get(key) for item in items]
        if all(isinstance(name, str) and name for name in names) and len(set(names)) == len(names):
            return key
    return None


def _item_text(item, path):
    if isinstance(item, (dict, list)):
        return _json(_redacted(item, path), separators=(', ', ': '))
    return _scalar(redact_text(item) if isinstance(item, str) else item)


def _order_changed(old_keys, new_keys):
    common = set(old_keys) & set(new_keys)
    return [key for key in old_keys if key in common] != [key for key in new_keys if key in common]


def _identity_text(item):
    # Typed, so the string "1" and the number 1 stay different entries.
    if isinstance(item, str):
        return 's' + item
    return 'j' + _json(item)


def _scalar_list_diff(old, new, path):
    # Compare raw values: equal redacted texts can hide a changed credential inside an entry.
    old_raw = [_identity_text(item) for item in old]
    new_raw = [_identity_text(item) for item in new]
    removed = Counter(old_raw) - Counter(new_raw)
    added = Counter(new_raw) - Counter(old_raw)
    entries, omitted = [], 0
    for change, counter, raws, items in (('added', added, new_raw, new), ('removed', removed, old_raw, old)):
        remaining = Counter(counter)
        for raw, item in zip(raws, items):
            if remaining[raw] <= 0:
                continue
            remaining[raw] -= 1
            if len(entries) < MAX_LIST_ENTRIES:  # only listed entries are rendered and redacted
                entries.append({'change': change, 'text': _clip(_item_text(item, path))})
            else:
                omitted += 1
    reordered = not added and not removed and old_raw != new_raw
    if not reordered and (added or removed):
        reordered = _order_changed(list(dict.fromkeys(old_raw)), list(dict.fromkeys(new_raw)))
    return {'before': len(old), 'after': len(new), 'added': sum(added.values()), 'removed': sum(removed.values()),
            'modified': 0, 'reordered': reordered, 'entries': entries, 'omitted': omitted}


def _entry_fields(old, new, path):
    """Field changes inside one named entry (a node, a proxy group, a plugin)."""
    budget = _Budget(MAX_ENTRY_FIELDS)
    _walk(old, new, (), budget, base=path, nested=True)
    return budget.rows, budget.omitted


def _named_list_diff(old, new, key, path):
    old_by = {item[key]: item for item in old}
    new_by = {item[key]: item for item in new}
    added = [name for name in new_by if name not in old_by]
    removed = [name for name in old_by if name not in new_by]
    modified = [name for name in new_by if name in old_by and not _same(old_by[name], new_by[name])]
    entries, omitted = [], 0
    for change, names in (('added', added), ('removed', removed), ('modified', modified)):
        for name in names:
            if len(entries) >= MAX_LIST_ENTRIES:
                omitted += 1
                continue
            entry = {'change': change, 'text': _clip(redact_text(name))}
            if change == 'modified':
                entry['fields'], entry['fields_omitted'] = _entry_fields(old_by[name], new_by[name], path)
            entries.append(entry)
    return {'before': len(old), 'after': len(new), 'added': len(added), 'removed': len(removed),
            'modified': len(modified), 'reordered': _order_changed(list(old_by), list(new_by)),
            'entries': entries, 'omitted': omitted, 'identity': key}


def _list_diff(old, new, path):
    key = _identity(old) if old else _identity(new)
    if key and (not old or _identity(old) == key) and (not new or _identity(new) == key):
        return _named_list_diff(old, new, key, path)
    return _scalar_list_diff(old, new, path)


def _brief_list(diff):
    """A list change inside an entry: a few changed items, no nested detail."""
    items = {'added': [], 'removed': []}
    for entry in diff['entries']:
        if entry['change'] in items and len(items[entry['change']]) < MAX_FIELD_ITEMS:
            items[entry['change']].append(entry['text'])
    return {'before': diff['before'], 'after': diff['after'], 'added': diff['added'], 'removed': diff['removed'],
            'modified': diff['modified'], 'reordered': diff['reordered'],
            'added_items': items['added'], 'removed_items': items['removed']}


def _row(budget, path, full, change, old=_MISSING, new=_MISSING, *, masked=False, listed=None, nested=False):
    row = {'key': '.'.join(path), 'change': change, 'masked': masked}
    if listed is not None:
        row['list'] = _brief_list(listed) if nested else listed
    elif not masked:
        row['old'] = None if old is _MISSING else _show(old, full)
        row['new'] = None if new is _MISSING else _show(new, full)
    budget.add(row)


def _walk(old, new, path, budget, *, base=(), nested=False):
    """Append one row per changed leaf, list or secret below ``path``."""
    full = (*base, *path)
    if old is _MISSING and new is _MISSING:
        return
    if len(full) and is_secret_key(full[-1]) and full[:-1] not in _DATA_KEYED:
        if old is _MISSING or new is _MISSING or not _same(old, new):
            _row(budget, path, full, 'added' if old is _MISSING else 'removed' if new is _MISSING else 'modified',
                 masked=True)
        return
    change = 'added' if old is _MISSING else 'removed' if new is _MISSING else 'modified'
    if change != 'modified' and full[:-1] in _DATA_KEYED:
        # A whole provider or policy entry appears or goes: one row, not one per field.
        _row(budget, path, full, change, old, new)
        return
    if isinstance(old, dict) and isinstance(new, dict) or (
            change != 'modified' and isinstance(old if new is _MISSING else new, dict)
            and (old if new is _MISSING else new)):
        old_map = old if isinstance(old, dict) else {}
        new_map = new if isinstance(new, dict) else {}
        keys = list(new_map) + [key for key in old_map if key not in new_map]
        for key in keys:
            _walk(old_map.get(key, _MISSING), new_map.get(key, _MISSING), (*path, str(key)), budget,
                  base=base, nested=nested)
        return
    if isinstance(old, list) and isinstance(new, list) or (
            change != 'modified' and isinstance(old if new is _MISSING else new, list)):
        old_list = old if isinstance(old, list) else []
        new_list = new if isinstance(new, list) else []
        if _same(old_list, new_list) and change == 'modified':
            return
        _row(budget, path, full, change, listed=_list_diff(old_list, new_list, full), nested=nested)
        return
    if change == 'modified' and _same(old, new):
        return
    _row(budget, path, full, change, old, new)


def _load_yaml(data):
    text = data.decode('utf-8')
    loader = getattr(yaml, 'CSafeLoader', None)
    if loader is not None:
        try:
            return yaml.load(text, Loader=loader)
        except Exception:  # libyaml differs on edge cases; PyYAML decides
            pass
    return yaml.safe_load(text)


def _raw_diff(old_doc, new_doc, old_name, new_name):
    old_text = yaml.safe_dump(_redacted(old_doc), allow_unicode=True, sort_keys=False)
    new_text = yaml.safe_dump(_redacted(new_doc), allow_unicode=True, sort_keys=False)
    old_lines, new_lines = old_text.splitlines(), new_text.splitlines()
    if len(old_lines) + len(new_lines) > MAX_RAW_INPUT_LINES:
        return {'text': None, 'reason': 'too_large', 'lines': len(old_lines) + len(new_lines)}
    lines, size, total = [], 0, 0
    for line in difflib.unified_diff(old_lines, new_lines, 'a/' + old_name, 'b/' + new_name, lineterm=''):
        total += 1
        length = len(line.encode()) + 1
        if len(lines) < MAX_RAW_LINES and size + length <= MAX_RAW_BYTES:
            lines.append(line)
            size += length
    return {'text': '\n'.join(lines), 'truncated': total > len(lines), 'omitted_lines': total - len(lines)}


def diff_config(old_bytes, new_bytes, *, old_name='config.yaml', new_name='config.yaml'):
    """Key-path comparison of two YAML main files, bounded and redacted."""
    try:
        old_doc, new_doc = _load_yaml(old_bytes), _load_yaml(new_bytes)
    except (UnicodeDecodeError, yaml.YAMLError, RecursionError):
        return {'format': 'yaml', 'status': 'unparsable', 'items': [], 'omitted': 0}
    budget = _Budget(MAX_CONFIG_ROWS)
    _walk(old_doc, new_doc, (), budget)
    result = {'format': 'yaml', 'status': 'compared' if budget.rows else 'formatting', 'items': budget.rows,
              'omitted': budget.omitted, 'total': len(budget.rows) + budget.omitted}
    result['raw'] = _raw_diff(old_doc, new_doc, old_name, new_name)
    return result


def _nodes(data):
    document = _load_yaml(data)
    proxies = document.get('proxies') if isinstance(document, dict) else None
    if not isinstance(proxies, list):
        raise ValueError('not a node list')
    nodes = {}
    for index, item in enumerate(proxies):
        name = item.get('name') if isinstance(item, dict) else None
        nodes[name if isinstance(name, str) else f'#{index}'] = _json(item)
    return nodes


def _node_summary(old, new):
    try:
        before = _nodes(old) if old is not None else {}
        after = _nodes(new) if new is not None else {}
    except (UnicodeDecodeError, yaml.YAMLError, ValueError, RecursionError, AttributeError):
        return _size_summary(old, new)
    return {'kind': 'nodes', 'before': None if old is None else len(before), 'after': None if new is None else len(after),
            'added': len(after.keys() - before.keys()), 'removed': len(before.keys() - after.keys()),
            'modified': sum(1 for name in after.keys() & before.keys() if after[name] != before[name])}


def rule_entries(data):
    """Rule lines of a text or YAML payload rule file, without comments or blanks."""
    text = data.decode('utf-8')
    lines = text.splitlines()
    head = [line.strip() for line in lines[:200] if line.strip() and not line.lstrip().startswith('#')]
    if head and head[0].startswith('payload:'):
        entries = []
        for line in lines:
            match = _YAML_ITEM.match(line)
            if match:
                value = match.group(1)
                if len(value) >= 2 and value[0] == value[-1] and value[0] in '\'"':
                    value = value[1:-1]
                entries.append(value)
        return entries
    return [stripped for line in lines if (stripped := line.strip()) and not stripped.startswith(('#', '//'))]


def _rule_summary(old, new):
    try:
        before = rule_entries(old) if old is not None else []
        after = rule_entries(new) if new is not None else []
    except UnicodeDecodeError:  # binary rule sets (mrs)
        return _size_summary(old, new)
    old_count, new_count = Counter(before), Counter(after)
    return {'kind': 'rules', 'before': None if old is None else len(before), 'after': None if new is None else len(after),
            'added': sum((new_count - old_count).values()), 'removed': sum((old_count - new_count).values())}


def _size_summary(old, new):
    return {'kind': 'size', 'before': None if old is None else len(old), 'after': None if new is None else len(new)}


def _read(archive, paths):
    """Selected members of a bundle, in one pass over the compressed stream."""
    wanted = {'files/' + path: path for path in paths}
    found = {}
    if not wanted:
        return found
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as tar:
        for member in tar:
            path = wanted.get(member.name)
            if path is not None and member.isfile():
                found[path] = tar.extractfile(member).read()
                if len(found) == len(wanted):
                    break
    return found


def _canonical(entry):
    return 'config.yaml' if entry['role'] == CONFIG_ROLE else entry['path']


def _comparison_digests(manifest, comparison_config=None):
    if comparison_config:
        manifest = {**manifest, 'files': [comparison_config if item['role'] == CONFIG_ROLE else item
                                          for item in manifest['files']]}
    return deployment_content_digests(manifest)


def _public_file(entry, status, summary=None):
    item = {'path': entry['path'], 'role': entry.get('role'), 'status': status}
    if entry.get('name'):
        item['name'] = redact_text(entry['name'])
    if summary is not None:
        item['summary'] = summary
    return item


_ROLE_ORDER = {CONFIG_ROLE: 0, 'provider': 1, 'rule': 2, 'custom': 3, None: 4}
_STATUS_ORDER = {'modified': 0, 'added': 1, 'removed': 2, 'unchanged': 3}


def build_push_diff(agent, bundle, repository, *, receipt=_MISSING):
    """File and main-configuration changes from the applied receipt to ``bundle``.

    ``agent`` is the snapshot whose ``applied_content`` passed the content
    comparison's preconditions; ``bundle`` is the candidate it prepared.
    """
    from . import rule_sync_store
    applied = agent.get('applied_content') or {}
    if receipt is _MISSING:
        receipt = rule_sync_store.load(repository, agent, with_bundle=True)
    baseline_digests = applied.get('files') if isinstance(applied.get('files'), dict) else None
    if baseline_digests is None and receipt is not None:
        baseline_digests = _comparison_digests(receipt['manifest'], receipt.get('comparison_config'))
    if baseline_digests is None:
        return {'status': 'changed', 'files': None, 'reason': 'baseline_files_missing'}
    if receipt is None:
        content_reason = 'service_without_bundle' if agent.get('service_type') == 'mosdns' else 'bundle_missing'
    else:
        content_reason = None

    new_entries = {_canonical(item): item for item in bundle.manifest['files']}
    new_digests = _comparison_digests(bundle.manifest, bundle.comparison_config)
    old_entries = {_canonical(item): item for item in receipt['manifest']['files']} if receipt else {}
    statuses = {}
    for path in set(new_digests) | set(baseline_digests):
        if path not in baseline_digests:
            statuses[path] = 'added'
        elif path not in new_digests:
            statuses[path] = 'removed'
        else:
            statuses[path] = 'unchanged' if baseline_digests[path] == new_digests[path] else 'modified'
    def entry_for(path):
        return new_entries.get(path) or old_entries.get(path) or {
            'path': path, 'role': CONFIG_ROLE if path == 'config.yaml' else None}

    def order(path):
        entry = entry_for(path)
        return (_ROLE_ORDER.get(entry.get('role'), 4), _STATUS_ORDER.get(statuses[path], 3),
                entry.get('name') or entry['path'])

    changed = sorted((path for path, status in statuses.items() if status != 'unchanged'), key=order)
    listed = changed[:MAX_LISTED_FILES]  # only listed files are read and summarised
    new_bytes = _read(bundle.archive, [new_entries[path]['path'] for path in listed if path in new_entries])
    old_bytes = (_read(receipt['bundle'].archive, [old_entries[path]['path'] for path in listed if path in old_entries])
                 if receipt else {})

    files, config = [], None
    for path in listed:
        status = statuses[path]
        entry = entry_for(path)
        old = old_bytes.get(old_entries[path]['path']) if path in old_entries else None
        new = new_bytes.get(new_entries[path]['path']) if path in new_entries else None
        known_old = status == 'added' or old is not None
        role = entry.get('role')
        summary = None
        if not known_old and status == 'modified':
            summary = None
        elif role == CONFIG_ROLE:
            if old is not None and new is not None:
                config = diff_config(old, new, old_name=old_entries[path]['path'], new_name=new_entries[path]['path'])
                summary = {'kind': 'config', 'changes': config.get('total', 0),
                           'before': len(old), 'after': len(new)}
            else:
                summary = _size_summary(old, new)
        elif role == 'provider':
            summary = _node_summary(old, new)
        elif role == 'rule':
            summary = _rule_summary(old, new)
        elif status != 'removed' or old is not None:
            summary = _size_summary(old, new)
        files.append(_public_file(entry, status, summary))
    if config is None and statuses.get('config.yaml') == 'modified':
        config = {'format': 'yaml', 'status': 'unavailable', 'reason': content_reason or 'bundle_missing',
                  'items': [], 'omitted': 0}
    unchanged = sorted((path for path, status in statuses.items() if status == 'unchanged'), key=order)
    counts = Counter(statuses.values())
    return {
        'status': 'changed',
        'content': 'digests' if receipt is None else 'full',
        'content_reason': content_reason,
        'counts': {key: counts.get(key, 0) for key in ('added', 'removed', 'modified', 'unchanged')},
        'files': files,
        'files_omitted': len(changed) - len(listed),
        'unchanged': [_public_file(new_entries[path], 'unchanged') for path in unchanged[:MAX_LISTED_FILES]],
        'unchanged_omitted': max(0, len(unchanged) - MAX_LISTED_FILES),
        'config': config,
    }
