"""Keep the owned rule-update pipeline valid as its resource set changes."""
from copy import deepcopy
from urllib.parse import parse_qsl, urlsplit

import yaml

from backend.common.oxidns_common_routing import (
    ROUTING_FIELDS, _references, resource_downloads, _catalog, resource_readers, resource_is_read,
    replace_download_entries,
)
from backend.common.oxidns_native import load_native_yaml, native_document, resource_references, relative_resource
from backend.converters.oxidns import _base, _rule_path, _tag
from backend.converters.mosdns import _build_rule_proxy_url
from backend.common.profile_context import profile_api_path
from backend.common.oxidns_settings import OxidnsSettingsError, get_default_oxidns_settings
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


def reconcile_native_withdrawals(before, candidate, original_settings, settings, config):
    """Withdraw proven managed targets, retaining every other reader/download.

    Compare actual resource references, including supplied include files. A
    filename prefix alone never establishes ownership; neither custom download
    plugins nor unrelated entries in the standard downloader are edited.
    """
    def expanded(document, values):
        return native_document({**values, 'native_config': yaml.safe_dump(document)},
                               validate_arguments=False) if document.get('include') else document

    try:
        previous = expanded(before, original_settings)
        current = expanded(candidate, settings)
        old_paths = {relative_resource(path) for path in resource_references(previous)}
        references = resource_references(current)
        live = {relative_resource(path) for path in references}
    except OxidnsSettingsError:
        # Complete-form drafts may contain unfinished unrelated resource fields.
        # Without a proven reference set, leave every download untouched.
        return 0, False
    # Unexpanded paths could still consume one of the withdrawn files.
    if any('${' in path for path in references):
        return 0, False
    paths = {_rule_path(record['item'], part).removeprefix('./')
             for record in _catalog(config)[0].values() if record['suffix'] == 'rulesets'
             for part in record['parts']}
    adguard = original_settings.get('adguard', {})
    paths.update(f'rules/{_tag("adguard", url)}.txt' for url in adguard.get('urls', [])
                 if isinstance(url, str))
    withdrawn = (old_paths - live) & paths
    # Future/custom plugins may name their input differently (e.g. rule_path).
    # Literal readers remain protected; download destinations are writers.
    readers = [*references, *resource_readers(current)]
    withdrawn = {path for path in withdrawn if not resource_is_read(path, readers)}
    downloader = next((p for p in candidate.get('plugins', []) if p.get('tag') == 'rules_download'), None)
    if not withdrawn or not downloader or downloader.get('type') != 'download':
        return 0, False
    args = downloader.get('args')
    if not isinstance(args, dict) or not isinstance(args.get('downloads'), list):
        return 0, False  # Leave unfinished/custom native values to their editor.
    prior = deepcopy(candidate)
    retained = [item for item in args['downloads'] if _path(item) not in withdrawn]
    removed = len(args['downloads']) - len(retained)
    replace_download_entries(downloader, retained)
    # Reuse the common editor's conservative empty-pipeline lifecycle. It keeps
    # user jobs/downloads, and refuses unknown executor edges atomically.
    from backend.common.oxidns_common import _Context
    finish_rule_updates(_Context(candidate, settings), {'direct_rulesets': []}, config)
    return removed, candidate != prior


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
    """Keep manual updates available for live subscriptions, independently of cron."""
    if not _relevant(changes):
        return
    if any(tag in ctx.included_tags or ctx.alias_capability(tag) for tag in _PIPELINE):
        if changes.get('rule_update.enabled') is True:
            raise OxidnsSettingsError('规则更新流水线来自 include 文件或已重命名，请在完整参数或附加文件中调整')
        return
    document = _document(ctx)
    desired = resource_downloads(document, ctx.settings, config, base_url)
    if not desired:
        return
    # Do not introduce a second update plan alongside custom native executors.
    custom = any(ctx.plugin(tag) and ctx.plugin(tag).get('type') != kind for tag, kind in _PIPELINE.items())
    if not all(ctx.plugin(tag) for tag in _PIPELINE):
        # Imported YAML may rename only part of a chain, without editor alias
        # metadata. A custom sequence can also reuse both standard executors.
        custom = custom or any(item.get('type') in ('download', 'reload_provider') and item.get('tag') not in _PIPELINE
                               for item in document.get('plugins', []))
        kinds = {item.get('tag'): item.get('type') for item in document.get('plugins', [])}
        for item in document.get('plugins', []):
            steps = item.get('args')
            if item.get('type') != 'sequence' or item.get('tag') == 'rules_refresh' or not isinstance(steps, list) or len(steps) != 2:
                continue
            refs = [step.get('exec', '') if isinstance(step, dict) and set(step) == {'exec'} else '' for step in steps]
            if all(isinstance(ref, str) and ref.startswith('$') for ref in refs) and \
                    [kinds.get(ref[1:]) for ref in refs] == ['download', 'reload_provider']:
                custom = True
    if custom:
        if changes.get('rule_update.enabled') is True:
            raise OxidnsSettingsError('已有自定义规则更新插件，请在完整参数中配置其定时任务，不能重复创建更新链')
        return
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
    defaults = get_default_oxidns_settings()['rule_update']['args']
    ctx.ensure('rules_download', {'timeout': defaults['timeout'], 'downloads': desired}, 'download')
    ctx.ensure('rules_reload', _providers(document, []), 'reload_provider')
    ctx.ensure('rules_refresh', _REFRESH, 'sequence')


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
    # A disabled scheduler still has a manual caller through the Agent API.
    # Only withdrawn resources or an explicitly empty chain suspend executors.
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


def _managed_downloads(document):
    downloader = next((item for item in document.get('plugins', [])
                       if item.get('tag') == 'rules_download'), None)
    if downloader is None or downloader.get('type') != 'download':
        return None
    args = downloader.get('args')
    if not isinstance(args, dict) or not isinstance(args.get('downloads'), list):
        return None
    return args['downloads']


def _proxy_identity(config):
    """(known capability tokens, profile rule-proxy endpoint), or None without a token."""
    system = config.get('system_config', {})
    token = system.get('rule_proxy_token')
    if not isinstance(token, str) or not token:
        return None
    retired = system.get('retired_rule_proxy_tokens', [])
    tokens = {value for value in [token, *(retired if isinstance(retired, list) else [])]
              if isinstance(value, str) and value}
    endpoint = profile_api_path(config, '/mosdns/rule-proxy')
    if endpoint == '/mosdns/rule-proxy':
        endpoint = '/api' + endpoint
    return tokens, endpoint


def _ruleset_sources(config, referenced=None):
    """Active ruleset sources by generated resource path: {path: (source, part)}."""
    sources = {}
    for record in _catalog(config)[0].values():
        source = record['item'].get('url')
        if record['suffix'] != 'rulesets' or not isinstance(source, str) or not source:
            continue
        for part in record['parts']:
            path = relative_resource(_rule_path(record['item'], part))
            if referenced is None or path in referenced:
                sources[path] = (source, part)
    return sources


def _generated_source(entry, identity, source, part):
    """The origin of a generated rule-proxy URL built from source, else None.

    The profile endpoint, exact query, part, a known capability token and the
    nested source together prove ConfigFlow rendered it; anything else is a
    manual override.
    """
    if not isinstance(entry.get('url'), str):
        return None
    tokens, endpoint = identity
    try:
        parsed = urlsplit(entry['url'])
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname
                or parsed.username is not None or parsed.fragment
                or not parsed.path.endswith(endpoint)):
            return None
        # Reading port also rejects malformed authorities conservatively.
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            return None
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
    except ValueError:
        return None
    query = dict(pairs)
    if (len(pairs) != 3 or set(query) != {'url', 'part', 'token'}
            or query['token'] not in tokens or query['part'] != part):
        return None
    origin = parsed._replace(path=parsed.path[:-len(endpoint)], query='', fragment='').geturl()
    return origin if query['url'] == (origin + source if source.startswith('/') else source) else None


def _with_downloads(document, output):
    rendered = deepcopy(document)
    downloader = next(item for item in rendered['plugins'] if item.get('tag') == 'rules_download')
    replace_download_entries(downloader, output)
    return rendered


def _refresh_managed_rule_urls(document, config, base_url):
    """Rebase proven generated downloads without changing custom native state.

    Old native drafts retain absolute URLs from their first template render.
    The resource path, profile endpoint, source, part and a known capability
    token together identify a generated URL, even after its origin changes.
    """
    downloads = _managed_downloads(document)
    identity = _proxy_identity(config)
    effective = _base(config, base_url)
    if downloads is None or identity is None or not effective:
        return document
    sources = _ruleset_sources(config, {relative_resource(path) for path in resource_references(document)})
    output, changed = [], False
    for entry in downloads:
        output.append(entry)
        source_part = sources.get(_path(entry))
        if source_part is None:
            continue
        source, part = source_part
        if _generated_source(entry, identity, source, part) is None:
            continue
        current_source = effective + source if source.startswith('/') else source
        url = _build_rule_proxy_url(config, effective, current_source, part=part)
        if entry['url'] != url:
            output[-1] = {**entry, 'url': url}
            changed = True
    return _with_downloads(document, output) if changed else document


def rebase_native_rule_sources(before, after):
    """Follow a rule-library source edit in after's native draft, in place.

    Native drafts keep the nested source of each generated download, and
    rendering only trusts a URL whose source matches the current declaration.
    Without this, editing a source leaves the device on the old one and later
    domain changes skip the entry. Only URLs generated from the previous
    source move; manual overrides and include files stay untouched.
    """
    settings = after.get('oxidns')
    if not isinstance(settings, dict) or settings.get('config_mode') != 'native':
        return False
    try:
        document = load_native_yaml(settings.get('native_config', ''))
    except OxidnsSettingsError:
        return False
    downloads = _managed_downloads(document)
    identity = _proxy_identity(before)
    if downloads is None or identity is None:
        return False
    previous, current = _ruleset_sources(before), _ruleset_sources(after)
    output, changed = [], False
    for entry in downloads:
        output.append(entry)
        path = _path(entry)
        if path not in previous or path not in current or previous[path] == current[path]:
            continue
        (old_source, part), (new_source, new_part) = previous[path], current[path]
        origin = _generated_source(entry, identity, old_source, part) if part == new_part else None
        if origin is None:
            continue
        source = origin + new_source if new_source.startswith('/') else new_source
        output[-1] = {**entry, 'url': _build_rule_proxy_url(after, origin, source, part=part)}
        changed = True
    if changed:
        settings['native_config'] = yaml.safe_dump(_with_downloads(document, output),
                                                   allow_unicode=True, sort_keys=False)
    return changed


def prepare_native_rule_updates(document, settings, config, base_url=''):
    """Render current routing and manual updates without changing the saved draft."""
    from backend.common.oxidns_common_routing import migrate_domain_only_routing
    document, settings = migrate_domain_only_routing(document, settings, config, base_url)
    document = _refresh_managed_rule_urls(document, config, base_url)
    if any(item.get('type') in ('download', 'reload_provider') or item.get('tag') in _PIPELINE
           for item in document.get('plugins', [])):
        return document
    from backend.common.oxidns_common import _Context
    rendered = deepcopy(document)
    current = deepcopy(settings)
    current['native_config'] = yaml.safe_dump(rendered, allow_unicode=True, sort_keys=False)
    prepare_rule_update(_Context(rendered, current), {'rule_update.enabled': False}, config, base_url)
    return rendered
