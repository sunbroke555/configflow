"""Compute what the upstream router should contain for the side-gateway setup.

Only IPv4 is handled in P0.  Proxy CIDRs are the mihomo fake-ip range plus the
public IP-CIDR entries of rule sets explicitly selected on the router (typically
Telegram, whose clients connect by IP without DNS).  Rule sets are not inferred
from policies: other IP rules usually have domain counterparts that already reach
mihomo through fake-ip, and the active choice of a select group is unknown.
"""
import ipaddress
from typing import Any, Dict, List, Optional

import yaml

DEFAULT_FAKE_IP_RANGE = '198.18.0.1/16'
BUILTIN_NON_PROXY = {'DIRECT', 'REJECT', 'REJECT-DROP', 'PASS', 'COMPATIBLE'}


def fake_ip_network(config: Dict[str, Any]) -> ipaddress.IPv4Network:
    """Return the fake-ip network from the mihomo base config (default 198.18.0.0/16)."""
    value = DEFAULT_FAKE_IP_RANGE
    custom = ((config.get('mihomo') or {}).get('custom_config') or '').strip()
    if custom:
        try:
            parsed = yaml.safe_load(custom) or {}
        except yaml.YAMLError:
            parsed = {}
        dns = parsed.get('dns') if isinstance(parsed, dict) else None
        if isinstance(dns, dict) and dns.get('fake-ip-range'):
            value = str(dns['fake-ip-range'])
    try:
        return ipaddress.IPv4Network(value, strict=False)
    except ValueError:
        return ipaddress.IPv4Network(DEFAULT_FAKE_IP_RANGE, strict=False)


def _direct_only_groups(config: Dict[str, Any]) -> set:
    """Strategy groups that can only resolve to DIRECT/REJECT behave like direct policies."""
    names = set()
    for group in config.get('proxy_groups') or []:
        if not isinstance(group, dict):
            continue
        manual = [str(node) for node in group.get('manual_nodes') or []]
        has_other_sources = any(group.get(key) for key in ('include_groups', 'subscriptions', 'aggregations', 'regex', 'follow_group'))
        if manual and not has_other_sources and all(node in BUILTIN_NON_PROXY for node in manual):
            names.add(group.get('name'))
    return names


def _public_ipv4_networks(lines: str) -> List[ipaddress.IPv4Network]:
    result = []
    for raw in lines.splitlines():
        value = raw.strip()
        if not value or value.startswith('#'):
            continue
        try:
            network = ipaddress.ip_network(value, strict=False)
        except ValueError:
            continue
        if isinstance(network, ipaddress.IPv4Network) and network.prefixlen > 0 and network.is_global:
            result.append(network)
    return result


def compute_desired(config: Dict[str, Any], proxy_gateway: Optional[str],
                    ip_rule_sets: Optional[List[str]] = None, content_loader=None) -> Dict[str, Any]:
    """Build the desired router state.

    ``content_loader(rule_item, library_rule)`` returns rule set text; the
    default only reads local caches/inline content (no network in P0).
    """
    if content_loader is None:
        from backend.routes.rules import get_ruleset_content

        def content_loader(item, library_rule):
            return get_ruleset_content(item, library_rule, allow_network=False, config_data=config)
    from backend.converters.mosdns import convert_rule_content_for_mosdns

    warnings: List[str] = []
    blocking_warnings: List[str] = []

    def incomplete(message: str) -> None:
        # Static routes keep their existing warning behavior. Policy bundles
        # must also know when selected input is missing, to avoid withdrawing it.
        warnings.append(message)
        blocking_warnings.append(message)
    sources: Dict[ipaddress.IPv4Network, List[str]] = {}
    fake_ip = fake_ip_network(config)
    sources[fake_ip] = ['fake-ip']

    direct_groups = _direct_only_groups(config)
    library = {rule.get('id'): rule for rule in config.get('rule_library') or [] if isinstance(rule, dict)}
    rulesets = {item.get('id'): item for item in config.get('rule_configs') or []
                if isinstance(item, dict) and item.get('itemType') == 'ruleset'}
    for rule_id in ip_rule_sets or []:
        item = rulesets.get(rule_id)
        if item is None:
            incomplete(f'所选规则集 {rule_id} 已不存在，已忽略')
            continue
        name = str(item.get('name') or item.get('id') or '规则集')
        if item.get('enabled') is False:
            incomplete(f'规则集「{name}」在策略规则中已停用，未计入')
            continue
        policy = str(item.get('policy') or '')
        if policy in BUILTIN_NON_PROXY or policy in direct_groups:
            warnings.append(f'规则集「{name}」的策略是 {policy}（不走代理），仍按勾选计入，请确认')
        library_rule = library.get(item.get('library_rule_id'))
        try:
            content = content_loader(item, library_rule) or ''
        except Exception:  # noqa: BLE001 - a broken rule source must not break the report
            content = ''
        if not content:
            incomplete(f'规则集「{name}」没有本地缓存，未计入其中的 IP 段')
            continue
        for network in _public_ipv4_networks(convert_rule_content_for_mosdns(content, 'ip')):
            sources.setdefault(network, []).append(name)

    collapsed = list(ipaddress.collapse_addresses(sources))
    proxy_cidrs = []
    for network in collapsed:
        names = sorted({name for original, labels in sources.items() if original.subnet_of(network) for name in labels})
        proxy_cidrs.append({'cidr': str(network), 'sources': names})
    proxy_cidrs.sort(key=lambda entry: ipaddress.IPv4Network(entry['cidr']))

    if not proxy_gateway:
        incomplete('未关联 mihomo Agent，无法确定代理路由的下一跳')
    return {
        'fake_ip_range': str(fake_ip),
        'proxy_gateway': proxy_gateway or '',
        'proxy_cidrs': proxy_cidrs,
        'warnings': warnings,
        'blocking_warnings': blocking_warnings,
    }
