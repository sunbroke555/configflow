"""Lossless, identity-checked edits of native listener flows and plugin parameters.

The graph describes declared references, not guessed execution or DNS roles.
Editing a node replaces only that plugin in its declaring document. It never
regenerates routing, rewrites tags, or turns a direct executor into a sequence.
"""
import base64
from collections import Counter
from copy import deepcopy

import yaml

from backend.common.oxidns_editor import _same
from backend.common.oxidns_match_targets import executable_targets
from backend.common.oxidns_native import load_native_yaml, managed_files, native_document
from backend.common.oxidns_reference_validation import _ENV, collect_reference_issues
from backend.common.oxidns_settings import OxidnsSettingsError, normalize_oxidns_settings
from backend.common.oxidns_validation import schemas


def _identity(source, index):
    return f'{source}#{index}'


def _expanded(settings):
    sources = {}
    document = native_document(settings, validate_arguments=False, sources=sources)
    return document, sources['plugins']


def read_entry_flows(document, settings):
    expanded, origins = _expanded(settings)
    raw_plugins = expanded.get('plugins', [])
    catalog = schemas()[1]
    graph, references = {}, []
    collect_reference_issues(expanded, catalog, dependency_graph=graph, reference_edges=references)
    counts = Counter(item.get('tag') for item in raw_plugins
                     if isinstance(item, dict) and isinstance(item.get('tag'), str))
    unique = {item['tag']: _identity(origin['source'], origin['index'])
              for item, origin in zip(raw_plugins, origins)
              if isinstance(item, dict) and isinstance(item.get('tag'), str) and counts[item['tag']] == 1}
    nodes, entries, seen = [], [], set()
    for item, origin in zip(raw_plugins, origins):
        identity = _identity(origin['source'], origin['index'])
        # The same include may be loaded twice. It is still one editable source
        # node; name resolution remains ambiguous and is shown as unresolved.
        if identity in seen:
            continue
        seen.add(identity)
        value = item if isinstance(item, dict) else {}
        tag, kind = value.get('tag'), value.get('type')
        editable = isinstance(tag, str) and isinstance(kind, str) and bool(tag) and bool(kind)
        schema = catalog.get(kind, {}) if isinstance(kind, str) else {}
        nodes.append({'id': identity, 'tag': tag, 'type': kind, 'kind': schema.get('x-kind', 'unknown'),
                      'source': origin['source'], 'index': origin['index'], 'value': deepcopy(item),
                      'editable': editable,
                      **({} if editable else {'reason': '请先在完整参数中填写插件 tag 和类型。'}),
                      'targets': executable_targets(expanded, origins, graph, (tag,))
                      if editable and kind == 'sequence' else []})
        args = value.get('args')
        args = args if isinstance(args, dict) else {}
        listener_entries = []
        if kind in ('udp_server', 'tcp_server', 'quic_server'):
            listener_entries.append((args.get('entry'), ['args', 'entry'], None))
        elif kind == 'http_server' and isinstance(args.get('entries'), list):
            listener_entries.extend((entry.get('exec'), ['args', 'entries', index, 'exec'], entry.get('path'))
                                    for index, entry in enumerate(args['entries']) if isinstance(entry, dict))
        for target, reference_path, path in listener_entries:
            if not isinstance(target, str) or not target:
                continue
            entries.append({'id': identity + ':' + '/'.join(map(str, reference_path)), 'listener_id': identity,
                            'listener_tag': tag, 'listener_type': kind, 'listen': deepcopy(args.get('listen')),
                            'entry': target, 'reference_path': reference_path,
                            **({'dynamic': True} if _ENV.search(target) else {}),
                            **({'path': path} if isinstance(path, str) else {})})
    edges, seen_edges = [], set()
    for reference in references:
        path = reference['path']
        if len(path) < 2 or path[0] != 'plugins' or not isinstance(path[1], int):
            continue
        origin = origins[path[1]]
        source = _identity(origin['source'], origin['index'])
        target = reference['target']
        edge = (source, target, tuple(path[2:]))
        if edge in seen_edges:
            continue
        seen_edges.add(edge)
        edges.append({'from': source, 'to': unique.get(target), 'target': target, 'path': path[2:]})
    return {'globals': {'source': 'config.yaml', 'value': deepcopy({key: value for key, value in document.items()
                                                                  if key != 'plugins'})},
            'entries': entries, 'plugins': nodes, 'edges': edges}


def _snapshot_equal(left, right):
    """JSON has one number kind; keep booleans distinct from 0 and 1."""
    if type(left) in (int, float) and type(right) in (int, float):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_snapshot_equal(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(_snapshot_equal(a, b) for a, b in zip(left, right))
    return left == right


def _preserve_unchanged(before, value):
    """Keep original scalar types and key order through a browser JSON roundtrip."""
    if _snapshot_equal(before, value):
        return deepcopy(before)
    if isinstance(before, dict) and isinstance(value, dict):
        return _ordered_replace(before, value)
    if isinstance(before, list) and isinstance(value, list):
        return [_preserve_unchanged(before[index], item) if index < len(before) else deepcopy(item)
                for index, item in enumerate(value)]
    return deepcopy(value)


def _ordered_replace(before, value):
    """Retain the positions of surviving keys and append explicitly new fields."""
    return {key: _preserve_unchanged(before[key], value[key]) if key in before else deepcopy(value[key])
            for key in [*before, *(key for key in value if key not in before)] if key in value}


def _source_document(settings, source):
    if source == 'config.yaml':
        return load_native_yaml(settings['native_config']), None
    for index, asset in enumerate(managed_files(settings.get('additional_files', []))):
        if asset['path'] == source:
            try:
                return load_native_yaml(asset['content'].decode('utf-8'), 'include 文件'), index
            except UnicodeDecodeError:
                raise OxidnsSettingsError('include 文件必须为 UTF-8 YAML') from None
    raise OxidnsSettingsError('流程所在的 include 文件已不存在，请刷新后重试。')


def apply_entry_flow(settings, change):
    if not isinstance(change, dict) or not isinstance(change.get('before'), dict) or not isinstance(change.get('value'), dict):
        raise OxidnsSettingsError('流程编辑需要完整的原值 before 和新值 value 对象。')
    if set(change) - {'kind', 'source', 'index', 'before', 'value'}:
        raise OxidnsSettingsError('流程编辑包含不支持的参数。')
    result = deepcopy(settings)
    kind = change.get('kind', 'plugin')
    source = change.get('source', 'config.yaml')
    if not isinstance(source, str):
        raise OxidnsSettingsError('流程源文件必须为文本。')
    doc, attachment_index = _source_document(result, source)
    before, value = change['before'], change['value']
    if kind == 'globals':
        if source != 'config.yaml' or 'index' in change or 'plugins' in value or 'plugins' in before:
            raise OxidnsSettingsError('全局参数只能编辑主配置中 plugins 以外的字段。')
        actual = {key: item for key, item in doc.items() if key != 'plugins'}
        if not _snapshot_equal(actual, before):
            raise OxidnsSettingsError('全局参数已变化，请刷新后重试。')
        if _snapshot_equal(actual, value):
            return result
        replacement = {**value, **({'plugins': doc['plugins']} if 'plugins' in doc else {})}
        doc = _ordered_replace(doc, replacement)
    elif kind == 'plugin':
        index = change.get('index')
        if type(index) is not int or index < 0:
            raise OxidnsSettingsError('流程编辑需要有效的插件位置 index。')
        _, origins = _expanded(result)
        if not any(origin == {'source': source, 'index': index} for origin in origins):
            raise OxidnsSettingsError('插件已不在当前配置中，请刷新后重试。')
        plugins = doc.get('plugins')
        if not isinstance(plugins, list) or index >= len(plugins) or not isinstance(plugins[index], dict) or not _snapshot_equal(plugins[index], before):
            raise OxidnsSettingsError('插件内容或位置已变化，请刷新当前流程后重试。')
        if any(not isinstance(before.get(field), str) or not before[field] or not _same(before.get(field), value.get(field))
               for field in ('tag', 'type')):
            raise OxidnsSettingsError('流程图中不能修改插件 tag 或类型，请使用完整参数中的重命名功能。')
        if _snapshot_equal(plugins[index], value):
            return result
        # A top-level extension may alias the entire plugins list. Detach the
        # edited list so replacing one node cannot change that other field.
        doc['plugins'] = list(plugins)
        doc['plugins'][index] = _ordered_replace(plugins[index], value)
    else:
        raise OxidnsSettingsError('流程编辑类型必须为 plugin 或 globals。')
    text = yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)
    # Enforce the native YAML bounds/safe structure while retaining incomplete
    # drafts and unknown fields. Publication performs full semantic validation.
    load_native_yaml(text)
    if attachment_index is None:
        result['native_config'] = text
    else:
        attachment = result['additional_files'][attachment_index]
        attachment['content'] = base64.b64encode(text.encode('utf-8')).decode('ascii') if attachment.get('encoding', 'text') == 'base64' else text
    normalize_oxidns_settings(result, validate_native_arguments=False)
    native_document(result, validate_arguments=False)
    return result
