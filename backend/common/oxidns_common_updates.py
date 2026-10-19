"""Keep the owned rule-update pipeline valid as its resource set changes."""
from copy import deepcopy

import yaml

from backend.common.oxidns_common_routing import ROUTING_FIELDS, _references, resource_downloads
from backend.common.oxidns_native import load_native_yaml, native_document
from backend.common.oxidns_settings import OxidnsSettingsError
from backend.common.oxidns_validation import schemas
from backend.common.oxidns_references import reference_locations

_PIPELINE = {'rules_download': 'download', 'rules_reload': 'reload_provider', 'rules_refresh': 'sequence'}
_REFRESH = [{'exec': '$rules_download'}, {'exec': '$rules_reload'}]
_DOWNLOAD_ONLY = [{'exec': '$rules_download'}]


def _relevant(changes):
    return bool(set(changes) & ROUTING_FIELDS) or any(
        field.startswith(('rule_update.', 'adguard.')) for field in changes)


def _document(ctx):
    if not ctx.doc.get('include'):
        return ctx.doc
    settings = {**ctx.settings, 'native_config': yaml.safe_dump(ctx.doc, allow_unicode=True, sort_keys=False)}
    return native_document(settings, validate_arguments=False)


def _error():
    return OxidnsSettingsError('规则更新流水线仍被自定义执行链引用或包含自定义步骤，请先在完整参数中调整，不能自动停用')


def _path(entry):
    if not isinstance(entry, dict) or not isinstance(entry.get('dir'), str) or not isinstance(entry.get('filename'), str):
        return None
    return (entry['dir'].rstrip('/') + '/' + entry['filename']).removeprefix('./')


def _providers(document, existing):
    plugin_schemas = schemas()[1]
    available = {item['tag'] for item in document.get('plugins', []) if isinstance(item, dict) and
                 (item.get('type') not in plugin_schemas or plugin_schemas[item['type']].get('x-kind') == 'provider')}
    result = []
    for value in existing if isinstance(existing, list) else []:
        if isinstance(value, str) and value.startswith('$') and value[1:] in available and value not in result:
            result.append(value)
    for item in document.get('plugins', []):
        if item.get('type') in ('domain_set', 'ip_set', 'adguard_rule') and isinstance(item.get('args'), dict) and item['args'].get('files'):
            value = '$' + item['tag']
            if value not in result:
                result.append(value)
    return result


def _restore(ctx, tag):
    if ctx.plugin(tag) is not None:
        return False
    saved = ctx.parked.get(tag)
    if isinstance(saved, dict) and saved.get('tag') == tag and saved.get('type') == _PIPELINE[tag]:
        ctx.ensure(tag, kind=_PIPELINE[tag])
        return True
    return False


def prepare_rule_update(ctx, changes, config, base_url=''):
    """Restore parked options before explicit enabling, using only current targets."""
    if changes.get('rule_update.enabled') is not True:
        return
    if any(tag in ctx.included_tags for tag in _PIPELINE):
        raise OxidnsSettingsError('规则更新流水线来自 include 文件，请在完整参数或附加文件中调整')
    document = _document(ctx)
    desired = resource_downloads(document, ctx.settings, config, base_url)
    restored_download = _restore(ctx, 'rules_download')
    restored_reload = _restore(ctx, 'rules_reload')
    restored_refresh = _restore(ctx, 'rules_refresh')
    download = ctx.plugin('rules_download')
    if restored_download:
        if not isinstance(download.get('args'), dict):
            raise _error()
        # Empty owned downloads were parked only after resource reconciliation.
        # Preserve any surviving entry's options, and never restore old resources.
        historical = ctx.parked.get('rules_download_entries', [])
        historical = historical if isinstance(historical, list) else []
        old = {_path(item): item for item in [*historical, *download['args'].get('downloads', [])] if _path(item) is not None}
        download['args']['downloads'] = [deepcopy(old.get(_path(item), item)) for item in desired]
        custom = ctx.parked.get('rules_download_custom_entries', [])
        if isinstance(custom, list):
            present = {_path(item) for item in download['args']['downloads']}
            download['args']['downloads'].extend(deepcopy(item) for item in custom if _path(item) is not None and _path(item) not in present)
    reload = ctx.plugin('rules_reload')
    if restored_reload:
        if not isinstance(reload.get('args'), list):
            raise _error()
        reload['args'] = _providers(document, reload['args'])
    refresh = ctx.plugin('rules_refresh')
    if restored_refresh and refresh.get('args') == []:
        refresh['args'] = deepcopy(_REFRESH)
    if refresh and refresh.get('type') == 'sequence' and refresh.get('args') == _DOWNLOAD_ONLY and reload and reload.get('args'):
        refresh['args'] = deepcopy(_REFRESH)


def _managed_jobs(cron):
    if cron is None:
        return []
    if cron.get('type') != 'cron' or not isinstance(cron.get('args'), dict) or not isinstance(cron['args'].get('jobs'), list):
        raise _error()
    jobs = [job for job in cron['args']['jobs'] if isinstance(job, dict) and (
        job.get('name') == 'refresh_rules' or _references(job.get('executors'), 'rules_refresh'))]
    if len(jobs) > 1 or any(job.get('name') != 'refresh_rules' or job.get('executors') != ['$rules_refresh'] for job in jobs):
        raise _error()
    return jobs


def _external_references(ctx, targets, managed_jobs):
    document = _document(ctx)
    if reference_locations(document):
        return True
    external = {key: value for key, value in document.items() if key != 'plugins'}
    for tag in targets:
        if _references(external, tag):
            return True
    for item in document.get('plugins', []):
        if item.get('tag') in targets:
            continue
        value = item
        if item.get('tag') == 'rules_cron' and isinstance(item.get('args'), dict):
            value = deepcopy(item)
            value['args']['jobs'] = [job for job in item['args'].get('jobs', []) if job not in managed_jobs]
        if any(_references(value, tag) for tag in targets):
            return True
    return False


def _stop_pipeline(ctx, refresh_only=False):
    targets = {tag for tag in _PIPELINE if ctx.plugin(tag) is not None and (not refresh_only or tag == 'rules_refresh')}
    for tag in targets:
        if ctx.plugin(tag).get('type') != _PIPELINE[tag]:
            raise _error()
    refresh = ctx.plugin('rules_refresh')
    if refresh and refresh.get('args') not in (_REFRESH, _DOWNLOAD_ONLY, []):
        raise _error()
    cron = ctx.plugin('rules_cron')
    jobs = _managed_jobs(cron)
    if _external_references(ctx, targets, jobs):
        raise _error()
    for job in jobs:
        ctx.park('rules_cron_job', job)
        cron['args']['jobs'].remove(job)
    if 'rules_download' in targets:
        # Resource synchronization already removed the withdrawn entries. Retain
        # their options separately from the empty parked plugin, so changing a
        # disabled option cannot resurrect a removed download.
        original = load_native_yaml(ctx.settings['native_config'])
        prior = next((item for item in original.get('plugins', []) if item.get('tag') == 'rules_download' and item.get('type') == 'download'), {})
        entries = prior.get('args', {}).get('downloads', []) if isinstance(prior.get('args'), dict) else []
        saved = ctx.parked.get('rules_download_entries', [])
        saved = saved if isinstance(saved, list) else []
        by_path = {_path(item): item for item in [*saved, *(entries if isinstance(entries, list) else [])] if _path(item) is not None}
        if by_path:
            ctx.park('rules_download_entries', list(by_path.values()))
    for tag in _PIPELINE:
        if tag in targets:
            ctx.park_plugin(tag)
    if cron and cron['args']['jobs'] == []:
        ctx.park_plugin('rules_cron')


def _remove_empty_reload(ctx):
    refresh = ctx.plugin('rules_refresh')
    if refresh and (refresh.get('type') != 'sequence' or refresh.get('args') not in (_REFRESH, _DOWNLOAD_ONLY)):
        raise _error()
    # The canonical refresh is the only reference that can safely be rewritten.
    document = _document(ctx)
    for item in document.get('plugins', []):
        if item.get('tag') not in ('rules_reload', 'rules_refresh') and _references(item, 'rules_reload'):
            raise _error()
    if _references({key: value for key, value in document.items() if key != 'plugins'}, 'rules_reload'):
        raise _error()
    ctx.park_plugin('rules_reload')
    if refresh:
        ctx.park('rules_refresh', refresh)
        refresh['args'] = deepcopy(_DOWNLOAD_ONLY)


def finish_rule_updates(ctx, changes, config, base_url=''):
    """Suspend an empty owned pipeline without deleting independent user jobs."""
    if not _relevant(changes):
        return
    # Turning off the managed scheduler also suspends its unused executors.
    # Otherwise incomplete options keep failing validation despite the switch
    # being off. Independent jobs/callers continue to own their live pipeline.
    if any(field.startswith('rule_update.') for field in changes):
        try:
            jobs = _managed_jobs(ctx.plugin('rules_cron'))
            refresh = ctx.plugin('rules_refresh')
            targets = {tag for tag in _PIPELINE if ctx.plugin(tag) is not None}
            recognizable = all(ctx.plugin(tag).get('type') == _PIPELINE[tag] for tag in targets)
            recognizable = recognizable and (not refresh or refresh.get('args') in (_REFRESH, _DOWNLOAD_ONLY, []))
            if not jobs and targets and recognizable and not _external_references(ctx, targets, []):
                download = ctx.plugin('rules_download')
                if download and isinstance(download.get('args'), dict) and isinstance(download['args'].get('downloads'), list):
                    managed = {_path(item) for item in resource_downloads(_document(ctx), ctx.settings, config, base_url)}
                    historical = ctx.parked.get('rules_download_managed_paths', [])
                    if isinstance(historical, list):
                        managed.update(path for path in historical if isinstance(path, str))
                    ctx.park('rules_download_managed_paths', sorted(path for path in managed if path is not None))
                    ctx.park('rules_download_custom_entries', [item for item in download['args']['downloads'] if _path(item) not in managed])
                _stop_pipeline(ctx)
                return
        except OxidnsSettingsError:
            # A custom cron/refresh is not owned by the common switch.
            pass
    download = ctx.plugin('rules_download')
    reload = ctx.plugin('rules_reload')
    refresh = ctx.plugin('rules_refresh')
    empty_download = download and download.get('type') == 'download' and isinstance(download.get('args'), dict) and download['args'].get('downloads', []) == []
    empty_refresh = refresh and refresh.get('type') == 'sequence' and refresh.get('args') == []
    if empty_download or empty_refresh:
        _stop_pipeline(ctx, refresh_only=bool(empty_refresh and not empty_download and reload and reload.get('args')))
        return
    if reload and reload.get('type') == 'reload_provider' and reload.get('args') == []:
        if download and download.get('type') == 'download' and isinstance(download.get('args'), dict) and download['args'].get('downloads'):
            _remove_empty_reload(ctx)
        else:
            _stop_pipeline(ctx)
        return
    # A custom download may remain useful after its last managed provider was
    # removed. Restore only live provider references when resources return.
    saved = ctx.parked.get('rules_reload')
    if download and not reload and isinstance(saved, dict) and saved.get('tag') == 'rules_reload' and \
            saved.get('type') == 'reload_provider' and isinstance(saved.get('args'), list):
        providers = _providers(_document(ctx), saved['args'])
        if providers:
            _restore(ctx, 'rules_reload')
            ctx.plugin('rules_reload')['args'] = providers
            if refresh and refresh.get('type') == 'sequence' and refresh.get('args') == _DOWNLOAD_ONLY:
                refresh['args'] = deepcopy(_REFRESH)
