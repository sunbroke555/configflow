"""OxiDNS only probes the local upstream when an IP group can change an answer."""
import os

import pytest
import yaml

from backend.common import rule_ip_presence
from backend.common.oxidns_common_routing import apply_routing, read_routing
from backend.common.oxidns_editor import prepare_editor
from backend.common.rule_dns import has_ip_part, probe_needed
from backend.converters.oxidns import generate_oxidns_config, get_oxidns_ruleset_downloads
from backend.utils.rule_utils import rule_cache_filename

LIST_URL = 'https://rules.test/TikTok.list'


@pytest.fixture
def rules_dir(tmp_path, monkeypatch):
    monkeypatch.setattr('backend.utils.rule_utils.get_rules_dir', lambda: str(tmp_path))
    rule_ip_presence._MEMO.clear()
    return tmp_path


def cache(rules_dir, entry, text):
    path = rules_dir / rule_cache_filename(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    return path


def library_config(text=None, rules_dir=None, **settings):
    """A profile that references one classical library list, optionally cached."""
    entry = {'id': 'lib-tiktok', 'name': 'TikTok', 'source_type': 'url', 'url': LIST_URL,
             'behavior': 'classical', 'format': 'text'}
    config = {'system_config': {'server_domain': 'http://config.test', 'rule_proxy_token': 'test-only'},
              'rule_library': [entry],
              'rule_configs': [{'id': 'tiktok', 'itemType': 'ruleset', 'library_rule_id': 'lib-tiktok', 'name': 'TikTok',
                                'behavior': 'classical', 'url': LIST_URL, 'policy': 'Proxy', 'enabled': True}],
              'oxidns': {'proxy_rulesets': ['tiktok'], **settings}}
    if text is not None:
        cache(rules_dir, entry, text)
    return config


def main_of(config):
    document = yaml.safe_load(generate_oxidns_config(config))
    return next(p for p in document['plugins'] if p['tag'] == 'sequence_main')['args'], document


@pytest.mark.parametrize('text, expected', [
    ('DOMAIN-SUFFIX,tiktok.com\nDOMAIN-KEYWORD,musical\n', False),
    ('# only comments\n\nDOMAIN-SUFFIX,ip.com\nip.com\n', False),
    ('DOMAIN-SUFFIX,tiktok.com\nIP-CIDR,203.0.113.0/24,no-resolve\n', True),
    ('IP-CIDR6,2001:db8::/32\n', True),
    ('IP-ASN,13335\nGEOIP,CN\n', False),
])
def test_classical_content_decides_the_ip_part(rules_dir, text, expected):
    config = library_config(text, rules_dir)
    item = config['rule_configs'][0]
    assert rule_ip_presence.classical_ip_lines(item, config) is expected
    assert has_ip_part(item, config) is expected
    # Without a config the old behaviour check still applies.
    assert has_ip_part(item) is True


def test_unknown_content_keeps_the_ip_part(rules_dir):
    config = library_config()
    item = config['rule_configs'][0]
    assert rule_ip_presence.classical_ip_lines(item, config) is None
    assert has_ip_part(item, config) is True
    mrs = {**config, 'rule_library': [{**config['rule_library'][0], 'format': 'mrs'}]}
    cache(rules_dir, mrs['rule_library'][0], 'DOMAIN,a.test\n')
    assert rule_ip_presence.classical_ip_lines(item, mrs) is None


def test_inline_library_content_is_read_from_the_library_entry():
    config = {'rule_library': [{'id': 'lib-x', 'source_type': 'content', 'behavior': 'classical',
                                'content': 'DOMAIN,corp.lan\n'}]}
    item = {'id': 'x', 'itemType': 'ruleset', 'library_rule_id': 'lib-x', 'behavior': 'classical'}
    assert has_ip_part(item, config) is False
    config['rule_library'][0]['content'] = 'DOMAIN,corp.lan\n10.0.0.0/8\n'
    assert has_ip_part(item, config) is True


def test_cache_change_is_noticed_through_the_memo(rules_dir):
    config = library_config('DOMAIN,a.test\n', rules_dir)
    item = config['rule_configs'][0]
    assert has_ip_part(item, config) is False
    path = cache(rules_dir, config['rule_library'][0], 'DOMAIN,a.test\nIP-CIDR,198.51.100.0/24\n')
    os.utime(path, ns=(1, 1))
    assert has_ip_part(item, config) is True


def test_classical_set_without_ip_lines_writes_no_ip_group_or_probe(rules_dir):
    config = library_config('DOMAIN-SUFFIX,tiktok.com\n', rules_dir, direct_rules=['cn'])
    config['rule_configs'].append({'id': 'cn', 'itemType': 'rule', 'rule_type': 'DOMAIN-SUFFIX', 'value': 'cn.test'})
    main, document = main_of(config)
    assert {'exec': 'jump local_probe'} not in main
    assert not any('resp_ip' in str(entry) for entry in main)
    assert not any(p['tag'].startswith('rules_ip') for p in document['plugins'])
    downloads = get_oxidns_ruleset_downloads(config)
    assert downloads and not any(item['local_path'].endswith('_ip.txt') for item in downloads)
    assert all('part=ip' not in item['url'] for item in downloads)


def test_ip_groups_that_all_match_the_fallback_skip_the_probe(rules_dir):
    # Proxy IP group with the proxy fallback: the answer is the same either way.
    config = library_config('IP-CIDR,203.0.113.0/24\nDOMAIN,a.test\n', rules_dir, default_forward='forward_remote')
    main, _ = main_of(config)
    assert {'exec': 'jump local_probe'} not in main
    assert {'exec': 'drop_resp'} not in main
    # The group is kept so the native YAML still records its DNS choice; without a
    # response resp_ip never matches, so the result is the fallback.
    assert {'matches': ['resp_ip $rules_ip_1'], 'exec': 'goto ip_requery_proxy'} in main
    assert main[-1] == {'exec': 'goto proxy_dns_seq'}


def test_one_ip_group_away_from_the_fallback_keeps_the_probe(rules_dir):
    config = library_config('IP-CIDR,203.0.113.0/24\n', rules_dir, default_forward='forward_local')
    main, _ = main_of(config)
    probe = main.index({'exec': 'jump local_probe'})
    assert main[probe + 1] == {'matches': ['resp_ip $rules_ip_1'], 'exec': 'goto ip_requery_proxy'}
    assert main[probe + 2] == {'matches': ['has_resp'], 'exec': 'goto default_direct_ip'}
    assert main[-1] == {'exec': 'goto china_dns'}


@pytest.mark.parametrize('routes, default_forward, expected', [
    ([], 'forward_remote', False),
    (['proxy', 'proxy'], 'forward_remote', False),
    (['direct'], 'forward_local', False),
    (['proxy', 'direct'], 'forward_remote', True),
    (['proxy'], 'forward_local', True),
    (['blackhole'], 'forward_remote', True),
    (['upstream:dot'], 'forward_remote', True),
])
def test_probe_needed(routes, default_forward, expected):
    assert probe_needed(routes, default_forward) is expected


def test_native_legacy_empty_ip_part_is_recognised_and_dropped_on_rebuild(rules_dir, monkeypatch):
    config = library_config(None, rules_dir)
    config['rule_configs'] += [{'id': 'cn', 'itemType': 'ruleset', 'behavior': 'ipcidr', 'url': 'https://rules.test/cn.txt'},
                               {'id': 'gh', 'itemType': 'rule', 'rule_type': 'DOMAIN-SUFFIX', 'value': 'github.com'}]
    fields = {'proxy_rulesets': ['tiktok'], 'direct_rulesets': ['cn'], 'default_forward': 'forward_remote'}
    # Published before this change: the uncached classical list got an IP group.
    settings = prepare_editor(fields, config)
    legacy = yaml.safe_load(settings['native_config'])
    tags = {p['tag'] for p in legacy['plugins']}
    assert sum(tag.startswith('rules_ip') for tag in tags) == 2

    # Its content is now known to have no IP lines.
    cache(rules_dir, config['rule_library'][0], 'DOMAIN-SUFFIX,tiktok.com\n')
    rule_ip_presence._MEMO.clear()
    result = read_routing(legacy, settings, config)
    assert result['capabilities']['proxy_rulesets']['editable'] is True
    assert result['values']['proxy_rulesets'] == ['tiktok']
    assert result['values']['direct_rulesets'] == ['cn']

    # Any rule selection edit rebuilds the routing from the current content.
    updated, _ = apply_routing(legacy, settings, config, {'proxy_rules': ['gh']})
    main = next(p for p in updated['plugins'] if p['tag'] == 'sequence_main')['args']
    ip_entries = [entry for entry in main if 'resp_ip' in str(entry)]
    assert len(ip_entries) == 1 and 'goto direct_ip' in ip_entries[0]['exec']
    # The cn group routes away from the proxy fallback, so the probe stays.
    assert {'exec': 'jump local_probe'} in main
    after = read_routing(updated, settings, config)
    assert after['values']['proxy_rules'] == ['gh']
    assert after['values']['proxy_rulesets'] == ['tiktok']
    assert after['values']['direct_rulesets'] == ['cn']
