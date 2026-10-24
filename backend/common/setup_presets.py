"""Reuse ConfigFlow's first-start template for the setup rule library."""
from copy import deepcopy
from functools import lru_cache
from hashlib import sha256
import ipaddress
import json
import re
from urllib.parse import urlsplit

import yaml

from backend.common.resource import get_backend_resource

_LEGACY_ALIASES = {
    'preset:direct': 'builtin:chinamax', 'preset:proxy': 'builtin:proxy',
    'library:setup_dns_cn': 'builtin:chinamax', 'library:setup_cn_domains': 'builtin:chinamax',
    'library:setup_dns_proxy': 'builtin:proxy', 'library:setup_proxy_domains': 'builtin:proxy',
}


def _source_key(item):
    if item.get('source_type') == 'content':
        source = ('content', sha256(item.get('content', '').encode()).hexdigest())
    elif item.get('url'):
        source = ('url', item['url'].replace('/refs/heads/', '/'))
    else:
        return None
    return (*source, item.get('behavior') or 'domain', item.get('format') or 'text')


_DOMAIN_KINDS = ('DOMAIN', 'DOMAIN-SUFFIX', 'DOMAIN-KEYWORD', 'DOMAIN-WILDCARD')


@lru_cache(maxsize=256)
def _parsed_rules(content, behavior, rule_format):
    """What a hand-written rule set matches, ignoring order, case and spelling.

    Bare Classical lines take the delivery normalization; a Domain "+.x" is a
    Classical DOMAIN-SUFFIX and a plain name a DOMAIN. Anything ambiguous makes
    the whole set incomparable (None) rather than equal by accident.
    """
    from backend.utils.ruleset_content import _classical_line
    from backend.utils.rule_syntax import split_rule_payload
    try:
        if rule_format == 'yaml':
            lines = yaml.safe_load(content.removeprefix('\ufeff'))['payload']
        else:
            lines = content.removeprefix('\ufeff').splitlines()
        rules = set()
        for line in lines:
            line = line.strip()
            if not line or line.startswith(('#', '//')):
                continue
            if behavior == 'domain':
                value = line.lower()
                if value.startswith('+.'):
                    rules.add(('DOMAIN-SUFFIX', value[2:]))
                elif value.startswith('.') or '*' in value:
                    rules.add(('domain', value))  # No exact Classical counterpart.
                else:
                    rules.add(('DOMAIN', value))
                continue
            kind, payload, params = split_rule_payload(_classical_line(line))
            if kind in _DOMAIN_KINDS:
                payload = payload.lower()
            elif kind in ('IP-CIDR', 'IP-CIDR6'):
                payload = str(ipaddress.ip_network(payload, strict=False))
            rules.add((kind, payload, *sorted(param.lower() for param in params)))
    except (AttributeError, KeyError, TypeError, ValueError, yaml.YAMLError):
        return None
    return frozenset(rules) or None


def _content_rules(item):
    if item.get('source_type') != 'content' or (item.get('behavior') or 'domain') not in ('domain', 'classical'):
        return None
    return _parsed_rules(item.get('content', ''), item.get('behavior') or 'domain', item.get('format') or 'text')


def _same_rules(item, wanted):
    """The same source (URL or exact text), or hand-written rules matching the same."""
    key = _source_key(item)
    if key is not None and key == _source_key(wanted):
        return True
    rules = _content_rules(item)
    return rules is not None and rules == _content_rules(wanted)


def _builtin_identity(item):
    return next((identity for identity, wanted in builtin_rule_catalog().items() if _same_rules(item, wanted)), None)


def _domain_supported(item):
    return (item.get('enabled', True) and (item.get('behavior') or 'domain') in ('domain', 'classical')
            and (item.get('format') or 'text') in ('text', 'yaml')
            and not urlsplit(item.get('url') or '').path.lower().endswith('.mrs'))


@lru_cache(maxsize=1)
def builtin_rule_catalog():
    with open(get_backend_resource('config_template.json'), encoding='utf-8') as handle:
        template = json.load(handle)
    routes = {identity: route for route in ('direct', 'proxy')
              for identity in template.get('mosdns', {}).get(route + '_rulesets', [])}
    library_routes = {rule.get('library_rule_id'): routes[rule['id']]
                      for rule in template.get('rule_configs', []) if rule.get('id') in routes}
    items = []
    order = {rule['library_rule_id']: index for index, rule in enumerate(template.get('rule_configs', []))
             if rule.get('itemType') == 'ruleset'}
    for item in template.get('rule_library', []):
        source_type = item.get('source_type') or ('content' if 'content' in item else 'url')
        items.append({'name': item['name'], 'source_type': source_type,
                      'content' if source_type == 'content' else 'url': item.get('content' if source_type == 'content' else 'url', ''),
                      'behavior': item.get('behavior') or 'domain', 'format': item.get('format') or 'text',
                      'enabled': item.get('enabled', True), 'default_route': library_routes.get(item['id']),
                      '_template_order': order.get(item['id'], len(order))})
    return {'builtin:' + re.sub('[^a-z0-9]+', '_', item['name'].lower()): item
            for item in sorted(items, key=lambda item: item['_template_order'])}


@lru_cache(maxsize=1)
def _template():
    with open(get_backend_resource('config_template.json'), encoding='utf-8') as handle:
        return json.load(handle)


def template_strategy(rules, traffic_fallback, subscription_ids, node_ids, subscription_nodes, manual_nodes):
    """Adapt the first-start template's proxy groups to a setup profile.

    ``rules`` lists ``(identity, route)`` per selected rule set in rule order.
    Returns the groups, one policy per rule and the policy for unmatched
    connections. The wizard's direct/proxy choice stays authoritative: a
    service group that follows a direct-first strategy in the template follows
    the proxy-first one when its rules were chosen as proxy, and vice versa.
    """
    from backend.utils.group_candidates import filter_candidates
    template = _template()
    groups = deepcopy(template['proxy_groups'])
    by_name = {group['name']: group for group in groups}
    by_id = {group['id']: group for group in groups}
    template_subscriptions = {item['id'] for item in template.get('subscriptions', [])}
    library = {item['id']: item['name'] for item in template.get('rule_library', [])}
    template_policy = {'builtin:' + re.sub('[^a-z0-9]+', '_', library[rule['library_rule_id']].lower()): rule['policy']
                       for rule in template['rule_configs']
                       if rule.get('itemType') == 'ruleset' and rule.get('library_rule_id') in library}

    # Bind template sources to the user's; drop filtered groups that would be
    # empty, since Mihomo silently turns an empty group into a direct exit.
    removed = set()
    for group in groups:
        if not set(group.get('subscriptions') or []) & template_subscriptions:
            continue
        include, exclude = group.get('regex', ''), group.get('exclude_regex', '')
        matched = filter_candidates(subscription_nodes, include=include, exclude=exclude)
        manual = [node['id'] for node in filter_candidates(manual_nodes, include=include, exclude=exclude)]
        if not include and not exclude:
            manual = list(node_ids)
        if not matched and not manual:
            removed.add(group['id'])
        group.update(subscriptions=list(subscription_ids) if matched else [], manual_nodes=manual, aggregations=[])
    groups = [group for group in groups if group['id'] not in removed]
    for group in groups:
        group['include_groups'] = [item for item in group.get('include_groups') or [] if item not in removed]
        group['proxies_order'] = [item for item in group.get('proxies_order') or [] if item.get('id') not in removed]

    direct_name = '🎯 全球直连'
    direct_ids = {by_name[direct_name]['id']}
    def direct_first(group):
        followed = by_id.get(group.get('follow_group'), group)
        return (followed.get('include_groups') or [None])[0] in direct_ids or group['name'] == direct_name
    follow = {'proxy': by_name['优先代理']['id'], 'direct': by_name['优先直连']['id']}

    policies, proxied = [], set()
    for identity, route in rules:
        group = by_name.get(template_policy.get(identity, ''))
        if route == 'proxy':
            # Template sets routed direct there (e.g. ChinaMax) have no proxy group.
            name = group['name'] if group and group.get('follow_group') else '✈️ 节点选择'
            proxied.add(name)
        elif template_policy.get(identity) == 'DIRECT':
            name = 'DIRECT'
        else:
            name = group['name'] if group and direct_first(group) else direct_name
        policies.append(name)
    # A shared group cannot be both: rules chosen as direct leave it.
    policies = [direct_name if route == 'direct' and name in proxied else name
                for (_, route), name in zip(rules, policies)]
    final = '🐟 漏网之鱼' if traffic_fallback == 'proxy' else 'DIRECT'
    if traffic_fallback == 'proxy':
        proxied.add(final)
    for name in proxied:
        if by_name[name].get('follow_group') and direct_first(by_name[name]):
            by_name[name]['follow_group'] = follow['proxy']
    return groups, policies, final


# Template proxy rule sets for apps that connect to IP addresses without a DNS
# lookup. Their ranges do not overlap the direct ones; broad lists such as
# Apple, GlobalMedia or Proxy do, and taking them over would proxy direct sites
# whose real IPs fall inside them, since Mihomo then matches by IP alone.
IP_TAKEOVER_RULES = ('builtin:telegram', 'builtin:googlefcm')


def recommended_domain_rules(repository=None):
    available = None if repository is None else {item['id'] for item in domain_rule_options(repository)}
    return {route: [identity for identity, item in builtin_rule_catalog().items()
                    if item['default_route'] == route and _domain_supported(item)
                    and (available is None or identity in available)] for route in ('direct', 'proxy')}


def canonical_domain_rules(repository, selections):
    aliases = dict(_LEGACY_ALIASES)
    for item in repository.get_shared()['rule_library']:
        identity = _builtin_identity(item)
        if identity:
            aliases['library:' + item['id']] = identity
    return {route: list(dict.fromkeys(aliases.get(identity, identity) for identity in values))
            for route, values in selections.items()}


def _copy_name(shared, name):
    names = {entry['name'] for entry in shared['rule_library']}
    return name if name not in names else next(
        candidate for suffix in range(1, len(names) + 2)
        if (candidate := f'{name}（内置）' if suffix == 1 else f'{name}（内置 {suffix}）') not in names)


def ensure_builtin_library(repository, selected=()):
    """Add the template's rule sets that the shared library does not yet hold.

    A rule set whose name is already taken by different rules is added only
    when ``selected`` asks for it, so an unrelated wizard run never leaves a
    "（内置）" copy beside the user's own set.
    """
    catalog = builtin_rule_catalog()
    def find(shared, wanted):
        matches = [item for item in shared['rule_library'] if _same_rules(item, wanted)]
        return next((item for item in matches if item.get('enabled', True)), matches[0] if matches else None)
    def missing(shared):
        names = {entry['name'] for entry in shared['rule_library']}
        return [identity for identity, wanted in catalog.items() if not find(shared, wanted)
                and (wanted['name'] not in names or identity in selected)]
    current = repository.get_shared()
    if not missing(current):
        return {identity: deepcopy(item) for identity, wanted in catalog.items() if (item := find(current, wanted))}
    selected_items = {}
    def update(shared):
        added = set(missing(shared))
        for identity, wanted in catalog.items():
            item = find(shared, wanted)
            if not item and identity in added:
                item = {key: deepcopy(value) for key, value in wanted.items() if key not in ('default_route', '_template_order')}
                ids = {entry['id'] for entry in shared['rule_library']}
                item['id'] = next(candidate for suffix in range(1, len(ids) + 2)
                                  if (candidate := identity.replace(':', '_') + ('' if suffix == 1 else f'_{suffix}')) not in ids)
                item['name'] = _copy_name(shared, wanted['name'])
                shared['rule_library'].append(item)
            if item:
                selected_items[identity] = deepcopy(item)
    repository.update_shared_transaction(update)
    return selected_items


def domain_rule_options(repository):
    """Wizard choices: template rule sets first, then the user's other sets.

    A template set the library lacks but whose name a different library set
    already uses carries ``conflict``, so the wizard can offer that set instead
    of adding a "（内置）" copy.
    """
    shared = repository.get_shared()['rule_library']
    identities = {item['id']: _builtin_identity(item) for item in shared}
    own = [item for item in shared if _domain_supported(item) and not identities[item['id']]
           and 'library:' + item['id'] not in _LEGACY_ALIASES]
    options = []
    for identity, item in builtin_rule_catalog().items():
        matches = [entry for entry in shared if identities[entry['id']] == identity]
        if not _domain_supported(item) or (matches and not any(entry.get('enabled', True) for entry in matches)):
            continue
        option = {'id': identity, 'name': item['name'], 'source': 'builtin', 'behavior': item['behavior'],
                  'aliases': [alias for alias, target in _LEGACY_ALIASES.items() if target == identity]
                             + ['library:' + entry['id'] for entry in matches]}
        namesake = None if matches else next((entry for entry in own if entry['name'] == item['name']), None)
        if namesake:
            option['conflict'] = {'id': 'library:' + namesake['id'], 'name': namesake['name']}
        options.append(option)
    options += [{'id': 'library:' + item['id'], 'name': item['name'], 'source': 'library'} for item in own]
    return options


def resolve_domain_rules(repository, selections):
    """Resolve selected identities against the existing template library."""
    builtins = ensure_builtin_library(repository, {identity for values in selections.values() for identity in values})
    library = {item['id']: item for item in repository.get_shared()['rule_library']}
    result = {'direct': [], 'proxy': []}
    for route, identities in selections.items():
        for identity in identities:
            if identity.startswith('builtin:'):
                item, rule_id = builtins[identity], 'setup_' + identity.replace(':', '_')
            else:
                item = library[identity.removeprefix('library:')]
                rule_id = 'setup_domain_' + sha256(identity.encode()).hexdigest()[:20]
            result[route].append((rule_id, {'behavior': 'domain', 'format': 'text',
                                          'source_type': 'content' if item.get('content') else 'url', **deepcopy(item)}))
    return result
