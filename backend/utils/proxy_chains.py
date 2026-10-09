"""Compile profile-local chains into independent Mihomo nodes and groups."""
from copy import deepcopy
import hashlib


def validate_proxy_chains(profile):
    from backend.utils.dialer_references import DialerReferenceError, group_members

    groups = {g['id']: g for g in profile.get('proxy_groups', [])}
    nodes = {n['id']: n for n in profile.get('nodes', [])}
    chains = {id: g for id, g in groups.items() if g.get('type') == 'chain'}
    for group in groups.values():
        if group.get('follow_group') in chains:
            raise DialerReferenceError('跟随模式不能引用代理链，请使用策略组引用')
        if chains and group.get('enabled', True) and group.get('type') != 'chain':
            for kind, id in group_members(group):
                if kind == 'group' and (id not in groups or not groups[id].get('enabled', True)):
                    raise DialerReferenceError('策略组引用不存在或未启用')
    for group in chains.values():
        chain = group.get('chain')
        if not isinstance(chain, dict) or set(chain) != {'entry', 'exit'}:
            raise DialerReferenceError('代理链 chain 必须包含 entry 和 exit')
        for endpoint in ('entry', 'exit'):
            ref = chain[endpoint]
            if (not isinstance(ref, dict) or set(ref) != {'type', 'id'} or
                    ref.get('type') not in ('node', 'group') or
                    not isinstance(ref.get('id'), str) or not ref['id'].strip()):
                raise DialerReferenceError('代理链端点必须为 {type: node|group, id: 非空字符串}')
            target = (nodes if ref['type'] == 'node' else groups).get(ref['id'])
            if (not target or ((endpoint == 'entry' or group.get('enabled', True)) and
                               not target.get('enabled', True))):
                raise DialerReferenceError('代理链依赖不存在或未启用')
            if ref['type'] == 'node' and target.get('subscription_id'):
                raise DialerReferenceError('代理链仅支持共享手动节点，订阅请通过策略组使用')
        if chain['entry']['type'] == 'node' and chain['entry'] == chain['exit']:
            raise DialerReferenceError('代理链前置与落地不能是同一资源')
        if any(group.get(k) for k in ('manual_nodes', 'include_groups', 'proxies_order',
                'proxies', 'subscriptions', 'aggregations', 'use', 'follow_group', 'include_all', 'include-all')):
            raise DialerReferenceError('代理链不能同时配置普通策略组来源')


def lower_proxy_chains(profile):
    """Private copies retain dynamic sources; the emitter clones their providers."""
    from backend.utils.dialer_references import validate_dialers, group_members, DialerReferenceError

    validate_dialers(profile)
    active = any(g.get('type') == 'chain' for g in profile.get('proxy_groups', [])) or any(
        n.get('dialer_ref') is not None for n in profile.get('nodes', []))
    if not active:
        return profile
    runtime = deepcopy(profile)
    groups = {g['id']: g for g in runtime.get('proxy_groups', [])}
    providers = [*runtime.get('subscriptions', []), *runtime.get('subscription_aggregations', [])]
    provider_names = [p['name'] for p in providers if p.get('enabled', True)]
    if len(provider_names) != len(set(provider_names)):
        raise DialerReferenceError('订阅与聚合 Provider 名称必须唯一')
    for group in runtime.get('proxy_groups', []):
        original = group
        seen = set()
        while original.get('follow_group'):
            if original['id'] in seen or original['follow_group'] not in groups:
                raise DialerReferenceError('跟随策略组依赖不存在或存在循环')
            seen.add(original['id'])
            original = groups[original['follow_group']]
        if original is not group:
            identity = {key: group[key] for key in ('id', 'name', 'enabled') if key in group}
            group.clear()
            group.update(deepcopy(original), **identity)
            group.pop('follow_group', None)
        for name in group.pop('use', []):
            for key in ('subscriptions', 'subscription_aggregations'):
                source = next((p for p in runtime.get(key, []) if p['name'] == name), None)
                if source:
                    field = 'aggregations' if key == 'subscription_aggregations' else 'subscriptions'
                    if source['id'] not in group.setdefault(field, []):
                        group[field].append(source['id'])
        for item in group.get('proxies_order', []):
            field = {'aggregation': 'aggregations', 'subscription': 'subscriptions'}.get(item['type'])
            if field and item['id'] not in group.setdefault(field, []):
                group[field].append(item['id'])
    chains = {id: g for id, g in groups.items() if g.get('type') == 'chain'}
    if not chains:
        return runtime
    nodes = {n['id']: n for n in runtime.get('nodes', [])}
    runtime['proxy_groups'] = [g for g in runtime['proxy_groups'] if g.get('type') != 'chain']
    occupied_ids = set(nodes) | set(groups)
    occupied_names = {o['name'] for o in [*nodes.values(), *groups.values()]}
    results = {}

    def private_id(seed):
        value = '__chain__' + hashlib.sha256(seed.encode()).hexdigest()[:20]
        while value in occupied_ids:
            value += '_'
        occupied_ids.add(value)
        return value

    def private_name(base):
        name, suffix = base, 2
        while name in occupied_names:
            name = f'{base} ({suffix})'
            suffix += 1
        occupied_names.add(name)
        return name

    def reference(ref):
        if ref['type'] == 'group' and ref['id'] in chains:
            return build_chain(ref['id'])
        return deepcopy(ref)

    def clone(ref, dialer, seed, scope, name=None):
        id = private_id(seed)
        original = (nodes if ref['type'] == 'node' else groups)[ref['id']]
        name = name or private_name(f"{scope}_{original['name']}")
        if ref['type'] == 'node':
            obj = deepcopy(nodes[ref['id']])
            obj.update(id=id, name=name, enabled=True, dialer_ref=deepcopy(dialer), _proxy_chain_id=seed)
            runtime['nodes'].append(obj)
            return {'type': 'node', 'id': id}
        if original.get('type') == 'chain':
            # A -> (B -> C) is (A -> B) -> C, not A -> C.
            entry = clone(original['chain']['entry'], dialer, seed + '/entry', scope)
            return clone(original['chain']['exit'], entry, seed + '/exit', scope, name)
        seen = set()
        while original.get('follow_group'):
            if original['id'] in seen:
                raise DialerReferenceError('跟随策略组依赖存在循环')
            seen.add(original['id'])
            original = groups[original['follow_group']]
        obj = deepcopy(original)
        obj.update(id=id, name=name, enabled=True, _chain_dialer=deepcopy(dialer))
        obj.pop('follow_group', None)
        members = group_members(original)
        if not original.get('proxies_order') and original.get('proxy_order') == 'strategies_first':
            members.sort(key=lambda item: item[0] != 'group')
        order = []
        originals = {}
        for index, (kind, target) in enumerate(members):
            if kind == 'builtin':
                raise DialerReferenceError('代理链落地组不能包含 DIRECT/REJECT 等非代理候选')
            candidate = clone({'type': kind, 'id': target}, dialer, f'{seed}/{index}', scope)
            order.append({'type': 'node' if candidate['type'] == 'node' else 'strategy', 'id': candidate['id']})
            original_candidate = (nodes if kind == 'node' else groups)[target]
            originals[candidate['id']] = {'name': original_candidate['name']}
        obj['manual_nodes'] = [i['id'] for i in order if i['type'] == 'node']
        obj['include_groups'] = [i['id'] for i in order if i['type'] == 'strategy']
        obj['proxies_order'] = order + [deepcopy(i) for i in original.get('proxies_order', [])
                                       if i['type'] not in ('node', 'strategy')]
        obj['_chain_original_candidates'] = originals
        obj.pop('proxies', None)
        runtime['proxy_groups'].append(obj)
        return {'type': 'group', 'id': id}

    def build_chain(id):
        if id not in results:
            chain = chains[id]
            results[id] = clone(chain['chain']['exit'], reference(chain['chain']['entry']),
                                id, chain['name'], chain['name'])
        return deepcopy(results[id])

    for id, chain in chains.items():
        if chain.get('enabled', True):
            build_chain(id)
    for node in runtime.get('nodes', []):
        if node.get('dialer_ref') is not None:
            node['dialer_ref'] = reference(node['dialer_ref'])
    for group in runtime['proxy_groups']:
        if group.get('_chain_dialer'):
            continue
        includes = group.get('include_groups', [])
        legacy = not any(group.get(k) for k in ('manual_nodes', 'include_groups', 'subscriptions', 'aggregations'))
        if legacy and group.get('source') == 'strategy':
            includes = group.get('proxies', [])
        if not group.get('proxies_order') and any(id in chains for id in includes):
            node_order = [{'type': 'node', 'id': id} for id in group.get('manual_nodes', [])]
            group_order = [{'type': 'strategy', 'id': id} for id in includes]
            group['proxies_order'] = (group_order + node_order if group.get('proxy_order') == 'strategies_first'
                                      else node_order + group_order)
        for item in group.get('proxies_order', []):
            if item['type'] == 'strategy' and item['id'] in results:
                ref = results[item['id']]
                item.update(type='node' if ref['type'] == 'node' else 'strategy', id=ref['id'])
        group['include_groups'] = [results[id]['id'] if id in results and results[id]['type'] == 'group' else id
                                   for id in includes if id not in chains or (id in results and results[id]['type'] == 'group')]
        if legacy and group.get('source') == 'strategy':
            group['proxies'] = group['include_groups'][:]
    return runtime


def lower_chain_providers(runtime, main):
    """Mihomo filters original names before applying provider overrides."""
    import re
    from backend.utils.dialer_references import DialerReferenceError, resolve_dialer, filter_provider_candidates

    def name_pattern(name):
        # Literal name backticks must not become Mihomo filter separators.
        return re.escape(name).replace('`', r'\x60')

    providers = main.setdefault('proxy-providers', {})
    groups = {g['name']: g for g in main.get('proxy-groups', [])}
    proxies = {p['name']: p for p in main.get('proxies', [])}
    occupied_names = set(providers) | set(groups) | set(proxies)
    for source in runtime.get('proxy_groups', []):
        if not source.get('_chain_dialer'):
            continue
        group = groups[source['name']]
        filter_value = group.pop('filter', '')
        excluded = {key: group.pop(key) for key in ('exclude-filter', 'exclude-type') if key in group}
        use = []
        patterns = filter_value.split('`') if '`' in filter_value else []
        order_tag = '排序_'
        static = []
        for id, original in source.get('_chain_original_candidates', {}).items():
            ref_type = 'node' if any(n['id'] == id for n in runtime.get('nodes', [])) else 'group'
            emitted = resolve_dialer(runtime, {'dialer_ref': {'type': ref_type, 'id': id}})
            converted = (proxies if ref_type == 'node' else groups)[emitted]
            static.append({**original, 'type': converted['type'], 'emitted': emitted})
        static = filter_provider_candidates(static, exclude=excluded.get('exclude-filter', ''),
                                             exclude_type=excluded.get('exclude-type', ''))
        if group.get('proxies'):
            allowed = {p['emitted'] for p in static}
            group['proxies'] = [name for name in group['proxies'] if name in allowed]
        for index, original in enumerate(group.get('use', [])):
            base = f"{source['name']}_{original}"
            name, suffix = base, 2
            while name in occupied_names:
                name = f'{base} ({suffix})'
                suffix += 1
            occupied_names.add(name)
            provider = deepcopy(providers[original])
            provider['path'] = f"./providers/{source['id']}_provider_{index}.yaml"
            provider['override'] = {'additional-prefix': name + '_',
                                    'dialer-proxy': resolve_dialer(runtime, {'dialer_ref': source['_chain_dialer']})}
            if filter_value:
                provider['filter'] = filter_value
            if patterns:
                provider['filter'] = '|'.join(f'(?:{pattern})' for pattern in patterns)
                # Tag the first matching original-name bucket before prefixing.
                # Native group sorting can then retain cross-provider backtick order.
                provider['override']['proxy-name'] = [
                    {'pattern': f'^(?!{re.escape(order_tag)})(?=[\\s\\S]*?(?:{pattern}))([\\s\\S]*)$',
                     'target': f'{order_tag}{rank + 1}_${re.compile(pattern).groups + 1}'}
                    for rank, pattern in enumerate(patterns)]
            provider.update(excluded)
            providers[name] = provider
            use.append(name)
        if use:
            group['use'] = use
        if patterns and use:
            ordered_filters = []
            for rank, pattern in enumerate(patterns):
                dynamic = [f"^{name_pattern(name + '_' + order_tag + str(rank + 1) + '_')}" for name in use]
                matching = filter_provider_candidates(static, pattern)
                dynamic.extend(f"^{name_pattern(p['emitted'])}$" for p in matching)
                ordered_filters.append('|'.join(dynamic))
            group['filter'] = '`'.join(ordered_filters)
    if not providers:
        main.pop('proxy-providers', None)
