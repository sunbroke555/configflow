"""Comment-preserving edits of fixed listener targets, validated with PyYAML.

PyYAML remains the generator's semantic authority. The round-trip editor is
used only when a group edit actually changes a listener, never on read/render.
"""
from copy import deepcopy
from collections import Counter
from io import StringIO
import re

import yaml
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import YAMLError
from ruamel.yaml.scalarint import OctalInt
from ruamel.yaml.scalarstring import DoubleQuotedScalarString


def _detach(listener):
    result = deepcopy(listener)
    if result.merge:
        # Removing an explicit proxy must not reveal an inherited proxy again.
        # Materialize this one consumer; the original merge/alias stays intact.
        materialized = CommentedMap(result.items())
        materialized.ca.items.update(deepcopy(result.ca.items))
        materialized.ca.comment = deepcopy(result.ca.comment)
        materialized.ca.end.extend(deepcopy(result.ca.end))
        if result.fa.flow_style(): materialized.fa.set_flow_style()
        result = materialized
    seen = set()
    def clear_anchor(value):
        if id(value) in seen: return
        seen.add(id(value))
        if hasattr(value, 'yaml_set_anchor'): value.yaml_set_anchor(None)
        if isinstance(value, dict):
            for child in value.values(): clear_anchor(child)
        elif isinstance(value, list):
            for child in value: clear_anchor(child)
    clear_anchor(result)
    return result


def _removed_comments(value):
    if isinstance(value, list):
        return ''.join(_removed_comments(item) for item in value)
    return getattr(value, 'value', '') if value is not None else ''


def _comments(source):
    # Inspect lexer comment tokens, never '#' text inside quoted/block scalars.
    # Round-trip emitters can omit comments between flow-map entries; preserve
    # those rare comments as standalone explanations rather than losing them.
    result = Counter()
    for token in YAML(typ='rt').scan(source):
        for line in _removed_comments(getattr(token, 'comment', None)).splitlines():
            if line.lstrip().startswith('#'):
                result[line.strip()] += 1
    return result


def _same(left, right):
    if type(left) is not type(right): return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_same(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right))
    return left == right


def edit_listener_targets(source, indexes, new_name, expected):
    original_comments = _comments(source)
    editor = YAML(typ='rt')
    editor.version = (1, 1)  # Match the existing PyYAML generator's scalars.
    editor.preserve_quotes = True
    editor.line_break = '\r\n' if '\r\n' in source else '\n'
    editor.width = max(80, *(len(line) for line in source.splitlines()))
    # Old-style octal values must stay integers when read again by PyYAML.
    editor.representer.add_representer(OctalInt, lambda r, value: r.represent_int(int(value)))
    document = editor.load(source)
    original = document['listeners']
    listeners = CommentedSeq(original)
    listeners.ca.items.update(deepcopy(original.ca.items))
    listeners.ca.comment = deepcopy(original.ca.comment)
    listeners.ca.end.extend(deepcopy(original.ca.end))
    if original.fa.flow_style(): listeners.fa.set_flow_style()
    document['listeners'] = listeners
    for index in indexes:
        replacement = _detach(listeners[index])
        if new_name is None:
            comments = _removed_comments(replacement.ca.items.pop('proxy', None))
            replacement.pop('proxy', None)
            if comments:
                # Keep explanations attached to a removed field as standalone
                # comments before this listener instead of throwing them away.
                text = '\n'.join(re.sub(r'^\s*# ?', '', line) for line in comments.splitlines())
                listeners.yaml_set_comment_before_after_key(index, before=text)
        else:
            replacement['proxy'] = DoubleQuotedScalarString(new_name)
        listeners[index] = replacement
    editor.version = None  # Do not add a YAML version directive to user files.
    for event in yaml.parse(source):
        if isinstance(event, yaml.events.DocumentStartEvent): editor.explicit_start = event.explicit
        elif isinstance(event, yaml.events.DocumentEndEvent): editor.explicit_end = event.explicit
    output = StringIO()
    editor.dump(document, output)
    result = output.getvalue()
    missing = original_comments - _comments(result)
    if missing:
        result = editor.line_break.join(missing.elements()) + editor.line_break + result
    if not _same(yaml.safe_load(result), expected):
        raise ValueError('注释保留编辑无法保持原有 YAML 参数，原配置已保留')
    return result
