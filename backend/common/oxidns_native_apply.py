"""Explicit native-form edits with the same lifecycle as common controls.

This is deliberately separate from loading, saving and YAML rendering. Only the
complete parameter form calls it, supplying both the old and proposed draft.
"""
from copy import deepcopy
import re

import yaml

from backend.common.oxidns_common import apply_common_settings, read_common_settings, _TAG_TYPES, _Context
from backend.common.oxidns_editor import prepare_editor, _same
from backend.common.oxidns_native import load_native_yaml, TAG
from backend.common.oxidns_references import OxidnsReferenceError, reference_locations
from backend.common.oxidns_settings import OxidnsSettingsError

_FEATURES = {'lazy_cache': 'cache_enabled', 'local_probe_cache': 'probe_cache_enabled',
             **{name: name + '.enabled' for name in ('query_recorder', 'ip_selector', 'ros_address_list', 'ros_route')}}
_FALLBACK = {'forward_fallback', 'fallback_dns', 'fallback'}
_ADGUARD = {'adguard_rules', 'adguard_block', 'adguard_reject'}
_UPDATER = {'rules_download', 'rules_reload', 'rules_refresh', 'rules_cron'}
_DYNAMIC = {'dynamic_domains', 'learn_classified_domain'}
_ADDITIONS = set(_FEATURES) | _DYNAMIC | _ADGUARD | _UPDATER | _FALLBACK | {'custom_hosts'}
_STANDARD = set(_FEATURES) | _DYNAMIC | {'adguard_rules', 'rules_download', 'custom_hosts', 'forward_fallback'}
_WRAPPERS = ('china_dns', 'direct_ip', 'default_direct_ip')
_MANAGED = set(_TAG_TYPES) | set(_WRAPPERS) | {
    'sequence_main', 'remote_dns', 'local_probe', 'proxy_dns_seq', 'ip_requery_proxy',
    'fallback_dns', 'adguard_reject', 'rules_refresh',
}


def _plugins(document):
    items = document.get('plugins', [])
    if not isinstance(items, list) or any(not isinstance(item, dict) or not isinstance(item.get('tag'), str) for item in items):
        raise OxidnsSettingsError('插件结构尚未完成，请先填写插件 tag 和参数')
    result = {item['tag']: item for item in items}
    if len(result) != len(items):
        raise OxidnsSettingsError('插件 tag 重复，请先使用不同名称再联动修改')
    return result


def _without_tag(plugin):
    return {key: value for key, value in plugin.items() if key != 'tag'}


def _upstreams_state(value):
    if value is None or value == []:
        return 'empty'
    if not isinstance(value, list):
        return 'draft'
    if all(isinstance(item, dict) and (item.get('addr') is None or isinstance(item.get('addr'), str) and not item['addr'].strip()) for item in value):
        return 'empty'
    if all(isinstance(item, dict) and isinstance(item.get('addr'), str) and item['addr'].strip() for item in value):
        return 'ready'
    return 'draft'


def _rename(document, old, new):
    """Rewrite only explicit reference fields, never credentials or file paths."""
    token = re.compile(r'(?<![\w.\-])\$' + re.escape(old) + r'(?![\w.\-])')

    def walk(value, field=None):
        if isinstance(value, dict):
            for key, item in value.items():
                if key not in ('tag', 'type'):
                    value[key] = walk(item, key)
        elif isinstance(value, list):
            return [walk(item, field) for item in value]
        elif isinstance(value, str) and field in ('exec', 'matches', 'entry', 'primary', 'secondary', 'provider', 'executors', 'args'):
            if field in ('entry', 'primary', 'secondary', 'provider') and value == old:
                return new
            if field == 'exec' and value in ('goto ' + old, 'jump ' + old):
                return value.split(' ', 1)[0] + ' ' + new
            return token.sub(lambda _: '$' + new, value)
        return value

    return walk(document)


def _mask_owned(document, targets):
    """Keep candidate indices intact so errors jump to the exact form field."""
    result = deepcopy(document)
    for item in result.get('plugins', []):
        tag = item.get('tag')
        if tag in targets:
            item.clear()
            continue
        args = item.get('args')
        if tag == 'rules_cron' and isinstance(args, dict) and isinstance(args.get('jobs'), list) and targets & _UPDATER:
            args['jobs'] = [{} if isinstance(job, dict) and job.get('name') == 'refresh_rules' and job.get('executors') == ['$rules_refresh'] else job for job in args['jobs']]
        if tag == 'rules_reload' and isinstance(args, list) and 'adguard_rules' in targets:
            item['args'] = [None if value == '$adguard_rules' else value for value in args]
        if not isinstance(args, list) or item.get('type') != 'sequence':
            continue
        expected = []
        for target in targets:
            chains = ('china_dns',) if target == 'lazy_cache' else ('local_probe',) if target == 'local_probe_cache' else \
                     ('sequence_main',) if target in ('query_recorder', 'custom_hosts') else \
                     ('direct_ip', 'ip_requery_proxy') if target == 'learn_classified_domain' else _WRAPPERS if target in _FEATURES else ()
            if tag in chains:
                expected.append({'exec': '$' + target})
        if tag == 'sequence_main' and 'dynamic_domains' in targets:
            expected.extend({'matches': ['qname $dynamic_domains'], 'exec': 'goto ' + route} for route in ('china_dns', 'proxy_dns_seq'))
        if tag == 'sequence_main' and targets & _ADGUARD:
            expected.append({'matches': ['question $adguard_rules'], 'exec': 'goto adguard_reject'})
        if tag == 'proxy_dns_seq' and targets & _FALLBACK and args == [{'exec': '$fallback'}, {'exec': 'accept'}]:
            item['args'] = []
        else:
            item['args'] = [{} if entry in expected else entry for entry in args]
    return result


def _reject_references(candidate, targets):
    refs = reference_locations(_mask_owned(candidate, targets), targets)
    if refs:
        raise OxidnsReferenceError('不能自动删除共享插件，请先在完整参数中调整引用', refs)


def _shape_reference(candidate, targets, message):
    for index, item in enumerate(candidate.get('plugins', [])):
        if item.get('tag') in targets or item.get('tag') in ('proxy_dns_seq', 'sequence_main'):
            return [{'path': ['plugins', index, 'args'], 'tag': item.get('tag'), 'reason': message}]
    return []


def _stop_removed_cache_dump(current, before, candidate, config, base_url, notices):
    """A removed dump path disables persistence, not the cache executor."""
    previous = before.get('lazy_cache')
    cache = _plugins(candidate).get('lazy_cache')
    if not previous or not cache or previous.get('type') != 'cache' or cache.get('type') != 'cache':
        return current, candidate
    old_args, args = previous.get('args'), cache.get('args')
    if not isinstance(old_args, dict) or not isinstance(args, dict):
        return current, candidate
    old_path, path = old_args.get('dump_file'), args.get('dump_file')
    if not isinstance(old_path, str) or not old_path.strip() or not (
            path is None or isinstance(path, str) and not path.strip()):
        return current, candidate
    working = deepcopy(candidate)
    holder = _plugins(working)['lazy_cache']['args']
    # The old path was removed from the form already. Restore it only inside the
    # lifecycle transaction so common off can park both options before removing
    # them. A simultaneously edited interval (including null) takes precedence.
    holder['dump_file'] = old_path
    if 'dump_interval' not in holder and 'dump_interval' in old_args:
        holder['dump_interval'] = deepcopy(old_args['dump_interval'])
    current['native_config'] = yaml.safe_dump(working, allow_unicode=True, sort_keys=False)
    try:
        current = apply_common_settings(current, {'cache_dump_enabled': False}, config, base_url)['settings']
    except OxidnsSettingsError as exc:
        raise OxidnsReferenceError(str(exc), _shape_reference(candidate, {'lazy_cache'}, str(exc))) from None
    notices.append('已停用缓存持久化并移除保存路径和间隔；缓存处理链保持不变，参数已暂存')
    return current, load_native_yaml(current['native_config'])


def _stop_empty_hosts(current, before, candidate, config, base_url, notices):
    """An explicit last-source removal withdraws only the standard Hosts chain."""
    previous = before.get('custom_hosts')
    hosts = _plugins(candidate).get('custom_hosts')
    if not previous or not hosts or previous.get('type') != 'hosts' or hosts.get('type') != 'hosts':
        return current, candidate
    old_args, args = previous.get('args'), hosts.get('args')
    if not isinstance(old_args, dict) or args is not None and not isinstance(args, dict):
        return current, candidate
    args = args or {}
    if not any(isinstance(old_args.get(field), list) and old_args[field] for field in ('files', 'entries')):
        return current, candidate
    if any(args.get(field) is not None and args.get(field) != [] for field in ('files', 'entries')):
        return current, candidate
    if candidate.get('include'):
        raise OxidnsReferenceError('配置包含 include，不能自动移除 Hosts 处理链', [
            {'path': ['include'], 'tag': 'custom_hosts', 'reason': '请先确认附加文件中的共享引用'}])
    _reject_references(candidate, {'custom_hosts'})
    working = deepcopy(candidate)
    holder = _plugins(working)['custom_hosts']
    if holder.get('args') is None:
        holder['args'] = {}
    # Null source fields are an explicit removal here, not custom data. Keep
    # parked options valid without restoring any source the user withdrew.
    for field in ('files', 'entries'):
        if holder['args'].get(field) is None:
            holder['args'].pop(field, None)
    current['native_config'] = yaml.safe_dump(working, allow_unicode=True, sort_keys=False)
    try:
        current = apply_common_settings(current, {'custom_hosts': ''}, config, base_url)['settings']
    except OxidnsSettingsError as exc:
        raise OxidnsReferenceError(str(exc), _shape_reference(candidate, {'custom_hosts'}, str(exc))) from None
    notices.append('Hosts 来源已清空，已移除空插件及标准调用；已撤回的来源不会在恢复时重新加入')
    return current, load_native_yaml(current['native_config'])


def _merge_missing(default, explicit):
    """Standard-entry intent supplies defaults only for omitted object fields."""
    if isinstance(default, dict) and isinstance(explicit, dict):
        return {**deepcopy(default), **{key: _merge_missing(default.get(key), value) for key, value in explicit.items()}}
    return deepcopy(explicit)


def _addition_targets(added):
    targets = set(added)
    for group in (_DYNAMIC, _ADGUARD, _UPDATER, _FALLBACK):
        if added & group:
            targets.update(group)
    return targets


def _activate(current, candidate, added, standard_add, config, base_url, notices):
    """Connect canonical additions using common lifecycle, without guessing custom tags."""
    proposed = _plugins(candidate)
    additions = {tag for tag in added & _ADDITIONS if proposed[tag].get('type') == _TAG_TYPES.get(tag, 'sequence')}
    if not additions:
        return current, candidate
    targets = _addition_targets(additions)
    if candidate.get('include'):
        raise OxidnsReferenceError('配置包含 include，不能自动接入标准功能', [{'path': ['include'], 'tag': None, 'reason': '附加文件中可能存在共享引用'}])
    refs = reference_locations(_mask_owned(candidate, targets), targets)
    # Masking owned nodes must not hide unresolved executors inside a new node.
    refs += [item for item in reference_locations(candidate) if item not in refs]
    if refs:
        raise OxidnsReferenceError('不能自动接入共享或自定义处理链，请先调整引用', refs)
    explicit = {tag: deepcopy(proposed[tag]) for tag in additions}
    working = deepcopy(candidate)
    parked = current.setdefault('_common_disabled', {})
    if not isinstance(parked, dict):
        raise OxidnsSettingsError('暂存参数结构无效，请先在完整参数中修正')
    # A dedicated standard-entry action may restore omitted options. Ordinary
    # native additions retain their exact args, including null/empty values.
    for tag in standard_add & additions:
        if tag == 'forward_fallback':
            saved = parked.get(tag, {})
            saved = saved if isinstance(saved, dict) and saved.get('type') == 'forward' else {'tag': tag, 'type': 'forward', 'args': {}}
            saved = deepcopy(saved)
            if not isinstance(saved.get('args'), dict):
                saved['args'] = {}
            saved['args']['upstreams'] = []  # Empty never revives parked DNS.
            proposed[tag].clear()
            proposed[tag].update(_merge_missing(saved, explicit[tag]))
    working = deepcopy(candidate)
    working['plugins'] = [item for item in working['plugins'] if item['tag'] not in standard_add - {'forward_fallback'}]

    changes = {_FEATURES[tag]: True for tag in additions & set(_FEATURES)}
    if additions & _DYNAMIC:
        changes['dynamic_domain_set.enabled'] = True
        if 'learn_classified_domain' in additions:
            changes['dynamic_domain_set.learn'] = True
    if additions & _ADGUARD:
        changes['adguard.enabled'] = True
        if additions & _UPDATER or 'adguard_rules' in standard_add:
            # Resource providers must exist before the updater computes its live
            # download targets; a batch can restore both standard entries.
            current['native_config'] = yaml.safe_dump(working, allow_unicode=True, sort_keys=False)
            try:
                current = apply_common_settings(current, {'adguard.enabled': True}, config, base_url)['settings']
            except OxidnsSettingsError as exc:
                raise OxidnsReferenceError(str(exc), _shape_reference(candidate, _ADGUARD, str(exc))) from None
            working = load_native_yaml(current['native_config'])
            if 'adguard_rules' in standard_add:
                provider = _plugins(working)['adguard_rules']
                restored = _merge_missing(provider, explicit['adguard_rules'])
                provider.clear()
                provider.update(restored)
    if additions & _UPDATER:
        from backend.common.oxidns_common_routing import resource_downloads
        from backend.common.oxidns_common_updates import _path, _REFRESH, _DOWNLOAD_ONLY
        available = resource_downloads(working, current, config, base_url)
        if not available:
            raise OxidnsReferenceError('规则定时更新需要当前配置引用至少一个可下载订阅', _shape_reference(candidate, additions & _UPDATER, '请先添加当前使用的订阅源'))
        refresh = proposed.get('rules_refresh')
        if refresh and refresh.get('args') not in (_REFRESH, _DOWNLOAD_ONLY):
            raise OxidnsReferenceError('规则更新包含自定义步骤，不能自动接入定时任务', _shape_reference(candidate, {'rules_refresh'}, '请在执行序列表单配置更新调用'))
        download = proposed.get('rules_download')
        stored_paths = parked.get('rules_download_managed_paths', [])
        historical = {path for path in stored_paths if isinstance(path, str)} if isinstance(stored_paths, list) else set()
        desired = {_path(item) for item in available}
        if download and isinstance(download.get('args'), dict) and isinstance(download['args'].get('downloads'), list):
            if any(_path(item) in historical - desired for item in download['args']['downloads']):
                raise OxidnsReferenceError('新增下载插件仍包含已撤回的受管资源，请先删除旧下载条目', _shape_reference(candidate, {'rules_download'}, '下载目标已经不再被当前配置引用'))
        # An added cron already has its job, but its dependencies may be parked.
        # Let common enabling recreate them before restoring the explicit job.
        if 'rules_cron' in additions:
            cron = next((item for item in working['plugins'] if item['tag'] == 'rules_cron'), None)
            if cron and isinstance(cron.get('args'), dict) and isinstance(cron['args'].get('jobs'), list):
                jobs = cron['args']['jobs']
                if any(isinstance(job, dict) and job.get('name') == 'refresh_rules' and job.get('executors') == ['$rules_refresh'] for job in jobs):
                    cron['args']['jobs'] = [job for job in jobs if not (isinstance(job, dict) and job.get('name') == 'refresh_rules')]
        changes['rule_update.enabled'] = True
    if additions & _FALLBACK:
        backup = next((item for item in working['plugins'] if item['tag'] == 'forward_fallback'), {})
        args = backup.get('args')
        if isinstance(args, dict) and _upstreams_state(args.get('upstreams')) == 'ready':
            changes['fallback_dns'] = yaml.safe_dump(args['upstreams'], allow_unicode=True, sort_keys=False)
        else:
            notices.append('备用 DNS 地址尚未填写，已保留表单草稿，未接入代理回退链')

    current['native_config'] = yaml.safe_dump(working, allow_unicode=True, sort_keys=False)
    if changes:
        try:
            current = apply_common_settings(current, changes, config, base_url)['settings']
        except OxidnsSettingsError as exc:
            raise OxidnsReferenceError(str(exc), _shape_reference(candidate, targets, str(exc))) from None
        working = load_native_yaml(current['native_config'])
    active = _plugins(working)
    for tag, value in explicit.items():
        if tag == 'custom_hosts':
            continue
        if tag in active:
            restored = _merge_missing(active[tag], value) if tag in standard_add else value
            if tag == 'forward_fallback' and tag in standard_add:
                restored = deepcopy(proposed[tag])
            active[tag].clear()
            active[tag].update(deepcopy(restored))

    if 'custom_hosts' in additions:
        ctx = _Context(working, current)
        _, capability = ctx.call_state('custom_hosts', ('sequence_main',))
        if not capability['editable']:
            raise OxidnsReferenceError(capability['reason'], _shape_reference(candidate, {'custom_hosts'}, capability['reason']))
        source = ctx.parked.get('native_hosts_source')
        defaults = ctx.parked.get('custom_hosts', {'tag': 'custom_hosts', 'type': 'hosts', 'args': {'files': []}})
        value = _merge_missing(defaults, explicit['custom_hosts']) if 'custom_hosts' in standard_add else explicit['custom_hosts']
        host = ctx.ensure('custom_hosts', {}, 'hosts')
        host.clear()
        host.update(deepcopy(value))
        args = host.get('args')
        paths = args.get('files', []) if isinstance(args, dict) else []
        if isinstance(source, str) and any(path in ('./rules/custom_hosts.txt', 'rules/custom_hosts.txt') for path in paths):
            current['custom_hosts'] = source
        ctx.parked.pop('native_hosts_source', None)
        main = ctx.sequence('sequence_main')
        call, accept = {'exec': '$custom_hosts'}, {'matches': ['has_resp'], 'exec': 'accept'}
        if call in main:
            index = main.index(call)
            if main[index + 1:index + 2] != [accept]:
                raise OxidnsReferenceError('Hosts 使用了自定义返回处理，请在完整参数中调整', _shape_reference(candidate, {'custom_hosts'}, 'Hosts 调用后需要明确的响应处理'))
        else:
            index = next((i for i, item in enumerate(main) if item.get('exec') not in ('metrics_collector metrics', '$query_recorder', 'prefer_ipv4') and not str(item.get('matches', [''])[0] if item.get('matches') else '').startswith('qtype ')), 0)
            main[index:index] = [call, accept]
    current['native_config'] = yaml.safe_dump(working, allow_unicode=True, sort_keys=False)
    if changes or 'custom_hosts' in additions:
        notices.append('已接入新增的标准功能，并同步恢复所需依赖和处理链')
    return current, working


def apply_native_settings(settings, native_config, config, base_url='', renames=None, standard_add=None):
    if not isinstance(native_config, str):
        raise OxidnsSettingsError('native_config 必须为 YAML 文本')
    current = prepare_editor(settings, config, base_url)
    before = load_native_yaml(current['native_config'])
    candidate = load_native_yaml(native_config)
    old, proposed = _plugins(before), _plugins(candidate)
    removed, added = set(old) - set(proposed), set(proposed) - set(old)
    notices = []

    # A tag-only edit has an unambiguous identity. More complex edits remain
    # ordinary add/remove operations; we never guess from type or list index.
    if renames is not None and (not isinstance(renames, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in renames.items())):
        raise OxidnsSettingsError('renames 必须为原 tag 到新 tag 的文本映射')
    renames = deepcopy(renames or {})
    if standard_add is not None and (not isinstance(standard_add, list) or any(not isinstance(tag, str) for tag in standard_add) or len(set(standard_add)) != len(standard_add)):
        raise OxidnsSettingsError('standard_add 必须为不重复的标准功能 tag 数组')
    standard_add = set(standard_add or [])
    if any(tag not in _STANDARD or tag not in added or proposed[tag].get('type') != _TAG_TYPES.get(tag, 'sequence') for tag in standard_add):
        raise OxidnsSettingsError('标准功能新增必须对应唯一、类型正确且尚不存在的标准插件')
    if len(set(renames.values())) != len(renames) or any(tag not in removed or new not in added for tag, new in renames.items()):
        raise OxidnsSettingsError('重命名映射必须对应唯一的原插件和新增 tag，不能覆盖现有插件')
    for tag in sorted(removed - set(renames)):
        matches = [name for name in added - set(renames.values()) - standard_add if _same(_without_tag(old[tag]), _without_tag(proposed[name]))]
        if len(matches) == 1 and sum(_same(_without_tag(old[other]), _without_tag(proposed[matches[0]])) for other in removed - set(renames)) == 1:
            renames[tag] = matches[0]
    if standard_add & set(renames.values()):
        raise OxidnsSettingsError('标准功能新增不能同时作为插件重命名目标')
    if any(not new or len(new) > 64 or not TAG.fullmatch(new) or new.startswith(('qs.exec.', 'qs.match.', 'qs.cron.')) or old[tag].get('type') != proposed[new].get('type') for tag, new in renames.items()):
        raise OxidnsSettingsError('重命名需要有效的非空 tag，并保持插件类型；请继续在完整参数中填写')
    if removed and any(not name.strip() for name in added):
        raise OxidnsSettingsError('请先填写非空的新插件 tag，再同步重命名或删除')
    if renames:
        dynamic = reference_locations(candidate)
        if dynamic or candidate.get('include'):
            raise OxidnsReferenceError('重命名前请先确认外部或环境变量中的引用', dynamic or [{'path': ['include'], 'tag': None, 'reason': 'include 中可能仍引用原 tag'}])
        for tag, new in renames.items():
            _rename(candidate, tag, new)
            left = reference_locations(candidate, [tag], dynamic=False)
            if left:
                raise OxidnsReferenceError('存在无法安全自动重命名的自定义引用', left)
            removed.remove(tag)
            notices.append(f'已将插件「{tag}」的明确引用同步为「{new}」')
        disabled = current.setdefault('_common_disabled', {})
        if not isinstance(disabled, dict):
            raise OxidnsSettingsError('暂存参数结构无效，不能记录插件重命名')
        aliases = disabled.setdefault('renamed_plugins', {})
        if not isinstance(aliases, dict):
            raise OxidnsSettingsError('插件重命名记录结构无效，请在完整参数中修正')
        for tag, new in renames.items():
            originals = [original for original, value in aliases.items() if value == tag]
            for original in originals:
                if new == original:
                    aliases.pop(original)
                else:
                    aliases[original] = new
            if tag in _MANAGED and not originals:
                aliases[tag] = new
        proposed = _plugins(candidate)

    additions = (added - set(renames.values())) & _ADDITIONS
    additions = {tag for tag in additions if proposed[tag].get('type') == _TAG_TYPES.get(tag, 'sequence')}

    changes, targets = {}, set()
    for tag in removed:
        if tag in _FEATURES:
            changes[_FEATURES[tag]] = False
            targets.add(tag)
        elif tag == 'dynamic_domains':
            changes['dynamic_domain_set.enabled'] = False
            targets.update(('dynamic_domains', 'learn_classified_domain'))
        elif tag == 'learn_classified_domain':
            changes['dynamic_domain_set.learn'] = False
            targets.add(tag)
        elif tag == 'custom_hosts':
            changes['custom_hosts'] = ''
            targets.add(tag)
        elif tag in _ADGUARD:
            changes['adguard.enabled'] = False
            targets.update(_ADGUARD)
        elif tag in _FALLBACK:
            changes['fallback_dns'] = ''
            targets.update(_FALLBACK)
        elif tag in _UPDATER:
            # Respect a user who already rewired a download-only pipeline.
            if tag == 'rules_cron' or reference_locations(candidate, [tag], dynamic=False):
                changes['rule_update.enabled'] = False
                targets.update({'rules_cron'} if tag == 'rules_cron' else _UPDATER - {'rules_cron'})
    backup = proposed.get('forward_fallback')
    if backup and backup.get('type') == 'forward' and isinstance(backup.get('args'), dict):
        previous = old.get('forward_fallback', {}).get('args', {})
        previous_state = _upstreams_state(previous.get('upstreams')) if isinstance(previous, dict) else 'draft'
        next_state = _upstreams_state(backup['args'].get('upstreams'))
        if previous_state == 'ready' and next_state == 'empty':
            changes['fallback_dns'] = ''
            targets.update(_FALLBACK)
        elif previous_state != 'ready' and next_state == 'ready' and 'forward_fallback' not in renames.values():
            additions.add('forward_fallback')
    if removed & _addition_targets(additions):
        raise OxidnsReferenceError('同一次修改不能删除标准功能的依赖并重新启用该功能，请分步调整', _shape_reference(candidate, additions, '新增功能仍需要本次删除的依赖'))
    cron = proposed.get('rules_cron')
    previous_cron = old.get('rules_cron')
    if cron and previous_cron and isinstance(cron.get('args'), dict) and isinstance(previous_cron.get('args'), dict):
        previous_jobs, jobs = previous_cron['args'].get('jobs', []), cron['args'].get('jobs', [])
        if isinstance(previous_jobs, list) and isinstance(jobs, list) and any(isinstance(job, dict) and job.get('name') == 'refresh_rules' for job in previous_jobs) and not any(isinstance(job, dict) and (job.get('name') == 'refresh_rules' or job.get('executors') == ['$rules_refresh']) for job in jobs):
            changes['rule_update.enabled'] = False
            targets.add('rules_cron')

    original_candidate = deepcopy(candidate)
    if targets:
        if candidate.get('include'):
            raise OxidnsReferenceError('配置包含 include，不能自动修改其依赖链', [{'path': ['include'], 'tag': None, 'reason': '请先在完整参数或附加文件中调整共享引用'}])
        _reject_references(candidate, targets)
        # Restore only removed definitions while the common editor removes their
        # owned edges and parks options. Candidate fields and order stay intact.
        working = deepcopy(candidate)
        for tag in removed & targets:
            if tag in old:
                restored = deepcopy(old[tag])
                if tag == 'custom_hosts' and restored.get('type') == 'hosts' and isinstance(restored.get('args'), dict):
                    # Whole-plugin deletion is explicit. Common Hosts clearing
                    # otherwise preserves inline rules and their live call.
                    restored['args'].update(files=[], entries=[])
                working.setdefault('plugins', []).append(restored)
        current['native_config'] = yaml.safe_dump(working, allow_unicode=True, sort_keys=False)
        try:
            result = apply_common_settings(current, changes, config, base_url)
        except OxidnsSettingsError as exc:
            # The form may have explicitly removed all affected calls already.
            # Such a native repair needs no guessing about a custom chain.
            if removed and not additions and not reference_locations(candidate, removed):
                current['native_config'] = yaml.safe_dump(candidate, allow_unicode=True, sort_keys=False)
            else:
                raise OxidnsReferenceError(str(exc), _shape_reference(candidate, targets, str(exc))) from None
        else:
            current = result['settings']
            candidate = load_native_yaml(current['native_config'])
            if removed & (_UPDATER - {'rules_cron'}) and targets & (_UPDATER - {'rules_cron'}):
                # Explicitly deleting the update capability still withdraws its
                # owned edges; deleting only cron leaves manual updates intact.
                from backend.common.oxidns_common_updates import _stop_pipeline
                _stop_pipeline(_Context(candidate, current))
        # Keep explicit removal authoritative even when a common field leaves a
        # reusable definition; the original Hosts definition is parked below.
        candidate['plugins'] = [item for item in candidate.get('plugins', []) if item.get('tag') not in removed]
        if 'custom_hosts' in removed:
            parked = current.setdefault('_common_disabled', {})
            parked['custom_hosts'] = deepcopy(old['custom_hosts'])
            if isinstance(settings.get('custom_hosts'), str) and settings['custom_hosts'].strip():
                parked['native_hosts_source'] = settings['custom_hosts']
        notices.append('已同步停用关联功能、处理链和受管资源；可恢复参数已保留')
    else:
        current['native_config'] = native_config

    if additions:
        current, candidate = _activate(current, candidate, additions, standard_add, config, base_url, notices)

    current, candidate = _stop_removed_cache_dump(current, old, candidate, config, base_url, notices)
    current, candidate = _stop_empty_hosts(current, old, candidate, config, base_url, notices)

    # This also protects deletion of custom executors. We do not repair arbitrary
    # native chains, but point to their exact remaining references in the form.
    if removed:
        refs = reference_locations(candidate, removed)
        if refs:
            original_refs = reference_locations(_mask_owned(original_candidate, targets), removed)
            raise OxidnsReferenceError('删除后仍有插件引用，请在完整参数中调整', original_refs or refs)
    if changes or renames or additions:
        current['native_config'] = yaml.safe_dump(candidate, allow_unicode=True, sort_keys=False)
    projection = read_common_settings(current, config, base_url)
    return {'settings': current, **projection, 'notices': notices}
