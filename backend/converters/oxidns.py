"""OxiDNS templates: ordered routing, isolated probe cache and opt-in features.

Rule text conversion is shared with MosDNS. Runtime settings and the request
pipeline are deliberately generated independently because cache/control-flow
semantics differ between the two engines.
"""
from copy import deepcopy
from hashlib import sha256
from ipaddress import ip_address
from pathlib import PurePosixPath
import re
import yaml

from backend.common.oxidns_settings import normalize_oxidns_settings, OxidnsSettingsError
from backend.common.oxidns_native import (managed_files, native_document,
    relative_resource, resource_references)
from backend.converters.mosdns import (
    _build_rule_proxy_url, _parse_custom_matches, _ruleset_parts,
    convert_rule_content_for_mosdns, parse_dns_upstreams,
    split_rules_and_rulesets,
)

_TAG = re.compile(r'^[A-Za-z0-9](?:[A-Za-z0-9_-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9_-]*[A-Za-z0-9])?)*$')


def _tag(prefix, identity):
    return f'{prefix}_{sha256(str(identity).encode()).hexdigest()[:16]}'


def _settings(config):
    return normalize_oxidns_settings(config.get('oxidns', {}))


def _base(config, base_url):
    return (config.get('system_config', {}).get('server_domain') or base_url).rstrip('/')


def _items(config):
    rules, sets = split_rules_and_rulesets(config)
    if config.get('rule_configs'):
        return [item for item in config['rule_configs']
                if item.get('enabled', True) and item.get('library_enabled', True)]
    return [dict(item, itemType='rule') for item in rules] + [dict(item, itemType='ruleset') for item in sets]


def _selected(config, settings):
    for item in _items(config):
        suffix = 'rulesets' if item.get('itemType') == 'ruleset' else 'rules'
        identity = item.get('id')
        if identity in settings[f'direct_{suffix}']:
            yield item, 'direct'
        elif identity in settings[f'proxy_{suffix}']:
            yield item, 'proxy'


def _rule_path(item, part):
    return f'./rules/{_tag("rule", item["id"])}_{part}.txt'


def get_oxidns_ruleset_downloads(config_data, base_url=''):
    settings = _settings(config_data)
    native_paths = ({relative_resource(path) for path in resource_references(native_document(settings))}
                    if settings['config_mode'] == 'native' else None)
    effective = _base(config_data, base_url)
    downloads = []
    selections = {key: value if isinstance(value, list) else [] for key, value in settings.items()}
    for item, _ in _selected(config_data, selections):
        if item.get('itemType') != 'ruleset':
            continue
        parts = _ruleset_parts(item.get('behavior', 'domain'))
        if native_paths is not None:
            parts = [part for part in parts if relative_resource(_rule_path(item, part)) in native_paths]
        if not parts:
            continue
        url = item.get('url', '')
        if not url:
            raise OxidnsSettingsError('已选择的规则集缺少下载来源')
        if url.startswith('/'):
            url = effective + url
        for part in parts:
            downloads.append({'name': item.get('name') or '规则集',
                'url': _build_rule_proxy_url(config_data, effective, url, part=part),
                'local_path': _rule_path(item, part)})
    adguard = settings['adguard'] if isinstance(settings['adguard'], dict) else {}
    if adguard.get('enabled') and isinstance(adguard.get('urls'), list):
        for url in adguard['urls']:
            if native_paths is not None and relative_resource(f'./rules/{_tag("adguard", url)}.txt') not in native_paths:
                continue
            downloads.append({'name': 'AdGuard DNS 规则', 'url': url,
                              'local_path': f'./rules/{_tag("adguard", url)}.txt'})
    return downloads


def get_oxidns_custom_files(config_data):
    settings = _settings(config_data)
    native_paths = ({relative_resource(path) for path in resource_references(native_document(settings))}
                    if settings['config_mode'] == 'native' else None)
    files = []
    if (native_paths is None or 'rules/custom_hosts.txt' in native_paths) and isinstance(settings['custom_hosts'], str) and settings['custom_hosts'].strip():
        files.append({'path': './rules/custom_hosts.txt',
                      'content': _hosts_file_content(settings['custom_hosts'])})
    adguard = settings['adguard'] if isinstance(settings['adguard'], dict) else {}
    if (native_paths is None or 'rules/adguard-custom.txt' in native_paths) and adguard.get('enabled') and isinstance(adguard.get('content'), str) and adguard['content'].strip():
        files.append({'path': './rules/adguard-custom.txt', 'content': adguard['content']})
    files.extend(managed_files(settings['additional_files']))
    return files


def _hosts_file_content(text):
    """Keep native OxiDNS hosts rules intact; convert unambiguous IP-first rows.

    v1.6.1 files use ``matcher ip1 ip2 ...`` and strip everything from the
    first ``#`` on each line. Do not truncate addresses or reinterpret IPv6 as
    a domain. Legacy hosts aliases become separate full-match rules.
    """
    def is_ip(value):
        try:
            # Python accepts scoped IPv6, but Rust's IpAddr used by OxiDNS does not.
            return '%' not in value and bool(ip_address(value))
        except ValueError:
            return False

    def is_legacy_name(value):
        if value.startswith(('full:', 'domain:', 'keyword:', 'regexp:')):
            return bool(value.split(':', 1)[1])
        if re.fullmatch(r'[\d.]+', value):
            return False  # Do not turn a mistyped IPv4 address into an alias.
        return bool(re.fullmatch(r'[A-Za-z0-9_](?:[A-Za-z0-9_.-]*[A-Za-z0-9_])?\.?', value))

    output = []
    for number, raw in enumerate(text.splitlines(keepends=True), 1):
        line = raw.rstrip('\r\n')
        ending = raw[len(line):]
        rule, marker, comment = line.partition('#')
        fields = rule.split()
        if not fields:
            output.append(raw)
            continue
        if len(fields) >= 2 and all(is_ip(value) for value in fields[1:]):
            # Keep spelling, whitespace, address order, duplicates and comments.
            output.append(raw)
            continue
        if (len(fields) >= 2 and is_ip(fields[0])
                and all(not is_ip(value) and is_legacy_name(value) for value in fields[1:])):
            converted = [f'{name} {fields[0]}' for name in fields[1:]]
            if marker:
                converted[0] += ' #' + comment
            output.append((ending or '\n').join(converted) + ending)
            continue
        raise OxidnsSettingsError(
            f'自定义 Hosts 第 {number} 行格式无效；请使用“域名规则 IPv4/IPv6 [更多 IP]”'
            '，或“IP 域名 [别名]”，不能混合地址与别名')
    return ''.join(output)


def _routing_groups(config, settings):
    groups = {'domain': [], 'ip': []}
    for item, route in _selected(config, settings):
        if item.get('itemType') == 'ruleset':
            entries = [(part, {'files': [_rule_path(item, part)]})
                       for part in _ruleset_parts(item.get('behavior', 'domain'))]
        else:
            kind, value = item.get('rule_type', ''), item.get('value', '')
            part = 'ip' if kind in ('IP-CIDR', 'IP-CIDR6') else 'domain'
            if kind not in ('DOMAIN', 'DOMAIN-SUFFIX', 'DOMAIN-KEYWORD', 'DOMAIN-REGEX', 'IP-CIDR', 'IP-CIDR6'):
                raise OxidnsSettingsError(f'OxiDNS 不支持所选规则类型：{kind}')
            converted = convert_rule_content_for_mosdns(f'{kind},{value}', part).strip()
            if not converted or converted.startswith('#'):
                raise OxidnsSettingsError('所选单条规则无法转换为 DNS 规则')
            entries = [(part, {'ips' if part == 'ip' else 'exps': [converted]})]
        for part, args in entries:
            target = groups[part]
            if settings['aggregate_rules'] and target and target[-1]['route'] == route:
                group = target[-1]
                for key, values in args.items():
                    group['args'].setdefault(key, []).extend(values)
            else:
                target.append({'route': route, 'args': args, 'tag': f'rules_{part}_{len(target) + 1}'})
    return groups


def _forward(text, defaults):
    if not text.strip():
        return [{'addr': value} for value in defaults]
    if text.lstrip().startswith(('-', '[', '{', 'addr:')):
        try:
            parsed = yaml.safe_load(text)
        except yaml.YAMLError:
            raise OxidnsSettingsError('DNS 上游 YAML 语法无效') from None
        values = parsed if isinstance(parsed, list) else [parsed]
        if not values or any(not isinstance(item, (str, dict)) or (isinstance(item, dict) and not item.get('addr')) for item in values):
            raise OxidnsSettingsError('DNS 上游 YAML 每项需要 addr 地址')
    result = parse_dns_upstreams(text)
    if not result or any(not isinstance(item.get('addr'), str) or not item['addr'].strip() for item in result):
        raise OxidnsSettingsError('DNS 上游配置无效')
    return result


def _fallback_upstreams(text):
    """An empty backup is disabled, never an implicit copy of local DNS."""
    if not text.strip():
        return []
    if text.lstrip().startswith(('-', '[', '{', 'addr:')) or text.strip() in ('null', '~'):
        try:
            value = yaml.safe_load(text)
        except yaml.YAMLError:
            raise OxidnsSettingsError('DNS 上游 YAML 语法无效') from None
        if value is None or value == []:
            return []
    return _forward(text, [])


def generate_oxidns_config(config_data, base_url=''):
    s = _settings(config_data)
    if s['config_mode'] == 'native':
        return yaml.safe_dump(native_document(s), allow_unicode=True, sort_keys=False)
    try:
        custom = yaml.safe_load(s['custom_config']) if s['custom_config'].strip() else {}
    except yaml.YAMLError:
        raise OxidnsSettingsError('OxiDNS 高级 YAML 语法无效') from None
    if not isinstance(custom, dict):
        raise OxidnsSettingsError('OxiDNS 高级 YAML 顶层必须为对象')
    if custom.get('include'):
        raise OxidnsSettingsError('完整部署模板不支持外部 include；请将插件内联到高级 YAML')
    extra_plugins = custom.pop('plugins', [])
    if not isinstance(extra_plugins, list):
        raise OxidnsSettingsError('高级 YAML 的 plugins 必须为数组')
    result = deepcopy(custom)
    if result.get('runtime') is not None and not isinstance(result['runtime'], dict):
        raise OxidnsSettingsError('高级 YAML runtime 必须为对象')
    result['runtime'] = {**(result.get('runtime') or {}), 'worker_threads': s['worker_threads']}
    result.pop('api', None)
    if s['api']['enabled']:
        api = s['api']
        http = {'listen': api['listen']}
        if api['username']:
            http['auth'] = {'type': 'basic', 'username': api['username'], 'password': api['password']}
        if api['webui_root']:
            http['webui'] = {'root': api['webui_root'], 'index': 'index.html'}
        if api['ssl']:
            http['ssl'] = deepcopy(api['ssl'])
        result['api'] = {'http': http}
    result['log'] = {'level': s['log']['level']}
    if s['log']['file']:
        result['log'].update(file=s['log']['file'], rotation={
            'type': s['log']['rotation'], 'max_files': s['log']['max_files']})
    plugins = []

    def add(tag, kind, args):
        plugins.append({'tag': tag, 'type': kind, 'args': args})

    groups = _routing_groups(config_data, s)
    for part, entries in groups.items():
        for group in entries:
            add(group['tag'], 'domain_set' if part == 'domain' else 'ip_set', group['args'])
    if s['custom_hosts'].strip():
        _hosts_file_content(s['custom_hosts'])
        add('custom_hosts', 'hosts', {'files': ['./rules/custom_hosts.txt']})
    if s['cache_enabled']:
        cache = {'size': s['cache_size']}
        # OxiDNS rejects an explicit zero; omitting the field disables lazy cache.
        if s['cache_lazy_ttl'] > 0:
            cache['lazy_cache_ttl'] = s['cache_lazy_ttl']
        cache['short_circuit'] = True
        if s['cache_dump_enabled']:
            cache.update(dump_file=s['cache_dump_file'], dump_interval=s['cache_dump_interval'])
        add('lazy_cache', 'cache', cache)
    if s['probe_cache_enabled']:
        # This cache ends with the child sequence. It must capture the domestic
        # probe, never the final proxy/Fake-IP answer from its parent sequence.
        add('local_probe_cache', 'cache', {'size': s['probe_cache_size'], 'short_circuit': False})
    fallback_upstreams = _fallback_upstreams(s['fallback_dns'])
    for name, defaults in (('local', ['udp://223.5.5.5', 'udp://119.29.29.29']),
                           ('remote', ['https://1.1.1.1/dns-query', 'https://8.8.8.8/dns-query'])):
        add(f'forward_{name}', 'forward', {'concurrent': s[f'{name}_concurrent'],
            'upstreams': _forward(s[f'{name}_dns'], defaults)})
    if fallback_upstreams:
        add('forward_fallback', 'forward', {'concurrent': s['fallback_concurrent'], 'upstreams': fallback_upstreams})
    dynamic = s['dynamic_domain_set']
    if dynamic['enabled']:
        add('dynamic_domains', 'dynamic_domain_set', deepcopy(dynamic['args']))
        if dynamic['learn']:
            add('learn_classified_domain', 'learn_domain', {**deepcopy(dynamic['learn_args']), 'provider': 'dynamic_domains'})
    for name in ('query_recorder', 'ip_selector', 'ros_address_list', 'ros_route'):
        if s[name]['enabled']:
            args = deepcopy(s[name]['args'])
            # Empty optional gateway fields are absent in native configuration.
            if name == 'ros_route':
                args = {key: value for key, value in args.items() if value != ''}
            add(name, name, args)
    adguard = s['adguard']
    if adguard['enabled']:
        paths = [f'./rules/{_tag("adguard", url)}.txt' for url in adguard['urls']]
        if adguard['content'].strip():
            paths.insert(0, './rules/adguard-custom.txt')
        add('adguard_rules', 'adguard_rule', {'files': paths})
        add('adguard_block', 'black_hole', {'mode': adguard['mode']})
        add('adguard_reject', 'sequence', [{'exec': '$adguard_block'}, {'exec': 'accept'}])

    real_wrappers = [{'exec': f'${name}'} for name in ('ip_selector', 'ros_address_list', 'ros_route') if s[name]['enabled']]
    direct = deepcopy(real_wrappers)
    if s['cache_enabled']:
        direct.append({'exec': '$lazy_cache'})
    direct.extend([{'exec': '$forward_local'}, {'matches': ['!has_resp'], 'exec': 'reject 3'}])
    add('china_dns', 'sequence', direct)
    probe = [{'exec': '$local_probe_cache'}] if s['probe_cache_enabled'] else []
    probe.extend([{'matches': ['!has_resp'], 'exec': '$forward_local'}, {'exec': 'return'}])
    add('local_probe', 'sequence', probe)
    remote = [{'exec': '$forward_remote'}, {'matches': ['!has_resp'], 'exec': 'reject 3'}]
    if s['remote_ttl']:
        remote.append({'exec': f'ttl {s["remote_ttl"]}'})
    add('remote_dns', 'sequence', remote)
    if fallback_upstreams:
        add('fallback_dns', 'sequence', [{'exec': '$forward_fallback'}, {'matches': ['!has_resp'], 'exec': 'reject 3'}])
        add('fallback', 'fallback', {'primary': 'remote_dns', 'secondary': 'fallback_dns',
            'threshold': s['fallback_threshold'], 'always_standby': s['always_standby']})
    add('proxy_dns_seq', 'sequence', [{'exec': '$fallback'}, {'exec': 'accept'}] if fallback_upstreams
        else [{'exec': 'goto remote_dns'}])
    requery = [{'exec': 'drop_resp'}]
    direct_ip = deepcopy(real_wrappers)
    add('default_direct_ip', 'sequence', deepcopy(real_wrappers) + [{'exec': 'accept'}])
    if dynamic['enabled'] and dynamic['learn']:
        if dynamic['route'] == 'direct':
            direct_ip.append({'exec': '$learn_classified_domain'})
        else:
            requery.append({'exec': '$learn_classified_domain'})
    direct_ip.append({'exec': 'accept'})
    requery.append({'exec': 'goto proxy_dns_seq'})
    add('direct_ip', 'sequence', direct_ip)
    add('ip_requery_proxy', 'sequence', requery)

    main = []
    if s['metrics_enabled']:
        main.append({'exec': 'metrics_collector metrics'})
    if s['query_recorder']['enabled']:
        main.append({'exec': '$query_recorder'})
    if s['prefer_ipv4']:
        main.append({'exec': 'prefer_ipv4'})
    if s['block_qtypes']:
        main.append({'matches': ['qtype ' + ' '.join(str(t) for t in s['block_qtypes'])], 'exec': 'reject 0'})
    if s['custom_hosts'].strip():
        main.extend([{'exec': '$custom_hosts'}, {'matches': ['has_resp'], 'exec': 'accept'}])
    if adguard['enabled']:
        main.append({'matches': ['question $adguard_rules'], 'exec': 'goto adguard_reject'})
    matches = _parse_custom_matches(s['custom_matches'])
    if s['custom_match_position'] == 'head':
        main.extend(matches)
    for group in groups['domain']:
        main.append({'matches': [f'qname ${group["tag"]}'], 'exec': 'goto ' + ('china_dns' if group['route'] == 'direct' else 'proxy_dns_seq')})
    if s['custom_match_position'] == 'tail':
        main.extend(matches)
    # Explicit rules override previously learned classifications.
    if dynamic['enabled']:
        main.append({'matches': ['qname $dynamic_domains'], 'exec': 'goto ' + ('china_dns' if dynamic['route'] == 'direct' else 'proxy_dns_seq')})
    if groups['ip']:
        main.append({'exec': 'jump local_probe'})
        for group in groups['ip']:
            main.append({'matches': [f'resp_ip ${group["tag"]}'], 'exec': 'goto ' + ('direct_ip' if group['route'] == 'direct' else 'ip_requery_proxy')})
        if s['default_forward'] == 'forward_local':
            main.append({'matches': ['has_resp'], 'exec': 'goto default_direct_ip'})
        else:
            main.append({'exec': 'drop_resp'})
    main.append({'exec': 'goto china_dns' if s['default_forward'] == 'forward_local' else 'goto proxy_dns_seq'})
    add('sequence_main', 'sequence', main)
    add('server_udp', 'udp_server', {'listen': s['listen'], 'entry': 'sequence_main'})
    if s['tcp_enabled']:
        add('server_tcp', 'tcp_server', {'listen': s['listen'], 'entry': 'sequence_main'})

    if s['rule_update']['enabled']:
        downloads = get_oxidns_ruleset_downloads(config_data, base_url)
        if not downloads:
            raise OxidnsSettingsError('定时更新需要至少一个规则订阅')
        update = s['rule_update']['args']
        args = {key: update[key] for key in ('timeout', 'outbound', 'socks5') if update.get(key)}
        args['downloads'] = [{'url': item['url'], 'dir': str(PurePosixPath(item['local_path']).parent),
                              'filename': PurePosixPath(item['local_path']).name} for item in downloads]
        add('rules_download', 'download', args)
        providers = [f'${group["tag"]}' for entries in groups.values() for group in entries]
        if adguard['enabled']:
            providers.append('$adguard_rules')
        add('rules_reload', 'reload_provider', providers)
        # Native downloads are atomic per file, not across the whole ruleset.
        # A failed item retains its old file; provider reload then reads the
        # successful updates plus retained files. Keep this feature opt-in.
        add('rules_refresh', 'sequence', [{'exec': '$rules_download'}, {'exec': '$rules_reload'}])
        add('rules_cron', 'cron', {'timezone': update['timezone'], 'jobs': [{
            'name': 'refresh_rules', 'schedule': update['schedule'], 'executors': ['$rules_refresh']}]})
    plugins.extend(deepcopy(extra_plugins))
    tags = set()
    for plugin in plugins:
        if not isinstance(plugin, dict):
            raise OxidnsSettingsError('插件必须为对象')
        tag = plugin.get('tag', '')
        if not isinstance(tag, str) or len(tag) > 64 or not _TAG.fullmatch(tag) or tag.startswith(('qs.exec.', 'qs.match.', 'qs.cron.')):
            raise OxidnsSettingsError('OxiDNS 插件 tag 必须为有效的 1–64 位 ASCII 标识')
        if tag in tags:
            raise OxidnsSettingsError(f'OxiDNS 插件 tag 重复：{tag}')
        tags.add(tag)
    result['plugins'] = plugins
    if s.get('device_policies'):
        from backend.common.oxidns_device_policy import patch_document
        result = patch_document(result, [], s['device_policies'])
    return yaml.safe_dump(result, allow_unicode=True, sort_keys=False)
