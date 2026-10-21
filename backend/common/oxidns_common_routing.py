"""Read and edit the recognizable routing portion of an OxiDNS document.

The native providers and sequence are the source of truth. Historical template
inputs are only retained as resource sources; they never supply displayed values.
Unrecognized routing is left for the complete editor instead of being regenerated.
"""
from copy import deepcopy
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
import re

from backend.common.oxidns_settings import BLOCK_QTYPES_EXECS, OxidnsSettingsError
from backend.common.dns_watchdog import WATCHDOG_RULE
from backend.common.oxidns_native import resource_references, relative_resource
from backend.converters.oxidns import _items, _routing_groups, _rule_path, _base, _tag
from backend.converters.mosdns import _build_rule_proxy_url

SELECTORS = ('direct_rules', 'proxy_rules', 'direct_rulesets', 'proxy_rulesets')
ROUTING_FIELDS = frozenset((*SELECTORS, 'aggregate_rules', 'default_forward',
                           'custom_matches', 'custom_match_position'))
_TARGETS = {'domain': {'goto china_dns': 'direct', 'goto proxy_dns_seq': 'proxy'},
            'ip': {'goto direct_ip': 'direct', 'goto ip_requery_proxy': 'proxy'}}
_REASON = '当前分流执行链包含自定义结构，请在完整参数中编辑。'


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
            groups = _routing_groups(config, settings)
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
        records[identity] = {'suffix': suffix, 'parts': parts, 'item': item}
    return records, atoms


def _describe(group):
    return {'route': group['route'], 'args': group['args']}


def _analyze(document, settings, config):
    values = {key: [] for key in SELECTORS}
    values.update(aggregate_rules=None, default_forward=None, custom_matches=[], custom_match_position=None)
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
            route = _TARGETS[part].get(entry['exec'])
            if route and provider.get('type') == ('domain_set' if part == 'domain' else 'ip_set'):
                group = {'index': index, 'tag': parsed[2], 'route': route,
                         'args': provider.get('args'), 'entry': entry, 'provider': provider}
                groups[part].append(group)
                if part == 'ip':
                    middle_end = min(middle_end, index)
                continue
        if not entry.get('matches') and entry['exec'] == 'jump local_probe':
            middle_end = min(middle_end, index)
    if len(dynamic) > 1:
        return state
    # The generated suffix has a single probe, ordered IP routes, and one final fallback.
    suffix = []
    if dynamic:
        suffix.append(dynamic[0][1])
    if groups['ip']:
        suffix.append({'exec': 'jump local_probe'})
        suffix.extend(group['entry'] for group in groups['ip'])
        suffix.append({'matches': ['has_resp'], 'exec': 'goto default_direct_ip'} if values['default_forward'] == 'forward_local'
                      else {'exec': 'drop_resp'})
    suffix.append(final)
    # Extensions on IP clauses are preserved by direct edits, but regeneration of
    # these owned clauses is refused below. The probe/fallback wrappers must be exact.
    if not _same(args[middle_end:], suffix):
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
            '请先添加自定义匹配，再选择前后位置。' if not custom else
            '当前没有域名分流规则，前后位置效果相同。' if not domain_indices else '')
    else:
        recognized = False
    assigned = {}
    for part, entries in groups.items():
        for group in entries:
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
                        continue
                    identity = next(iter(candidates))
                    previous = assigned.setdefault(identity, {})
                    if part in previous:
                        recognized = False
                    previous[part] = group['route']
    for identity, parts in assigned.items():
        record = records[identity]
        routes = set(parts.values())
        if set(parts) != record['parts'] or len(routes) != 1:
            recognized = False
            continue
        values[next(iter(routes)) + '_' + record['suffix']].append(identity)
    # Reconstruct profile order instead of deriving it from stale selection metadata.
    for key in SELECTORS:
        selected = set(values[key])
        values[key] = [identity for identity in records if identity in selected]
    matches = []
    if recognized:
        for aggregate in (True, False):
            expected = _routing_groups(config, {**values, 'aggregate_rules': aggregate})
            if all(_same([_describe(group) for group in groups[part]], [_describe(group) for group in expected[part]])
                   for part in groups):
                matches.append(aggregate)
        recognized = bool(matches)
    if recognized:
        values['aggregate_rules'] = matches[0] if len(matches) == 1 else None
        for key in SELECTORS:
            caps[key] = _cap()
        caps['aggregate_rules'] = _cap(len(matches) == 1,
            '当前没有相邻且同策略的规则，无需合并。' if len(matches) == 2 else '')
    else:
        for key in (*SELECTORS, 'aggregate_rules'):
            caps[key] = _cap(False, '规则资源或顺序无法唯一对应当前规则库，请在完整参数中编辑。')
    state.update(plugins=plugins, main=main, groups=groups, prefix=args[:prefix_end],
                 dynamic=[entry for _, entry in dynamic], custom=custom, final=final,
                 records=records)
    return state


def read_routing(document, settings, config):
    state = _analyze(document, settings, config)
    return {'values': deepcopy(state['values']), 'capabilities': deepcopy(state['capabilities'])}


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


def _sync_updates(document, settings, config, old_groups, new_groups, base_url):
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
    references = [f'${tag}' for tag in new_tags]
    # Old providers retained for another caller also remain reloadable.
    references.extend(value for value in (reload['args'] if reload else []) if value not in references and
                      (not isinstance(value, str) or value.removeprefix('$') not in old_tags or value.removeprefix('$') in plugins))
    if reload is not None:
        reload['args'] = references
    records = _catalog(config)[0]
    known_paths = {_rule_path(record['item'], part).removeprefix('./')
                   for record in records.values() if record['suffix'] == 'rulesets'
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


def apply_routing(document, settings, config, changes, base_url=''):
    """Apply one atomic routing edit to copies; never persist or regenerate all YAML."""
    changes = {key: value for key, value in changes.items() if key in ROUTING_FIELDS}
    if not changes:
        return deepcopy(document), deepcopy(settings)
    current = _analyze(document, settings, config)
    for key in changes:
        cap = current['capabilities'][key]
        if not cap['editable']:
            raise OxidnsSettingsError(cap['reason'])
    values = {**deepcopy(current['values']), **deepcopy(changes)}
    if values['default_forward'] not in ('forward_local', 'forward_remote'):
        raise OxidnsSettingsError('默认转发目标无效。')
    if changes.keys() <= {'default_forward'}:
        result = deepcopy(document)
        args = _plugins(result)['sequence_main']['args']
        direct = values['default_forward'] == 'forward_local'
        target = 'china_dns' if direct else 'proxy_dns_seq'
        if _plugins(result).get(target, {}).get('type') != 'sequence':
            raise OxidnsSettingsError('目标分流执行链未配置，请在完整参数中添加后再切换。')
        args[-1]['exec'] = 'goto ' + target
        if current['groups']['ip']:
            args[-2] = {'matches': ['has_resp'], 'exec': 'goto default_direct_ip'} if direct else {'exec': 'drop_resp'}
        return result, deepcopy(settings)
    for key in SELECTORS:
        if not isinstance(values[key], list) or any(not isinstance(value, str) for value in values[key]):
            raise OxidnsSettingsError(f'{key} 必须为规则 ID 列表')
        values[key] = list(dict.fromkeys(values[key]))
        suffix = 'rulesets' if key.endswith('rulesets') else 'rules'
        if any(identity not in current['records'] or current['records'][identity]['suffix'] != suffix for identity in values[key]):
            raise OxidnsSettingsError('所选规则不在当前空间的可用规则库中，请刷新后重试。')
    for suffix in ('rules', 'rulesets'):
        if set(values['direct_' + suffix]) & set(values['proxy_' + suffix]):
            raise OxidnsSettingsError('同一规则不能同时选择直连和代理。')
    if 'aggregate_rules' in changes and type(values['aggregate_rules']) is not bool:
        raise OxidnsSettingsError('合并相邻规则必须为布尔值。')
    if 'custom_match_position' in changes and values['custom_match_position'] not in ('head', 'tail'):
        raise OxidnsSettingsError('自定义匹配位置必须为 head 或 tail。')
    if 'custom_matches' in changes:
        if not isinstance(values['custom_matches'], list):
            raise OxidnsSettingsError('自定义匹配必须为列表。')
        normalized = []
        for entry in values['custom_matches']:
            if not isinstance(entry, dict):
                raise OxidnsSettingsError('每条自定义匹配必须为对象。')
            if entry.get('enabled') is False:
                raise OxidnsSettingsError('常用设置直接编辑执行中的匹配条目，不支持 enabled: false；请移除该条目或改为启用。')
            match = _matches(entry)
            if not isinstance(match, list) or not match or any(not isinstance(value, str) or not value.strip() for value in match) or \
                    not isinstance(entry.get('exec'), str) or not entry['exec'].strip():
                raise OxidnsSettingsError('自定义匹配需填写非空条件与执行动作。')
            normalized.append({key: deepcopy(value) for key, value in entry.items() if key != 'enabled'})
        values['custom_matches'] = normalized
    result, result_settings = deepcopy(document), deepcopy(settings)
    main = _plugins(result)['sequence_main']
    routing_changed = any(key in changes for key in (*SELECTORS, 'aggregate_rules'))
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
    else:
        groups = current['groups']
    # Build only the identified sequence regions, retaining all surrounding nodes.
    prefix = deepcopy(current['prefix'])
    custom = deepcopy(values['custom_matches'])
    position = values['custom_match_position'] or 'tail'
    domain = [deepcopy(group['entry']) if not routing_changed else
              {'matches': [f'qname ${group["tag"]}'], 'exec': 'goto china_dns' if group['route'] == 'direct' else 'goto proxy_dns_seq'}
              for group in groups['domain']]
    ip = [deepcopy(group['entry']) if not routing_changed else
          {'matches': [f'resp_ip ${group["tag"]}'], 'exec': 'goto direct_ip' if group['route'] == 'direct' else 'goto ip_requery_proxy'}
          for group in groups['ip']]
    final = deepcopy(current['final'])
    final['exec'] = 'goto china_dns' if values['default_forward'] == 'forward_local' else 'goto proxy_dns_seq'
    suffix = deepcopy(current['dynamic'])
    if ip:
        suffix.extend([{'exec': 'jump local_probe'}, *ip,
                       {'matches': ['has_resp'], 'exec': 'goto default_direct_ip'} if values['default_forward'] == 'forward_local'
                       else {'exec': 'drop_resp'}])
    # Do not create a new jump into an absent helper sequence in a partial native
    # document. Existing unrelated custom references remain untouched.
    affected = [final] if 'default_forward' in changes else []
    if routing_changed:
        affected.extend(domain + ip)
        if ip:
            affected.append({'exec': 'jump local_probe'})
            if values['default_forward'] == 'forward_local':
                affected.append({'exec': 'goto default_direct_ip'})
    available = _plugins(result)
    for entry in affected:
        action = entry['exec']
        target = action.split(' ', 1)[1]
        if available.get(target, {}).get('type') != 'sequence':
            raise OxidnsSettingsError(f'分流执行链 {target} 未配置，请在完整参数中添加后再调整。')
    main['args'] = [*prefix, *(custom if position == 'head' else []), *domain,
                    *(custom if position == 'tail' else []), *suffix, final]
    if routing_changed:
        _sync_updates(result, result_settings, config, current['groups'], groups, base_url)
    return result, result_settings
