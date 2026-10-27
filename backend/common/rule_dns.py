"""Per-rule OxiDNS DNS handling shared by templates, native routing and the rules page.

A rule's route is a string: ``direct`` / ``proxy`` (the four selector lists),
one of the inline rejections, ``blackhole`` or ``upstream:<h12>``. Explicit
actions live in ``oxidns.rule_actions``; ``rule_dns_follow`` only records intent
and is never read by generation. Pure functions: no Flask, no repository access.
"""
from copy import deepcopy
from hashlib import sha256
import json
import re

from backend.common.config_repository import RULE_DNS_FIELDS, ProfileInUse
from backend.common.oxidns_settings import OxidnsSettingsError
from backend.converters.mosdns import DNS_RULE_TYPES as MOSDNS_RULE_TYPES

DNS_RULE_TYPES = frozenset(MOSDNS_RULE_TYPES) - {'IP-CIDR', 'IP-CIDR6'}

ACTIONS = ('refused', 'nxdomain', 'nodata', 'blackhole', 'upstream')
LIST_ROUTES = ('direct', 'proxy')
UPSTREAM_SOURCES = ('local', 'remote', 'fallback')
INLINE_EXEC = {'refused': 'reject', 'nxdomain': 'reject 3', 'nodata': 'reject 0'}
BLACKHOLE_TAG = 'cf_dns_blackhole'
# A bare YAML null is silently treated as nxdomain by OxiDNS; 'null' must stay a string.
BLACKHOLE_PLUGIN = {'tag': BLACKHOLE_TAG, 'type': 'black_hole', 'args': {'mode': 'null', 'short_circuit': True}}
UPSTREAM_PREFIX = 'cf_dns_up_'
FORWARD_PREFIX = 'cf_dns_fwd_'
CACHE_PREFIX = 'cf_dns_cache_'
UPSTREAM_CACHE_SIZE = 1024
# Generated provider tags, including the _2, _3 ... names given when a tag is taken.
GROUP_TAG_RE = re.compile(r'rules_(domain|ip)_\d+(?:_\d+)*')
ROUTE_LABELS = {'direct': '直连 DNS', 'proxy': '代理 DNS', 'refused': '拒绝', 'nxdomain': '不存在',
                'nodata': '空应答', 'blackhole': '黑洞', 'upstream': '指定上游'}
SOURCE_LABELS = {'local': '直连', 'remote': '代理', 'fallback': '备用'}
# Built-in policies that never send traffic through a proxy group.
NON_PROXY_POLICIES = ('REJECT', 'REJECT-DROP', 'PASS')
# Same response hooks, in the same order, as the template's china_dns.
REAL_WRAPPERS = ('ip_selector', 'ros_address_list', 'ros_route')
# Rule-library fields a profile ruleset reference inherits.
RULESET_SOURCE_FIELDS = ('name', 'url', 'behavior', 'content', 'source_type', 'format')

_GOTO = {'domain': {'direct': 'goto china_dns', 'proxy': 'goto proxy_dns_seq'},
         'ip': {'direct': 'goto direct_ip', 'proxy': 'goto ip_requery_proxy'}}
_REJECT = {'': 'refused', '5': 'refused', 'refused': 'refused',
           '3': 'nxdomain', 'nxdomain': 'nxdomain', '0': 'nodata', 'noerror': 'nodata'}
_KEY = re.compile(r'[0-9a-f]{12}')
_TTL = re.compile(r'ttl ([1-9][0-9]*)')
_SOURCE_TAGS = {'local': 'forward_local', 'remote': 'forward_remote', 'fallback': 'forward_fallback'}
_BLACKHOLE_REASON = (f'黑洞插件 {BLACKHOLE_TAG} 已被手动修改（应为 mode: "null"、short_circuit: true），'
                     '请在完整参数中恢复或改名')
_CHAIN_REASON = '指定上游处理链 {tag} 已被手动修改，请在完整参数中恢复'


class Invalid:
    """A recognizable rule DNS exec whose owned helper was edited by hand."""
    __slots__ = ('reason',)

    def __init__(self, reason):
        self.reason = reason

    def __eq__(self, other):
        return isinstance(other, Invalid) and other.reason == self.reason

    def __repr__(self):
        return f'Invalid({self.reason!r})'


class OxidnsRoutingLocked(ProfileInUse):
    """Native routing cannot be rewritten; nothing in the transaction is saved."""

    def __init__(self, reason):
        super().__init__(reason, [{'kind': 'oxidns', 'code': 'oxidns_routing_custom',
                                   'target': 'sequence_main', 'reason': reason}])


def entry_key(upstream, source):
    """12 hex digits naming one pinned upstream copy as chosen from one list."""
    text = json.dumps({'source': source, 'upstream': upstream}, sort_keys=True,
                      ensure_ascii=False, separators=(',', ':'))
    return sha256(text.encode()).hexdigest()[:12]


def route_key(action):
    """Route string of a stored action object (also accepts parked direct/proxy)."""
    if action['action'] == 'upstream':
        return 'upstream:' + entry_key(action['upstream'], action['source'])
    return action['action']


def route_exec(route, part):
    """sequence_main exec for a rule group; domain and IP groups differ only for lists."""
    if route in LIST_ROUTES:
        return _GOTO[part][route]
    if route in INLINE_EXEC:
        return INLINE_EXEC[route]
    if route == 'blackhole':
        return '$' + BLACKHOLE_TAG
    if isinstance(route, str) and route.startswith('upstream:') and _KEY.fullmatch(route[9:]):
        return 'goto ' + UPSTREAM_PREFIX + route[9:]
    raise OxidnsSettingsError(f'不支持的 DNS 处理：{route}')


def normalize_action(value, *, parked=False):
    """Validated canonical copy of one rule_actions (or rule_dns_parked) value."""
    allowed = (*LIST_ROUTES, *ACTIONS) if parked else ACTIONS
    if not isinstance(value, dict) or value.get('action') not in allowed:
        raise OxidnsSettingsError('规则 DNS 处理无效：' + ('、'.join(allowed)))
    action = value['action']
    if action != 'upstream':
        if set(value) != {'action'}:
            raise OxidnsSettingsError(f'规则 DNS 处理「{ROUTE_LABELS[action]}」不能包含其他参数')
        return {'action': action}
    if set(value) != {'action', 'source', 'upstream'} or value['source'] not in UPSTREAM_SOURCES:
        raise OxidnsSettingsError('指定上游需要 source（local、remote 或 fallback）和 upstream')
    upstream = value['upstream']
    if not isinstance(upstream, dict) or not isinstance(upstream.get('addr'), str) or not upstream['addr'].strip():
        raise OxidnsSettingsError('指定上游需要非空的 addr 地址')
    return {'action': 'upstream', 'source': value['source'], 'upstream': deepcopy(upstream)}


def _enabled(settings, name):
    section = settings.get(name)
    return isinstance(section, dict) and section.get('enabled') is True


def upstream_chain(action, settings):
    """forward + optional cache + sequence behind one pinned upstream.

    Local upstreams get the same IP selection / RouterOS hooks as china_dns;
    proxy and backup upstreams get the remote TTL rewrite. Each upstream has its
    own cache so a rule switching away from china_dns never reuses its answers.
    """
    key = entry_key(action['upstream'], action['source'])
    forward = FORWARD_PREFIX + key
    plugins = [{'tag': forward, 'type': 'forward', 'args': {'upstreams': [deepcopy(action['upstream'])]}}]
    steps = [{'exec': 'drop_resp'}]
    if action['source'] == 'local':
        steps.extend({'exec': f'${name}'} for name in REAL_WRAPPERS if _enabled(settings, name))
    if settings.get('cache_enabled'):
        cache = {'size': UPSTREAM_CACHE_SIZE}
        # OxiDNS rejects an explicit zero; omitting the field disables lazy cache.
        if settings.get('cache_lazy_ttl', 0) > 0:
            cache['lazy_cache_ttl'] = settings['cache_lazy_ttl']
        cache['short_circuit'] = True
        plugins.append({'tag': CACHE_PREFIX + key, 'type': 'cache', 'args': cache})
        steps.append({'exec': f'${CACHE_PREFIX}{key}'})
    steps.extend([{'exec': f'${forward}'}, {'matches': ['!has_resp'], 'exec': 'reject 3'}])
    if action['source'] != 'local' and settings.get('remote_ttl'):
        steps.append({'exec': f'ttl {settings["remote_ttl"]}'})
    steps.append({'exec': 'accept'})
    plugins.append({'tag': UPSTREAM_PREFIX + key, 'type': 'sequence', 'args': steps})
    return plugins


def helper_plugins(routes, actions, settings):
    """Owned plugins needed by the given group routes, in first-use order.

    actions is rule_actions (id -> action) and supplies pinned upstream copies;
    settings supplies the china_dns features copied into local upstream chains.
    """
    pinned = {}
    for action in (actions or {}).values():
        if action.get('action') == 'upstream':
            pinned.setdefault(route_key(action), action)
    result, seen = [], set()
    for route in routes:
        if route in seen:
            continue
        seen.add(route)
        if route == 'blackhole':
            result.append(deepcopy(BLACKHOLE_PLUGIN))
        elif isinstance(route, str) and route.startswith('upstream:'):
            if route not in pinned:
                raise OxidnsSettingsError('指定上游缺少上游地址，请重新选择')
            result.extend(upstream_chain(pinned[route], settings))
    return result


def chain_features(plugins):
    """china_dns / remote_dns features a pinned upstream chain copies, read from
    a native document's tag -> plugin map."""
    def args(tag):
        plugin = plugins.get(tag)
        return plugin.get('args') if isinstance(plugin, dict) else None

    def steps(tag):
        value = args(tag)
        return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []
    china = steps('china_dns')
    result = {name: {'enabled': {'exec': f'${name}'} in china} for name in REAL_WRAPPERS}
    cache = plugins.get('lazy_cache')
    result['cache_enabled'] = {'exec': '$lazy_cache'} in china and isinstance(cache, dict) and cache.get('type') == 'cache'
    lazy = args('lazy_cache').get('lazy_cache_ttl') if isinstance(args('lazy_cache'), dict) else None
    result['cache_lazy_ttl'] = lazy if type(lazy) is int and lazy > 0 else 0
    ttl = next((item['exec'] for item in steps('remote_dns') if isinstance(item.get('exec'), str) and
                _TTL.fullmatch(item['exec'])), None)
    result['remote_ttl'] = int(ttl[4:]) if ttl else 0
    return result


def _same(left, right):
    # Python otherwise equates True with 1, losing meaningful YAML differences.
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_same(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right))
    return left == right


def _pinned(key, plugins):
    """(entry, possible sources) of a canonical pinned chain, or None when edited."""
    chain, forward, cache = (plugins.get(prefix + key) for prefix in (UPSTREAM_PREFIX, FORWARD_PREFIX, CACHE_PREFIX))
    if not isinstance(chain, dict) or chain.get('type') != 'sequence' or not isinstance(chain.get('args'), list):
        return None
    steps = list(chain['args'])
    if not steps or not _same(steps.pop(0), {'exec': 'drop_resp'}) or not steps or not _same(steps.pop(), {'exec': 'accept'}):
        return None
    wrappers, ttl = [], None
    while steps and isinstance(steps[0], dict) and set(steps[0]) == {'exec'} and \
            steps[0]['exec'] in [f'${name}' for name in REAL_WRAPPERS]:
        wrappers.append(steps.pop(0)['exec'][1:])
    if wrappers != [name for name in REAL_WRAPPERS if name in wrappers]:
        return None
    if steps and _same(steps[0], {'exec': f'${CACHE_PREFIX}{key}'}):
        steps.pop(0)
        args = cache.get('args') if isinstance(cache, dict) and cache.get('type') == 'cache' else None
        lazy = args.get('lazy_cache_ttl') if isinstance(args, dict) else None
        if not isinstance(args, dict) or not (
                _same(args, {'size': UPSTREAM_CACHE_SIZE, 'short_circuit': True}) or
                (type(lazy) is int and lazy > 0 and
                 _same(args, {'size': UPSTREAM_CACHE_SIZE, 'lazy_cache_ttl': lazy, 'short_circuit': True}))):
            return None
    if steps and isinstance(steps[-1], dict) and set(steps[-1]) == {'exec'} and \
            isinstance(steps[-1]['exec'], str) and _TTL.fullmatch(steps[-1]['exec']):
        ttl = steps.pop()
    if not _same(steps, [{'exec': f'${FORWARD_PREFIX}{key}'}, {'matches': ['!has_resp'], 'exec': 'reject 3'}]):
        return None
    args = forward.get('args') if isinstance(forward, dict) and forward.get('type') == 'forward' else None
    upstreams = args.get('upstreams') if isinstance(args, dict) and set(args) == {'upstreams'} else None
    if not isinstance(upstreams, list) or len(upstreams) != 1 or not isinstance(upstreams[0], dict) or \
            not isinstance(upstreams[0].get('addr'), str) or not upstreams[0]['addr'].strip():
        return None
    if wrappers and ttl:
        return None
    sources = ('local',) if wrappers else ('remote', 'fallback') if ttl else UPSTREAM_SOURCES
    return upstreams[0], sources


def _source(key, entry, sources, plugins):
    """Source of a pinned chain: the one reproducing the tag's hash, else the
    first one whose chain the current features would generate in this shape,
    so a rebuild never adds or drops local hooks or the TTL rewrite. None
    when no source fits."""
    source = next((item for item in sources if entry_key(entry, item) == key), None)
    if source is not None or len(sources) == 1:
        return source or sources[0]
    if 'local' in sources:
        # A bare chain: no local hooks and no TTL rewrite.
        features = chain_features(plugins)
        hooks = any(_enabled(features, name) for name in REAL_WRAPPERS)
        sources = [item for item in sources if not (hooks if item == 'local' else features['remote_ttl'])]
    return sources[0] if sources else None


def parse_exec(value, part, plugins):
    """Route of one sequence_main rule-group exec, None when it is not a rule DNS
    action, or Invalid(reason) when an owned helper was edited by hand.

    plugins maps tag -> plugin. Aliased reject spellings are recognized and are
    written back in canonical form. A pinned chain whose tag hash no longer
    matches its content (a hand-edited address) is still recognized under the
    content's key; its source comes from the chain shape, preferring the source
    that reproduces the tag's own hash (see _source).
    """
    if not isinstance(value, str):
        return None
    for route, target in _GOTO[part].items():
        if value == target:
            return route
    words = value.split()
    if words and words[0].lower() == 'reject' and len(words) <= 2:
        return _REJECT.get(words[1].lower() if len(words) == 2 else '')
    if value == '$' + BLACKHOLE_TAG:
        plugin = plugins.get(BLACKHOLE_TAG)
        return 'blackhole' if _same(plugin, BLACKHOLE_PLUGIN) else Invalid(_BLACKHOLE_REASON)
    if value.startswith('goto ' + UPSTREAM_PREFIX) and _KEY.fullmatch(value[5 + len(UPSTREAM_PREFIX):]):
        key = value[5 + len(UPSTREAM_PREFIX):]
        found = _pinned(key, plugins)
        if found is None:
            return Invalid(_CHAIN_REASON.format(tag=UPSTREAM_PREFIX + key))
        entry, sources = found
        source = _source(key, entry, sources, plugins)
        if source is None:
            return Invalid(_CHAIN_REASON.format(tag=UPSTREAM_PREFIX + key))
        return 'upstream:' + entry_key(entry, source)
    return None


def pinned_action(key, plugins):
    """Stored action for a recognized ``goto cf_dns_up_<key>`` chain, else None."""
    route = parse_exec(f'goto {UPSTREAM_PREFIX}{key}', 'domain', plugins)
    if not isinstance(route, str):
        return None
    entry, sources = _pinned(key, plugins)
    source = next(item for item in sources if 'upstream:' + entry_key(entry, item) == route)
    return {'action': 'upstream', 'source': source, 'upstream': deepcopy(entry)}


def policy_dns_routes(config):
    """DNS routes for configured group defaults, never a device's live choice.

    Reuse Mihomo's managed membership ordering. Only manual selection has a
    stable first candidate; automatic/empty/invalid groups stay on proxy DNS.
    A follow group copies its source's type and candidates, as generation does.
    """
    from backend.utils.group_candidates import render_members
    groups = {group['name']: group for group in config.get('proxy_groups') or []
              if isinstance(group, dict) and isinstance(group.get('name'), str)}
    by_id = {group['id']: group for group in groups.values() if isinstance(group.get('id'), str)}
    resolved = {}

    def resolve(name, visiting):
        if name == 'DIRECT':
            return 'direct'
        if name in NON_PROXY_POLICIES:
            return None
        if name in resolved:
            return resolved[name]
        group = groups.get(name)
        if (group is None or not group.get('enabled', True) or group.get('type') == 'chain'
                or name in visiting or len(visiting) >= 64):
            return 'proxy'
        source = group
        if group.get('follow_group'):
            source = by_id.get(group['follow_group'])
            # Writes forbid following another follower. Do not invent defaults
            # for imported chains that Mihomo does not recursively expand.
            if source is None or source.get('follow_group'):
                return 'proxy'
        # A disabled source can still be a template for an enabled follower;
        # Mihomo copies its configuration without emitting the source itself.
        if source.get('type') != 'select':
            return 'proxy'
        emitted = render_members(source, config)
        # Backtick buckets can reorder even static candidates. Native exclusions
        # can also remove the first one; neither has a reliable static default.
        if '`' in emitted.get('filter', '') or source.get('exclude-filter') or source.get('exclude-type'):
            return 'proxy'
        candidates = emitted.get('proxies') or []
        route = resolve(candidates[0], visiting | {name}) if candidates else 'proxy'
        resolved[name] = route
        return route

    return {name: resolve(name, set()) for name in groups}


def follow_route(item, config=None, *, routes=None):
    """DNS route implied by the rule's policy and configured group default."""
    if not convertible(item):
        return None
    policy = item.get('policy')
    if policy == 'DIRECT':
        return 'direct'
    if not isinstance(policy, str) or not policy or policy in NON_PROXY_POLICIES:
        return None
    if routes is None:
        routes = policy_dns_routes(config) if config is not None else {}
    return routes.get(policy, 'proxy')


def hydrate_ruleset(item, library_entry, profile_id):
    """Copy a shared rule-library source onto a profile ruleset reference in place."""
    for key in (*RULESET_SOURCE_FIELDS, 'library_enabled'):
        item.pop(key, None)
    item.update({key: deepcopy(library_entry[key]) for key in RULESET_SOURCE_FIELDS if key in library_entry})
    item.setdefault('behavior', 'domain')
    item['library_enabled'] = deepcopy(library_entry.get('enabled', True))
    if library_entry.get('source_type') == 'content':
        item['url'] = f'/api/profiles/{profile_id or "default"}/rule-library/content/{library_entry["id"]}'
    return item


def _label(source, addr):
    return f'{SOURCE_LABELS[source]} · {addr.removeprefix("udp://")}'


def upstream_catalog(settings, document=None, bindings=()):
    """Upstreams a rule can pin: [{key, source, addr, label, entry, linked}].

    A document (native, or the prepared editor document) is read from its
    forward_local / forward_remote / forward_fallback plugins; a backup parked by
    the common settings is not in the document and is therefore not offered.
    Template settings use the same parsing as generation. An address in two
    lists is offered twice. bindings are DNS follow {upstream_tag, upstream_index}.
    """
    linked = {(item.get('upstream_tag'), item.get('upstream_index')) for item in bindings}
    lists = {}
    if document is None and settings.get('config_mode', 'template') == 'template':
        from backend.converters.oxidns import (LOCAL_DNS_DEFAULTS, REMOTE_DNS_DEFAULTS,
                                               _fallback_upstreams, _forward)
        parsers = {'local': lambda: _forward(settings.get('local_dns', ''), LOCAL_DNS_DEFAULTS),
                   'remote': lambda: _forward(settings.get('remote_dns', ''), REMOTE_DNS_DEFAULTS),
                   'fallback': lambda: _fallback_upstreams(settings.get('fallback_dns', ''))}
        for source, parse in parsers.items():
            try:
                lists[source] = parse()
            except OxidnsSettingsError:
                # Generation reports the broken list; the rules page just offers none of it.
                lists[source] = []
    else:
        if document is None:
            from backend.common.oxidns_native import load_native_yaml
            document = load_native_yaml(settings.get('native_config', ''))
        plugins = document.get('plugins') if isinstance(document, dict) else None
        plugins = [item for item in plugins if isinstance(item, dict)] if isinstance(plugins, list) else []
        tags = [item.get('tag') for item in plugins]
        for source, tag in _SOURCE_TAGS.items():
            plugin = next((item for item in plugins if item.get('tag') == tag), None)
            args = plugin.get('args') if plugin and plugin.get('type') == 'forward' and tags.count(tag) == 1 else None
            upstreams = args.get('upstreams') if isinstance(args, dict) else None
            lists[source] = upstreams if isinstance(upstreams, list) else []
    result, index_of = [], {}
    for source, upstreams in lists.items():
        for index, entry in enumerate(upstreams):
            if not isinstance(entry, dict) or not isinstance(entry.get('addr'), str) or not entry['addr'].strip():
                continue
            key = entry_key(entry, source)
            bound = (_SOURCE_TAGS[source], index) in linked
            if key in index_of:
                result[index_of[key]]['linked'] |= bound
                continue
            index_of[key] = len(result)
            result.append({'key': key, 'source': source, 'addr': entry['addr'],
                           'label': _label(source, entry['addr']), 'entry': deepcopy(entry), 'linked': bound})
    return result


# ---- Rules page read model and writes -------------------------------------

SELECTORS = ('direct_rules', 'proxy_rules', 'direct_rulesets', 'proxy_rulesets')
# The values an API caller may send as one rule's ``dns``.
CHOICES = ('follow', 'default', *LIST_ROUTES, *INLINE_EXEC, 'blackhole')
IP_RULE_TYPES = ('IP-CIDR', 'IP-CIDR6')
_IP_BEHAVIORS = ('ipcidr', 'classical')
_CACHE_TAGS = {'lazy_cache': 'cache', 'local_probe_cache': 'cache'}
_UNCHANGED = 'OxiDNS 分流含自定义结构，DNS 未修改：{reason}'
_STALE_UPSTREAM = '所选上游已不在 OxiDNS 上游设置中，请刷新后重选'
_LINKED_UPSTREAM = '已绑定 DNS 联动，请改选直连 DNS'
_CACHED_HEAD = '自定义匹配位于规则之前且含缓存，拦截结果可能被缓存'


def _oxidns(config):
    value = config.get('oxidns')
    return value if isinstance(value, dict) else {}


def _native(settings):
    return settings.get('config_mode') == 'native'


def convertible(item):
    """Whether a rule can take part in DNS routing at all."""
    if item.get('itemType') == 'ruleset':
        return item.get('behavior', 'domain') != 'ipcidr'
    return item.get('rule_type') in DNS_RULE_TYPES


def has_ip_part(item, config=None):
    """Whether the source has IP entries (also used to recognize legacy routing).

    A classical set counts only when its known content has IP lines; unknown
    content, or no config to look it up in, keeps the old assumption that it does.
    """
    if item.get('itemType') == 'ruleset':
        behavior = item.get('behavior', 'domain')
        if behavior != 'classical' or config is None:
            return behavior in _IP_BEHAVIORS
        from backend.common.rule_ip_presence import classical_ip_lines
        return classical_ip_lines(item, config) is not False
    return item.get('rule_type') in IP_RULE_TYPES


def default_route(default_forward):
    """Where a query goes when no rule matched it."""
    return 'direct' if default_forward == 'forward_local' else 'proxy'


def probe_needed(routes, default_forward):
    """Recognize historical IP pipelines, never used to enable a new probe.

    IP groups only changed an answer when one routed away from the fallback.

    Without such a group the local probe would send every unmatched query to the
    local upstream and still end at the fallback, so it is left out.
    """
    fallback = default_route(default_forward)
    return any(route != fallback for route in routes)


def profile_agents(config):
    """Enabled agents bound to the profile of a resolved config."""
    default = (config.get('system_config') or {}).get('default_profile_id') or 'default'
    profile_id = config.get('profile_id') or default
    return [agent for agent in config.get('agents') or []
            if isinstance(agent, dict) and agent.get('enabled', True) and
            (agent.get('profile_id') or default) == profile_id]


def _bindings(agents):
    return [agent['dns_follow'] for agent in agents
            if agent.get('service_type') == 'oxidns' and isinstance(agent.get('dns_follow'), dict)]


def _valid_action(action, parked):
    try:
        return normalize_action(action, parked=parked)
    except (OxidnsSettingsError, TypeError, AttributeError):
        return None


def _stored(settings):
    """Stored routing fields, with the defaults older documents lack.

    Malformed actions (the repository refuses them, older documents may still
    hold one) are dropped, so they read as the default upstream instead of
    failing the rules page; the next routing write removes them.
    """
    values = {key: [value for value in settings.get(key) or [] if isinstance(value, str)] for key in SELECTORS}
    for key in ('rule_actions', 'rule_dns_parked'):
        value = settings.get(key)
        actions = {identity: _valid_action(action, key == 'rule_dns_parked')
                   for identity, action in value.items()} if isinstance(value, dict) else {}
        values[key] = {identity: action for identity, action in actions.items() if action is not None}
    follow = settings.get('rule_dns_follow')
    values['rule_dns_follow'] = list(dict.fromkeys(follow)) if isinstance(follow, list) else []
    return values


def _read(config, settings=None):
    """Current routing of a profile: values (the four lists, rule_actions,
    rule_dns_follow, rule_dns_parked), whether it can be edited and why not, and
    for native drafts the parsed document and full read_routing values."""
    settings = _oxidns(config) if settings is None else settings
    values = _stored(settings)
    state = {'values': values, 'editable': True, 'reason': '', 'document': None, 'routing': None}
    if not _native(settings):
        return state
    from backend.common.oxidns_common_routing import read_routing
    from backend.common.oxidns_native import load_native_yaml
    try:
        document = load_native_yaml(settings.get('native_config', ''))
    except OxidnsSettingsError as exc:
        return {**state, 'editable': False, 'reason': str(exc)}
    routing = read_routing(document, settings, config)
    cap = routing['capabilities']['direct_rules']
    values.update({key: routing['values'][key] for key in (*SELECTORS, 'rule_actions', 'rule_dns_follow')})
    return {**state, 'editable': cap['editable'], 'reason': cap['reason'], 'document': document,
            'routing': routing['values']}


def _route(values, identity):
    action = values['rule_actions'].get(identity)
    if action is not None:
        return route_key(action)
    return next((route for route in LIST_ROUTES
                 if identity in values[f'{route}_rules'] or identity in values[f'{route}_rulesets']), None)


def _active(config):
    from backend.converters.oxidns import _items
    return {item['id'] for item in _items(config) if isinstance(item.get('id'), str)}


def _upstream_label(action):
    return _label(action['source'], action['upstream']['addr'])


def _catalog_upstreams(settings, current, bindings):
    if _native(settings) and current['document'] is None:
        return []
    return upstream_catalog(settings, current['document'], bindings)


def _cache_warnings(settings, current):
    """Head custom matches run before every rule; a cache there may answer for a rule."""
    if current['routing'] is not None:
        matches, position = current['routing']['custom_matches'], current['routing']['custom_match_position']
        plugins = current['document'].get('plugins') if isinstance(current['document'], dict) else None
        types = {item.get('tag'): item.get('type') for item in plugins if isinstance(item, dict)} \
            if isinstance(plugins, list) else {}
    else:
        matches, position, types = settings.get('custom_matches') or [], settings.get('custom_match_position'), _CACHE_TAGS
    if position != 'head':
        return []
    for entry in matches if isinstance(matches, list) else ():
        words = entry.get('exec').split() if isinstance(entry, dict) and isinstance(entry.get('exec'), str) else []
        if any(types.get(word.lstrip('$')) == 'cache' for word in words):
            return [_CACHED_HEAD]
    return []


def rule_dns_state(config, base_url='', agents=None):
    """GET /api/rules/dns for one resolved profile config.

    Rules routed nowhere are omitted (default upstream). agents defaults to
    the enabled agents bound to the profile.
    """
    from backend.agents.rule_sync import settings_for
    settings = _oxidns(config)
    agents = profile_agents(config) if agents is None else agents
    current = _read(config, settings)
    values, stored = current['values'], _stored(settings)
    native = _native(settings)
    follow = set(values['rule_dns_follow'])
    active = _active(config)
    policy_routes = policy_dns_routes(config)
    listed = {identity for key in SELECTORS for identity in stored[key]}
    rules = {}
    for item in config.get('rule_configs') or []:
        identity = item.get('id') if isinstance(item, dict) else None
        if not isinstance(identity, str):
            continue
        if not convertible(item):
            kind = item.get("rule_type") or item.get("behavior") or "该类型"
            reason = ("IP 规则仅用于连接分流，不参与 DNS 处理"
                      if kind in (*IP_RULE_TYPES, "ipcidr") else f"{kind} 规则不参与 DNS 分流")
            rules[identity] = {"state": "unsupported", "reason": reason}
            continue
        route, action = _route(values, identity), values['rule_actions'].get(identity)
        if identity not in active:
            if identity in follow:
                route, action = follow_route(item, routes=policy_routes), None
            elif native and identity in values['rule_dns_parked']:
                action = values['rule_dns_parked'][identity]
                route = route_key(action)
            entry = {'state': 'disabled', 'route': route, **({'follow': True} if identity in follow else {})}
        elif identity in follow:
            expected = follow_route(item, routes=policy_routes)
            entry = {'state': 'follow' if route == expected else 'follow_drift', 'route': route,
                     'follow_route': expected}
        elif route is None:
            continue
        else:
            entry = {'state': 'explicit', 'route': route}
        if action is not None and action['action'] == 'upstream':
            entry['upstream_label'] = _upstream_label(action)
        if not native and identity in values['rule_actions'] and identity in listed:
            entry['in_lists'] = True
        rules[identity] = entry
    routing = current['routing'] or {}
    document = current['document'] if isinstance(current['document'], dict) else {}
    plugins = document.get('plugins') if isinstance(document.get('plugins'), list) else []
    main = next((item for item in plugins if isinstance(item, dict) and item.get('tag') == 'sequence_main'), {})
    steps = main.get('args') if isinstance(main.get('args'), list) else []
    default_forward = routing.get('default_forward') or settings.get('default_forward', 'forward_remote')
    # Retained for older clients and hand-written native pipelines. Managed
    # routing no longer probes a resolver to classify a returned IP.
    probe = native and any(step.get("exec") == "jump local_probe" for step in steps)
    in_use = (native or any(agent.get('service_type') == 'oxidns' for agent in agents) or
              any(stored[key] for key in (*SELECTORS, *RULE_DNS_FIELDS)))
    mosdns = config.get('mosdns') if isinstance(config.get('mosdns'), dict) else {}
    engine = {
        'mode': settings.get('config_mode', 'template'),
        'editable': current['editable'], 'reason': current['reason'],
        'default_forward': default_forward,
        'policy_routes': policy_routes,
        'prefer_ipv4': (any(isinstance(step, dict) and step.get('exec') == 'prefer_ipv4' for step in steps)
                        if native else settings.get('prefer_ipv4', True) is not False),
        'local_probe_active': probe,
        'oxidns_in_use': bool(in_use),
        'warnings': _cache_warnings(settings, current),
        'agents': [{'id': agent.get('id'), 'name': agent.get('name', agent.get('id')),
                    'service_type': agent.get('service_type'), 'auto_sync': settings_for(agent)}
                   for agent in agents],
    }
    upstreams = [{key: item[key] for key in ('key', 'source', 'addr', 'label', 'linked')}
                 for item in _catalog_upstreams(settings, current, _bindings(agents))]
    return {'engine': engine, 'upstreams': upstreams, 'rules': rules,
            'mosdns': {identity: route for route in LIST_ROUTES
                       for key in (f'{route}_rules', f'{route}_rulesets')
                       for identity in mosdns.get(key) or [] if isinstance(identity, str)}}


def explicit_route(config, identity):
    """A rule's own route (parked ones included), or None when it has none or
    follows its policy."""
    values = _read(config)['values']
    if identity in values['rule_dns_follow']:
        return None
    route = _route(values, identity)
    parked = values['rule_dns_parked'].get(identity)
    return route_key(parked) if route is None and parked is not None else route


def rule_dns_entries(config, identities, base_url=''):
    """Read-model entries of some rules, default ones spelled out."""
    rules = rule_dns_state(config, base_url)['rules']
    return {identity: rules.get(identity, {'state': 'default', 'route': None}) for identity in identities}


def check_choice(value):
    """A valid ``dns`` vocabulary value, or ProfileValidationError."""
    from backend.common.config_repository import ProfileValidationError
    if isinstance(value, str) and (value in CHOICES or (value.startswith('upstream:') and _KEY.fullmatch(value[9:]))):
        return value
    raise ProfileValidationError(f'不支持的 DNS 处理：{value}')


def _routing_view(config, routes):
    """What the routing of a profile depends on, besides the OxiDNS settings."""
    return [(item.get('id'), item.get('itemType'), item.get('rule_type'), item.get('value'),
             item.get('behavior'), item.get('url'), item.get('enabled', True), item.get('library_enabled', True),
             follow_route(item, routes=routes))
            for item in config.get('rule_configs') or [] if isinstance(item, dict)]


def _place(values, item, action):
    """Make action (None, or a parked-shape action) the only holder of a rule."""
    identity = item['id']
    for key in SELECTORS:
        values[key] = [value for value in values[key] if value != identity]
    values['rule_actions'].pop(identity, None)
    values['rule_dns_parked'].pop(identity, None)
    if action is None:
        return
    if action['action'] in LIST_ROUTES:
        values[f'{action["action"]}_{"rulesets" if item.get("itemType") == "ruleset" else "rules"}'].append(identity)
    else:
        values['rule_actions'][identity] = deepcopy(action)


def _resolve(choice, item, values, catalog, routes):
    """The stored action for one assignment; None for default upstream."""
    from backend.common.config_repository import ProfileValidationError
    if choice == 'default':
        return None
    if choice == 'follow':
        route = follow_route(item, routes=routes)
        return None if route is None else {'action': route}
    if not choice.startswith('upstream:'):
        return {'action': choice}
    current = values['rule_actions'].get(item['id']) or values['rule_dns_parked'].get(item['id'])
    if current is not None and route_key(current) == choice:
        return deepcopy(current)
    entry = next((entry for entry in catalog if entry['key'] == choice[9:]), None)
    if entry is None:
        raise ProfileValidationError(_STALE_UPSTREAM)
    if entry['linked']:
        raise ProfileValidationError(_LINKED_UPSTREAM)
    return {'action': 'upstream', 'source': entry['source'], 'upstream': deepcopy(entry['entry'])}


def _error_issues(document):
    from backend.common.oxidns_validation import validate_native_config
    return {(tuple(issue.get('path') or ()), issue.get('code'), issue.get('message'))
            for issue in validate_native_config(document) if issue.get('severity', 'error') == 'error'}


def _dirty(settings, stored, key):
    """Whether a stored field differs from what _stored() read from it."""
    return settings.get(key) is not None and settings[key] != stored[key]


def apply_rule_dns(before, after, assignments, *, base_url='', strict=True):
    """Apply one profile write's per-rule DNS choices to after['oxidns'] in place.

    before / after are the resolved profile snapshots around the write and
    assignments maps rule id -> ``dns`` choice. Follow rules whose policy
    route changed move with the policy; explicit choices never do. Native
    drafts are rebuilt for the new rule order, enablement and values in the
    same transaction. When the native routing cannot be rewritten, strict
    writes raise OxidnsRoutingLocked (nothing is saved) and the others keep the
    YAML and return a warning. Returns warning strings.
    """
    from backend.common.config_repository import ProfileValidationError
    from backend.common.oxidns_settings import _normalize_rule_dns
    rules = {item['id']: item for item in after.get('rule_configs') or []
             if isinstance(item, dict) and isinstance(item.get('id'), str)}
    previous = {item['id']: item for item in before.get('rule_configs') or []
                if isinstance(item, dict) and isinstance(item.get('id'), str)}
    settings = _oxidns(after)
    assignments = {identity: check_choice(choice) for identity, choice in assignments.items()}
    for identity, choice in assignments.items():
        if identity not in rules:
            raise ProfileValidationError(f'规则不存在：{identity}，请刷新后重试')
        if choice != 'default' and not convertible(rules[identity]):
            kind = rules[identity].get('rule_type') or rules[identity].get('behavior') or '该类型'
            raise ProfileValidationError(f'{kind} 规则不参与 DNS 分流，请把 DNS 设为默认上游')
    stored = _stored(settings)
    pruned = {key: ({identity: value for identity, value in stored[key].items() if identity in rules}
                    if isinstance(stored[key], dict) else [identity for identity in stored[key] if identity in rules])
              for key in RULE_DNS_FIELDS}
    before_routes, after_routes = policy_dns_routes(before), policy_dns_routes(after)
    if not assignments and _routing_view(before, before_routes) == _routing_view(after, after_routes):
        if any(pruned[key] != stored[key] or _dirty(settings, stored, key) for key in pruned):
            after['oxidns'].update(deepcopy(pruned))
        return []
    native = _native(settings)
    current = _read(before, settings)
    if native and current['document'] is None:
        if strict:
            raise OxidnsRoutingLocked(current['reason'])
        after['oxidns'].update(deepcopy(pruned))
        return [_UNCHANGED.format(reason=current['reason'])]
    values = deepcopy(current['values'])
    for key in SELECTORS:
        values[key] = [identity for identity in values[key] if identity in rules]
    # Native actions are read from the YAML; follow and parked are always stored.
    values['rule_actions'] = {identity: action for identity, action in values['rule_actions'].items()
                              if identity in rules}
    values['rule_dns_follow'] = deepcopy(pruned['rule_dns_follow'])
    values['rule_dns_parked'] = deepcopy(pruned['rule_dns_parked'])
    catalog = _catalog_upstreams(settings, current, _bindings(profile_agents(after))) \
        if any(choice.startswith('upstream:') for choice in assignments.values()) else []
    follow = values['rule_dns_follow']
    for identity, choice in assignments.items():
        item = rules[identity]
        _place(values, item, _resolve(choice, item, values, catalog, after_routes))
        if choice == 'follow':
            if identity not in follow:
                follow.append(identity)
        elif identity in follow:
            follow.remove(identity)
    for identity in follow:
        if identity in assignments or identity not in previous or identity not in rules:
            continue
        route = follow_route(rules[identity], routes=after_routes)
        if route != follow_route(previous[identity], routes=before_routes):
            _place(values, rules[identity], None if route is None else {'action': route})
    warnings = []
    if not native:
        try:
            _normalize_rule_dns(values)
        except OxidnsSettingsError as exc:
            raise ProfileValidationError(str(exc)) from None
        changed = {key: values[key] for key in (*SELECTORS, *RULE_DNS_FIELDS)
                   if values[key] != stored[key] or _dirty(settings, stored, key)}
        if changed:
            after.setdefault('oxidns', {}).update(deepcopy(changed))
    else:
        import yaml
        from backend.common.oxidns_common_routing import apply_routing
        active = _active(after)
        for identity in follow:
            # Follow rules are never parked: enabling them asks the policy again.
            if identity not in active:
                _place(values, rules[identity], None)
        changes = {key: values[key] for key in (*SELECTORS, 'rule_actions', 'rule_dns_follow')}
        source = {**deepcopy(settings), 'rule_dns_parked': values['rule_dns_parked'],
                  'rule_dns_follow': list(follow)}
        document = current['document']
        try:
            result, result_settings = apply_routing(document, source, after, changes, base_url, before_config=before)
            added = _error_issues(result) - _error_issues(document)
            if added:
                raise OxidnsSettingsError('写入后 OxiDNS 配置校验失败：' + sorted(added)[0][2])
        except OxidnsSettingsError as exc:
            if strict:
                raise OxidnsRoutingLocked(str(exc)) from None
            after['oxidns'].update(deepcopy(pruned))
            return [_UNCHANGED.format(reason=exc)]
        if not _same(result, document):
            settings['native_config'] = yaml.safe_dump(result, allow_unicode=True, sort_keys=False)
        for key in (*SELECTORS, *RULE_DNS_FIELDS):
            settings[key] = deepcopy(result_settings[key])
    return warnings


def invalidate_follow(config, settings):
    """settings, about to replace config's OxiDNS settings, without the follow
    marks of active rules whose actual route the replacement changes: a manual
    change in the OxiDNS editor makes the choice explicit."""
    follow = settings.get('rule_dns_follow')
    if not isinstance(follow, list) or not follow:
        return settings
    old, new = _read(config), _read(config, settings)
    if not old['editable'] or not new['editable']:
        return settings
    active = _active(config)
    changed = {identity for identity in follow if identity in active and
               _route(old['values'], identity) != _route(new['values'], identity)}
    return {**settings, 'rule_dns_follow': [identity for identity in follow if identity not in changed]} \
        if changed else settings
