"""Explicit native-form edits with the same lifecycle as common controls.

This is deliberately separate from loading, saving and YAML rendering. Only the
complete parameter form calls it, supplying both the old and proposed draft.
"""
from copy import deepcopy
import re

import yaml

from backend.common.oxidns_common import apply_common_settings, read_common_settings, _TAG_TYPES
from backend.common.oxidns_editor import prepare_editor, _same
from backend.common.oxidns_native import load_native_yaml, TAG
from backend.common.oxidns_references import OxidnsReferenceError, reference_locations
from backend.common.oxidns_settings import OxidnsSettingsError

_FEATURES = {'lazy_cache': 'cache_enabled', 'local_probe_cache': 'probe_cache_enabled',
             **{name: name + '.enabled' for name in ('query_recorder', 'ip_selector', 'ros_address_list', 'ros_route')}}
_FALLBACK = {'forward_fallback', 'fallback_dns', 'fallback'}
_ADGUARD = {'adguard_rules', 'adguard_block', 'adguard_reject'}
_UPDATER = {'rules_download', 'rules_reload', 'rules_refresh', 'rules_cron'}
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


def apply_native_settings(settings, native_config, config, base_url='', renames=None):
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
    if len(set(renames.values())) != len(renames) or any(tag not in removed or new not in added for tag, new in renames.items()):
        raise OxidnsSettingsError('重命名映射必须对应唯一的原插件和新增 tag，不能覆盖现有插件')
    for tag in sorted(removed - set(renames)):
        matches = [name for name in added - set(renames.values()) if _same(_without_tag(old[tag]), _without_tag(proposed[name]))]
        if len(matches) == 1 and sum(_same(_without_tag(old[other]), _without_tag(proposed[matches[0]])) for other in removed - set(renames)) == 1:
            renames[tag] = matches[0]
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
                targets.update(_UPDATER - {'rules_cron'})
    backup = proposed.get('forward_fallback')
    if backup and backup.get('type') == 'forward' and isinstance(backup.get('args'), dict):
        previous = old.get('forward_fallback', {}).get('args', {})
        previous_state = _upstreams_state(previous.get('upstreams')) if isinstance(previous, dict) else 'draft'
        next_state = _upstreams_state(backup['args'].get('upstreams'))
        if previous_state == 'ready' and next_state == 'empty':
            changes['fallback_dns'] = ''
            targets.update(_FALLBACK)
        elif previous_state != 'ready' and next_state == 'ready':
            changes['fallback_dns'] = yaml.safe_dump(backup['args']['upstreams'], allow_unicode=True, sort_keys=False)
            targets.update(_FALLBACK)
    cron = proposed.get('rules_cron')
    previous_cron = old.get('rules_cron')
    if cron and previous_cron and isinstance(cron.get('args'), dict) and isinstance(previous_cron.get('args'), dict):
        previous_jobs, jobs = previous_cron['args'].get('jobs', []), cron['args'].get('jobs', [])
        if isinstance(previous_jobs, list) and isinstance(jobs, list) and any(isinstance(job, dict) and job.get('name') == 'refresh_rules' for job in previous_jobs) and not any(isinstance(job, dict) and (job.get('name') == 'refresh_rules' or job.get('executors') == ['$rules_refresh']) for job in jobs):
            changes['rule_update.enabled'] = False
            targets.update(_UPDATER - {'rules_cron'})

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
                working.setdefault('plugins', []).append(deepcopy(old[tag]))
        current['native_config'] = yaml.safe_dump(working, allow_unicode=True, sort_keys=False)
        try:
            result = apply_common_settings(current, changes, config, base_url)
        except OxidnsSettingsError as exc:
            # The form may have explicitly removed all affected calls already.
            # Such a native repair needs no guessing about a custom chain.
            if removed and not reference_locations(candidate, removed):
                current['native_config'] = yaml.safe_dump(candidate, allow_unicode=True, sort_keys=False)
            else:
                raise OxidnsReferenceError(str(exc), _shape_reference(candidate, targets, str(exc))) from None
        else:
            current = result['settings']
            candidate = load_native_yaml(current['native_config'])
        # Hosts are a resource field rather than a feature switch; common clear
        # removes the call/file but retains an empty definition. Explicit form
        # deletion removes that now-unused definition too.
        candidate['plugins'] = [item for item in candidate.get('plugins', []) if item.get('tag') not in removed]
        notices.append('已同步停用关联功能、处理链和受管资源；可恢复参数已保留')
    else:
        current['native_config'] = native_config

    # This also protects deletion of custom executors. We do not repair arbitrary
    # native chains, but point to their exact remaining references in the form.
    if removed:
        refs = reference_locations(candidate, removed)
        if refs:
            original_refs = reference_locations(_mask_owned(original_candidate, targets), removed)
            raise OxidnsReferenceError('删除后仍有插件引用，请在完整参数中调整', original_refs or refs)
    if changes or renames:
        current['native_config'] = yaml.safe_dump(candidate, allow_unicode=True, sort_keys=False)
    projection = read_common_settings(current, config, base_url)
    return {'settings': current, **projection, 'notices': notices}
