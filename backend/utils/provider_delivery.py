"""Request-local provider delivery validation, before any durable side effect."""
from dataclasses import dataclass
import json

from backend.utils.dialer_references import DialerReferenceError, validate_emitted


@dataclass(frozen=True)
class DeliverySnapshot:
    profile_id: str
    main_json: str
    chain_names: frozenset = frozenset()

    @classmethod
    def capture(cls, profile_id, main, *, profile=None):
        names = frozenset(g['name'] for g in (profile or {}).get('proxy_groups', [])
                          if g.get('type') == 'chain' and g.get('enabled', True))
        return cls(profile_id, json.dumps(main), names)

    @property
    def has_chains(self):
        main = json.loads(self.main_json)
        return bool(self.chain_names) or any(p.get('dialer-proxy') is not None for p in main.get('proxies', [])) or any(
            p.get('override', {}).get('dialer-proxy') for p in main.get('proxy-providers', {}).values())

    def validate(self, proxies, *, force_chains=False, provider_name=None):
        validate_proxy_shapes(proxies)
        main = json.loads(self.main_json)
        # Suppress only cross-artifact overlap, never duplicates inside a provider.
        names = {p['name'] for p in proxies}
        if names.intersection(self.chain_names):
            raise DialerReferenceError('Provider 节点名称与代理链出口冲突')
        # Cross-artifact overlap can remove the last visible main dialer. Strict
        # activation belongs to the immutable original snapshot, not that filtered
        # graph; an explicit False from a caller must never deactivate it.
        if provider_name is not None and provider_name in main.get('proxy-providers', {}):
            definitions = main.get('proxy-providers', {})
            source = definitions.get(provider_name, {})
            payloads = {name: proxies for name, definition in definitions.items()
                        if name == provider_name or (source.get('url') and definition.get('url') == source['url'])}
            validate_emitted(main, force_chains=self.has_chains or force_chains, provider_proxies=payloads)
        else:
            validate_emitted({**main,
                'proxies': [p for p in main.get('proxies', []) if p['name'] not in names] + proxies,
            }, force_chains=self.has_chains or force_chains)


def validate_proxy_shapes(proxies):
    if not isinstance(proxies, list):
        raise DialerReferenceError('Provider proxies 必须是数组')
    for proxy in proxies:
        if (not isinstance(proxy, dict) or not isinstance(proxy.get('name'), str)
                or not proxy['name'].strip()):
            raise DialerReferenceError('Provider 代理必须是含非空 name 的对象')
        if 'proxies' in proxy and (not isinstance(proxy['proxies'], list) or
                any(not isinstance(name, str) or not name.strip() for name in proxy['proxies'])):
            raise DialerReferenceError('Provider proxies 引用必须是字符串数组')
        if 'type' in proxy and not isinstance(proxy['type'], str):
            raise DialerReferenceError('Provider type 必须是字符串')


def parse_provider_proxies(text):
    """Reject malformed metadata before legacy field normalization can mask it."""
    import yaml
    from backend.utils.sub_store_client import parse_proxies_from_yaml
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        # Content errors must propagate through the transport-fallback boundary.
        # Do not include provider bytes or parser excerpts in the public error.
        raise DialerReferenceError('Provider YAML 内容无效') from exc
    if not isinstance(data, dict):
        raise DialerReferenceError('Provider 必须是 YAML 对象')
    validate_proxy_shapes(data.get('proxies', []))
    return parse_proxies_from_yaml(text)


def prepare_subscription_provider(sub, config, snapshot, *, fetch=None,
                                  allow_transport_fallback=False):
    """Return exact validated output and staged cache update; never write here."""
    import uuid
    import yaml
    from backend.utils.sub_store_client import (get_subscription_proxies_yaml,
                                               parse_proxies_from_yaml, proxies_to_nodes)
    from backend.utils.subscription_cache import load_subscription_cache
    from backend.utils.strategy_references import StrategyReferenceError
    from backend.utils.dialer_references import validate_shapes
    from backend.converters.mihomo import convert_node_to_mihomo, IndentDumper

    fetch = fetch or get_subscription_proxies_yaml
    update = None
    proxies = None
    if sub.get('url'):
        try:
            text, _source = fetch(sub['id'], sub['url'])
            proxies = parse_provider_proxies(text)
            snapshot.validate(proxies, provider_name=sub['name'])
            if proxies:
                nodes = proxies_to_nodes(proxies)
                for node in nodes:
                    node['subscription_id'] = sub['id']
                    node['subscription_name'] = sub['name']
                    node.setdefault('id', f"node_{uuid.uuid4().hex[:8]}")
                update = (sub['id'], nodes, {'subscription_name': sub['name'], 'url': sub['url']})
        except StrategyReferenceError:
            raise
        except Exception:
            proxies = None
    if proxies is None:
        cache = load_subscription_cache(sub['id'])
        if not cache or not cache.get('nodes'):
            if allow_transport_fallback and not snapshot.has_chains:
                return {'content': '', 'cache_updates': []}
            raise DialerReferenceError('订阅没有可用缓存或远程代理')
        validate_shapes({'nodes': cache['nodes']}, require_ids=False)
        proxies = [p for n in cache['nodes'] if (p := convert_node_to_mihomo(n))]
    snapshot.validate(proxies, provider_name=sub['name'])
    content = yaml.dump({'proxies': proxies}, Dumper=IndentDumper, allow_unicode=True,
                        default_flow_style=False, sort_keys=False, indent=2)
    return {'name': sub['name'], 'content': content, 'cache_updates': [update] if update else []}


def validate_rendered_bundle(snapshot, providers):
    """Validate all exact provider collections with one monotonic activation.

    Providers are already materialized and their cache updates remain staged.
    Never fetch/render again here: discovery and final validation must inspect
    the same bytes, including providers rendered before a late chain appeared.
    """
    import yaml
    collections = []
    for provider in providers:
        content = provider['content']
        if content:
            try:
                data = yaml.safe_load(content)
            except yaml.YAMLError as exc:
                raise DialerReferenceError('Provider YAML 内容无效') from exc
            if not isinstance(data, dict):
                raise DialerReferenceError('Provider 必须是 YAML 对象')
            proxies = data.get('proxies', [])
            validate_proxy_shapes(proxies)
        else:
            proxies = None
        collections.append(proxies)
    chains = snapshot.has_chains or any(
        p.get('dialer-proxy') is not None
        for proxies in collections if proxies is not None for p in proxies)
    if chains and any(proxies is None for proxies in collections):
        raise DialerReferenceError('拨号拓扑不能使用未验证的 Provider URL fallback')
    # Provider-only activation also applies to the original main namespace.
    snapshot.validate([], force_chains=chains)
    main = json.loads(snapshot.main_json)
    payloads = {}
    for provider, proxies in zip(providers, collections):
        if proxies is None:
            continue
        name = provider.get('name')
        snapshot.validate(proxies, force_chains=chains, provider_name=name)
        if name:
            payloads[name] = proxies
    if payloads:
        definitions = main.get('proxy-providers', {})
        by_url = {definitions[name]['url']: proxies for name, proxies in payloads.items()
                  if name in definitions and definitions[name].get('url')}
        for name, definition in definitions.items():
            if definition.get('url') in by_url:
                payloads[name] = by_url[definition['url']]
    validate_emitted(main, force_chains=chains, provider_proxies=payloads, require_providers=chains)
    return chains


def commit_cache_updates(updates):
    from backend.utils.subscription_cache import save_subscription_nodes
    for sub_id, nodes, metadata in updates:
        save_subscription_nodes(sub_id, nodes, metadata)


def prepare_provider_bundle(config, main, *, requested=None, render_all=False, allow_transport_fallback=False,
                            prepared=None, subscription_fetch=None):
    """Capture once, render each original source once, validate all copies, never commit."""
    from urllib.parse import urlsplit, unquote
    from backend.routes.aggregations import (generate_aggregation_provider,
        get_subscription_proxies_yaml as get_aggregation_subscription)
    from backend.routes.subscriptions import get_subscription_proxies_yaml
    from backend.converters.mihomo import _parse_structured_proxy_string
    from concurrent.futures import Future
    from threading import Lock
    from copy import deepcopy
    from functools import partial

    requested_name = None
    if requested:
        kind, resource = requested
        requested_name = resource['name']
        catalog = 'subscriptions' if kind == 'subscription' else 'subscription_aggregations'
        if not any(item['id'] == resource['id'] for item in config.get(catalog, [])):
            config = deepcopy(config)
            config.setdefault(catalog, []).append(deepcopy(resource))
    fetch_results, fetch_lock = {}, Lock()
    for sub in config.get('subscriptions', []):
        if prepared and sub['name'] in prepared:
            result = Future()
            result.set_result((prepared[sub['name']]['content'], 'prepared_yaml'))
            fetch_results[(sub['id'], sub.get('url'))] = result

    def fetch_subscription(id, url, *, fetch):
        key = (id, url)
        with fetch_lock:
            owner = key not in fetch_results
            if owner:
                fetch_results[key] = Future()
            result = fetch_results[key]
        if owner:
            try:
                result.set_result((subscription_fetch or fetch)(id, url))
            except Exception as exc:
                result.set_exception(exc)
        return result.result()

    snapshot = DeliverySnapshot.capture(config.get('profile_id') or 'default', main, profile=config)
    definitions = main.get('proxy-providers', {})
    if requested and requested_name not in definitions:
        main = deepcopy(main)
        definitions = main.setdefault('proxy-providers', {})
        kind, resource = requested
        segment, suffix = ('subscriptions', 'proxies') if kind == 'subscription' else ('aggregations', 'provider')
        definitions[requested_name] = {'url': f"/{segment}/{resource['id']}/{suffix}",
                                       'path': f"./providers/{resource['id']}.yaml"}
        snapshot = DeliverySnapshot.capture(config.get('profile_id') or 'default', main, profile=config)
    sources = {}
    for name, definition in definitions.items():
        url = definition.get('url', '')
        sources.setdefault(url, name)
    rendered = {}
    opaque = {n['id'] for n in config.get('nodes', []) if n.get('proxy_string') and
              _parse_structured_proxy_string(n['proxy_string']) is None}

    def materialize(url, name):
        if prepared and name in prepared:
            rendered[url] = {**prepared[name], 'name': name}
            return
        path = unquote(urlsplit(url).path)
        if '/subscriptions/' in path:
            id = path.rsplit('/subscriptions/', 1)[1].split('/')[0]
            sub = next(s for s in config.get('subscriptions', []) if s['id'] == id)
            value = prepare_subscription_provider(sub, config, snapshot,
                                                  fetch=partial(fetch_subscription, fetch=get_subscription_proxies_yaml),
                                                  allow_transport_fallback=allow_transport_fallback)
        elif '/aggregations/' in path:
            id = path.rsplit('/aggregations/', 1)[1].split('/')[0]
            agg = next(a for a in config.get('subscription_aggregations', []) if a['id'] == id)
            value = generate_aggregation_provider(agg, config=config, main_config=main, persist=False,
                                                  subscription_fetch=partial(fetch_subscription, fetch=get_aggregation_subscription))
            value['aggregation_id'] = id
        else:
            raise DialerReferenceError('拨号拓扑 Provider 必须使用当前配置空间的订阅或聚合来源')
        value['name'] = name
        rendered[url] = value

    for url, name in sources.items():
        path = unquote(urlsplit(url).path)
        agg = next((a for a in config.get('subscription_aggregations', [])
                    if f"/aggregations/{a['id']}/" in path), None)
        if render_all or snapshot.has_chains or name == requested_name or (agg and opaque.intersection(agg.get('nodes', []))):
            materialize(url, name)
    import yaml
    active = snapshot.has_chains or any(p.get('dialer-proxy') is not None for value in rendered.values()
                                        for p in (yaml.safe_load(value['content']) or {}).get('proxies', []))
    if active:
        for url, name in sources.items():
            if url not in rendered:
                materialize(url, name)
    bundle = [{**rendered[definition['url']], 'name': name, 'url': definition['url'],
               'local_path': definition['path']} for name, definition in definitions.items()
              if definition.get('url') in rendered]
    validate_rendered_bundle(snapshot, bundle)
    return bundle


def commit_provider_bundle(profile_id, bundle):
    """Only call after the complete exact bundle passes validation."""
    from backend.common.config import get_repository
    updates, artifacts = {}, {}
    for item in bundle:
        for update in item.get('cache_updates', []):
            updates[update[0]] = update
        if item.get('aggregation_id'):
            artifacts[item['aggregation_id']] = item['content']
    commit_cache_updates(updates.values())
    for id, content in artifacts.items():
        get_repository().write_profile_text(profile_id, f'providers/{id}.yaml', content)
