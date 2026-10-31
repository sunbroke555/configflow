"""Indexed native routing recognition must match the original rescanning results."""
import random

import pytest

from backend.common.oxidns_common_routing import SELECTORS, _Mentions, _atom, _catalog, _references
from backend.common.oxidns_settings import OxidnsSettingsError
from backend.common.rule_dns import has_ip_part
from backend.converters.oxidns import _items, _routing_groups, ruleset_parts


def _rescanning_catalog(config):
    """The catalog as first written: every rule rescans all active rules.

    This baseline calls _routing_groups without items, so it relies on that
    items=None path staying the original scan of every active rule.
    """
    records, atoms = {}, {}
    for item in _items(config):
        identity = item.get('id')
        if not isinstance(identity, str) or identity in records:
            continue
        suffix = 'rulesets' if item.get('itemType') == 'ruleset' else 'rules'
        settings = {key: [] for key in SELECTORS}
        settings.update(aggregate_rules=False)
        settings['direct_' + suffix] = [identity]
        try:
            groups = _routing_groups(config, settings, legacy=True)
        except (OxidnsSettingsError, KeyError, TypeError, ValueError):
            continue
        parts = set()
        for part, entries in groups.items():
            for group in entries:
                for key, values in group['args'].items():
                    for value in values:
                        atom = _atom(part, key, value)
                        if atom is not None:
                            atoms.setdefault(atom, set()).add(identity)
                            parts.add(part)
        current = set(ruleset_parts(item, config)) if item.get('itemType') == 'ruleset' else (parts - {'ip'})
        content_aware = parts if has_ip_part(item, config) else parts - {'ip'}
        records[identity] = {'suffix': suffix, 'parts': parts,
                             'shapes': {frozenset(parts), frozenset(content_aware), frozenset(current)},
                             'item': item}
    return records, atoms


_IDS = [f'r{index}' for index in range(12)] + [None, 3, '']
_RULE_TYPES = ['DOMAIN', 'DOMAIN-SUFFIX', 'DOMAIN-KEYWORD', 'DOMAIN-REGEX', 'IP-CIDR', 'IP-CIDR6',
               'GEOIP', 'PROCESS-NAME', 'MATCH', None]
_VALUES = ['a.test', 'b.example', 'shared.test', 'ads', r'^x\.test$', '192.0.2.0/24', '2001:db8::/32',
           'CN', '', '# comment']
_CONTENTS = ['DOMAIN,a.test\n', 'DOMAIN,a.test\nIP-CIDR,192.0.2.0/24\n', 'IP-CIDR6,2001:db8::/32\n', '']


def _random_item(rng):
    item = {}
    if rng.random() < 0.95:
        item['id'] = rng.choice(_IDS)
    kind = rng.choice(['rule', 'rule', 'ruleset', 'ruleset', None, 'other'])
    if kind is not None:
        item['itemType'] = kind
    if kind == 'ruleset':
        behavior = rng.choice(['domain', 'ipcidr', 'classical', 'classical', None])
        if behavior is not None:
            item['behavior'] = behavior
        if behavior == 'classical' and rng.random() < 0.8:
            item.update(source_type='content', content=rng.choice(_CONTENTS))
        else:
            item['url'] = f'https://example.test/{rng.randrange(4)}.list'
    else:
        rule_type = rng.choice(_RULE_TYPES)
        if rule_type is not None:
            item['rule_type'] = rule_type
        item['value'] = rng.choice(_VALUES)
    for key in ('enabled', 'library_enabled'):
        if rng.random() < 0.15:
            item[key] = False
    return item


def _random_config(rng):
    items = [_random_item(rng) for _ in range(rng.randrange(0, 50))]
    if rng.random() < 0.15:
        # Profiles written before rule_configs keep separate rule lists.
        return {'rules': [item for item in items if item.get('itemType') != 'ruleset'],
                'rule_sets': [item for item in items if item.get('itemType') == 'ruleset']}
    return {'rule_configs': items}


@pytest.mark.parametrize('seed', range(300))
def test_catalog_matches_rescanning_catalog(seed):
    config = _random_config(random.Random(seed))
    assert _catalog(config) == _rescanning_catalog(config)


def test_catalog_keeps_duplicate_ids_of_each_kind():
    config = {'rule_configs': [
        {'id': 'dup', 'itemType': 'rule', 'rule_type': 'DOMAIN', 'value': 'a.test'},
        {'id': 'dup', 'itemType': 'ruleset', 'behavior': 'domain', 'url': 'https://example.test/x.list'},
        {'id': 'dup', 'itemType': 'rule', 'rule_type': 'IP-CIDR', 'value': '192.0.2.0/24'},
        {'id': 'bad', 'itemType': 'rule', 'rule_type': 'GEOIP', 'value': 'CN'},
        {'id': 'bad', 'itemType': 'ruleset', 'behavior': 'domain', 'url': 'https://example.test/y.list'},
    ]}
    records, atoms = _catalog(config)
    assert (records, atoms) == _rescanning_catalog(config)
    # Both same-kind items of one id stay in its record; another kind never joins it.
    assert records['dup']['suffix'] == 'rules' and records['dup']['parts'] == {'domain', 'ip'}
    assert records['bad']['suffix'] == 'rulesets'


_PIECES = ['rules_domain_1', 'rules_domain_10', 'rules_ip_2', 'cf_dns_up_0123456789ab', 'rules_reload',
           'rules_refresh', '$', '.', '-', '_', ' ', '/', ':', 'é', 'qname ', 'goto ', 'x', '1']
_TAGS = ['rules_domain_1', 'rules_domain_10', 'rules_ip_2', 'cf_dns_up_0123456789ab', 'rules_reload',
         'rules_refresh', 'domain_1', 'é', '.', 'a b', 'x$y', 'q/r', '$rules_domain_1', 'rules_domain_1 ', '']


def _random_value(rng, depth=0):
    choice = rng.random()
    if depth > 2 or choice < 0.5:
        if rng.random() < 0.1:
            return rng.choice([None, 1, True, 2.5])
        return ''.join(rng.choice(_PIECES) for _ in range(rng.randrange(0, 5)))
    if choice < 0.75:
        return [_random_value(rng, depth + 1) for _ in range(rng.randrange(0, 4))]
    # Keys are never references, only values.
    return {rng.choice(_TAGS): _random_value(rng, depth + 1) for _ in range(rng.randrange(0, 4))}


@pytest.mark.parametrize('seed', range(300))
def test_mentions_match_references(seed):
    rng = random.Random(seed)
    values = [_random_value(rng) for _ in range(rng.randrange(0, 8))]
    mentions = _Mentions(values)
    for tag in _TAGS:
        assert mentions.positions(tag) == tuple(position for position, value in enumerate(values)
                                                if _references(value, tag)), tag
