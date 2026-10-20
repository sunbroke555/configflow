"""Reuse ConfigFlow's first-start template for the setup rule library."""
from copy import deepcopy
from functools import lru_cache
from hashlib import sha256
import json
import re
from urllib.parse import urlsplit

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


def recommended_domain_rules(repository=None):
    available = None if repository is None else {item['id'] for item in domain_rule_options(repository)}
    return {route: [identity for identity, item in builtin_rule_catalog().items()
                    if item['default_route'] == route and _domain_supported(item)
                    and (available is None or identity in available)] for route in ('direct', 'proxy')}


def canonical_domain_rules(repository, selections):
    by_source = {_source_key(item): identity for identity, item in builtin_rule_catalog().items()}
    aliases = {**_LEGACY_ALIASES, **{
        'library:' + item['id']: by_source[_source_key(item)]
        for item in repository.get_shared()['rule_library'] if _source_key(item) in by_source}}
    return {route: list(dict.fromkeys(aliases.get(identity, identity) for identity in values))
            for route, values in selections.items()}


def ensure_builtin_library(repository):
    catalog = builtin_rule_catalog()
    def find(shared, wanted):
        matches = [item for item in shared['rule_library'] if _source_key(item) == _source_key(wanted)]
        return next((item for item in matches if item.get('enabled', True)), matches[0] if matches else None)
    current = repository.get_shared()
    if all(find(current, item) for item in catalog.values()):
        return {identity: deepcopy(find(current, item)) for identity, item in catalog.items()}
    selected = {}
    def update(shared):
        for identity, wanted in catalog.items():
            item = find(shared, wanted)
            if not item:
                item = {key: deepcopy(value) for key, value in wanted.items() if key not in ('default_route', '_template_order')}
                item['id'] = identity.replace(':', '_')
                ids = {entry['id'] for entry in shared['rule_library']}
                names = {entry['name'] for entry in shared['rule_library']}
                suffix = 1
                while item['id'] in ids or item['name'] in names:
                    suffix += 1
                    item.update(id=identity.replace(':', '_') + f'_{suffix}', name=wanted['name'] + f'（内置 {suffix}）')
                shared['rule_library'].append(item)
            selected[identity] = deepcopy(item)
    repository.update_shared_transaction(update)
    return selected


def domain_rule_options(repository):
    shared = repository.get_shared()['rule_library']
    disabled_sources = {_source_key(item) for item in shared if not item.get('enabled', True)}
    disabled_sources -= {_source_key(item) for item in shared if item.get('enabled', True)}
    options = [{'id': identity, 'name': item['name'], 'source': 'builtin', 'behavior': item['behavior'],
                'aliases': [alias for alias, target in _LEGACY_ALIASES.items() if target == identity]
                           + ['library:' + entry['id'] for entry in shared if _source_key(entry) == _source_key(item)]}
               for identity, item in builtin_rule_catalog().items()
               if _domain_supported(item) and _source_key(item) not in disabled_sources]
    builtin_sources = {_source_key(item) for item in builtin_rule_catalog().values()}
    for item in shared:
        if (not _domain_supported(item) or _source_key(item) in builtin_sources
                or 'library:' + item['id'] in _LEGACY_ALIASES):
            continue
        options.append({'id': 'library:' + item['id'], 'name': item['name'], 'source': 'library'})
    return options


def resolve_domain_rules(repository, selections):
    """Resolve selected identities against the existing template library."""
    builtins = ensure_builtin_library(repository)
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
