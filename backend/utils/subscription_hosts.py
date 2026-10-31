"""Top-level ``hosts`` carried by Clash/Mihomo subscriptions.

Some providers put a ``hosts`` section next to ``proxies`` to steer node
domains, for example ``node.example.net: entry.example.net``. Mihomo reads only
``proxies`` from a proxy provider, so ConfigFlow keeps that section itself:

* Extraction runs on the downloaded body (never on Sub-Store conversions,
  which only contain ``proxies``) and stores validated entries with the node
  cache. Wildcards, ``lan`` and malformed values are dropped with a reason.
* Generation merges an entry only when its domain is the ``server`` of a node
  the generated configuration delivers from that same subscription, so a
  subscription cannot redirect any other domain (google.com, the panel, ...).
* Hand-written Mihomo ``hosts`` win, then MosDNS hosts synced for LAN Agents,
  then subscriptions in the order of the subscription list.

Mihomo (checked on v1.19.33) follows ``hosts`` for a proxy server inside its
own lookup only while the chain ends in IP addresses; for a domain-to-domain
entry it asks DNS for the node's original domain. Its DNS listener treats the
same entry as a CNAME, so the mapping applies once that node's domain is
resolved through the listener: ``backend.utils.proxy_server_dns`` routes just
the mapped node domains there with ``proxy-server-nameserver-policy``.
"""
import hashlib
import ipaddress
import json
import os
import re
import threading
from collections import OrderedDict
from urllib.parse import parse_qs, unquote, urlsplit

import yaml

from backend.utils.logger import get_logger, log_text

logger = get_logger(__name__)

MAX_ENTRIES = 1000
MAX_ADDRESSES = 8
MAX_SKIPPED_SAMPLES = 20
MAX_REPORT_ITEMS = 200

REASONS = {
    'parse_error': 'hosts 段无法解析',
    'not_mapping': 'hosts 不是「域名: 地址」的映射',
    'wildcard': '通配符条目不合并，只合并节点自己的域名',
    'invalid_domain': '域名无效',
    'invalid_value': '取值不是合法的 IP、IP 列表或域名',
    'too_many_addresses': f'IP 超过 {MAX_ADDRESSES} 个',
    'over_limit': f'超过每个订阅 {MAX_ENTRIES} 条的上限',
    'not_node_domain': '不是这个订阅里被使用节点的 server 域名',
    'custom_hosts': 'Mihomo 基础配置已手写这个域名，保留手写值',
    'mosdns_hosts': 'MosDNS 自定义 Hosts 已写这个域名，保留同步值',
    'conflict': '另一个订阅给了不同的值，按订阅列表顺序采用排在前面的订阅',
    'cycle': '会和已有 hosts 形成域名映射环',
}

_KEY_LINE = re.compile(r'^(?:hosts|"hosts"|\'hosts\')[ \t]*:', re.M)
_LABEL = re.compile(r'[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?')


def _domain(value):
    """A lowercase exact domain with at least two labels, or None."""
    if not isinstance(value, str):
        return None
    name = value.strip().lower().rstrip('.')
    if not name or len(name) > 253:
        return None
    labels = name.split('.')
    if len(labels) < 2 or not all(_LABEL.fullmatch(label) for label in labels):
        return None
    if all(label.isdigit() for label in labels):
        return None
    return name


def _address(value):
    if not isinstance(value, str):
        return None
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    if address.is_unspecified or address.is_multicast:
        return None
    return str(address)


def _normalize_value(key, value):
    """Return (stored value, reason). IPs are a list, a domain is a string."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        return None, 'invalid_value'
    if len(value) > MAX_ADDRESSES:
        return None, 'too_many_addresses'
    addresses = [_address(item) for item in value]
    if all(addresses):
        return list(dict.fromkeys(addresses)), None
    if len(value) == 1:
        target = _domain(value[0])
        if target and target != key:
            return target, None
    return None, 'invalid_value'


def _hosts_block(text):
    """Only the top-level ``hosts:`` block; a full YAML parse is far slower."""
    match = _KEY_LINE.search(text)
    if not match:
        return None
    lines = text[match.start():].splitlines()
    block = [lines[0]]
    for line in lines[1:]:
        if line[:1] in (' ', '\t') or not line.strip() or line.startswith('#'):
            block.append(line)
            continue
        break
    return '\n'.join(block)


def extract_subscription_hosts(text):
    """Validated ``hosts`` of a subscription body, or None when it has none.

    Returns ``{'entries': {domain: [ip, ...] | domain}, 'total': n,
    'skipped_count': n, 'skipped': [{'domain', 'reason'}, ...]}``.
    """
    if not isinstance(text, str) or 'hosts' not in text:
        return None
    block = _hosts_block(text)
    if block is None:
        return None
    try:
        document = yaml.safe_load(block)
    except yaml.YAMLError:
        return {'entries': {}, 'total': 0, 'skipped_count': 1,
                'skipped': [{'domain': '', 'reason': 'parse_error'}]}
    raw = document.get('hosts') if isinstance(document, dict) else None
    if raw is None:
        return None
    if not isinstance(raw, dict):
        return {'entries': {}, 'total': 0, 'skipped_count': 1,
                'skipped': [{'domain': '', 'reason': 'not_mapping'}]}
    entries, skipped, skipped_count = {}, [], 0
    for index, (key, value) in enumerate(raw.items()):
        reason = None
        name = str(key).strip().lower() if isinstance(key, (str, int, float)) else ''
        if index >= MAX_ENTRIES:
            reason = 'over_limit'
        elif '*' in name or name.startswith(('.', '+.')):
            reason = 'wildcard'
        elif not _domain(name) or _address(name):
            reason = 'invalid_domain'
        else:
            name = _domain(name)
            normalized, reason = _normalize_value(name, value)
            if reason is None:
                entries[name] = normalized
        if reason is not None:
            skipped_count += 1
            if len(skipped) < MAX_SKIPPED_SAMPLES:
                skipped.append({'domain': name[:253], 'reason': reason})
    return {'entries': entries, 'total': len(raw), 'skipped_count': skipped_count, 'skipped': skipped}


def hosts_digest(record):
    entries = (record or {}).get('entries') or {}
    return hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()


def describe_change(previous, current):
    """Counts of added / removed / changed entries between two records."""
    old = (previous or {}).get('entries') or {}
    new = (current or {}).get('entries') or {}
    return {'added': len(new.keys() - old.keys()), 'removed': len(old.keys() - new.keys()),
            'changed': sum(1 for key in new.keys() & old.keys() if new[key] != old[key])}


# ---------------------------------------------------------------- cache index

_INDEX_LIMIT = 256
_index = OrderedDict()
_index_lock = threading.Lock()


def _normalize_server(value):
    return _domain(value) if isinstance(value, str) and not _address(value) else None


def subscription_hosts_index(sub):
    """Stored hosts of the current URL and the names of nodes per hosts domain.

    Memoized per cache file stamp, so generation does not re-read node caches
    that did not change. Returns None when the cache has no hosts.
    """
    from backend.utils.subscription_cache import _get_cache_path, load_subscription_cache
    try:
        path = _get_cache_path(sub['id'])
        stat = os.stat(path)
    except (OSError, KeyError, TypeError):
        return None
    stamp = (stat.st_mtime_ns, stat.st_size)
    with _index_lock:
        cached = _index.get(path)
        if cached is not None and cached[0] == stamp:
            _index.move_to_end(path)
            value = cached[1]
            return value if value is None or value['url'] == sub.get('url') else None
    cache = load_subscription_cache(sub['id']) or {}
    metadata = cache.get('metadata') or {}
    record = metadata.get('hosts')
    value = None
    if isinstance(record, dict) and isinstance(record.get('entries'), dict):
        entries = record['entries']
        nodes = {}
        for node in cache.get('nodes') or []:
            server = _normalize_server(node.get('server')) if isinstance(node, dict) else None
            if server in entries:
                nodes.setdefault(server, []).append(str(node.get('name', '')))
        value = {'url': metadata.get('url'), 'entries': entries, 'nodes': nodes,
                 'total': int(record.get('total') or 0), 'skipped_count': int(record.get('skipped_count') or 0),
                 'skipped': list(record.get('skipped') or [])}
    with _index_lock:
        _index[path] = (stamp, value)
        _index.move_to_end(path)
        while len(_index) > _INDEX_LIMIT:
            _index.popitem(last=False)
    return value if value is None or value['url'] == sub.get('url') else None


# --------------------------------------------------------------------- merge

def _matches(pattern, domain):
    """Mihomo domain-trie matching for one hosts key (exact, ``*``, ``.``, ``+.``)."""
    if not isinstance(pattern, str):
        return False
    pattern = pattern.strip().lower().rstrip('.')
    if pattern.startswith('+.'):
        base = pattern[2:]
        return domain == base or domain.endswith('.' + base)
    if pattern.startswith('.'):
        return domain.endswith(pattern)
    if '*' not in pattern:
        return pattern == domain
    left, right = pattern.split('.'), domain.split('.')
    return len(left) == len(right) and all(a == '*' or a == b for a, b in zip(left, right))


def _provider_sources(main, config):
    """(kind, id, query) of every provider ConfigFlow generated in ``main``."""
    subscriptions = {item.get('id') for item in config.get('subscriptions', [])}
    aggregations = {item.get('id') for item in config.get('subscription_aggregations', [])}
    for definition in (main.get('proxy-providers') or {}).values():
        url = definition.get('url') if isinstance(definition, dict) else None
        if not isinstance(url, str):
            continue
        parts = urlsplit(url)
        path = unquote(parts.path)
        query = parse_qs(parts.query)
        if '/subscriptions/' in path:
            ident = path.rsplit('/subscriptions/', 1)[1].split('/')[0]
            if ident in subscriptions:
                yield 'subscription', ident, query
        elif '/aggregations/' in path:
            ident = path.rsplit('/aggregations/', 1)[1].split('/')[0]
            if ident in aggregations:
                yield 'aggregation', ident, query


def _query_filter(query):
    include = (query.get('regex') or [''])[0]
    exclude = (query.get('exclude_regex') or [''])[0]
    if not include and not exclude:
        return None
    try:
        keep = re.compile(include) if include.strip() else None
        drop = re.compile(exclude) if exclude.strip() else None
    except re.error:
        return None
    return lambda name: (keep is None or keep.search(name)) and (drop is None or not drop.search(name))


def _used_domains(main, config, inline_proxies):
    """Map subscription id -> node server domains the configuration delivers."""
    from backend.utils.subscription_filter import compile_subscription_filter
    subscriptions = {item['id']: item for item in config.get('subscriptions', [])
                     if isinstance(item, dict) and item.get('id') and item.get('enabled', True)}
    used, indexes = {}, {}

    def index_of(sub_id):
        if sub_id not in indexes:
            indexes[sub_id] = subscription_hosts_index(subscriptions[sub_id]) if sub_id in subscriptions else None
        return indexes[sub_id]

    def add(sub_id, domains):
        if domains:
            used.setdefault(sub_id, set()).update(domains)

    def from_subscription(sub_id, *name_filters):
        index = index_of(sub_id)
        if index is None:
            return
        regex = compile_subscription_filter(subscriptions[sub_id])
        checks = [check for check in ((lambda n: regex.search(n)) if regex else None, *name_filters) if check]
        used.setdefault(sub_id, set())
        add(sub_id, {domain for domain, names in index['nodes'].items()
                     if any(all(check(name) for check in checks) for name in names)})

    nodes = {node.get('id'): node for node in config.get('nodes', []) if isinstance(node, dict)}

    def from_node(node, proxy):
        sub_id = node.get('subscription_id')
        server = _normalize_server((proxy or {}).get('server', node.get('server')))
        if sub_id in subscriptions and server:
            used.setdefault(sub_id, set()).add(server)

    for kind, ident, query in _provider_sources(main, config):
        query_check = _query_filter(query)
        extra = (query_check,) if query_check else ()
        if kind == 'subscription':
            if ident in subscriptions:
                from_subscription(ident, *extra)
            continue
        aggregation = next(item for item in config.get('subscription_aggregations', []) if item.get('id') == ident)
        if not aggregation.get('enabled', True):
            continue
        pattern = (aggregation.get('regex_filter') or '').strip()
        try:
            agg_regex = re.compile(pattern) if pattern else None
        except re.error:
            continue
        checks = tuple(check for check in ((lambda n, r=agg_regex: r.search(n)) if agg_regex else None, query_check) if check)
        for sub_id in aggregation.get('subscriptions', []):
            if sub_id in subscriptions:
                from_subscription(sub_id, *checks)
        for node_id in aggregation.get('nodes', []):
            node = nodes.get(node_id)
            if node and node.get('enabled', True) and all(check(str(node.get('name', ''))) for check in checks):
                from_node(node, None)
    for node, proxy in inline_proxies:
        from_node(node, proxy)
    return used, subscriptions, index_of


def _creates_cycle(hosts, key, value):
    """Mihomo refuses the whole configuration when domain mappings loop.

    Its check follows trie matches, so wildcard keys count as well.
    """
    if not isinstance(value, str):
        return False
    pending, seen = [value], set()
    while pending:
        current = pending.pop()
        if current == key:
            return True
        if current in seen or len(seen) > 256:
            continue
        seen.add(current)
        for pattern, target in hosts.items():
            if _matches(pattern, current) and isinstance(target, str) and not _address(target):
                target = target.strip().lower().rstrip('.')
                if target == key:
                    return True
                pending.append(target)
    return False


def _emit(value):
    if isinstance(value, list):
        return value[0] if len(value) == 1 else list(value)
    return value


def merge_subscription_hosts(mihomo_config, config, *, user_keys, mosdns_keys, inline_proxies, merged_out=None):
    """Merge permitted subscription hosts into ``mihomo_config``; returns a report.

    ``merged_out`` receives the merged ``{domain: value}`` (values as stored).
    """
    used, subscriptions, index_of = _used_domains(mihomo_config, config, inline_proxies)
    hosts = mihomo_config.get('hosts')
    hosts = dict(hosts) if isinstance(hosts, dict) else {}
    items, merged, ignored = [], {}, 0
    per_subscription = []
    owners = {}

    def note(entry):
        if len(items) < MAX_REPORT_ITEMS:
            items.append(entry)

    for sub in config.get('subscriptions', []):
        sub_id = sub.get('id') if isinstance(sub, dict) else None
        if sub_id not in used:
            continue
        index = index_of(sub_id)
        if index is None:
            continue
        name = sub.get('name') or sub_id
        accepted = 0
        domains = used[sub_id]
        for domain, value in index['entries'].items():
            reason = None
            if domain not in domains:
                reason = 'not_node_domain'
            elif any(_matches(key, domain) for key in user_keys):
                reason = 'custom_hosts'
            elif any(_matches(key, domain) for key in mosdns_keys):
                reason = 'mosdns_hosts'
            elif domain in owners:
                if merged[domain] != value:
                    reason = 'conflict'
            elif _creates_cycle(hosts, domain, value):
                reason = 'cycle'
            entry = {'subscription': name, 'domain': domain, 'value': _emit(value)}
            if reason:
                ignored += 1
                detail = REASONS[reason]
                if reason == 'conflict':
                    detail = f'订阅「{owners[domain]}」给了不同的值，按订阅列表顺序采用「{owners[domain]}」'
                note({**entry, 'status': 'ignored', 'reason': reason, 'detail': detail})
                continue
            accepted += 1
            if domain not in owners:
                owners[domain] = name
                merged[domain] = value
                hosts[domain] = _emit(value)
                note({**entry, 'status': 'merged', 'kind': 'domain' if isinstance(value, str) else 'address'})
        for sample in index['skipped']:
            note({'subscription': name, 'domain': sample.get('domain', ''), 'status': 'ignored',
                  'reason': sample.get('reason'), 'detail': REASONS.get(sample.get('reason'), '条目无效')})
        ignored += index['skipped_count']
        per_subscription.append({'id': sub_id, 'name': name, 'total': index['total'], 'merged': accepted,
                                 'ignored': len(index['entries']) - accepted + index['skipped_count']})
    if merged:
        mihomo_config['hosts'] = hosts
    if merged_out is not None:
        merged_out.update(merged)
    domain_mapped = sum(1 for value in merged.values() if isinstance(value, str))
    return {'merged': len(merged), 'ignored': ignored, 'domain_mapped': domain_mapped,
            'subscriptions': per_subscription, 'items': items}


_logged = {}
_logged_lock = threading.Lock()


def log_report(profile_id, report):
    """Log each distinct outcome once per profile, not on every generation."""
    if not report or not (report['merged'] or report['ignored']):
        return
    summary = {key: report[key] for key in ('merged', 'ignored', 'domain_mapped')}
    reasons = {}
    for item in report['items']:
        if item['status'] == 'ignored':
            reasons[item['reason']] = reasons.get(item['reason'], 0) + 1
    digest = hashlib.sha256(json.dumps([summary, report['items']], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    with _logged_lock:
        if _logged.get(profile_id) == digest:
            return
        _logged[profile_id] = digest
        while len(_logged) > 64:
            _logged.pop(next(iter(_logged)))
    detail = '；'.join(f'{REASONS.get(reason, reason)} {count} 条' for reason, count in reasons.items())
    logger.info('订阅 hosts：合并 %d 条（其中域名映射 %d 条），忽略 %d 条%s', report['merged'],
                report['domain_mapped'], report['ignored'], f'（{log_text(detail)}）' if detail else '')
