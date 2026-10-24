"""Owned, pre-cache device-group dispatch for OxiDNS templates and native drafts."""
from copy import deepcopy
from hashlib import sha256
import json

import yaml

from backend.common.oxidns_settings import OxidnsSettingsError

DISPATCH = 'cf_dg_dispatch'
LOCAL = 'cf_dg_local'
HOOK = {'exec': 'jump ' + DISPATCH}
BASELINE = '_device_groups_patch'
PREFIXES = ('cf_dg_', 'cf_scope_')


def require_native_blocking_supported(config):
    from backend.common.rule_dns import OxidnsRoutingLocked
    model = config.get('device_groups')
    if isinstance(model, dict) and any(group.get('keep_blocking') for group in model.get('groups', [])):
        raise OxidnsRoutingLocked('完整参数模式无法可靠识别需要复制的全部 DNS 拦截规则。'
                                  '请先关闭设备组的「仍执行 DNS 拦截」，或使用模板模式。')


def _owned(document):
    return [plugin for plugin in document.get('plugins', [])
            if isinstance(plugin, dict) and str(plugin.get('tag', '')).startswith(PREFIXES)]


def _blocking(document, config):
    """Only reuse a whole provider; a MATCH cutoff may split old aggregates."""
    from backend.common.device_group_blocking import compile_blocking_rules
    from backend.common.rule_dns import route_exec
    from backend.converters.oxidns import _routing_groups, _settings
    records = compile_blocking_rules(config)['oxidns']
    if not records:
        return [], []
    settings = _settings(config)
    settings.update(direct_rules=[], proxy_rules=[], direct_rulesets=[], proxy_rulesets=[],
                    rule_actions={record['item']['id']: {'action': record['action']} for record in records})
    subset = {**config, 'rule_configs': [record['item'] for record in records]}
    original = _routing_groups(config, _settings(config))['domain']
    existing = {plugin.get('tag'): plugin for plugin in document.get('plugins', [])}
    providers, steps = [], []
    for group in _routing_groups(subset, settings)['domain']:
        reusable = next((item for item in original if item['route'] == group['route'] and
                         item['args'] == group['args'] and existing.get(item['tag']) ==
                         {'tag': item['tag'], 'type': 'domain_set', 'args': item['args']}), None)
        if reusable:
            tag = reusable['tag']
        else:
            key = json.dumps([group['route'], group['args']], sort_keys=True, ensure_ascii=False)
            tag = 'cf_dg_block_' + sha256(key.encode()).hexdigest()[:16]
            providers.append({'tag': tag, 'type': 'domain_set', 'args': deepcopy(group['args'])})
        steps.append({'matches': ['qname $' + tag], 'exec': route_exec(group['route'], 'domain')})
    return providers, steps


def build_plugins(document, config, *, compiled=None):
    from backend.common.device_groups import compile_groups
    from backend.common.oxidns_device_policy import DIRECT
    compiled = compiled if compiled is not None else compile_groups(config)
    if not compiled['enabled']:
        return []
    if config.get('oxidns', {}).get('config_mode') == 'native':
        require_native_blocking_supported(config)
    groups = [group for group in compiled['groups'] if group['id'] != 'default' and
              group['mode'] != 'follow' and compiled['effective'].get(group['id'])]
    if not groups:
        return []
    providers, blocking = _blocking(document, config) if any(g.get('keep_blocking') for g in groups) else ([], [])
    plugins, dispatch = list(providers), []
    if any(group['mode'] == 'direct' and group.get('dns', 'follow') == 'follow' for group in groups):
        plugins.append({**deepcopy(DIRECT), 'tag': LOCAL})
    for group in groups:
        suffix = group['id'].removeprefix('dg_')
        source, segment = 'cf_dg_set_' + suffix, 'cf_dg_seg_' + suffix
        plugins.append({'tag': source, 'type': 'ip_set',
                        'args': {'ips': list(compiled['effective'][group['id']])}})
        steps = deepcopy(blocking) if group.get('keep_blocking') else []
        if group['mode'] == 'direct' and group.get('dns', 'follow') == 'follow':
            steps.append({'exec': 'goto ' + LOCAL})
        else:
            target = 'china_dns' if group['mode'] == 'direct' else 'proxy_dns_seq'
            # A goto can return through the dispatch jump into global rules.
            # Terminate explicitly after the selected chain has responded.
            steps.extend([{'exec': 'jump ' + target}, {'exec': 'accept'}])
        plugins.append({'tag': segment, 'type': 'sequence', 'args': steps})
        dispatch.append({'matches': ['client_ip $' + source], 'exec': 'goto ' + segment})
    plugins.append({'tag': DISPATCH, 'type': 'sequence', 'args': dispatch + [{'exec': 'return'}]})
    return plugins


def _check_dependencies(document, additions, index):
    from backend.common.dns_watchdog import WATCHDOG_RULE
    from backend.common.oxidns_common_routing import _plugins
    plugins = _plugins(document)
    main = plugins.get('sequence_main', {})
    args = main.get('args')
    servers = [p for p in plugins.values() if str(p.get('type', '')).endswith('_server')]
    if document.get('include') or main.get('type') != 'sequence' or not isinstance(args, list) or not args or \
            any(not isinstance(item, dict) or not isinstance(item.get('exec'), str) for item in args) or \
            args[-1] not in ({'exec': 'goto china_dns'}, {'exec': 'goto proxy_dns_seq'}) or not servers or \
            any(not isinstance(p.get('args'), dict) or p['args'].get('entry') != 'sequence_main' for p in servers):
        raise OxidnsSettingsError('当前主处理链或监听入口为自定义结构，无法安全插入设备组；请在完整参数中调整。')
    # Recognized names are not evidence of cache safety: verify their types and
    # the only callable prefix sequence as well as the main-table positions.
    for step in args[:index]:
        action = step['exec']
        if step == WATCHDOG_RULE:
            continue
        tag, kind = {'$query_recorder': ('query_recorder', 'query_recorder'),
                     '$custom_hosts': ('custom_hosts', 'hosts')}.get(action, (None, None))
        if tag and plugins.get(tag, {}).get('type') != kind:
            raise OxidnsSettingsError('设备组分发前的插件已被改为其他类型，无法确认缓存隔离；请恢复前缀插件。')
        if action == 'goto adguard_reject':
            expected = {'tag': 'adguard_reject', 'type': 'sequence',
                        'args': [{'exec': '$adguard_block'}, {'exec': 'accept'}]}
            if plugins.get('adguard_reject') != expected or \
                    plugins.get('adguard_block', {}).get('type') != 'black_hole' or \
                    plugins.get('adguard_rules', {}).get('type') != 'adguard_rule':
                raise OxidnsSettingsError('设备组分发前的 AdGuard 处理链已被修改，无法确认缓存隔离。')
    required = set()
    for plugin in additions:
        if plugin.get('tag') == LOCAL:
            required.add('forward_local')
        for step in plugin.get('args', []) if plugin.get('type') == 'sequence' else []:
            if step.get('exec') in ('jump china_dns', 'jump proxy_dns_seq'):
                required.add(step['exec'][5:])
    for tag in required:
        plugin = plugins.get(tag, {})
        if tag == 'forward_local':
            valid = plugin.get('type') == 'forward' and isinstance(plugin.get('args'), dict) and plugin['args'].get('upstreams')
        else:
            valid = plugin.get('type') == 'sequence' and isinstance(plugin.get('args'), list) and plugin['args']
        if not valid:
            raise OxidnsSettingsError(f'设备组需要可用的 {tag} 处理链；请在完整参数中恢复后再保存。')
    # No fallback may reenter the global entry or a managed dispatch segment.
    visiting, visited = set(), set()

    def walk(tag):
        if tag in visiting:
            raise OxidnsSettingsError('设备组依赖的处理链存在循环，无法安全分发。')
        if tag in visited:
            return
        if tag == 'sequence_main' or tag.startswith(PREFIXES) or tag in ('cf_device_policy', 'cf_device_direct'):
            raise OxidnsSettingsError('设备组的兜底处理链不能返回主表或再次进入设备分发。')
        visiting.add(tag)
        plugin = plugins.get(tag)
        if plugin is None:
            raise OxidnsSettingsError(f'设备组依赖的插件 {tag} 不存在，请在完整参数中恢复。')
        if plugin.get('type') == 'sequence':
            steps = plugin.get('args')
            if not isinstance(steps, list) or any(not isinstance(step, dict) for step in steps):
                raise OxidnsSettingsError(f'设备组依赖的 {tag} 不是可用的执行链。')
            for step in steps:
                action = step.get('exec')
                if not isinstance(action, str) or '${' in action:
                    raise OxidnsSettingsError('设备组依赖的执行链含不明确的插件调用，请先在完整参数中确认。')
                target = action[1:] if action.startswith('$') else \
                    action.split(' ', 1)[1] if action.startswith(('goto ', 'jump ')) else None
                if target:
                    walk(target)
        elif plugin.get('type') == 'fallback':
            values = plugin.get('args') or {}
            for key in ('primary', 'secondary'):
                target = values.get(key)
                if not isinstance(target, str) or not target:
                    raise OxidnsSettingsError('设备组依赖的备用执行链不完整。')
                walk(target.removeprefix('$'))
        visiting.remove(tag)
        visited.add(tag)

    for tag in required:
        walk(tag)
    _check_cache_isolation(plugins, required)


def _check_cache_isolation(plugins, required):
    """A pre-cache hook is insufficient when custom fallback chains alias caches.

    Caches inside one resolution chain may be reused by groups taking that same
    path. A cache also called by the other path or outside that chain can hold a
    response with different semantics, so that native/custom shape is refused.
    """
    edges = {}
    for tag, plugin in plugins.items():
        children = set()
        if plugin.get('type') == 'sequence' and isinstance(plugin.get('args'), list):
            for step in plugin['args']:
                action = step.get('exec') if isinstance(step, dict) else None
                if isinstance(action, str):
                    if action.startswith('$'):
                        children.add(action[1:])
                    elif action.startswith(('goto ', 'jump ')):
                        children.add(action.split(' ', 1)[1])
        elif plugin.get('type') == 'fallback' and isinstance(plugin.get('args'), dict):
            children.update(value.removeprefix('$') for key in ('primary', 'secondary')
                            if isinstance(value := plugin['args'].get(key), str))
        edges[tag] = children

    def reachable(root):
        seen, pending = set(), [root]
        while pending:
            tag = pending.pop()
            if tag not in seen:
                seen.add(tag)
                pending.extend(edges.get(tag, ()))
        return seen

    caches = {tag for tag, plugin in plugins.items() if plugin.get('type') == 'cache'}
    from posixpath import normpath
    dump_paths = {tag: normpath(path) for tag in caches
                  if isinstance(path := (plugins[tag].get('args') or {}).get('dump_file'), str) and path}
    paths = {root: reachable(root) for root in ('china_dns', 'proxy_dns_seq')}
    for root in required & paths.keys():
        chain = paths[root]
        used = chain & caches
        other = paths['proxy_dns_seq' if root == 'china_dns' else 'china_dns']
        shared = used & other
        shared.update(cache for parent, children in edges.items() if parent not in chain
                      for cache in children & used)
        shared.update(cache for cache in used if cache in dump_paths and any(
            other_cache != cache and path == dump_paths[cache] for other_cache, path in dump_paths.items()))
        if shared:
            raise OxidnsSettingsError('设备组的直连、代理或全局处理链共用缓存，无法保证缓存隔离：' +
                                      '、'.join(sorted(shared)) + '。请为不同解析路径使用独立缓存后再保存。')


def _check_template_cache_aliases(config):
    from backend.common.device_groups import compile_groups
    compiled = compile_groups(config)
    if not any(group.get('mode') == 'direct' and group.get('dns') == 'direct_full' and
               compiled['effective'].get(group['id']) for group in compiled['groups']):
        return
    settings = config.get('oxidns') or {}
    if not settings.get('cache_enabled', True):
        return
    # The template owns china_dns and its lazy_cache; only custom execution
    # rows/plugins can introduce an alias. Inspect those without compiling
    # unrelated rule-library drafts at the repository write boundary.
    from backend.converters.mosdns import _parse_custom_matches
    try:
        custom = yaml.safe_load(settings.get('custom_config') or '') or {}
    except yaml.YAMLError as error:
        raise OxidnsSettingsError('高级 YAML 无法解析，不能确认设备组缓存隔离。') from error
    extras = custom.get('plugins', []) if isinstance(custom, dict) else []
    plugins = {plugin['tag']: plugin for plugin in extras if isinstance(plugin, dict) and
               isinstance(plugin.get('tag'), str)} if isinstance(extras, list) else {}
    cache = {'dump_file': settings.get('cache_dump_file', './state/cache.dump')} if settings.get('cache_dump_enabled') else {}
    plugins.update({
        'lazy_cache': {'type': 'cache', 'args': cache},
        'china_dns': {'type': 'sequence', 'args': [{'exec': '$lazy_cache'}]},
        'proxy_dns_seq': {'type': 'sequence', 'args': []},
        'sequence_main': {'type': 'sequence', 'args': _parse_custom_matches(settings.get('custom_matches', []))},
    })
    _check_cache_isolation(plugins, {'china_dns'})


def patch_document(document, previous, additions):
    """Replace exactly the recorded plugins and the one pre-cache dispatch hook."""
    from backend.common.oxidns_common_routing import _plugins, _prefix_end, _references
    result = deepcopy(document)
    plugins = _plugins(result)
    args = plugins.get('sequence_main', {}).get('args')
    if not isinstance(args, list) or any(not isinstance(row, dict) for row in args):
        raise OxidnsSettingsError('主处理链结构无效，无法安全更新设备组。')
    owned = _owned(result)
    hooks = [index for index, row in enumerate(args) if row == HOOK]
    if previous:
        if owned != previous or len(hooks) != 1:
            raise OxidnsSettingsError('设备组受管处理链已被手动修改，请先在完整参数中恢复。')
        index = hooks[0]
        args.pop(index)
        if index != _prefix_end(args):
            raise OxidnsSettingsError('设备组分发位置已被移动，必须位于所有缓存和分流规则之前。')
    elif owned or hooks:
        raise OxidnsSettingsError('完整参数中存在 cf_dg_ 或 cf_scope_ 同名资源，请先处理命名冲突。')
    if any(plugin.get('tag') in ('cf_device_policy', 'cf_device_direct') for plugin in result.get('plugins', [])):
        raise OxidnsSettingsError('旧设备策略仍在执行，请通过「升级为设备组」完成迁移。')
    tags = {p['tag'] for p in previous}
    result['plugins'] = [p for p in result.get('plugins', []) if p.get('tag') not in tags]
    # A generated reload list is the only permitted secondary reference to a
    # provider. Remove exactly the old owned providers from it before checking.
    reload = next((p for p in result['plugins'] if p.get('tag') == 'rules_reload'), None)
    if reload and reload.get('type') == 'reload_provider' and isinstance(reload.get('args'), list):
        providers = {p['tag'] for p in previous if p.get('type') in ('domain_set', 'ip_set')}
        reload['args'] = [ref for ref in reload['args'] if ref not in {'$' + tag for tag in providers}]
    for tag in tags:
        if _references(result, tag):
            raise OxidnsSettingsError('设备组受管插件存在额外引用，请先在完整参数中移除引用。')
    if additions:
        index = _prefix_end(args)
        _check_dependencies(result, additions, index)
        args.insert(index, deepcopy(HOOK))
        result['plugins'].extend(deepcopy(additions))
        if reload and reload.get('type') == 'reload_provider' and isinstance(reload.get('args'), list):
            reload['args'].extend('$' + p['tag'] for p in additions if p.get('type') == 'domain_set' and p['args'].get('files'))
    return result


def render_document(document, config):
    """Pure generation; a stored native baseline proves ownership across IP changes."""
    settings = config.get('oxidns', {})
    if config.get('device_groups') is None:
        return document
    if settings.get('device_policies') or (settings.get('template_settings') or {}).get('device_policies'):
        raise OxidnsSettingsError('旧设备策略尚未清空，请先升级为设备组。')
    additions = build_plugins(document, config)
    previous = settings.get(BASELINE, []) if settings.get('config_mode') == 'native' else []
    if not additions and not previous and not _owned(document):
        return document
    if settings.get('config_mode') == 'native':
        from backend.common.oxidns_native import load_native_yaml
        if load_native_yaml(settings.get('native_config', '')).get('include'):
            raise OxidnsSettingsError('设备组暂不能修改由 include 组成的主处理链，请先将相关插件内联到完整参数。')
    # A template just prepared for the native editor has canonical generated
    # plugins but has not yet passed the repository write gate.
    if not previous and _owned(document) == additions:
        previous = additions
    return patch_document(document, previous, additions)


def normalize_transition(before_config, after_config):
    """Repository write gate, including imports, shared writes and mode changes.

    Only the persisted before snapshot is trusted on ordinary updates. Import
    without a trusted source requires the exact canonical imported shape.
    """
    from backend.common.oxidns_native import load_native_yaml
    from backend.common.oxidns_device_policy import patch_document as patch_legacy
    before, after = before_config.get('oxidns', {}), after_config.get('oxidns', {})
    if before_config.get('device_groups') is None and after_config.get('device_groups') is None:
        return
    if after.get('config_mode', 'template') != 'native':
        _check_template_cache_aliases(after_config)
        after.pop(BASELINE, None)
        return
    require_native_blocking_supported(after_config)
    document = load_native_yaml(after.get('native_config', ''))
    previous = before.get(BASELINE) if before.get('config_mode') == 'native' else None
    if previous is not None and BASELINE in after and after[BASELINE] != previous:
        raise OxidnsSettingsError('设备组受管基线不能手动修改，请刷新完整参数后重试。')
    if before_config.get('device_groups') is None and before.get('device_policies') and after_config.get('device_groups') is not None:
        document = patch_legacy(document, before['device_policies'], [])
    additions = build_plugins(document, after_config)
    if previous is None:
        # An import/clone/editor conversion must prove its resources directly,
        # never establish ownership merely by providing arbitrary metadata.
        previous = additions if _owned(document) == additions else []
    if additions or previous or _owned(document):
        if document.get('include'):
            raise OxidnsSettingsError('设备组暂不能修改由 include 组成的主处理链，请先将相关插件内联到完整参数。')
        document = patch_document(document, previous, additions)
        after['native_config'] = yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
    if additions:
        after[BASELINE] = deepcopy(additions)
    else:
        after.pop(BASELINE, None)
