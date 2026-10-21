"""Project opted-in rule changes onto a successful deployment's source snapshot.

The source snapshot and archive must come from the same successful deployment.
Neither function mutates its inputs. No network requests or repository writes are
made here. Settings are ``{'library': bool, 'rules': bool}``; omitted scopes are off.
"""
from copy import deepcopy
import hashlib
import io
import json
import re
import tarfile
from urllib.parse import parse_qs, urlsplit

import yaml

from .deployment_bundle import (
    DeploymentPreparationError, _local_rule, _pack, deployment_content_fingerprint, relative_path,
)


class RuleSyncProjectionError(ValueError):
    pass


_SOURCE_FIELDS = ('name', 'url', 'behavior', 'content', 'source_type', 'format')
_SELECTORS = ('direct_rulesets', 'proxy_rulesets', 'direct_rules', 'proxy_rules')
_GROUP_TAG = re.compile(r'rules_(?:domain|ip)_\d+(?:_\d+)*\Z')


def _scope(item):
    return 'library' if item.get('itemType') == 'ruleset' else 'rules' if item.get('itemType') == 'rule' else None


def _merge_items(previous, desired, settings):
    """Refill opted-in slots in desired order; inactive items remain anchors.

    Overflow follows the last selected slot. If the old list had no selected
    slot, new entries precede the anchored list, matching the rule editor.
    """
    selected = lambda item: bool(settings.get(_scope(item), False))
    replacements = [deepcopy(item) for item in desired if selected(item)]
    if settings.get('library') and settings.get('rules'):
        return replacements + [deepcopy(item) for item in previous if _scope(item) is None]
    slots = [index for index, item in enumerate(previous) if selected(item)]
    if not slots:
        return replacements + deepcopy(previous)
    output, offset = [], 0
    for index, item in enumerate(previous):
        if selected(item):
            if offset < len(replacements):
                output.append(replacements[offset])
                offset += 1
            if index == slots[-1]:
                output.extend(replacements[offset:])
        else:
            output.append(deepcopy(item))
    return output


def _hydrate(config):
    library = {entry['id']: entry for entry in config.get('rule_library', [])}
    for item in config.get('rule_configs', []):
        if item.get('itemType') != 'ruleset' or not item.get('library_rule_id'):
            continue
        source = library.get(item['library_rule_id'])
        if source is None:
            raise RuleSyncProjectionError('规则集引用的共享来源不存在，请先修复规则引用。')
        for key in (*_SOURCE_FIELDS, 'library_enabled'):
            item.pop(key, None)
        item.update({key: deepcopy(source[key]) for key in _SOURCE_FIELDS if key in source})
        item.setdefault('behavior', 'domain')
        item['library_enabled'] = source.get('enabled', True)
        if source.get('source_type') == 'content':
            item['url'] = f'/api/profiles/{config.get("profile_id") or "default"}/rule-library/content/{source["id"]}'
    return config


def _native_projection(base, current, projected, settings):
    """Edit only a recognizable native routing graph, preserving its surroundings."""
    from backend.common.oxidns_native import load_native_yaml
    from backend.common.oxidns_common_routing import (
        _analyze, _references, place_routing_groups, reload_references, resource_downloads,
    )
    from backend.converters.oxidns import _routing_groups

    original = base.get('oxidns') or {}
    document = load_native_yaml(original.get('native_config', ''))
    before = _analyze(document, original, base)
    keys = [key for key in _SELECTORS if settings.get('library' if key.endswith('rulesets') else 'rules')]
    if not keys:
        return
    # Content-only updates can use existing file references even when a custom
    # routing graph cannot be uniquely associated with editor rows.
    source_before = _source_rule_state(base, include_content=False)
    source_after = _source_rule_state(projected, include_content=False)
    current_oxi = current.get('oxidns') or {}
    same_document = current_oxi.get('native_config') == original.get('native_config')
    if source_before == source_after and same_document:
        return
    if any(not before['capabilities'][key]['editable'] for key in keys):
        raise RuleSyncProjectionError('OxiDNS 原生分流包含无法识别的自定义结构，无法自动应用规则结构；请手动检查并发布。')
    values = deepcopy(before['values'])
    if current_oxi.get('config_mode') == 'native' and not same_document:
        desired_document = load_native_yaml(current_oxi.get('native_config', ''))
        desired = _analyze(desired_document, current_oxi, current)
        if any(not desired['capabilities'][key]['editable'] for key in keys):
            raise RuleSyncProjectionError('OxiDNS 草稿分流无法唯一对应当前规则，请手动检查并发布。')
        for key in keys:
            values[key] = deepcopy(desired['values'][key])
    elif current_oxi.get('config_mode') != 'native':
        for key in keys:
            values[key] = deepcopy(current_oxi.get(key, []))
    # Native source selectors are a retained resource catalog, not routing truth.
    # If YAML did not change, retain the deployed graph's selections.
    available = {item['id'] for item in projected.get('rule_configs', [])
                 if item.get('enabled', True) and item.get('library_enabled', True)}
    for key in _SELECTORS:
        values[key] = [identity for identity in values[key] if identity in available]
        projected.setdefault('oxidns', {})[key] = deepcopy(values[key])
    values['aggregate_rules'] = True if values.get('aggregate_rules') is None else values['aggregate_rules']
    groups = _routing_groups(projected, values)
    old_groups = [group for entries in before['groups'].values() for group in entries]
    old_tags = {group['tag'] for group in old_groups}
    if any(set(group['provider']) - {'tag', 'type', 'args'} for group in old_groups):
        raise RuleSyncProjectionError('OxiDNS 规则 Provider 包含额外原生参数，请手动检查并发布。')
    for tag in old_tags:
        if (_references({key: value for key, value in document.items() if key != 'plugins'}, tag)
                or _references(values.get('custom_matches', []), tag)
                or any(_references(plugin, tag) for plugin in document['plugins']
                       if plugin.get('tag') not in old_tags | {'sequence_main', 'rules_reload'})):
            raise RuleSyncProjectionError('OxiDNS 规则 Provider 被自定义插件共用，无法安全自动替换；请手动发布。')
    result = deepcopy(document)
    # Same naming and placement as the editor: DNS follow compares the draft
    # it edited with the bytes rule sync deployed.
    result['plugins'], groups = place_routing_groups(result['plugins'], old_groups, groups)
    plugins = {plugin['tag']: plugin for plugin in result['plugins']}
    domain = [{'matches': [f'qname ${group["tag"]}'],
               'exec': 'goto china_dns' if group['route'] == 'direct' else 'goto proxy_dns_seq'} for group in groups['domain']]
    ip = [{'matches': [f'resp_ip ${group["tag"]}'],
           'exec': 'goto direct_ip' if group['route'] == 'direct' else 'goto ip_requery_proxy'} for group in groups['ip']]
    custom = deepcopy(before['custom'])
    suffix = deepcopy(before['dynamic'])
    if ip:
        suffix.extend([{'exec': 'jump local_probe'}, *ip,
                       {'matches': ['has_resp'], 'exec': 'goto default_direct_ip'}
                       if values['default_forward'] == 'forward_local' else {'exec': 'drop_resp'}])
    head = values.get('custom_match_position') == 'head'
    plugins['sequence_main']['args'] = [*deepcopy(before['prefix']), *(custom if head else []),
                                       *domain, *(custom if not head else []), *suffix, deepcopy(before['final'])]
    for entry in domain + ip + ([{'exec': 'jump local_probe'}] if ip else []):
        target = entry['exec'].split(' ', 1)[1]
        if plugins.get(target, {}).get('type') != 'sequence':
            raise RuleSyncProjectionError(f'OxiDNS 缺少分流执行链 {target}，请手动检查并发布。')
    download, reload = plugins.get('rules_download'), plugins.get('rules_reload')
    if download is not None or reload is not None:
        if (not download or download.get('type') != 'download' or not isinstance(download.get('args'), dict)
                or not isinstance(download['args'].get('downloads'), list)
                or not reload or reload.get('type') != 'reload_provider' or not isinstance(reload.get('args'), list)):
            raise RuleSyncProjectionError('OxiDNS 下载和重载任务包含自定义结构，请手动检查并发布。')
        def path(entry):
            return str(entry.get('dir', '')).rstrip('/') + '/' + str(entry.get('filename', ''))
        old = {path(item): item for item in resource_downloads(document, original, base)}
        new = {path(item): item for item in resource_downloads(result, projected['oxidns'], projected)}
        downloads = []
        for item in download['args']['downloads']:
            location = path(item)
            if location not in old:
                downloads.append(item)
            elif location in new:
                replacement = new.pop(location)
                if item.get('url') != old[location].get('url') and replacement.get('url') != old[location].get('url'):
                    raise RuleSyncProjectionError('OxiDNS 规则下载地址已手动覆盖，请手动检查并发布。')
                downloads.append({**item, **replacement} if item.get('url') == old[location].get('url') else item)
        download['args']['downloads'] = downloads + list(new.values())
        reload['args'] = reload_references(reload['args'], old_tags,
                                           [group['tag'] for entries in groups.values() for group in entries], plugins)
    projected['oxidns']['native_config'] = yaml.safe_dump(result, allow_unicode=True, sort_keys=False)


def project_rule_changes(base, current, settings, *, service_type=None):
    """Return the deployed source with only opted-in rule scopes advanced."""
    result = deepcopy(base)
    if not settings.get('library') and not settings.get('rules'):
        return result
    if settings.get('library'):
        result['rule_library'] = deepcopy(current.get('rule_library', []))
    result['rule_configs'] = _merge_items(base.get('rule_configs', []), current.get('rule_configs', []), settings)
    _hydrate(result)
    for key in _SELECTORS:
        if settings.get('library' if key.endswith('rulesets') else 'rules'):
            result.setdefault('oxidns', {})[key] = deepcopy((current.get('oxidns') or {}).get(key, []))
    if service_type != 'mihomo' and (base.get('oxidns') or {}).get('config_mode') == 'native':
        _native_projection(base, current, result, settings)
    return result


def _source_rule_state(config, *, include_content=True):
    state = []
    for item in config.get('rule_configs', []):
        if not item.get('enabled', True) or not item.get('library_enabled', True):
            continue
        if _scope(item) == 'library':
            keys = ('id', 'library_rule_id', 'name', 'url', 'behavior', 'source_type', 'format', 'policy', 'no_resolve')
            if include_content:
                keys += ('content',)
        elif _scope(item) == 'rules':
            keys = ('rule_type', 'value', 'policy', 'no_resolve')
        else:
            continue
        state.append({key: deepcopy(item[key]) for key in keys if key in item})
    return state


def _rule_state(config, service_type, *, include_content):
    config = _hydrate(deepcopy(config))
    if service_type == 'mihomo':
        return _source_rule_state(config, include_content=include_content)
    if service_type != 'oxidns':
        raise RuleSyncProjectionError('规则自动同步仅支持 Mihomo 和 OxiDNS。')
    from backend.converters.oxidns import generate_oxidns_config, _rule_path, _ruleset_parts
    from backend.common.oxidns_native import resource_references, relative_resource
    document = yaml.safe_load(generate_oxidns_config(config))
    referenced = {relative_resource(path) for path in resource_references(document)}
    sources = []
    for item in config.get('rule_configs', []):
        if item.get('itemType') != 'ruleset' or not item.get('enabled', True) or not item.get('library_enabled', True):
            continue
        paths = [relative_resource(_rule_path(item, part)) for part in _ruleset_parts(item.get('behavior', 'domain'))]
        paths = [path for path in paths if path in referenced]
        if not paths:
            continue
        source = {key: deepcopy(item[key]) for key in ('source_type', 'url', 'behavior') if key in item}
        if include_content and item.get('source_type') == 'content':
            source['content'] = item.get('content', '')
        sources.append({'paths': paths, **source})
    return {'document': document, 'sources': sources}


def rule_signature(config, service_type, *, include_content=True):
    """Hash effective consumers, excluding unused library entries and metadata."""
    state = _rule_state(config, service_type, include_content=include_content)
    return hashlib.sha256(json.dumps(state, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def rule_change_kind(base, current, service_type, settings):
    """Classify the isolated projection as none, source-content, or structure."""
    projected = project_rule_changes(base, current, settings, service_type=service_type)
    if rule_signature(base, service_type) == rule_signature(projected, service_type):
        return 'none'
    if rule_signature(base, service_type, include_content=False) == rule_signature(projected, service_type, include_content=False):
        return 'content'
    return 'structure'


def rule_source_fingerprint(url, config, base_url, *, depth=0):
    """Describe what a rule URL serves in ``config`` without fetching it.

    Mirrors the resolution of ``materialize_rule``: an external URL stands for
    itself, local callbacks resolve to the rule library entry they serve. Returns
    None when the source cannot be identified, so callers refresh the file.
    """
    from backend.utils.rule_fetch import is_internal_rule_url
    if depth > 3 or not isinstance(url, str) or not url:
        return None
    parsed = urlsplit(url)
    settings = {**config.get('system_config', {}),
                'server_domain': config.get('system_config', {}).get('server_domain') or base_url}
    if parsed.scheme or parsed.netloc:
        if not is_internal_rule_url(url, {'system_config': settings}):
            return ['url', url]
    if parsed.path.endswith(('/mosdns/rule-proxy', '/oxidns/rule-proxy')):
        inner = rule_source_fingerprint(parse_qs(parsed.query).get('url', [''])[0], config, base_url, depth=depth + 1)
        return inner and ['proxy', inner]
    try:
        rule = _local_rule(url, config, base_url)
    except DeploymentPreparationError:
        return None
    # Gateway and other generated callbacks depend on more than one entry.
    if rule is None:
        return None
    shape = [rule.get('format'), rule.get('behavior')]
    if rule.get('source_type') == 'content':
        return ['content', *shape, hashlib.sha256(str(rule.get('content', '')).encode()).hexdigest()]
    source = rule.get('url', '')
    inner = None if source == url else rule_source_fingerprint(source, config, base_url, depth=depth + 1)
    return inner and ['library', *shape, inner]


def unchanged_rule_paths(old_entries, new_entries, base, current, base_url):
    """Rule files whose download URL and served source are identical.

    Entries are ``{'url', 'local_path'}`` download descriptors generated from
    ``base`` and ``current`` respectively. A file absent from either side, or
    with an unidentifiable source, is never reported as unchanged.
    """
    old = {}
    for item in old_entries:
        old.setdefault(relative_path(item['local_path']), set()).add(item.get('url'))
    result = set()
    for item in new_entries:
        path = relative_path(item['local_path'])
        if old.get(path) != {item.get('url')}:
            continue
        before = rule_source_fingerprint(item.get('url'), base, base_url)
        if before is not None and before == rule_source_fingerprint(item.get('url'), current, base_url):
            result.add(path)
    duplicates = [relative_path(item['local_path']) for item in new_entries]
    return {path for path in result if duplicates.count(path) == 1}


def mihomo_rule_entries(document):
    """HTTP rule-provider download descriptors of a Mihomo main document."""
    return [{'name': name, 'url': provider['url'], 'local_path': provider['path']}
            for name, provider in ((document or {}).get('rule-providers') or {}).items()
            if isinstance(provider, dict) and provider.get('type') == 'http'
            and isinstance(provider.get('url'), str) and isinstance(provider.get('path'), str)]


def read_bundle_files(bundle):
    """Read and verify a trusted stored deployment; return path -> (bytes, role)."""
    deployment_content_fingerprint(bundle.manifest)
    if hashlib.sha256(bundle.archive).hexdigest() != bundle.sha256:
        raise DeploymentPreparationError('Stored rule-sync deployment digest mismatch')
    result = {}
    with tarfile.open(fileobj=io.BytesIO(bundle.archive), mode='r:gz') as archive:
        for entry in bundle.manifest['files']:
            member = archive.getmember('files/' + entry['path'])
            if not member.isfile() or member.size != entry['size']:
                raise DeploymentPreparationError('Stored rule-sync deployment file mismatch')
            content = archive.extractfile(member).read()
            if hashlib.sha256(content).hexdigest() != entry['sha256']:
                raise DeploymentPreparationError('Stored rule-sync deployment file digest mismatch')
            result[entry['path']] = (content, entry['role'])
    return result


def _sequence_skeleton(plugin):
    if not isinstance(plugin, dict) or plugin.get('type') != 'sequence' or not isinstance(plugin.get('args'), list):
        raise RuleSyncProjectionError('OxiDNS 主执行链无法安全隔离规则更新。')
    args = plugin['args']
    removable, ip_indices = set(), []
    for index, entry in enumerate(args):
        matches = entry.get('matches') if isinstance(entry, dict) else None
        if isinstance(matches, list) and len(matches) == 1 and isinstance(matches[0], str):
            match = re.fullmatch(r'(qname|resp_ip) \$(rules_(?:domain|ip)_\d+(?:_\d+)*)', matches[0])
            if match and set(entry) == {'matches', 'exec'}:
                targets = ('goto china_dns', 'goto proxy_dns_seq') if match[1] == 'qname' else ('goto direct_ip', 'goto ip_requery_proxy')
                if entry['exec'] in targets:
                    removable.add(index)
                    if match[1] == 'resp_ip':
                        ip_indices.append(index)
    if ip_indices:
        start, end = min(ip_indices), max(ip_indices)
        if (start == 0 or args[start - 1] != {'exec': 'jump local_probe'}
                or ip_indices != list(range(start, end + 1)) or end + 1 >= len(args)
                or args[end + 1] not in ({'exec': 'drop_resp'}, {'matches': ['has_resp'], 'exec': 'goto default_direct_ip'})):
            raise RuleSyncProjectionError('OxiDNS IP 分流区包含自定义结构，无法自动隔离。')
        removable.update((start - 1, end + 1))
    return {**deepcopy(plugin), 'args': [deepcopy(entry) for index, entry in enumerate(args) if index not in removable]}


def _isolate_oxidns_document(previous, candidate):
    left = {plugin['tag']: plugin for plugin in previous.get('plugins', [])}
    right = {plugin['tag']: plugin for plugin in candidate.get('plugins', [])}
    if len(left) != len(previous.get('plugins', [])) or len(right) != len(candidate.get('plugins', [])):
        raise RuleSyncProjectionError('OxiDNS 插件标签重复，无法隔离规则更新。')
    allowed = lambda tag: bool(_GROUP_TAG.fullmatch(tag)) or tag in ('sequence_main', 'rules_download', 'rules_reload')
    if _sequence_skeleton(left.get('sequence_main')) != _sequence_skeleton(right.get('sequence_main')):
        raise RuleSyncProjectionError('OxiDNS 候选配置包含规则范围之外的执行链变化。')
    for tag in set(left) | set(right):
        if allowed(tag):
            continue
        if left.get(tag) != right.get(tag):
            raise RuleSyncProjectionError('OxiDNS 候选配置包含规则范围之外的插件变化。')
    for tag in ('rules_download', 'rules_reload'):
        old, new = left.get(tag), right.get(tag)
        if (old is None) != (new is None):
            raise RuleSyncProjectionError('自动同步不能启用或关闭 OxiDNS 下载和重载服务。')
        if old is not None:
            ignored = ('downloads',) if tag == 'rules_download' else ()
            old_meta, new_meta = deepcopy(old), deepcopy(new)
            if ignored:
                for value in (old_meta, new_meta):
                    value.get('args', {}).pop('downloads', None)
            else:
                old_meta.pop('args', None)
                new_meta.pop('args', None)
            if old_meta != new_meta:
                raise RuleSyncProjectionError('OxiDNS 候选配置改变了下载和重载服务参数。')
    result = deepcopy(previous)
    # Non-rule plugin order is unchanged by normal generation. Reject a reordered
    # candidate rather than change lifecycle ordering of custom plugins.
    if [tag for tag in left if not allowed(tag)] != [tag for tag in right if not allowed(tag)]:
        raise RuleSyncProjectionError('OxiDNS 候选配置改变了非规则插件顺序。')
    result['plugins'] = [deepcopy(plugin if allowed(plugin['tag']) else left[plugin['tag']])
                         for plugin in candidate.get('plugins', [])]
    result_plugins = {plugin['tag']: plugin for plugin in result['plugins']}
    def managed_paths(plugins):
        return {relative_path(path) for tag, plugin in plugins.items() if _GROUP_TAG.fullmatch(tag)
                for path in (plugin.get('args') or {}).get('files', [])}
    old_paths, new_paths = managed_paths(left), managed_paths(right)
    from backend.common.oxidns_native import resource_references, relative_resource
    other_paths = {relative_resource(path) for path in resource_references({'plugins': [
        plugin for tag, plugin in left.items() if not _GROUP_TAG.fullmatch(tag)]})}
    if (old_paths | new_paths) & other_paths:
        raise RuleSyncProjectionError('OxiDNS 规则文件被其他插件共用，无法单独自动更新。')
    if 'rules_download' in result_plugins:
        def location(entry):
            if not isinstance(entry, dict):
                raise RuleSyncProjectionError('OxiDNS 规则下载条目格式无效。')
            return relative_path(str(entry.get('dir', '')).rstrip('/') + '/' + str(entry.get('filename', '')))
        candidate_downloads = right['rules_download']['args']['downloads']
        managed = [deepcopy(entry) for entry in candidate_downloads if location(entry) in new_paths]
        unrelated = [deepcopy(entry) for entry in left['rules_download']['args']['downloads'] if location(entry) not in old_paths]
        result_plugins['rules_download']['args']['downloads'] = managed + unrelated
        managed_refs = [value for value in right['rules_reload']['args']
                        if isinstance(value, str) and _GROUP_TAG.fullmatch(value.removeprefix('$'))]
        other_refs = [value for value in left['rules_reload']['args']
                      if not isinstance(value, str) or not _GROUP_TAG.fullmatch(value.removeprefix('$'))]
        result_plugins['rules_reload']['args'] = deepcopy(managed_refs + other_refs)
    return result


def isolate_rule_bundle(baseline_bundle, candidate_bundle, service_type):
    """Keep deployed non-rule configuration/files byte-for-byte in a candidate.

    Candidate sources must already be projected with ``project_rule_changes``.
    This second boundary also protects against regenerated/fetched node data.
    """
    for field in ('agent_id', 'profile_id', 'service_type'):
        if baseline_bundle.manifest.get(field) != candidate_bundle.manifest.get(field):
            raise RuleSyncProjectionError('规则同步的设备或配置空间已经改变。')
    if candidate_bundle.manifest.get('service_type') != service_type:
        raise RuleSyncProjectionError('规则同步服务类型不匹配。')
    baseline, candidate = read_bundle_files(baseline_bundle), read_bundle_files(candidate_bundle)
    old_path, new_path = baseline_bundle.manifest['config_path'], candidate_bundle.manifest['config_path']
    previous = yaml.safe_load(baseline[old_path][0])
    proposed = yaml.safe_load(candidate[new_path][0])
    if service_type == 'mihomo':
        document = deepcopy(previous)
        for key in ('rules', 'rule-providers'):
            if key in proposed:
                document[key] = deepcopy(proposed[key])
            else:
                document.pop(key, None)
        paths = {relative_path(provider['path']) for provider in document.get('rule-providers', {}).values()
                 if isinstance(provider, dict) and provider.get('path')}
        old_paths = {relative_path(provider['path']) for provider in previous.get('rule-providers', {}).values()
                     if isinstance(provider, dict) and provider.get('path')}
    elif service_type == 'oxidns':
        document = _isolate_oxidns_document(previous, proposed)
        def paths_for(value):
            return {relative_path(path) for plugin in value.get('plugins', []) if _GROUP_TAG.fullmatch(plugin['tag'])
                    for path in (plugin.get('args') or {}).get('files', [])}
        paths, old_paths = paths_for(document), paths_for(previous)
    else:
        raise RuleSyncProjectionError('规则自动同步仅支持 Mihomo 和 OxiDNS。')
    files = {path: value for path, value in baseline.items() if path != old_path and path not in old_paths}
    for path in paths:
        if path in files:
            raise RuleSyncProjectionError('规则文件与非规则文件冲突，无法自动隔离。')
        value = candidate.get(path)
        if value is None or value[1] != 'rule':
            raise RuleSyncProjectionError('候选规则文件缺失或角色不符。')
        files[path] = value
    content = yaml.safe_dump(document, allow_unicode=True, sort_keys=False).encode('utf-8')
    files[old_path] = (content, 'config')
    manifest = deepcopy(candidate_bundle.manifest)
    manifest['config_path'] = old_path
    manifest['files'] = [{'path': path, 'role': role, 'size': len(value), 'sha256': hashlib.sha256(value).hexdigest()}
                         for path, (value, role) in sorted(files.items())]
    deployment_content_fingerprint(manifest)
    return _pack(manifest, files, hashlib.sha256(content).hexdigest()[:8])
