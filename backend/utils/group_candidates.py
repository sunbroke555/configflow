"""Managed group membership shared by write validation and Mihomo rendering.

Engine-native use/include-all are deliberately not managed sources. Legacy
empty records remain readable; new writes must have a consumed source.
"""
from copy import deepcopy
import hashlib
import re

class CandidateValidationError(ValueError):
    """Controlled write-validation error, distinct from repository conflicts."""


SOURCE_FIELDS = ('manual_nodes', 'subscriptions', 'aggregations', 'include_groups',
                 'follow_group', 'source', 'proxies', 'proxies_order')
HEALTH_FIELDS = ('url', 'interval', 'timeout', 'lazy', 'expected-status', 'tolerance')
FILTER_FIELDS = ('regex', 'aggregation_regex', 'exclude_regex', 'aggregation_exclude_regex')


def filter_candidates(items, include='', exclude=''):
    """Apply name filters without changing source records or their ordering."""
    patterns = []
    for value in (include, exclude):
        if value is not None and not isinstance(value, str):
            raise CandidateValidationError('正则筛选必须是字符串或 null')
        try:
            patterns.append(re.compile(value.strip()) if value and value.strip() else None)
        except re.error as exc:
            raise CandidateValidationError('正则表达式无效') from exc
    keep, remove = patterns
    return [item for item in items
            if (keep is None or keep.search(item.get('name', '')))
            and (remove is None or not remove.search(item.get('name', '')))]


def _filter_value(group, field):
    value = group.get(field)
    return value.strip() if isinstance(value, str) else ''


def managed_providers(group, config):
    """Return provider resources together with their source-specific filters."""
    selection = managed_members(group)
    aggregation_ids = list(selection['aggregations'])
    for kind, ident in selection['members']:
        if kind == 'aggregation' and ident not in aggregation_ids:
            aggregation_ids.append(ident)
    aggregations = [item for ident in aggregation_ids
                    for item in config.get('subscription_aggregations', [])
                    if item.get('id') == ident and item.get('enabled', True)]
    covered = {ident for agg in aggregations for ident in agg.get('subscriptions', [])}
    subscriptions = [item for ident in selection['subscriptions'] if ident not in covered
                     for item in config.get('subscriptions', []) if item.get('id') == ident]
    return [(item, _filter_value(group, 'regex'), _filter_value(group, 'exclude_regex'))
            for item in subscriptions] + [
                (item, _filter_value(group, 'aggregation_regex'),
                 _filter_value(group, 'aggregation_exclude_regex')) for item in aggregations]


def managed_members(group):
    """Resolve source precedence and the exact ordering branch, without mutation."""
    nodes = group.get('manual_nodes', [])
    subs = group.get('subscriptions', [])
    aggs = group.get('aggregations', [])
    groups = group.get('include_groups', [])
    if not any((nodes, subs, aggs, groups)):
        if group.get('source') == 'node':
            nodes = group.get('proxies', [])
        elif group.get('source') == 'strategy':
            groups = group.get('proxies', [])
    order = group.get('proxies_order', [])
    if order:
        members = [(item.get('type'), item.get('id')) for item in order if isinstance(item, dict)]
    else:
        members = [('node', item) for item in nodes]
        strategies = [('strategy', item) for item in groups]
        members = strategies + members if group.get('proxy_order') == 'strategies_first' else members + strategies
    return {'nodes': nodes, 'subscriptions': subs, 'aggregations': aggs,
            'groups': groups, 'members': members, 'order': order}


def render_members(group, config):
    """Return exactly the consumed managed proxies/providers for one source group."""
    selection = managed_members(group)
    proxies = []
    aggregations = list(selection['aggregations'])
    for kind, ident in selection['members']:
        if kind == 'aggregation':
            if ident not in aggregations:
                aggregations.append(ident)
        elif kind == 'node' and ident in ('DIRECT', 'REJECT'):
            proxies.append(ident)
        elif kind in ('node', 'strategy'):
            catalog = config.get('nodes' if kind == 'node' else 'proxy_groups', [])
            item = next((item for item in catalog if item.get('id') == ident), None)
            if item:
                proxies.append(item['name'])
    agg_items = [item for item in config.get('subscription_aggregations', [])
                 if item.get('id') in aggregations and item.get('enabled', True)]
    covered = {ident for agg in agg_items for ident in agg.get('subscriptions', [])}
    use = [item['name'] for ident in selection['subscriptions'] if ident not in covered
           for item in config.get('subscriptions', []) if item.get('id') == ident]
    use += [item['name'] for ident in aggregations for item in agg_items if item.get('id') == ident]
    emitted = {}
    if proxies:
        emitted['proxies'] = proxies
    if use:
        emitted['use'] = list(dict.fromkeys(use))
        filters = [value.strip() for key in ('regex', 'aggregation_regex')
                   if isinstance(value := group.get(key), str) and value.strip()]
        if filters:
            emitted['filter'] = filters[0] if len(filters) == 1 else '|'.join(f'({item})' for item in filters)
    return emitted


def validate_filters(group, original=None):
    for field in FILTER_FIELDS:
        if group.get(field) is not None and not isinstance(group[field], str):
            # Unchanged legacy values are allowed only for final merged records.
            if original is None or field not in original or type(original[field]) is not type(group[field]) or original[field] != group[field]:
                raise CandidateValidationError(f'{field} 必须是字符串或 null')
        if field in ('exclude_regex', 'aggregation_exclude_regex') and _filter_value(group, field):
            try:
                re.compile(_filter_value(group, field))
            except re.error as exc:
                raise CandidateValidationError(f'{field} 正则表达式无效') from exc


_LEADING_FLAGS = re.compile(r'\(\?([aiLmsux]+)\)')


def _embedded(pattern):
    """A user pattern as one group that keeps its meaning inside a larger one.

    Leading global flags such as (?i) become scoped flags: Python rejects global
    flags after the start, and both engines accept (?i:...). The group does not
    capture, so the user's own group numbers stay the same.
    """
    flags = ''
    while match := _LEADING_FLAGS.match(pattern):
        flags += match[1]
        pattern = pattern[match.end():]
    return f'(?{flags}:{pattern})' if flags else f'(?:{pattern})'


def _literal_backticks(pattern):
    # Backticks separate Mihomo filters; in an exclusion they are name characters.
    return re.sub(r'(\\*)`', lambda match: match[1][:len(match[1]) // 2 * 2] + r'\x60', pattern)


def exclusion_filter(include, exclude):
    """One Mihomo group filter that keeps include and drops exclude.

    A group filter leaves the group's own proxies and groups alone, so this
    excludes provider nodes only, like the per-provider exclude-filter it
    replaces. Only for a single include: with backtick buckets Mihomo reorders
    every member, the group's own ones too, by the buckets they match.
    """
    if not exclude:
        return include
    without = f'^(?![\\s\\S]*?{_embedded(_literal_backticks(exclude))})'
    return without + (f'(?=[\\s\\S]*?{_embedded(include)})' if include else '')


def lower_group_provider_filters(config, main):
    """Keep managed exclusions scoped to each live provider, never manual nodes.

    Private definitions retain the original URL, so refreshes and bundled
    provider delivery use the same source bytes. Existing inclusion-only groups
    retain their historical rendering.
    """
    providers = main.get('proxy-providers', {})
    groups = {group['name']: group for group in main.get('proxy-groups', [])}
    sources = {group.get('id'): group for group in config.get('proxy_groups', [])}
    occupied = set(providers) | set(groups) | {node['name'] for node in main.get('proxies', [])}
    clones = {}
    for record in config.get('proxy_groups', []):
        group = groups.get(record['name'])
        if group is None:
            continue
        source = sources.get(record.get('follow_group'), record)
        if not any(_filter_value(source, field) for field in ('exclude_regex', 'aggregation_exclude_regex')):
            continue
        selections = {item['name']: (include, exclude)
                      for item, include, exclude in managed_providers(source, config)}
        selected = [(name, *selections[name]) for name in group.get('use', [])
                    if name in selections and name in providers]
        if not selected:
            continue
        rules = {(include, exclude) for _, include, exclude in selected}
        if len(rules) == 1 and len(selected) == len(group.get('use', [])) and '`' not in next(iter(rules))[0]:
            # Every provider shares one rule: the group filter expresses it, so
            # the providers stay shared and no node is listed twice.
            filter_value = exclusion_filter(*next(iter(rules)))
            group.pop('filter', None)
            if filter_value:
                group['filter'] = filter_value
            continue
        # A shared include stays at group level to preserve Mihomo's backtick
        # bucket ordering, including when the group becomes a chain exit.
        includes = {include for _, include, _ in selected}
        shared_include = next(iter(includes)) if len(includes) == 1 else None
        group.pop('filter', None)
        if shared_include:
            group['filter'] = shared_include
        use = []
        for original, include, exclude in selected:
            provider_include = include if shared_include is None else ''
            if not provider_include and not exclude:
                use.append(original)
                continue
            key = (original, provider_include, exclude)
            if key not in clones:
                base = f"{source['name']}_{original}_筛选"
                name, suffix = base, 2
                while name in occupied:
                    name = f'{base} ({suffix})'
                    suffix += 1
                occupied.add(name)
                provider = deepcopy(providers[original])
                digest = hashlib.sha256(repr(key).encode()).hexdigest()[:20]
                provider['path'] = f'./providers/group_filter_{digest}.yaml'
                if provider_include:
                    provider['filter'] = provider_include
                if exclude:
                    provider['exclude-filter'] = _literal_backticks(exclude)
                providers[name] = provider
                clones[key] = name
            use.append(clones[key])
        group['use'] = use


def validate_candidates(group, profile, shared, *, original=None):
    """Validate the final merged group, not raw incidental source flags."""
    if group.get('type') == 'chain':
        return
    for field in ('manual_nodes', 'subscriptions', 'aggregations', 'include_groups', 'proxies', 'proxies_order'):
        if field in group and not isinstance(group[field], list):
            raise CandidateValidationError(f'{field} 必须是数组')
    if group.get('follow_group') is not None and not isinstance(group.get('follow_group'), str):
        raise CandidateValidationError('follow_group 必须是 ID 字符串')
    validate_filters(group, original)
    catalog = {**shared, 'proxy_groups': profile.get('proxy_groups', [])}
    source = group
    follow = group.get('follow_group')
    if follow:
        source = next((item for item in catalog['proxy_groups']
                       if item.get('id') == follow and item.get('enabled', True)), None)
        if source is None or source.get('type') == 'chain' or source.get('follow_group'):
            raise CandidateValidationError('跟随来源必须是有效启用的普通来源组')
    selection = managed_members(source)
    for field, kind in (('nodes', 'nodes'), ('subscriptions', 'subscriptions'),
                        ('aggregations', 'subscription_aggregations'), ('groups', 'proxy_groups')):
        values = selection[field]
        if not isinstance(values, list):
            raise CandidateValidationError('候选来源必须是 ID 数组')
        available = {item.get('id') for item in catalog.get(kind, []) if item.get('enabled', True)}
        if kind == 'nodes':
            available.update(('DIRECT', 'REJECT'))
        if any(not isinstance(value, str) or value not in available for value in values):
            raise CandidateValidationError('候选来源必须引用有效启用资源')
    order = selection['order']
    if not isinstance(order, list):
        raise CandidateValidationError('proxies_order 必须是数组')
    if order:
        allowed = {'node': set(selection['nodes']), 'strategy': set(selection['groups']),
                   'aggregation': set(selection['aggregations'])}
        seen = set()
        for item in order:
            if not isinstance(item, dict) or set(item) != {'type', 'id'}:
                raise CandidateValidationError('排序项必须包含且仅包含 type 和 id')
            kind, ident = item['type'], item['id']
            if not isinstance(kind, str) or not isinstance(ident, str) or kind not in allowed or ident not in allowed[kind]:
                raise CandidateValidationError('排序项必须与已选择的受支持来源一致')
            if (kind, ident) in seen:
                raise CandidateValidationError('排序项不能重复')
            seen.add((kind, ident))
        required = {('node', value) for value in selection['nodes']} | {('strategy', value) for value in selection['groups']}
        if not required <= seen:
            raise CandidateValidationError('排序必须包含所有手动节点和引用策略组')
    emitted = render_members(source, catalog)
    if not emitted.get('proxies') and not emitted.get('use'):
        # Only an already empty record with unchanged source fields is grandfathered.
        if original is not None and all(original.get(key) == group.get(key) for key in SOURCE_FIELDS):
            old = render_members(original, catalog)
            if not old.get('proxies') and not old.get('use') and not follow:
                return
        raise CandidateValidationError('策略组必须选择实际消费的候选来源，不能隐式使用 DIRECT')


def merge_group_update(original, patch):
    """Partial merge; optional health removal is an explicit, validated operation."""
    validate_filters(patch)
    deletes = patch.get('delete_fields', [])
    if not isinstance(deletes, list) or any(not isinstance(key, str) or key not in HEALTH_FIELDS for key in deletes) or len(set(deletes)) != len(deletes):
        raise CandidateValidationError('delete_fields 必须是无重复的健康检查字段数组')
    if any(key in patch for key in deletes):
        raise CandidateValidationError('同一字段不能同时赋值和删除')
    merged = {**deepcopy(original), **deepcopy(patch)}
    merged.pop('delete_fields', None)
    for key in deletes:
        merged.pop(key, None)
    return merged
