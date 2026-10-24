"""Non-persistent preparation and three-way template tools for one native draft.

The native YAML is the executable source. Template inputs and their last generated
baseline are editing metadata, never an alternative active configuration.
"""
from copy import deepcopy

import yaml

from backend.common.oxidns_native import (load_native_yaml, native_document,
    relative_resource, resource_references)
from backend.common.oxidns_settings import normalize_oxidns_settings, OxidnsSettingsError

_EDITOR_FIELDS = {
    'config_mode', 'native_config', 'additional_files', 'template_settings',
    'template_base_config', 'legacy_native_config', 'editor_version',
}
_RESOURCE_LISTS = ('direct_rules', 'proxy_rules', 'direct_rulesets', 'proxy_rulesets')
_MISSING = object()


def _rule_ids(config):
    """(every rule id -> item, ids of active rules) of a space."""
    from backend.converters.oxidns import _items
    items = config.get('rule_configs') or _items(config)
    known = {}
    for item in items:
        if isinstance(item, dict) and isinstance(item.get('id'), str):
            known.setdefault(item['id'], item)
    return known, {item.get('id') for item in _items(config)}


def _restore_parked(inputs, config):
    """Template inputs route disabled rules again: the generator skips them itself.

    Parked choices return to the lists or rule_actions, and rule_actions ids
    leave the selector lists, whose native union catalog may still hold them.
    """
    parked, actions = inputs.get('rule_dns_parked'), inputs.get('rule_actions')
    if not isinstance(parked, dict) or not isinstance(actions, dict) or \
            any(not isinstance(inputs.get(key, []), list) for key in _RESOURCE_LISTS):
        return inputs  # Normalization reports the malformed value.
    known = _rule_ids(config)[0] if config is not None else {}
    for identity, action in parked.items():
        route = action.get('action') if isinstance(action, dict) else None
        if route in ('direct', 'proxy'):
            item = known.get(identity)
            if item is not None:
                suffix = 'rulesets' if item.get('itemType') == 'ruleset' else 'rules'
            else:
                suffix = 'rulesets' if identity in inputs.get(f'{route}_rulesets', []) else 'rules'
            other = f'{"proxy" if route == "direct" else "direct"}_{suffix}'
            inputs[other] = [value for value in inputs.get(other, []) if value != identity]
            if identity not in inputs.setdefault(f'{route}_{suffix}', []):
                inputs[f'{route}_{suffix}'].append(identity)
        elif route is not None:
            actions.setdefault(identity, action)
    inputs['rule_dns_parked'] = {}
    for key in _RESOURCE_LISTS:
        if key in inputs:
            inputs[key] = [value for value in inputs[key] if value not in actions]
    return inputs


def template_inputs(settings, config=None):
    """Copy template controls without recursively embedding previous editor data.

    Choices parked by native routing are restored; config, when given, says
    whether a parked direct/proxy id is a rule or a ruleset.
    """
    return _restore_parked({key: deepcopy(value) for key, value in settings.items()
                            if key not in _EDITOR_FIELDS and not key.startswith('_editor_')}, config)


def _park_inactive(settings, config):
    """Native routing keeps only active rules; park the choices of the others.

    rule_actions keeps active ids only. The selector lists stay a retained
    catalog. Follow rules need no parking: their policy decides on enabling.
    """
    known, active = _rule_ids(config)
    follow = set(settings['rule_dns_follow'])
    parked = settings['rule_dns_parked']
    for identity, action in list(settings['rule_actions'].items()):
        if identity in active:
            continue
        del settings['rule_actions'][identity]
        if identity in known and identity not in follow:
            parked.setdefault(identity, action)
    for key in _RESOURCE_LISTS:
        for identity in settings[key]:
            if identity in known and identity not in active and identity not in follow:
                parked.setdefault(identity, {'action': key.split('_')[0]})


def _template(settings, config, base_url):
    from backend.converters.oxidns import generate_oxidns_config
    source = deepcopy(config)
    source['oxidns'] = normalize_oxidns_settings({
        **template_inputs(settings, config), 'config_mode': 'template', 'native_config': '',
        'additional_files': deepcopy(settings.get('additional_files', [])),
    })
    text = generate_oxidns_config(source, base_url)
    return source['oxidns'], text, load_native_yaml(text)


def prepare_editor(settings, config, base_url=''):
    """Prepare a unified draft without writing or normalizing native YAML text."""
    current = normalize_oxidns_settings(settings, validate_native_arguments=False)
    if current['config_mode'] == 'native':
        # A former native user may have an invalid, inactive template. Opening
        # the editor must not attempt to regenerate it or replace their YAML.
        if not isinstance(current.get('template_settings'), dict):
            current['template_settings'] = template_inputs(current, config)
        return current
    template, text, document = _template(current, config, base_url)
    old_native = current['native_config']
    if old_native.strip() and 'legacy_native_config' not in current:
        try:
            same = _same(load_native_yaml(old_native), document)
        except OxidnsSettingsError:
            same = False
        if not same:
            current['legacy_native_config'] = old_native
    current.update(config_mode='native', native_config=text,
                   template_settings=template_inputs(template, config), template_base_config=text)
    _park_inactive(current, config)
    return current


def _same(left, right):
    # Python otherwise equates false with 0, losing meaningful YAML differences.
    if left is _MISSING or right is _MISSING:
        return left is right
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_same(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right))
    return left == right


def _copy(value):
    return _MISSING if value is _MISSING else deepcopy(value)


def _pointer(path):
    return '/' + '/'.join(str(part).replace('~', '~0').replace('/', '~1') for part in path)


def _keys(*objects):
    return dict.fromkeys(key for obj in objects if isinstance(obj, dict) for key in obj)


def _plugin_map(value):
    if not isinstance(value, list):
        return None
    result = {}
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get('tag'), str) or item['tag'] in result:
            return None
        result[item['tag']] = item
    return result


def _label(path):
    if len(path) > 1 and path[0] == 'plugins':
        return f'插件「{path[1]}」' + (' · ' + '.'.join(path[2:]) if len(path) > 2 else '')
    if path == ('resources', 'custom_hosts'):
        return 'Hosts 规则文件内容'
    if path == ('resources', 'adguard', 'content'):
        return 'AdGuard 自定义规则文件内容'
    return '.'.join(path)


def _merge_plugins(base, current, proposed, path, resolutions, conflicts):
    base_map, current_map, next_map = (_plugin_map(value) for value in (base, current, proposed))
    if any(value is None for value in (base_map, current_map, next_map)):
        return None
    merged = {}
    for tag in _keys(current_map, next_map, base_map):
        value = _merge(base_map.get(tag, _MISSING), current_map.get(tag, _MISSING),
                       next_map.get(tag, _MISSING), (*path, tag), resolutions, conflicts)
        if value is not _MISSING:
            merged[tag] = value
    order = [tag for tag in current_map if tag in merged]
    template_order = list(next_map)
    for index, tag in enumerate(template_order):
        if tag not in merged or tag in order:
            continue
        previous = next((item for item in reversed(template_order[:index]) if item in order), None)
        following = next((item for item in template_order[index + 1:] if item in order), None)
        if previous is not None:
            order.insert(order.index(previous) + 1, tag)
        elif following is not None:
            order.insert(order.index(following), tag)
        else:
            order.append(tag)
    return [merged[tag] for tag in order]


def _merge(base, current, proposed, path, resolutions, conflicts):
    if _same(proposed, base) or _same(current, proposed):
        return _copy(current)
    if all(isinstance(value, dict) for value in (current, proposed)) and (base is _MISSING or isinstance(base, dict)):
        previous = {} if base is _MISSING else base
        result = {}
        for key in _keys(current, proposed, previous):
            value = _merge(previous.get(key, _MISSING), current.get(key, _MISSING),
                           proposed.get(key, _MISSING), (*path, key), resolutions, conflicts)
            if value is not _MISSING:
                result[key] = value
        return result
    if path == ('plugins',):
        result = _merge_plugins([] if base is _MISSING else base, current, proposed, path, resolutions, conflicts)
        if result is not None:
            return result
    if _same(current, base):
        return _copy(proposed)
    location = _pointer(path)
    choice = resolutions.get(location, 'current')
    conflicts.append({'path': location, 'label': _label(path), 'choice': choice})
    return _copy(proposed if choice == 'template' else current)


def _changes(current, proposed, path=()):
    if _same(current, proposed):
        return []
    if current is _MISSING or proposed is _MISSING:
        return [{'path': _pointer(path), 'kind': 'add' if current is _MISSING else 'remove'}]
    if isinstance(current, dict) and isinstance(proposed, dict):
        return [change for key in _keys(current, proposed)
                for change in _changes(current.get(key, _MISSING), proposed.get(key, _MISSING), (*path, key))]
    if path == ('plugins',):
        left, right = _plugin_map(current), _plugin_map(proposed)
        if left is not None and right is not None:
            changes = [change for tag in _keys(left, right)
                       for change in _changes(left.get(tag, _MISSING), right.get(tag, _MISSING), (*path, tag))]
            if left.keys() == right.keys() and list(left) != list(right):
                changes.append({'path': _pointer(path), 'kind': 'reorder'})
            return changes
    return [{'path': _pointer(path), 'kind': 'update'}]


def _union(left, right):
    result = deepcopy(left) if isinstance(left, list) else []
    for item in right if isinstance(right, list) else []:
        if not any(_same(item, existing) for existing in result):
            result.append(deepcopy(item))
    return result



def _keep_referenced_content(current, merged, resource, path, referenced, resolutions, conflicts):
    """Deleting template inputs cannot remove a file retained by a manual plugin."""
    if (resource not in referenced or not isinstance(current, str) or not current.strip()
            or (isinstance(merged, str) and merged.strip())):
        return merged
    location = _pointer(path)
    choice = resolutions.get(location, 'current')
    if choice == 'template':
        raise OxidnsSettingsError(f'{_label(path)}仍被完整配置引用，请先移除相关文件引用再清空内容')
    if not any(conflict['path'] == location for conflict in conflicts):
        conflicts.append({'path': location, 'label': _label(path), 'choice': 'current'})
    return current


def preview_template(settings, proposed_inputs, config, base_url='', resolutions=None):
    """Return a merge preview, retaining manual edits and all resource sources."""
    if not isinstance(proposed_inputs, dict):
        raise OxidnsSettingsError('template_settings 必须为模板参数对象')
    if resolutions is None:
        resolutions = {}
    if not isinstance(resolutions, dict) or any(
        not isinstance(path, str) or choice not in ('current', 'template')
        for path, choice in resolutions.items()
    ):
        raise OxidnsSettingsError('resolutions 必须按参数路径选择 current 或 template')
    current = prepare_editor(settings, config, base_url)
    previous_inputs = current.get('template_settings', template_inputs(current, config))
    # Validate only on invoking this tool. Merely opening a native draft never
    # depends on whether its retained template controls are still renderable.
    old_template = {**template_inputs(previous_inputs, config), 'additional_files': current['additional_files']}
    base_text = current.get('template_base_config')
    if isinstance(base_text, str) and base_text.strip():
        base = load_native_yaml(base_text, '模板基线')
    elif isinstance(base_text, dict):
        # Accept early draft metadata stored as a parsed document as well.
        base = load_native_yaml(yaml.safe_dump(base_text), '模板基线')
    else:
        _, _, base = _template(old_template, config, base_url)
    next_template, next_text, proposed = _template({
        **template_inputs(proposed_inputs, config), 'additional_files': current['additional_files'],
    }, config, base_url)
    document = load_native_yaml(current['native_config'])
    conflicts = []
    merged = _merge(base, document, proposed, (), resolutions, conflicts)
    changes = _changes(document, merged)
    result = deepcopy(current)
    result.update(config_mode='native', template_settings=template_inputs(next_template, config),
                  template_base_config=next_text)
    # Keep comments and original formatting when the actual document is equal.
    if not _same(document, merged):
        result['native_config'] = yaml.safe_dump(merged, allow_unicode=True, sort_keys=False)
    referenced = {relative_resource(path) for path in resource_references(
        native_document(result, validate_arguments=False))}
    for key in _RESOURCE_LISTS:
        result[key] = _union(current.get(key), next_template.get(key))
        changes.extend(_changes(current.get(key, _MISSING), result[key], ('resources', key)))
    # The draft's own rule DNS choices win; follow ends where the template set an action.
    result['rule_actions'] = {**next_template['rule_actions'], **current['rule_actions']}
    result['rule_dns_follow'] = [identity for identity in current['rule_dns_follow']
                                 if identity not in result['rule_actions']]
    _park_inactive(result, config)
    result['custom_hosts'] = _merge(previous_inputs.get('custom_hosts', ''), current.get('custom_hosts', ''),
                                    next_template['custom_hosts'], ('resources', 'custom_hosts'), resolutions, conflicts)
    result['custom_hosts'] = _keep_referenced_content(
        current.get('custom_hosts', ''), result['custom_hosts'], 'rules/custom_hosts.txt',
        ('resources', 'custom_hosts'), referenced, resolutions, conflicts)
    changes.extend(_changes(current.get('custom_hosts', ''), result['custom_hosts'], ('resources', 'custom_hosts')))
    old_adguard = previous_inputs.get('adguard') if isinstance(previous_inputs.get('adguard'), dict) else {}
    current_adguard = current.get('adguard') if isinstance(current.get('adguard'), dict) else {}
    next_adguard = next_template['adguard']
    adguard = deepcopy(current_adguard)
    adguard['enabled'] = bool(current_adguard.get('enabled') or next_adguard['enabled'])
    adguard['urls'] = _union(current_adguard.get('urls'), next_adguard['urls'])
    adguard['content'] = _merge(old_adguard.get('content', ''), current_adguard.get('content', ''),
                                next_adguard['content'], ('resources', 'adguard', 'content'), resolutions, conflicts)
    adguard['content'] = _keep_referenced_content(
        current_adguard.get('content', ''), adguard['content'], 'rules/adguard-custom.txt',
        ('resources', 'adguard', 'content'), referenced, resolutions, conflicts)
    result['adguard'] = adguard
    changes.extend(_changes(current_adguard, adguard, ('resources', 'adguard')))
    # Includes and attached files must still be structurally valid, while an
    # incomplete legacy plugin is allowed to remain editable in this preview.
    normalize_oxidns_settings(result, validate_native_arguments=False)
    return {'settings': result, 'changes': changes, 'conflicts': conflicts}
