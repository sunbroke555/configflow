"""Read and edit the recognizable routing portion of an OxiDNS document.

The native providers and sequence are the source of truth. Historical template
inputs are only retained as resource sources; they never supply displayed values.
Unrecognized routing is left for the complete editor instead of being regenerated.

Per-rule DNS actions (rejections, blackhole, pinned upstreams) are rule groups
too, but only on generated providers that nothing else uses; their owned helper
plugins are created and removed with the groups and never overwritten when edited
by hand.
"""
from copy import deepcopy
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
import re

from backend.common.oxidns_settings import BLOCK_QTYPES_EXECS, OxidnsSettingsError
from backend.common.dns_watchdog import WATCHDOG_RULE
from backend.common.oxidns_native import resource_references, relative_resource
from backend.common.rule_dns import (
    BLACKHOLE_TAG, GROUP_TAG_RE, LIST_ROUTES, REAL_WRAPPERS, ROUTE_LABELS, UPSTREAM_PREFIX, Invalid,
    chain_features, convertible, follow_route, has_ip_part, helper_plugins, normalize_action, parse_exec, pinned_action, probe_needed,
    policy_dns_routes, route_exec, route_key,
)
from backend.converters.oxidns import _items, _routing_groups, _rule_path, _base, _tag, ruleset_parts, unavailable_rulesets
from backend.converters.mosdns import _build_rule_proxy_url

SELECTORS = ('direct_rules', 'proxy_rules', 'direct_rulesets', 'proxy_rulesets')
RULE_DNS_FIELDS = ('rule_actions', 'rule_dns_follow')
ROUTING_FIELDS = frozenset((*SELECTORS, *RULE_DNS_FIELDS, 'aggregate_rules', 'default_forward',
                           'custom_matches', 'custom_match_position'))
_TARGETS = {'domain': {'goto china_dns': 'direct', 'goto proxy_dns_seq': 'proxy'},
            'ip': {'goto direct_ip': 'direct', 'goto ip_requery_proxy': 'proxy'}}
_REASON = '当前分流执行链包含自定义结构，请在完整参数中编辑。'
_MISSING_RULE = '所选规则不在当前空间的可用规则库中，请刷新后重试。'
_HELPER_TAG = re.compile(rf'{BLACKHOLE_TAG}|cf_dns_(?:up|fwd|cache)_([0-9a-f]{{12}})')
_HELPER_EXEC = re.compile(r'(?:\$|goto |jump )cf_dns_')


def _cap(editable=True, reason='', target='sequence_main'):
    return {'editable': editable, 'reason': reason, 'target': target}


def _same(left, right):
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_same(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right))
    return left == right


def _plugins(document):
    result = {}
    for item in document.get('plugins', []) if isinstance(document.get('plugins'), list) else []:
        if not isinstance(item, dict) or not isinstance(item.get('tag'), str) or item['tag'] in result:
            return {}
        result[item['tag']] = item
    return result


def _matches(entry):
    value = entry.get('matches')
    return [value] if isinstance(value, str) else value


def _single_match(entry):
    values = _matches(entry)
    return values[0] if isinstance(values, list) and len(values) == 1 and isinstance(values[0], str) else None


def _prefix_end(args):
    """Skip recognized built-ins, including imported deployment probe rules."""
    index = 0
    while index < len(args):
        item = args[index]
        action = item.get('exec')
        match = _single_match(item)
        if item == WATCHDOG_RULE:
            index += 1
        elif not item.get('matches') and (action in ('prefer_ipv4', '$query_recorder') or
                                        isinstance(action, str) and action.startswith('metrics_collector ')):
            index += 1
        elif action in BLOCK_QTYPES_EXECS and isinstance(match, str) and re.fullmatch(r'qtype(?:\s+\d+)+', match):
            index += 1
        elif action == '$custom_hosts' and not item.get('matches') and index + 1 < len(args) and \
                args[index + 1].get('exec') == 'accept' and _single_match(args[index + 1]) == 'has_resp':
            index += 2
        elif action == 'goto adguard_reject' and match == 'question $adguard_rules':
            index += 1
        elif item == {'exec': 'jump cf_device_policy'}:
            # Device policy owns this hook; routing edits must keep it ahead of
            # domain rules and must not expose it as a user custom match.
            index += 1
        else:
            break
    return index


def _atom(part, key, value):
    if not isinstance(value, str):
        return None
    return part, key, value.removeprefix('./') if key == 'files' else value


def _catalog(config):
    """Each rule's exact inline expression or generated resource path."""
    records, atoms = {}, {}
    for item in _items(config):
        identity = item.get('id')
        if not isinstance(identity, str) or identity in records:
            continue
        suffix = 'rulesets' if item.get('itemType') == 'ruleset' else 'rules'
        settings = {key: [] for key in SELECTORS}
        settings.update(aggregate_rules=False)
        settings['direct_' + suffix] = [identity]
        try:
            # Legacy shape: documents written before content-aware IP parts still
            # reference the IP file of a classical set without IP lines.
            groups = _routing_groups(config, settings, legacy=True)
        except (OxidnsSettingsError, KeyError, TypeError, ValueError):
            continue
        parts = set()
        for part, entries in groups.items():
            for group in entries:
                for key, values in group['args'].items():
                    for value in values:
                        atom = _atom(part, key, value)
                        if atom is not None:
                            atoms.setdefault(atom, set()).add(identity)
                            parts.add(part)
        current = set(ruleset_parts(item, config)) if item.get('itemType') == 'ruleset' else (parts - {'ip'})
        content_aware = parts if has_ip_part(item, config) else parts - {'ip'}
        records[identity] = {'suffix': suffix, 'parts': parts,
                             'shapes': {frozenset(parts), frozenset(content_aware), frozenset(current)},
                             'item': item}
    return records, atoms


def _describe(group):
    return {'route': group['route'], 'args': group['args']}


def _ip_suffix(entries, default_forward, probe):
    """The IP routing clauses before the final fallback.

    The probe gives the IP groups an answer to match. Without one they never
    match, which is only written when every IP route equals the fallback.
    """
    if not entries:
        return []
    if not probe:
        return [*entries]
    return [{'exec': 'jump local_probe'}, *entries,
            {'matches': ['has_resp'], 'exec': 'goto default_direct_ip'} if default_forward == 'forward_local'
            else {'exec': 'drop_resp'}]


def _suffix(item):
    return 'rulesets' if item.get('itemType') == 'ruleset' else 'rules'


def _known(config):
    """Every rule of the space by id, disabled ones included, so their DNS
    choices are parked instead of lost."""
    result = {}
    for item in config.get('rule_configs') or _items(config):
        if isinstance(item, dict) and isinstance(item.get('id'), str):
            result.setdefault(item['id'], item)
    return result


def _stored_follow(settings, config):
    follow = settings.get('rule_dns_follow')
    known = _known(config)
    return [identity for identity in dict.fromkeys(follow) if isinstance(identity, str) and identity in known] \
        if isinstance(follow, list) else []


def _route_of(values, identity):
    action = values['rule_actions'].get(identity)
    if action is not None:
        return route_key(action)
    return next((route for route in LIST_ROUTES
                 if identity in values[f'{route}_rules'] or identity in values[f'{route}_rulesets']), None)


def _owned_group(document, plugins, args, entry, tag):
    """A generated provider that only this one sequence_main entry uses."""
    if not GROUP_TAG_RE.fullmatch(tag) or set(entry) != {'matches', 'exec'}:
        return False
    if sum(1 for item in args if _references(item, tag)) != 1 or \
            _references({key: value for key, value in document.items() if key != 'plugins'}, tag):
        return False
    return not any(_references(plugin, tag) for name, plugin in plugins.items()
                   if name not in ('sequence_main', 'rules_reload', tag))


def _unique_atoms(part, provider, atoms):
    """Every provider value belongs to exactly one catalog rule."""
    args = provider.get('args')
    if not isinstance(args, dict) or not args or set(args) - {'files', 'exps' if part == 'domain' else 'ips'}:
        return False
    return all(isinstance(values, list) and values and
               all(len(atoms.get(_atom(part, key, value), ())) == 1 for value in values)
               for key, values in args.items())


def _analyze(document, settings, config):
    """Recognized routing state.

    Upgrading never locks a document that was editable before rule DNS actions
    existed: when claiming action entries breaks recognition and no helper is in
    use, those entries are read as custom matches, exactly as before.
    """
    state = _recognize(document, settings, config, claim=True)
    args = state.get('main', {}).get('args', [])
    if state.get('claimed') and not any(_HELPER_EXEC.match(entry['exec']) for entry in args):
        legacy = _recognize(document, settings, config, claim=False)
        caps = state['capabilities']
        if not caps['direct_rules']['editable'] or (
                not caps['custom_matches']['editable'] and legacy['capabilities']['custom_matches']['editable']):
            return legacy
    return state


def _observed_legacy_groups(config, values, assigned, atoms, aggregate):
    """Validate old per-rule parts even when a downloaded source later changed.

    Content-aware releases omitted an IP part after seeing its source. The old
    document may consequently contain any recognized per-rule combination, but
    its resources, route and order must still exactly match the current catalog.
    """
    raw = _routing_groups(config, {**values, 'aggregate_rules': False}, legacy=True)
    expected = {'domain': [], 'ip': []}
    for part, groups in raw.items():
        for group in groups:
            owners = {identity for key, entries in group['args'].items() for value in entries
                      for identity in atoms.get(_atom(part, key, value), ())}
            if len(owners) != 1 or part not in assigned.get(next(iter(owners)), {}):
                continue
            target = expected[part]
            if aggregate and target and target[-1]['route'] == group['route']:
                for key, entries in group['args'].items():
                    target[-1]['args'].setdefault(key, []).extend(entries)
            else:
                target.append(deepcopy(group))
    return expected


def _recognize(document, settings, config, claim):
    values = {key: [] for key in SELECTORS}
    values.update(aggregate_rules=None, default_forward=None, custom_matches=[], custom_match_position=None,
                  rule_actions={}, rule_dns_follow=_stored_follow(settings, config))
    caps = {key: _cap(False, _REASON) for key in ROUTING_FIELDS}
    state = {'values': values, 'capabilities': caps}
    plugins = _plugins(document)
    main = plugins.get('sequence_main', {})
    args = main.get('args')
    if main.get('type') != 'sequence' or not isinstance(args, list) or not args or \
            any(not isinstance(item, dict) or not isinstance(item.get('exec'), str) for item in args):
        return state
    final = args[-1]
    if not final.get('matches') and final['exec'] in _TARGETS['domain']:
        values['default_forward'] = 'forward_local' if final['exec'] == 'goto china_dns' else 'forward_remote'
        caps['default_forward'] = _cap()
    else:
        return state
    records, atoms = _catalog(config)
    groups = {'domain': [], 'ip': []}
    dynamic = []
    middle_end = len(args) - 1
    recognized = True
    invalid, claimed = None, False
    for index, entry in enumerate(args[:-1]):
        match = _single_match(entry)
        if match == 'qname $dynamic_domains' and entry['exec'] in _TARGETS['domain']:
            dynamic.append((index, entry))
            middle_end = min(middle_end, index)
            continue
        parsed = re.fullmatch(r'(qname|resp_ip) \$([^\s]+)', match or '')
        if parsed:
            part = 'domain' if parsed[1] == 'qname' else 'ip'
            provider = plugins.get(parsed[2], {})
            route = None
            if provider.get('type') == ('domain_set' if part == 'domain' else 'ip_set'):
                route = parse_exec(entry['exec'], part, plugins)
            if route is not None and route not in LIST_ROUTES:
                # A user's own match (or one reusing a generated provider) stays a custom match.
                if claim and _owned_group(document, plugins, args, entry, parsed[2]) and \
                        (isinstance(route, Invalid) or _unique_atoms(part, provider, atoms)):
                    claimed = True
                else:
                    route = None
            if route is not None:
                group = {'index': index, 'tag': parsed[2], 'route': route,
                         'args': provider.get('args'), 'entry': entry, 'provider': provider}
                if isinstance(route, Invalid):
                    invalid = invalid or route.reason
                elif route.startswith('upstream:'):
                    group['action'] = pinned_action(entry['exec'][len('goto ' + UPSTREAM_PREFIX):], plugins)
                groups[part].append(group)
                if part == 'ip':
                    middle_end = min(middle_end, index)
                continue
        if not entry.get('matches') and entry['exec'] == 'jump local_probe':
            middle_end = min(middle_end, index)
    if len(dynamic) > 1:
        return state
    # The generated suffix has a single probe, ordered IP routes, and one final fallback.
    entries = [group['entry'] for group in groups['ip']]
    candidates = [_ip_suffix(entries, values['default_forward'], probe=True)]
    if not probe_needed([group['route'] for group in groups['ip']], values['default_forward']):
        # Written without the probe; older documents still carry it.
        candidates.insert(0, _ip_suffix(entries, values['default_forward'], probe=False))
    head = [dynamic[0][1]] if dynamic else []
    # Extensions on IP clauses are preserved by direct edits, but regeneration of
    # these owned clauses is refused below. The probe/fallback wrappers must be exact.
    if not any(_same(args[middle_end:], [*head, *candidate, final]) for candidate in candidates):
        caps['default_forward'] = _cap(False, _REASON)
        return state
    prefix_end = _prefix_end(args[:middle_end])
    domain_indices = {group['index'] for group in groups['domain']}
    if any(index < prefix_end or index >= middle_end for index in domain_indices):
        return state
    custom_pairs = [(i, args[i]) for i in range(prefix_end, middle_end) if i not in domain_indices]
    custom = [entry for _, entry in custom_pairs]
    custom_valid = all(isinstance(_matches(entry), list) and _matches(entry) and
                       all(isinstance(match, str) and match.strip() for match in _matches(entry)) and
                       entry['exec'].strip() for entry in custom)
    position = 'tail'
    if custom_pairs and domain_indices:
        if max(i for i, _ in custom_pairs) < min(domain_indices):
            position = 'head'
        elif min(i for i, _ in custom_pairs) <= max(domain_indices):
            custom_valid = False
    values['custom_matches'] = deepcopy(custom)
    values['custom_match_position'] = position if custom_valid and custom else None
    if custom_valid:
        caps['custom_matches'] = _cap()
        caps['custom_match_position'] = _cap(bool(custom),
            '请先添加自定义匹配，再设置执行顺序。' if not custom else
            '当前没有域名分流规则，两种执行顺序效果相同。' if not domain_indices else '')
    else:
        recognized = False
    if invalid:
        # Rebuilding the chain for a custom match edit would silently drop the
        # entry whose helper was edited by hand, so custom matches lock too.
        recognized = False
        caps['custom_matches'] = caps['custom_match_position'] = _cap(False, invalid)
    assigned, orphans, actions = {}, [], {}
    for part, entries in groups.items():
        for group in entries:
            if isinstance(group['route'], Invalid):
                continue
            if group['route'] not in LIST_ROUTES:
                actions[group['route']] = group.get('action') or {'action': group['route']}
            args_value = group['args']
            allowed = {'files', 'exps' if part == 'domain' else 'ips'}
            if not isinstance(args_value, dict) or not args_value or set(args_value) - allowed or \
                    set(group['entry']) != {'matches', 'exec'}:
                recognized = False
                continue
            for key, entries in args_value.items():
                if not isinstance(entries, list) or not entries:
                    recognized = False
                    continue
                for value in entries:
                    candidates = atoms.get(_atom(part, key, value), set())
                    if len(candidates) != 1:
                        recognized = False
                        if not candidates:
                            orphans.append(_atom(part, key, value)[1:])
                        continue
                    identity = next(iter(candidates))
                    previous = assigned.setdefault(identity, {})
                    if part in previous:
                        recognized = False
                    previous[part] = group['route']
    for identity, parts in assigned.items():
        record = records[identity]
        routes = set(parts.values())
        if frozenset(parts) not in record['shapes'] or len(routes) != 1:
            recognized = False
            continue
        route = next(iter(routes))
        if route in LIST_ROUTES:
            values[route + '_' + record['suffix']].append(identity)
        else:
            values['rule_actions'][identity] = deepcopy(actions[route])
    # Reconstruct profile order instead of deriving it from stale selection metadata.
    for key in SELECTORS:
        selected = set(values[key])
        values[key] = [identity for identity in records if identity in selected]
    values['rule_actions'] = {identity: values['rule_actions'][identity]
                              for identity in records if identity in values['rule_actions']}
    matches = []
    if recognized:
        for aggregate in (True, False):
            expected = _observed_legacy_groups(config, values, assigned, atoms, aggregate)
            if all(_same([_describe(group) for group in groups[part]], [_describe(group) for group in expected[part]])
                   for part in groups):
                matches.append(aggregate)
        recognized = bool(matches)
    if recognized:
        values['aggregate_rules'] = matches[0] if len(matches) == 1 else None
        for key in (*SELECTORS, *RULE_DNS_FIELDS):
            caps[key] = _cap()
        caps['aggregate_rules'] = _cap(len(matches) == 1,
            '当前没有相邻且同策略的规则，无需合并。' if len(matches) == 2 else '')
    else:
        reason = invalid or '规则资源或顺序无法唯一对应当前规则库，请在完整参数中编辑。'
        hints = [] if invalid else unavailable_rulesets(config, [value for key, value in orphans if key == 'files'])
        if hints:
            reason = '分流里有不可用的规则集，暂时不能在这里编辑。' + '；'.join(hints) + '。'
        elif orphans and not invalid:
            reason = f'分流里有规则库中不存在的单条规则（{orphans[0][1]}），请在完整参数中删除后再编辑。'
        for key in (*SELECTORS, *RULE_DNS_FIELDS, 'aggregate_rules'):
            caps[key] = _cap(False, reason)
    state.update(plugins=plugins, main=main, groups=groups, prefix=args[:prefix_end],
                 dynamic=[entry for _, entry in dynamic], custom=custom, final=final,
                 records=records, claimed=claimed, custom_indices=[index for index, _ in custom_pairs])
    return state


_CUSTOM_FIELDS = frozenset(('custom_matches', 'custom_match_position'))


def _aliased_custom_state(document, settings, config):
    """Recognize recorded managed renames without changing their native names.

    Only custom-match controls use this view. General rule regeneration still
    requires its original managed structure, and unrecorded names stay custom.
    """
    parked = settings.get('_common_disabled')
    aliases = parked.get('renamed_plugins') if isinstance(parked, dict) else None
    if not isinstance(aliases, dict) or not aliases:
        return None
    from backend.common.oxidns_native_apply import _MANAGED, _TAG_TYPES, _rename
    from backend.common.oxidns_native import TAG, native_document
    aliases = {old: new for old, new in aliases.items() if old in _MANAGED}
    if not aliases or any(not isinstance(new, str) or not 0 < len(new) <= 64 or not TAG.fullmatch(new)
                          or new.startswith(('qs.exec.', 'qs.match.', 'qs.cron.')) for new in aliases.values()):
        return None
    plugins = _plugins(document)
    expanded = _plugins(native_document(settings, validate_arguments=False)) if document.get('include') else plugins
    if not expanded or len(set(aliases.values())) != len(aliases) or any(
            old in expanded or new not in plugins or plugins[new].get('type') != _TAG_TYPES.get(old, 'sequence')
            for old, new in aliases.items()):
        return None
    canonical = deepcopy(document)
    by_tag = _plugins(canonical)
    for old, new in aliases.items():
        by_tag[new]['tag'] = old
        _rename(canonical, new, old)
    state = _analyze(canonical, settings, config)
    main_tag = aliases.get('sequence_main', 'sequence_main')
    main = plugins.get(main_tag, {})
    if 'custom_indices' in state:
        state['values']['custom_matches'] = [deepcopy(main['args'][index]) for index in state['custom_indices']]
    for field in _CUSTOM_FIELDS:
        state['capabilities'][field]['target'] = main_tag
    state['actual_main_tag'] = main_tag
    return state


def read_routing(document, settings, config):
    state = _analyze(document, settings, config)
    aliased = _aliased_custom_state(document, settings, config)
    if aliased is not None:
        for field in _CUSTOM_FIELDS:
            state['values'][field] = aliased['values'][field]
            state['capabilities'][field] = aliased['capabilities'][field]
    return {'values': deepcopy(state['values']), 'capabilities': deepcopy(state['capabilities'])}


def migrate_domain_only_routing(document, settings, config, base_url=''):
    """Render old managed IP routing as domain-only without changing the draft.

    Only a fully recognizable owned suffix is migrated. Hand-written providers,
    includes, custom pipeline shapes and their downloads remain untouched. The
    normal routing editor still exposes its lock reason for those documents.
    """
    from backend.common.oxidns_native import load_native_yaml
    # Publication expands includes first. Check the source too so an expanded
    # custom document is not mistaken for a wholly managed draft.
    source = load_native_yaml(settings['native_config']) if settings.get('native_config', '').strip() else {}
    if document.get('include') or source.get('include'):
        return document, settings
    current = _analyze(document, settings, config)
    if not current.get('groups', {}).get('ip') or not all(
            current['capabilities'][key]['editable'] for key in (*SELECTORS, 'rule_actions')):
        return document, settings
    # A recognizable routing suffix can still share a custom update pipeline.
    # Automatic export preserves such a draft atomically; an explicit editor
    # change continues to report the precise unsafe-update error to the user.
    try:
        rendered, values = apply_routing(document, settings, config, {}, base_url, before_config=config)
        # Empty owned executors cannot be emitted with an empty download list.
        import yaml
        from backend.common.oxidns_common import _Context
        from backend.common.oxidns_common_updates import finish_rule_updates
        values['native_config'] = yaml.safe_dump(rendered, allow_unicode=True, sort_keys=False)
        finish_rule_updates(_Context(rendered, values), {'direct_rulesets': []}, config, base_url)
        values['native_config'] = yaml.safe_dump(rendered, allow_unicode=True, sort_keys=False)
        return rendered, values
    except OxidnsSettingsError:
        return document, settings


def _uses_rule(document, config, identity):
    """Whether any domain/IP provider still holds a resource generated for identity."""
    atoms = _catalog(config)[1]
    for plugin in document.get('plugins') or []:
        part = {'domain_set': 'domain', 'ip_set': 'ip'}.get(plugin.get('type')) if isinstance(plugin, dict) else None
        args = plugin.get('args') if part else None
        for key, values in (args.items() if isinstance(args, dict) else ()):
            for value in values if isinstance(values, list) else ():
                atom = _atom(part, key, value)
                if atom is not None and identity in atoms.get(atom, ()):
                    return True
    return False


def remove_rule(settings, config, identity, base_url=''):
    """Native settings without a rule that is about to be deleted, or None if unused.

    Deleting only the stored selector would strand its providers and download
    entries in the native YAML, which then fails every later publication.
    """
    from backend.common.oxidns_common import apply_common_settings
    from backend.common.oxidns_native import load_native_yaml
    document = load_native_yaml(settings['native_config'])
    state = _analyze(document, settings, config)
    values = state['values']
    # A rule is routed by a selector list or by its own DNS action.
    changes = {key: [value for value in values[key] if value != identity]
               for key in SELECTORS if identity in values[key]}
    if identity in values['rule_actions']:
        changes['rule_actions'] = {key: value for key, value in values['rule_actions'].items() if key != identity}
    result = None
    if changes and all(state['capabilities'][key]['editable'] for key in changes):
        # Same path as unchecking it in the editor, which also reconciles rule
        # updates (an emptied downloader is parked rather than left invalid)
        # and drops helpers no other rule uses.
        result = apply_common_settings(settings, changes, config, base_url)['settings']
    elif not changes and not _uses_rule(document, config, identity):
        return None
    # Hand-written providers outside the recognized routing are never rewritten.
    if result is None or _uses_rule(load_native_yaml(result['native_config']), config, identity):
        raise OxidnsSettingsError('OxiDNS 还在用这条规则，而且那部分配置是手动写的，没法自动移除。'
                                  '请先在 OxiDNS 设置的 YAML 中删掉对它的引用，再删除。')
    return result


def _references(value, tag):
    if isinstance(value, str):
        return bool(re.search(r'(?<![\w.\-])\$?' + re.escape(tag) + r'(?![\w.\-])', value))
    if isinstance(value, dict):
        return any(_references(item, tag) for item in value.values())
    if isinstance(value, list):
        return any(_references(item, tag) for item in value)
    return False


def _new_name(preferred, occupied):
    result, index = preferred, 2
    while result in occupied:
        result = f'{preferred}_{index}'
        index += 1
    occupied.add(result)
    return result


def place_routing_groups(plugins, old, expected, external=frozenset()):
    """Tag and insert routing providers; return the new plugin list and groups.

    Rule sync replays draft routing edits with this too, so the deployed YAML
    stays byte-identical to the draft: unchanged providers keep tag and
    position, and fresh tags avoid the old ones still present at edit time.
    """
    occupied = {plugin.get('tag') for plugin in plugins}
    retained, additions = set(), []
    groups = {'domain': [], 'ip': []}
    for part, entries in expected.items():
        for group in entries:
            kind = 'domain_set' if part == 'domain' else 'ip_set'
            previous = next((candidate for candidate in old if candidate['tag'] not in retained and
                             candidate['provider']['type'] == kind and _same(candidate['args'], group['args'])), None)
            if previous:
                tag = previous['tag']
                retained.add(tag)
            else:
                tag = _new_name(group['tag'], occupied)
                additions.append({'tag': tag, 'type': kind, 'args': deepcopy(group['args'])})
            groups[part].append({**deepcopy(group), 'tag': tag})
    removable = {group['tag'] for group in old} - retained - set(external)
    insertion = min((index for index, plugin in enumerate(plugins) if plugin.get('tag') in removable),
                    default=next(i for i, plugin in enumerate(plugins) if plugin.get('tag') == 'sequence_main'))
    placed = []
    for index, plugin in enumerate(plugins):
        if index == insertion:
            placed.extend(additions)
        if plugin.get('tag') not in removable:
            placed.append(plugin)
    return placed, groups


def reload_references(existing, old_tags, new_tags, plugins):
    """Edit a reload list in place: retained entries keep their order.

    The editor and rule sync both apply this to the same deployed list, so
    their output stays byte-identical; new providers are appended.
    """
    references = []
    for value in [*existing, *(f'${tag}' for tag in new_tags)]:
        tag = value.removeprefix('$') if isinstance(value, str) else None
        if value not in references and (tag is None or tag in new_tags or tag not in old_tags or tag in plugins):
            references.append(value)
    return references


def resource_downloads(document, settings, config, base_url=''):
    """Download descriptors for actual provider references, without core validation."""
    referenced = {relative_resource(path) for path in resource_references(document)}
    effective = _base(config, base_url)
    desired = []
    for record in _catalog(config)[0].values():
        if record['suffix'] != 'rulesets':
            continue
        for part in sorted(record['parts']):
            local_path = _rule_path(record['item'], part)
            if relative_resource(local_path) not in referenced:
                continue
            url = record['item'].get('url', '')
            if not isinstance(url, str) or not url:
                raise OxidnsSettingsError('已选择的规则集缺少下载来源。')
            if url.startswith('/'):
                url = effective + url
            desired.append({'url': _build_rule_proxy_url(config, effective, url, part=part),
                            'dir': str(PurePosixPath(local_path).parent),
                            'filename': PurePosixPath(local_path).name})
    adguard = settings.get('adguard')
    urls = adguard.get('urls') if isinstance(adguard, dict) else None
    for url in urls if isinstance(urls, list) else []:
        if not isinstance(url, str):
            continue
        local_path = f'./rules/{_tag("adguard", url)}.txt'
        if relative_resource(local_path) in referenced:
            desired.append({'url': url, 'dir': str(PurePosixPath(local_path).parent),
                            'filename': PurePosixPath(local_path).name})
    return desired


def resource_readers(document):
    """Conservative resource consumers, including unknown plugin fields."""
    def strings(value):
        if isinstance(value, str):
            if '/' in value or '${' in value or any(char in value for char in '*?['):
                yield value
        elif isinstance(value, dict):
            for item in value.values(): yield from strings(item)
        elif isinstance(value, list):
            for item in value: yield from strings(item)
    return list(strings({**{key: value for key, value in document.items() if key != 'plugins'},
                         'plugins': [p for p in document.get('plugins', []) if p.get('type') != 'download']}))


def resource_is_read(path, readers):
    for reference in readers:
        if '${' in reference or fnmatchcase(path, reference.removeprefix('./')) or reference.endswith('/' + path):
            return True
        # Absolute patterns may refer to the same resource under the target's
        # installation directory, whose prefix is not known while editing.
        if reference.startswith('/') and any(fnmatchcase(path, reference[index + 1:])
                                              for index, char in enumerate(reference) if char == '/'):
            return True
    return False


def replace_download_entries(download, entries):
    """Detach edited args from other YAML alias consumers, including entries.

    Copying the whole document preserves aliases between its plugins. Rebind
    only this consumer so custom downloaders keep their original parameters.
    """
    download['args'] = deepcopy({**download['args'], 'downloads': entries})


def _own_helpers(document, routes, actions, entries=()):
    """Create, keep, refresh or remove the owned helper plugins in place.

    Helpers needed by routes are created before sequence_main, or replaced when
    an owned pinned chain only differs by features. A same-named plugin that is
    not a recognized helper is never overwritten. A helper nothing else uses any
    more is removed; one a custom match or another plugin still uses is kept.
    entries are the rule group entries just written, which are not "elsewhere".
    """
    plugins = document['plugins']
    by_tag = _plugins(document)
    desired = helper_plugins(routes, actions, chain_features(by_tag))
    wanted = {plugin['tag']: plugin for plugin in desired}
    owned = {id(entry) for entry in entries}
    outside = [{**plugin, 'args': [item for item in plugin['args'] if id(item) not in owned]}
               if plugin.get('tag') == 'sequence_main' and isinstance(plugin.get('args'), list) else plugin
               for plugin in plugins if not _HELPER_TAG.fullmatch(str(plugin.get('tag')))]
    top = {key: value for key, value in document.items() if key != 'plugins'}

    def external(tag):
        return _references(top, tag) or any(_references(plugin, tag) for plugin in outside)

    def replaceable(tag):
        key = _HELPER_TAG.fullmatch(tag)[1]
        action = pinned_action(key, by_tag) if key else None
        if action is None:
            return False
        trio = [prefix + key for prefix in ('cf_dns_up_', 'cf_dns_fwd_', 'cf_dns_cache_')]
        # Another address under this tag (edited by hand) is replaced only if unused elsewhere.
        return route_key(action) == 'upstream:' + key or not any(external(item) for item in trio)

    for tag, plugin in wanted.items():
        existing = by_tag.get(tag)
        if existing is not None and not _same(existing, plugin) and not replaceable(tag):
            raise OxidnsSettingsError(f'已有同名插件 {tag}，无法创建 DNS 处理所需插件，请先在完整参数中改名')
    kept = set(wanted) | {tag for tag in by_tag if _HELPER_TAG.fullmatch(tag) and external(tag)}
    # A kept chain keeps the forward and cache it calls.
    pending = list(kept)
    while pending:
        tag = pending.pop()
        source = wanted.get(tag, by_tag.get(tag))
        for other in by_tag:
            if other not in kept and _HELPER_TAG.fullmatch(other) and _references(source, other):
                kept.add(other)
                pending.append(other)
    result = []
    for plugin in plugins:
        tag = plugin.get('tag')
        if tag in wanted:
            result.append(deepcopy(wanted[tag]))
        elif not _HELPER_TAG.fullmatch(str(tag)) or tag in kept:
            result.append(plugin)
    for plugin in desired:
        if plugin['tag'] in by_tag:
            continue
        # Before the first helper that uses it, else before the main sequence.
        index = next((i for i, item in enumerate(result) if item.get('tag') in wanted and
                      item.get('tag') != plugin['tag'] and _references(wanted[item['tag']], plugin['tag'])),
                     next((i for i, item in enumerate(result) if item.get('tag') == 'sequence_main'), len(result)))
        result.insert(index, deepcopy(plugin))
    plugins[:] = result


def owned_chains(document):
    """{chain tag: action} of the pinned upstream chains whose tag still names
    their content; refresh_rule_helpers keeps exactly these in sync."""
    plugins = _plugins(document)
    result = {}
    for tag in plugins:
        key = tag[len(UPSTREAM_PREFIX):] if tag.startswith(UPSTREAM_PREFIX) else None
        action = pinned_action(key, plugins) if key else None
        if action is not None and route_key(action) == 'upstream:' + key:
            result[tag] = action
    return result


def refresh_rule_helpers(document):
    """Rebuild owned pinned upstream chains for the YAML's current IP selection,
    RouterOS, cache and TTL features. Chains edited by hand are left alone."""
    actions = owned_chains(document)
    if actions:
        _own_helpers(document, [route_key(action) for action in actions.values()], actions)


def _sync_updates(document, settings, config, old_groups, new_groups, base_url, before_config=None):
    plugins = _plugins(document)
    download = plugins.get('rules_download')
    reload = plugins.get('rules_reload')
    if download is None and reload is None:
        return
    parked = settings.get('_common_disabled', {})
    parked_reload = parked.get('rules_reload') if isinstance(parked, dict) else None
    reload_parked = reload is None and isinstance(parked_reload, dict) and \
        parked_reload.get('tag') == 'rules_reload' and parked_reload.get('type') == 'reload_provider' and \
        isinstance(parked_reload.get('args'), list)
    if download is None or download.get('type') != 'download' or \
            not isinstance(download.get('args'), dict) or not isinstance(download['args'].get('downloads'), list) or \
            (not reload_parked and (reload is None or reload.get('type') != 'reload_provider' or not isinstance(reload.get('args'), list))):
        raise OxidnsSettingsError('规则定时更新包含自定义结构，请先在完整参数中调整下载与重载任务。')
    old_tags = {group['tag'] for entries in old_groups.values() for group in entries}
    new_tags = [group['tag'] for entries in new_groups.values() for group in entries]
    # Old providers retained for another caller also remain reloadable.
    if reload is not None:
        reload['args'] = reload_references(reload['args'], old_tags, new_tags, plugins)
    # Downloads of rulesets that left the catalog (deleted or disabled) are owned too.
    records = list(_catalog(config)[0].values())
    if before_config is not None:
        records.extend(_catalog(before_config)[0].values())
    known_paths = {_rule_path(record['item'], part).removeprefix('./')
                   for record in records if record['suffix'] == 'rulesets'
                   for part in record['parts']}
    desired = resource_downloads(document, settings, config, base_url)
    def path(entry):
        if not isinstance(entry, dict) or not isinstance(entry.get('dir'), str) or not isinstance(entry.get('filename'), str):
            return None
        return (entry['dir'].rstrip('/') + '/' + entry['filename']).removeprefix('./')
    desired_entries = {}
    for item in desired:
        local = path(item)
        if local in known_paths:
            desired_entries[local] = item
    output = []
    readers = resource_readers(document)
    for entry in download['args']['downloads']:
        location = path(entry)
        if location not in known_paths:
            output.append(entry)
        elif location in desired_entries:
            # Preserve per-download extensions and explicit URL overrides.
            output.append(entry)
            desired_entries.pop(location)
        elif resource_is_read(location, readers):
            output.append(entry)
    output.extend(desired_entries.values())
    replace_download_entries(download, output)


def _name(item):
    return item.get('name') or item.get('value') or item.get('id')


def _route_label(route):
    return ROUTE_LABELS[route.split(':', 1)[0]]


def _assign(values, item, action):
    """Route one active rule by a stored choice; direct/proxy live in the lists."""
    action = normalize_action(action, parked=True)
    if action['action'] in LIST_ROUTES:
        values[f'{action["action"]}_{_suffix(item)}'].append(item['id'])
    else:
        values['rule_actions'][item['id']] = action


def _validate_selections(values, changes, known):
    """Shape and existence of changed selections. Ids still in the space but not
    active are accepted (and parked); ids that no longer exist are refused."""
    for key in SELECTORS:
        if not isinstance(values[key], list) or any(not isinstance(value, str) for value in values[key]):
            raise OxidnsSettingsError(f'{key} 必须为规则 ID 列表')
        values[key] = list(dict.fromkeys(values[key]))
        suffix = 'rulesets' if key.endswith('rulesets') else 'rules'
        if key in changes and any(identity not in known or _suffix(known[identity]) != suffix
                                  for identity in values[key]):
            raise OxidnsSettingsError(_MISSING_RULE)
    if 'rule_actions' in changes:
        actions = values['rule_actions']
        if not isinstance(actions, dict) or any(not isinstance(identity, str) for identity in actions):
            raise OxidnsSettingsError('rule_actions 必须为以规则 ID 为键的对象')
        if any(identity not in known for identity in actions):
            raise OxidnsSettingsError(_MISSING_RULE)
        values['rule_actions'] = {identity: normalize_action(action)
                                  for identity, action in actions.items() if action is not None}
    if 'rule_dns_follow' in changes:
        follow = values['rule_dns_follow']
        if not isinstance(follow, list) or any(not isinstance(identity, str) for identity in follow):
            raise OxidnsSettingsError('rule_dns_follow 必须为规则 ID 数组')
        if any(identity not in known for identity in follow):
            raise OxidnsSettingsError(_MISSING_RULE)
        values['rule_dns_follow'] = list(dict.fromkeys(follow))
    for suffix in ('rules', 'rulesets'):
        if set(values['direct_' + suffix]) & set(values['proxy_' + suffix]):
            raise OxidnsSettingsError('同一规则不能同时选择直连和代理。')


def _take_over(values, current, changes, known, notices):
    """A changed key wins over the other kind of choice it newly takes a rule
    from: a selector list over rule_actions, and rule_actions over a list."""
    old, taken = current['values'], []
    if 'rule_actions' in changes:
        for identity, action in values['rule_actions'].items():
            if identity in old['rule_actions']:
                continue
            for key in SELECTORS:
                if identity in values[key]:
                    if key in changes:
                        raise OxidnsSettingsError('同一规则不能同时选择多种 DNS 处理。')
                    values[key].remove(identity)
                    taken.append((identity, key.split('_')[0], route_key(action)))
    for key in SELECTORS:
        for identity in values[key] if key in changes else ():
            if identity in old[key] or identity not in values['rule_actions']:
                continue
            if 'rule_actions' in changes:
                raise OxidnsSettingsError('同一规则不能同时选择多种 DNS 处理。')
            taken.append((identity, route_key(values['rule_actions'].pop(identity)), key.split('_')[0]))
    if notices is not None:
        notices.extend(f'规则「{_name(known[identity])}」的 DNS 处理已从「{_route_label(before)}」改为「{_route_label(after)}」'
                       for identity, before, after in taken)


def _settle(values, settings, changes, current, known, active, routes):
    """Keep only active rules in the selections; return (parked, follow).

    Choices of rules that left the active catalog are parked, parked choices of
    rules that rejoined are restored, and follow rules that rejoined get their
    policy's route. Follow rules are never parked: the policy decides again.
    """
    follow_given = 'rule_dns_follow' in changes
    follow = [identity for identity in values['rule_dns_follow'] if identity in known]
    stored = settings.get('rule_dns_parked')
    parked = {identity: deepcopy(action) for identity, action in stored.items() if identity in known} \
        if isinstance(stored, dict) else {}
    assigned = {identity for key in SELECTORS if key in changes for identity in values[key]}
    if 'rule_actions' in changes:
        assigned.update(values['rule_actions'])
    leaving = {}
    for key in SELECTORS:
        leaving.update((identity, {'action': key.split('_')[0]}) for identity in values[key] if identity not in active)
        values[key] = [identity for identity in values[key] if identity in active]
    leaving.update((identity, action) for identity, action in values['rule_actions'].items() if identity not in active)
    values['rule_actions'] = {identity: action for identity, action in values['rule_actions'].items() if identity in active}
    for identity, action in leaving.items():
        if identity in known and (identity not in follow or identity in assigned):
            parked[identity] = action
            if identity in follow and not follow_given:
                follow.remove(identity)
    before = current['records']
    for identity, record in active.items():
        if _route_of(values, identity) is not None:
            parked.pop(identity, None)
        elif identity in parked:
            _assign(values, record['item'], parked.pop(identity))
        elif identity in follow and identity not in before:
            route = follow_route(record['item'], routes=routes)
            if route is not None:
                _assign(values, record['item'], {'action': route})
    if not follow_given:
        # Follow intent ends for a rule active before and after whose actual
        # route this write changed, and for a rule with an explicit action.
        follow = [identity for identity in follow if identity not in values['rule_actions'] and not (
            identity in before and identity in active and
            _route_of(current['values'], identity) != _route_of(values, identity))]
    for identity in follow:
        if identity in values['rule_actions']:
            label = ROUTE_LABELS[values['rule_actions'][identity]['action']]
            raise OxidnsSettingsError(f'规则 {identity} 已设置 DNS 处理「{label}」，不能同时跟随策略')
        if identity in parked:
            raise OxidnsSettingsError(f'规则 {identity} 已暂存停用前的 DNS 处理，不能同时跟随策略')
    return parked, follow


def _normalize_custom_matches(value):
    if not isinstance(value, list):
        raise OxidnsSettingsError('自定义匹配必须为列表。')
    normalized = []
    for entry in value:
        if not isinstance(entry, dict):
            raise OxidnsSettingsError('每条自定义匹配必须为对象。')
        if entry.get('enabled') is False:
            raise OxidnsSettingsError('常用设置直接编辑执行中的匹配条目，不支持 enabled: false；请移除该条目或改为启用。')
        match = _matches(entry)
        if not isinstance(match, list) or not match or any(not isinstance(item, str) or not item.strip() for item in match) or \
                not isinstance(entry.get('exec'), str) or not entry['exec'].strip():
            raise OxidnsSettingsError('自定义匹配需填写非空条件与执行动作。')
        normalized.append({key: deepcopy(item) for key, item in entry.items() if key != 'enabled'})
    return normalized


def _apply_aliased_custom(document, settings, state, changes):
    for field in changes:
        if not state['capabilities'][field]['editable']:
            raise OxidnsSettingsError(state['capabilities'][field]['reason'])
    position = changes.get('custom_match_position', state['values']['custom_match_position'] or 'tail')
    if position not in ('head', 'tail'):
        raise OxidnsSettingsError('自定义匹配位置必须为 head 或 tail。')
    custom = _normalize_custom_matches(changes['custom_matches']) if 'custom_matches' in changes else \
        deepcopy(state['values']['custom_matches'])
    result = deepcopy(document)
    main = _plugins(result)[state['actual_main_tag']]
    args = main['args']
    prefix_end = len(state['prefix'])
    middle_end = prefix_end + len(state['custom_indices']) + len(state['groups']['domain'])
    # Indexes come from a read-only recognition copy. All retained entries come
    # from the original document, including renamed actions and future fields.
    domain = [args[group['index']] for group in state['groups']['domain']]
    main['args'] = [*args[:prefix_end], *(custom if position == 'head' else []), *domain,
                    *(custom if position == 'tail' else []), *args[middle_end:]]
    return result, deepcopy(settings)


def apply_routing(document, settings, config, changes, base_url='', *, before_config=None, notices=None):
    """Apply one atomic routing edit to copies; never persist or regenerate all YAML.

    before_config is the rule space the document was written for. Passing it
    rebuilds the owned routing for config even without changes, so reordering,
    disabling, enabling or editing rules never strands the routing; a document
    that cannot be rebuilt raises its lock reason and callers decide whether
    that is fatal. notices, when a list, receives a line for each rule a changed
    key took from another kind of DNS choice.
    """
    changes = {key: value for key, value in changes.items() if key in ROUTING_FIELDS}
    rebuild = before_config is not None
    if not changes and not rebuild:
        return deepcopy(document), deepcopy(settings)
    if not rebuild and changes.keys() & _CUSTOM_FIELDS:
        aliased = _aliased_custom_state(document, settings, config)
        if aliased is not None:
            if changes.keys() <= _CUSTOM_FIELDS:
                return _apply_aliased_custom(document, settings, aliased, changes)
            raise OxidnsSettingsError('执行链已重命名，请单独调整自定义匹配，再编辑其他分流设置。')
    current = _analyze(document, settings, before_config if rebuild else config)
    for key in changes:
        cap = current['capabilities'][key]
        if not cap['editable']:
            raise OxidnsSettingsError(cap['reason'])
    if rebuild and not current['capabilities']['direct_rules']['editable']:
        raise OxidnsSettingsError(current['capabilities']['direct_rules']['reason'])
    values = {**deepcopy(current['values']), **deepcopy(changes)}
    if values['default_forward'] not in ('forward_local', 'forward_remote'):
        raise OxidnsSettingsError('默认转发目标无效。')
    migrate_ip = bool(current.get('groups', {}).get('ip')
                      and current['capabilities']['direct_rules']['editable'])
    if not rebuild and not migrate_ip and changes.keys() <= {'default_forward'}:
        result = deepcopy(document)
        args = _plugins(result)['sequence_main']['args']
        direct = values['default_forward'] == 'forward_local'
        target = 'china_dns' if direct else 'proxy_dns_seq'
        if _plugins(result).get(target, {}).get('type') != 'sequence':
            raise OxidnsSettingsError('目标分流执行链未配置，请在完整参数中添加后再切换。')
        args[-1]['exec'] = 'goto ' + target
        ip_groups = current['groups']['ip']
        if ip_groups:
            # The fallback also decides whether the probe is written; rewrite only
            # the IP block, so an unrecognized custom middle stays untouched.
            first = min(group['index'] for group in ip_groups)
            start = first - 1 if first and args[first - 1] == {'exec': 'jump local_probe'} else first
            probe = probe_needed([group['route'] for group in ip_groups], values['default_forward'])
            if probe:
                for helper in ('local_probe', *(('default_direct_ip',) if direct else ())):
                    if _plugins(result).get(helper, {}).get('type') != 'sequence':
                        raise OxidnsSettingsError(f'分流执行链 {helper} 未配置，请在完整参数中添加后再调整。')
            args[start:len(args) - 1] = _ip_suffix([args[group['index']] for group in ip_groups],
                                                   values['default_forward'], probe)
        return result, deepcopy(settings)
    known = _known(config)
    active = {identity: record for identity, record in
              (_catalog(config)[0] if rebuild else current['records']).items()
              if convertible(record['item'])}
    _validate_selections(values, changes, known)
    _take_over(values, current, changes, known, notices)
    parked, follow = _settle(values, settings, changes, current, known, active, policy_dns_routes(config))
    if 'aggregate_rules' in changes and type(values['aggregate_rules']) is not bool:
        raise OxidnsSettingsError('合并相邻规则必须为布尔值。')
    if 'custom_match_position' in changes and values['custom_match_position'] not in ('head', 'tail'):
        raise OxidnsSettingsError('自定义匹配执行顺序必须为 head 或 tail。')
    if 'custom_matches' in changes:
        values['custom_matches'] = _normalize_custom_matches(values['custom_matches'])
    result, result_settings = deepcopy(document), deepcopy(settings)
    main = _plugins(result)['sequence_main']
    routing_changed = rebuild or migrate_ip or any(key in changes for key in (*SELECTORS, 'aggregate_rules', 'rule_actions'))
    if routing_changed:
        # There is no actual aggregate setting until adjacent same-route rules
        # exist. New rule selections use the existing grouped representation, or
        # the compact equivalent when the previous document could mean either.
        if values['aggregate_rules'] is None:
            values['aggregate_rules'] = True
        expected = _routing_groups(config, values)
        old = [group for entries in current['groups'].values() for group in entries]
        external = {group['tag'] for group in old if
                    set(group['provider']) - {'tag', 'type', 'args'} or
                    document.get('include') or
                    _references({key: value for key, value in document.items() if key != 'plugins'}, group['tag']) or
                    _references(values['custom_matches'], group['tag']) or
                    any(_references(plugin, group['tag']) for plugin in result['plugins']
                        if plugin.get('tag') not in ('sequence_main', 'rules_reload', group['tag']))}
        result['plugins'], groups = place_routing_groups(result['plugins'], old, expected, external)
        for key in SELECTORS:
            previous = result_settings.get(key)
            result_settings[key] = deepcopy(previous) if isinstance(previous, list) else []
            for identity in values[key]:
                if identity not in result_settings[key]:
                    result_settings[key].append(identity)
        # Unlike the retained list catalog, actions map exactly the active rules.
        result_settings['rule_actions'] = deepcopy(values['rule_actions'])
        result_settings['rule_dns_parked'] = parked
    else:
        groups = current['groups']
    if routing_changed or 'rule_dns_follow' in changes:
        result_settings['rule_dns_follow'] = follow
    # Build only the identified sequence regions, retaining all surrounding nodes.
    prefix = deepcopy(current['prefix'])
    custom = deepcopy(values['custom_matches'])
    position = values['custom_match_position'] or 'tail'
    domain = [deepcopy(group['entry']) if not routing_changed else
              {'matches': [f'qname ${group["tag"]}'], 'exec': route_exec(group['route'], 'domain')}
              for group in groups['domain']]
    ip = [deepcopy(group['entry']) if not routing_changed else
          {'matches': [f'resp_ip ${group["tag"]}'], 'exec': route_exec(group['route'], 'ip')}
          for group in groups['ip']]
    final = deepcopy(current['final'])
    final['exec'] = 'goto china_dns' if values['default_forward'] == 'forward_local' else 'goto proxy_dns_seq'
    suffix = deepcopy(current['dynamic'])
    probe = probe_needed([group['route'] for group in groups['ip']], values['default_forward'])
    suffix.extend(_ip_suffix(ip, values['default_forward'], probe))
    # Do not create a new jump into an absent helper sequence in a partial native
    # document. Existing unrelated custom references remain untouched.
    affected = [final] if 'default_forward' in changes else []
    if routing_changed:
        affected.extend(domain + ip)
    if ip and probe and (routing_changed or 'default_forward' in changes):
        affected.append({'exec': 'jump local_probe'})
        if values['default_forward'] == 'forward_local':
            affected.append({'exec': 'goto default_direct_ip'})
    available = _plugins(result)
    for entry in affected:
        verb, _, target = entry['exec'].partition(' ')
        # Rejections need no target; owned helpers are created below.
        if verb not in ('goto', 'jump') or target.startswith(UPSTREAM_PREFIX):
            continue
        if available.get(target, {}).get('type') != 'sequence':
            raise OxidnsSettingsError(f'分流执行链 {target} 未配置，请在完整参数中添加后再调整。')
    main['args'] = [*prefix, *(custom if position == 'head' else []), *domain,
                    *(custom if position == 'tail' else []), *suffix, final]
    if routing_changed:
        _own_helpers(result, [group['route'] for part in ('domain', 'ip') for group in groups[part]],
                     values['rule_actions'], domain + ip)
        _sync_updates(result, result_settings, config, current['groups'], groups, base_url, before_config)
    return result, result_settings
