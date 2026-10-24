"""Compile the two blocking views from one ordered, profile-owned rule list."""
from copy import deepcopy

from backend.common.oxidns_settings import OxidnsSettingsError
from backend.common.rule_dns import hydrate_ruleset, normalize_action
from backend.converters.mosdns import DNS_RULE_TYPES, convert_rule_content_for_mosdns


DNS_BLOCKING_ACTIONS = frozenset({'refused', 'nxdomain', 'nodata', 'blackhole'})
CONNECTION_BLOCKING_POLICIES = frozenset({'REJECT', 'REJECT-DROP'})


def _domain_part(item):
    if item.get('itemType') == 'ruleset':
        return item.get('behavior', 'domain') in ('domain', 'classical')
    kind = item.get('rule_type')
    if kind not in DNS_RULE_TYPES or kind in ('IP-CIDR', 'IP-CIDR6'):
        return False
    converted = convert_rule_content_for_mosdns(f"{kind},{item.get('value', '')}", 'domain').strip()
    return bool(converted) and not converted.startswith('#')


def compile_blocking_rules(config):
    """Return safe prefix records, independently classified for DNS and traffic.

    ``action`` is a canonical DNS action string. Parked DNS state never affects
    generation. Native YAML and unrelated upstream drafts are not normalized
    here: Mihomo must remain usable while those drafts are incomplete.
    After the first active MATCH, only excluded IDs are collected for preview.
    """
    from backend.converters.oxidns import _items

    settings = config.get('oxidns') or {}
    actions = settings.get('rule_actions') or {}
    actions = actions if isinstance(actions, dict) else {}
    library = {item['id']: item for item in config.get('rule_library') or []
               if isinstance(item, dict) and isinstance(item.get('id'), str)}
    result = {'oxidns': [], 'mihomo': [], 'cutoff': None, 'excluded': []}
    after_match = False
    for original in _items(config):
        item = deepcopy(original)
        reference = library.get(item.get('library_rule_id'))
        if item.get('itemType') == 'ruleset' and reference is not None:
            hydrate_ruleset(item, reference, config.get('profile_id'))
        if not item.get('enabled', True) or not item.get('library_enabled', True):
            continue
        if item.get('itemType') != 'ruleset' and item.get('rule_type') == 'MATCH':
            if not after_match:
                result['cutoff'] = item.get('id')
                after_match = True
            continue
        connection = item.get('policy') in CONNECTION_BLOCKING_POLICIES
        try:
            action = normalize_action(actions.get(item.get('id')))
        except (OxidnsSettingsError, TypeError, AttributeError):
            action = None
        dns = action is not None and action['action'] in DNS_BLOCKING_ACTIONS and _domain_part(item)
        if after_match:
            if connection or dns:
                result['excluded'].append(item.get('id'))
            continue
        if connection:
            result['mihomo'].append(item)
        if dns:
            result['oxidns'].append({'item': item, 'action': action['action']})
    return result
