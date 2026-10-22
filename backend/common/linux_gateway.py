"""Opt-in, profile-scoped IPv4 selective gateway generation.

Selections refer to composition IDs, never mutable rule-library names. Providers
always include the fake-IP network, so an empty real-IP selection cannot turn
Mihomo's route-address-set into unrestricted capture.
"""
from copy import deepcopy
import hashlib
import ipaddress
import re
from urllib.parse import quote, urlsplit

import yaml

from backend.common.profile_context import append_url_query, profile_api_path

PROVIDER_PREFIX = 'cf_gateway_'
FAKE_PROVIDER = PROVIDER_PREFIX + 'fake_ip'
REFRESH_INTERVAL = 300
DEFAULT_FAKE_RANGE = '198.18.0.1/16'
MAX_CONTENT_BYTES = 16 * 1024 * 1024
MAX_RULES = 250000
TUN_FILTER_KEYS = (
    'include-interface', 'exclude-interface', 'include-uid', 'include-uid-range',
    'exclude-uid', 'exclude-uid-range', 'include-mac-address', 'exclude-mac-address',
    'include-android-user', 'include-package', 'exclude-package',
)


class LinuxGatewayError(ValueError):
    """A gateway setting cannot safely be applied."""


def normalize_gateway_settings(value):
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise LinuxGatewayError('Linux 网关设置必须是对象')
    enabled = value.get('enabled', False)
    if type(enabled) is not bool:
        raise LinuxGatewayError('enabled 必须是布尔值')
    ids = value.get('rule_set_ids', [])
    if (not isinstance(ids, list) or len(ids) > 1024
            or any(not isinstance(item, str) or not item or len(item) > 256
                   or any(ord(c) < 32 or ord(c) == 127 for c in item) for item in ids)):
        raise LinuxGatewayError('请选择有效的 IP 规则集，最多 1024 个')
    return {'enabled': enabled, 'rule_set_ids': list(dict.fromkeys(ids))}


def gateway_settings(config):
    return normalize_gateway_settings(config.get('mihomo', {}).get('linux_gateway'))


def _base_config(config):
    source = config.get('mihomo', {}).get('custom_config', '')
    if not source or not source.strip():
        return {}
    try:
        document = yaml.safe_load(source)
    except (yaml.YAMLError, RecursionError) as exc:
        raise LinuxGatewayError('Mihomo 基础 YAML 无法解析，请先修正基础配置') from exc
    if not isinstance(document, dict):
        raise LinuxGatewayError('Mihomo 基础配置必须是 YAML 对象')
    return document


def fake_ip_network(document):
    dns = document.get('dns') or {}
    if not isinstance(dns, dict):
        raise LinuxGatewayError('Mihomo dns 必须是对象')
    value = dns.get('fake-ip-range', DEFAULT_FAKE_RANGE)
    try:
        network = ipaddress.ip_network(value, strict=False)
        if (network.version != 4 or not 8 <= network.prefixlen <= 30
                or network.is_loopback or network.is_multicast or network.is_link_local
                or network.network_address.is_unspecified):
            raise ValueError
    except (ValueError, TypeError) as exc:
        raise LinuxGatewayError('fake-ip-range 必须是有效 IPv4 网段（/8 至 /30），不能是回环、多播或链路本地地址') from exc
    return str(network)


def _rule(config, rule_id):
    return next((item for item in config.get('rule_configs', [])
                 if item.get('id') == rule_id and item.get('itemType') == 'ruleset'), None)


def _library(config, item):
    library_id = item.get('library_rule_id')
    return next((entry for entry in config.get('rule_library', [])
                 if entry.get('id') == library_id), None) if library_id else None


def _reason(config, item):
    if item is None:
        return '规则集已删除或不属于当前配置空间'
    library = _library(config, item)
    if item.get('library_rule_id') and library is None:
        return '共享规则库条目已删除'
    source = library or item
    behavior = source.get('behavior', item.get('behavior', 'domain'))
    if behavior not in ('ipcidr', 'classical'):
        return '仅支持 IP-CIDR 或 Classical 规则集'
    if (source.get('format') == 'mrs'
            or urlsplit(source.get('url', '')).path.lower().endswith('.mrs')):
        return '网关提取暂不支持 MRS，请使用 text / YAML 格式的 IP 规则集'
    if not item.get('enabled', True):
        return '当前配置空间已停用此规则集，接管范围将清空'
    if not item.get('library_enabled', True) or not source.get('enabled', True):
        return '共享规则库已停用此规则集，接管范围将清空'
    return ''


def _lines(content):
    if not isinstance(content, str) or len(content.encode('utf-8')) > MAX_CONTENT_BYTES:
        raise LinuxGatewayError('IP 规则内容必须为 UTF-8 文本且不超过 16 MiB')
    if '\x00' in content or '\ufffd' in content:
        raise LinuxGatewayError('IP 规则不是有效文本，暂不支持二进制规则')
    stripped = content.lstrip('\ufeff\r\n \t')
    if re.search(r'^\s*payload\s*:', stripped, re.M) or stripped.startswith(('[', '- ', '---')):
        try:
            document = yaml.safe_load(stripped)
        except (yaml.YAMLError, RecursionError) as exc:
            raise LinuxGatewayError('IP 规则集 YAML 语法错误') from exc
        values = document.get('payload') if isinstance(document, dict) else document
        if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
            raise LinuxGatewayError('IP 规则集 YAML 必须包含字符串数组 payload')
    else:
        values = stripped.splitlines()
    if len(values) > MAX_RULES:
        raise LinuxGatewayError('IP 规则数量超过 250000 条限制')
    return values


def extract_gateway_cidrs(content, behavior):
    """Extract only unconditional destination IPv4 CIDRs, never SRC/logic rules."""
    if behavior not in ('ipcidr', 'classical'):
        raise LinuxGatewayError('网关仅支持 IP-CIDR / Classical 规则集')
    networks = set()
    for line in _lines(content):
        line = line.split('#', 1)[0].strip()
        if not line or line.startswith(('//', ';')):
            continue
        parts = [part.strip() for part in line.split(',')]
        if behavior == 'classical':
            if parts[0].upper() not in ('IP-CIDR', 'IP-CIDR6'):
                continue
            if len(parts) not in (2, 3) or (len(parts) == 3 and parts[2].lower() != 'no-resolve'):
                raise LinuxGatewayError('Classical 目的 IP 规则只能包含 IP-CIDR,网段[,no-resolve]')
            value = parts[1]
        else:
            if len(parts) != 1:
                raise LinuxGatewayError('IP-CIDR 规则集中只能填写 IP / CIDR，不能包含其他匹配条件')
            value = parts[0]
        try:
            network = ipaddress.ip_network(value, strict=False)
        except ValueError as exc:
            raise LinuxGatewayError('IP 规则包含无效 IP / CIDR') from exc
        if behavior == 'classical' and ((parts[0].upper() == 'IP-CIDR6') != (network.version == 6)):
            raise LinuxGatewayError('IP-CIDR / IP-CIDR6 规则类型必须与目标 IP 版本一致')
        if network.version == 6:
            continue
        if network.prefixlen < 8:
            raise LinuxGatewayError('Linux 网关只接受 /8 至 /32 的 IPv4 网段，请拆分或移除 /0、/1 等超大接管范围')
        networks.add(network)
    collapsed = list(ipaddress.collapse_addresses(networks))
    if any(network.prefixlen == 0 for network in collapsed):
        raise LinuxGatewayError('所选 IP 规则覆盖全部 IPv4 地址，无法用于 Linux 网关')
    return [str(network) for network in collapsed]


def _selected_source(config, rule_id, content_loader=None):
    settings = gateway_settings(config)
    item = _rule(config, rule_id)
    if not settings['enabled'] or rule_id not in settings['rule_set_ids'] or _reason(config, item):
        return None, None
    library = _library(config, item)
    source = library or item
    if source.get('source_type') == 'content':
        content = source.get('content', '')
    elif content_loader is not None:
        content = content_loader(item, library)
    else:
        from backend.routes.rules import get_ruleset_content
        content = get_ruleset_content(item, library, config_data=config)
        if not content:
            from pathlib import Path
            from backend.utils.rule_utils import rule_cache_read_path
            cached = Path(rule_cache_read_path(source, config.get('rule_library', [])))
            if not cached.is_file():
                # An explicit empty cached source is legitimate; a failed
                # download retains the previous provider rather than clearing it.
                raise LinuxGatewayError('无法读取 IP 规则集，请先在规则库刷新并检查下载状态')
    return source.get('behavior', item.get('behavior', 'domain')), content


def gateway_provider_payload(config, rule_id, *, content_loader=None):
    behavior, content = _selected_source(config, rule_id, content_loader)
    return [] if behavior is None else extract_gateway_cidrs(content, behavior)


def gateway_provider_yaml(config, rule_id, *, content_loader=None):
    return yaml.safe_dump({'payload': gateway_provider_payload(config, rule_id, content_loader=content_loader)},
                          allow_unicode=True, sort_keys=False)


def gateway_source_yaml(config, rule_id, *, content_loader=None):
    """Refresh the selected rule's original routing semantics by stable ID too."""
    behavior, content = _selected_source(config, rule_id, content_loader)
    payload = []
    if behavior is not None:
        # Validate capture before accepting a new original rule source.
        extract_gateway_cidrs(content, behavior)
        for line in _lines(content):
            line = line.strip()
            if not line or line.startswith(('#', '//', ';')):
                continue
            if behavior == 'ipcidr' or line.split(',', 1)[0].upper() in ('IP-CIDR', 'IP-CIDR6'):
                line = line.split('#', 1)[0].strip()
                # Mihomo's IP strategy parses prefixes, not bare addresses.
                # Keep the normal routing provider in sync with capture's
                # normalization so single-IP rules cannot fall through.
                if behavior == 'ipcidr':
                    line = str(ipaddress.ip_network(line, strict=False))
                else:
                    parts = [part.strip() for part in line.split(',')]
                    parts[0] = parts[0].upper()
                    parts[1] = str(ipaddress.ip_network(parts[1], strict=False))
                    line = ','.join(parts)
            if line:
                payload.append(line)
    return yaml.safe_dump({'payload': payload}, allow_unicode=True, sort_keys=False)


def provider_name(rule_id):
    return PROVIDER_PREFIX + hashlib.sha256(rule_id.encode('utf-8')).hexdigest()[:20]


def provider_path(config, rule_id, *, source=False):
    suffix = '/linux-gateway/' + ('source/' if source else 'rules/') + quote(rule_id, safe='')
    if config.get('profile_id'):
        return profile_api_path(config, suffix)
    return '/api/custom-config' + suffix


def _routing_mark_path(value, path='Mihomo', seen=None):
    seen = seen if seen is not None else set()
    if not isinstance(value, (dict, list)) or id(value) in seen:
        return None
    seen.add(id(value))
    if isinstance(value, dict):
        if value.get('routing-mark') not in (None, 0, False, ''):
            return path + '.routing-mark'
        entries = value.items()
    else:
        entries = enumerate(value)
    for key, child in entries:
        found = _routing_mark_path(child, f'{path}.{key}', seen)
        if found:
            return found
    return None


def apply_linux_gateway(mihomo, config, base_url=''):
    """Patch only generated output. Disabled mode is an exact no-op."""
    settings = gateway_settings(config)
    if not settings['enabled']:
        return
    conflict = _routing_mark_path(mihomo) or _routing_mark_path(config.get('nodes', []), '节点')
    if conflict:
        raise LinuxGatewayError(f'Linux 网关与 {conflict} 冲突，请先移除 routing-mark')
    fake_range = fake_ip_network(mihomo)
    tun = deepcopy(mihomo.get('tun') or {})
    if not isinstance(tun, dict):
        raise LinuxGatewayError('Mihomo tun 必须是对象')
    for key in TUN_FILTER_KEYS:
        if tun.get(key):
            raise LinuxGatewayError(f'Linux 网关与现有 tun.{key} 过滤冲突，请先在 Mihomo 基础 YAML 中移除该过滤，避免客户端绕过接管')
    # CIDR routing and nftables provider routing intersect. Retaining the old
    # fake-IP-only route-address silently prevents real-IP rules from working.
    for key in ('route-address', 'inet4-route-address', 'inet6-route-address',
                'route-exclude-address-set', 'route-exclude-address',
                'inet4-route-exclude-address', 'inet6-route-exclude-address'):
        tun.pop(key, None)
    tun.update({'enable': True, 'auto-route': True, 'auto-redirect': True,
                'auto-detect-interface': True, 'strict-route': False,
                'dns-hijack': []})
    tun.setdefault('stack', 'mixed')
    tun.setdefault('device', 'Mihomo')
    providers = mihomo.setdefault('rule-providers', {})
    if not isinstance(providers, dict):
        raise LinuxGatewayError('Mihomo rule-providers 必须是对象')
    if any(name.startswith(PROVIDER_PREFIX) for name in providers):
        raise LinuxGatewayError('cf_gateway_ 是网关内部规则集前缀，请修改同名前缀的自定义规则集')
    providers[FAKE_PROVIDER] = {'type': 'inline', 'behavior': 'ipcidr',
                                'format': 'yaml', 'payload': [fake_range]}
    names = [FAKE_PROVIDER]
    effective_base = (config.get('system_config', {}).get('server_domain') or base_url).rstrip('/')
    token = config.get('system_config', {}).get('config_token', '')
    from backend.common.auth import is_auth_enabled
    if settings['rule_set_ids'] and is_auth_enabled() and not token:
        raise LinuxGatewayError('已开启登录验证，请先在系统设置中配置订阅令牌（config_token），供 Mihomo 自动刷新网关规则')
    for rule_id in settings['rule_set_ids']:
        name = provider_name(rule_id)
        url = effective_base + provider_path(config, rule_id)
        if token:
            url = append_url_query(url, {'token': token})
        providers[name] = {'type': 'http', 'behavior': 'ipcidr', 'format': 'yaml',
                           'url': url, 'path': f'./ruleset/{name}.yaml', 'interval': REFRESH_INTERVAL}
        names.append(name)
        item = _rule(config, rule_id)
        if item is not None and not _reason(config, item):
            original_name = (_library(config, item) or item).get('name', item.get('name'))
            original = providers.get(original_name)
            if original is not None:
                source_url = effective_base + provider_path(config, rule_id, source=True)
                if token:
                    source_url = append_url_query(source_url, {'token': token})
                original.update(url=source_url, format='yaml', interval=REFRESH_INTERVAL)
    tun['route-address-set'] = names
    mihomo['tun'] = tun
    mihomo['ipv6'] = False
    mihomo['mode'] = 'rule'
    dns = deepcopy(mihomo.get('dns') or {})
    dns.update({'enable': True, 'listen': '127.0.0.1:1053', 'enhanced-mode': 'fake-ip', 'ipv6': False})
    dns.setdefault('fake-ip-range', DEFAULT_FAKE_RANGE)
    mihomo['dns'] = dns
    mihomo['x-configflow-linux-gateway'] = {'enabled': True, 'version': 1, 'provider_prefix': PROVIDER_PREFIX,
                                                'profile_id': config.get('profile_id', '')}


def gateway_editor_state(config, *, include_guidance=True):
    settings = gateway_settings(config)
    warnings = [
        '仅支持 Linux + nftables 的 IPv4 网关；客户端需停用 IPv6 或另行配置 IPv6 路由。',
        'OxiDNS 直连上游请使用带 IP 地址的 DoH / DoT（例如 https://223.5.5.5/dns-query）；普通 UDP/TCP 53 可能被 auto-redirect 接管。',
        '这里只决定哪些 IP 进入 Mihomo；启用后使用 rule 模式，实际出口仍由规则顺序及策略组当前选择决定。',
        'IPv4 接管网段须为 /8 至 /32；/0、/1 等超大范围会被拒绝，避免意外接管全网。',
    ] if include_guidance else []
    try:
        fake_range = fake_ip_network(_base_config(config))
    except LinuxGatewayError as exc:
        fake_range = ''
        warnings.append(str(exc))
    candidates = []
    selected = set(settings['rule_set_ids'])
    seen = set()
    for item in config.get('rule_configs', []):
        if item.get('itemType') != 'ruleset':
            continue
        rule_id = item.get('id')
        source = _library(config, item) or item
        behavior = source.get('behavior', item.get('behavior', 'domain'))
        if behavior not in ('ipcidr', 'classical') and rule_id not in selected:
            continue
        seen.add(rule_id)
        reason = _reason(config, item)
        candidates.append({'id': rule_id, 'name': source.get('name', item.get('name', rule_id)),
            'behavior': behavior, 'policy': item.get('policy', ''),
            'enabled': bool(item.get('enabled', True) and item.get('library_enabled', True) and source.get('enabled', True)),
            'available': not reason, 'selected': rule_id in selected,
            'reason': reason, 'description': '只提取无条件目标 IP-CIDR；忽略域名、来源 IP、逻辑规则及 IPv6' if behavior == 'classical' else '只接管 IPv4 IP / CIDR'})
        if reason and rule_id in selected:
            warnings.append(f"{source.get('name', rule_id)}：{reason}")
    for rule_id in settings['rule_set_ids']:
        if rule_id not in seen:
            reason = '规则集已删除或不属于当前配置空间，接管范围将清空'
            candidates.append({'id': rule_id, 'name': rule_id, 'behavior': '', 'policy': '',
                               'enabled': False, 'available': False, 'selected': True, 'reason': reason})
            warnings.append(reason + f'（{rule_id}）')
    return {'settings': settings, 'candidates': candidates, 'fake_ip_range': fake_range,
            'warnings': warnings, 'refresh_interval': REFRESH_INTERVAL}


def validate_gateway_selection(config, value):
    settings = normalize_gateway_settings(value)
    if not settings['enabled']:
        return settings
    previous = set(gateway_settings(config)['rule_set_ids'])
    for rule_id in settings['rule_set_ids']:
        item = _rule(config, rule_id)
        if item is None:
            if rule_id not in previous:
                raise LinuxGatewayError('所选规则集不存在或不属于当前配置空间')
            continue
        source = _library(config, item) or item
        behavior = source.get('behavior', item.get('behavior', 'domain'))
        if behavior not in ('ipcidr', 'classical'):
            if rule_id in previous:
                continue
            raise LinuxGatewayError('只能选择 IP-CIDR / Classical 规则集')
        if source.get('source_type') == 'content' and not _reason(config, item):
            extract_gateway_cidrs(source.get('content', ''), behavior)
        if 'MRS' in _reason(config, item) and rule_id not in previous:
            raise LinuxGatewayError(_reason(config, item))
    if settings['enabled']:
        preview = deepcopy(config)
        preview.setdefault('mihomo', {})['linux_gateway'] = settings
        apply_linux_gateway(_base_config(preview), preview)
    return settings
