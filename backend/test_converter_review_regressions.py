"""Behavioral regressions from the converter review; all resources stay local."""
import copy
import ipaddress
import re
import zipfile

import pytest
import yaml

from backend.converters.mihomo import (
    generate_mihomo_config, get_mihomo_ruleset_downloads, parse_mosdns_custom_hosts,
)
from backend.converters.mosdns import (
    generate_mosdns_config, get_mosdns_custom_files, get_mosdns_ruleset_downloads,
)
from backend.converters.surge import generate_surge_config
from backend.converters.loon import generate_loon_config
from backend.utils.mosdns_archive import build_mosdns_zip


def _config(rules, **settings):
    return {
        'profile_id': 'default',
        'system_config': {'server_domain': 'https://config.test', 'rule_proxy_token': 'test'},
        'rule_configs': rules, 'rule_library': [], 'mosdns': settings,
    }


def _rule(ident, kind, value, **extra):
    return {'id': ident, 'itemType': 'rule', 'rule_type': kind, 'value': value,
            'policy': 'DIRECT', **extra}


def _library(config, ident, name, content, behavior='domain'):
    resource = {'id': ident, 'name': name, 'source_type': 'content',
                'content': content, 'behavior': behavior}
    config['rule_library'].append(resource)
    return {**resource, 'id': 'ref-' + ident, 'itemType': 'ruleset',
            'library_rule_id': ident, 'policy': 'DIRECT',
            'url': '/api/profiles/default/rule-library/content/' + ident}


def _bundle(config):
    before = copy.deepcopy(config)
    text = generate_mosdns_config(config)
    archive = build_mosdns_zip(config, text, get_mosdns_ruleset_downloads(config),
                              get_mosdns_custom_files(config))
    with zipfile.ZipFile(archive) as zip_file:
        files = {'./' + name: zip_file.read(name).decode() for name in zip_file.namelist()}
    assert config == before
    doc = yaml.safe_load(text)
    plugins = {p['tag']: p for p in doc['plugins'] if p.get('tag')}
    return doc, plugins, files


def _first_route(plugins, files, query, part='domain'):
    prefix = 'qname $' if part == 'domain' else 'resp_ip $'
    for entry in plugins['sequence_main']['args']:
        match = next((m for m in entry.get('matches', []) if m.startswith(prefix)), None)
        if not match:
            continue
        provider = plugins[match.removeprefix(prefix)]
        lines = [line for path in provider['args']['files'] for line in files[path].splitlines()]
        for line in lines:
            if part == 'ip':
                matches = ipaddress.ip_address(query) in ipaddress.ip_network(line)
            else:
                kind, _, value = line.partition(':')
                matches = ((kind == 'full' and query == value)
                           or (kind == 'domain' and (query == value or query.endswith('.' + value)))
                           or (kind == 'keyword' and value in query)
                           or (kind == 'regexp' and re.search(value, query)))
            if matches:
                return entry['exec']
    return None


@pytest.mark.parametrize('part,kind,first,exact,broad,query', [
    ('domain', 'DOMAIN', 'unrelated.test', 'www.example.test', 'example.test', 'www.example.test'),
    ('ip', 'IP-CIDR', '192.0.2.0/24', '203.0.113.7/32', '203.0.113.0/24', '203.0.113.7'),
    ('ip', 'IP-CIDR6', '2001:db8:1::/48', '2001:db8:2::7/128', '2001:db8:2::/48', '2001:db8:2::7'),
])
@pytest.mark.parametrize('middle_ruleset', [False, True])
@pytest.mark.parametrize('first_direct', [False, True])
def test_mosdns_preserves_interleaved_single_rules_and_rulesets(part, kind, first, exact, broad, query, middle_ruleset, first_direct):
    rules = [_rule('first', kind, first), _rule('middle', kind, exact),
             _rule('last', 'DOMAIN-SUFFIX' if part == 'domain' else kind, broad)]
    cfg = _config(rules, direct_rules=['first', 'last'] if first_direct else ['middle'],
                  proxy_rules=['middle'] if first_direct else ['first', 'last'])
    if middle_ruleset:
        item = _library(cfg, 'middle', 'Middle', exact,
                        behavior='domain' if part == 'domain' else 'ipcidr')
        cfg['rule_configs'][1] = item
        side = 'proxy' if first_direct else 'direct'
        cfg['mosdns'].update({side + '_rules': [], side + '_rulesets': [item['id']]})
    _, plugins, files = _bundle(cfg)
    expected = ('goto proxy_dns_seq' if part == 'domain' else 'goto ip_requery_proxy') if first_direct else (
        'goto china_dns' if part == 'domain' else 'accept')
    assert _first_route(plugins, files, query, part) == expected


def test_mosdns_adjacent_rules_still_share_files_and_disabled_rules_stay_absent():
    cfg = _config([
        _rule('first', 'DOMAIN', 'first.test'),
        _rule('second', 'DOMAIN-SUFFIX', 'second.test'),
        _rule('disabled', 'DOMAIN', 'disabled.test', enabled=False),
        _rule('library-disabled', 'DOMAIN', 'library-disabled.test', library_enabled=False),
        _rule('unselected', 'DOMAIN', 'unselected.test'),
    ], direct_rules=['first', 'second', 'disabled', 'library-disabled'])
    _, plugins, files = _bundle(cfg)
    assert files['./rules/direct_rules.txt'] == 'full:first.test\ndomain:second.test'
    assert _first_route(plugins, files, 'www.second.test') == 'goto china_dns'
    for name in ('disabled.test', 'library-disabled.test', 'unselected.test'):
        assert _first_route(plugins, files, name) is None


def test_mosdns_single_regex_survives_generation_and_archive():
    expression = r'^node[0-9]{1,3}\.example\.test$'
    cfg = _config([_rule('regex', 'DOMAIN-REGEX', expression)], direct_rules=['regex'])
    _, plugins, files = _bundle(cfg)
    assert any('regexp:' + expression in content for content in files.values())
    assert _first_route(plugins, files, 'node123.example.test') == 'goto china_dns'
    assert _first_route(plugins, files, 'node1234.example.test') is None


def test_mosdns_tags_and_files_do_not_collide_with_internal_or_derived_names(tmp_path):
    cfg = _config([], direct_rules=['single'], direct_rulesets=[])
    for index, (name, behavior) in enumerate([
        ('forward_local', 'domain'), ('sequence_main', 'domain'), ('direct_rules', 'domain'),
        ('Mixed', 'classical'), ('Mixed_ip', 'domain'), ('Mixed_2', 'domain'),
    ]):
        content = 'DOMAIN,rule%d.test\nIP-CIDR,192.0.2.0/24' % index if behavior == 'classical' else 'rule%d.test' % index
        item = _library(cfg, str(index), name, content, behavior)
        cfg['rule_configs'].append(item)
        cfg['mosdns']['direct_rulesets'].append(item['id'])
    # Repeated reference to Mixed must not collide with the real Mixed_2 label.
    repeated = {**cfg['rule_configs'][3], 'id': 'mixed-again'}
    cfg['rule_configs'].append(repeated)
    cfg['mosdns']['direct_rulesets'].append(repeated['id'])
    cfg['rule_configs'].append(_rule('single', 'DOMAIN', 'single.test'))
    from backend.common.config_repository import ProfileRepository
    repository = ProfileRepository(tmp_path)
    repository.save_shared({'rule_library': cfg['rule_library']})
    repository.save_profile('default', {key: cfg[key] for key in ('rule_configs', 'mosdns')})
    cfg = repository.get_compat_config('default')
    doc, plugins, files = _bundle(cfg)
    tags = [p['tag'] for p in doc['plugins'] if p.get('tag')]
    assert len(tags) == len(set(tags))
    assert plugins['forward_local']['type'] == 'forward'
    assert plugins['sequence_main']['type'] == 'sequence'
    assert _first_route(plugins, files, 'single.test') == 'goto china_dns'
    assert _first_route(plugins, files, 'rule2.test') == 'goto china_dns'
    for plugin in doc['plugins']:
        if isinstance(plugin.get('args'), dict):
            assert all(path in files for path in plugin['args'].get('files', []))
        if plugin.get('type') == 'sequence':
            for entry in plugin['args']:
                for match in entry.get('matches', []):
                    if match.startswith(('qname $', 'resp_ip $')):
                        assert match.split('$', 1)[1] in plugins
                action = entry.get('exec', '')
                if action.startswith('$'):
                    assert action[1:] in plugins
                elif action.startswith('goto '):
                    assert action[5:] in plugins


@pytest.mark.parametrize('source,expected', [
    ('router.test 192.0.2.1', 'router.test 192.0.2.1'),
    ('192.0.2.1 router.test', 'router.test 192.0.2.1'),
    ('router.test 2001:db8::1', 'router.test 2001:db8::1'),
    ('2001:db8::1 router.test', 'router.test 2001:db8::1'),
    ('# comment\nrouter.test ::1 # local\n', 'router.test ::1'),
])
def test_mosdns_and_mihomo_hosts_share_ipv4_ipv6_parsing(source, expected):
    cfg = _config([], custom_hosts=source)
    _, plugins, files = _bundle(cfg)
    assert plugins['custom_hosts']['args']['files'] == ['./rules/custom_hosts.txt']
    assert files['./rules/custom_hosts.txt'] == expected
    domain, address = expected.split()
    assert parse_mosdns_custom_hosts(source) == {domain: address}
    main = yaml.safe_load(generate_mihomo_config(cfg, sync_lan_hosts=True, preflight_providers=False))
    assert main['hosts'][domain] == address


@pytest.mark.parametrize('url,declared,expected', [
    ('https://rules.test/a.yaml?token=test', None, 'yaml'),
    ('https://rules.test/a.yml#download', None, 'yaml'),
    ('https://rules.test/a.mrs?token=test#download', None, 'mrs'),
    ('https://rules.test/a.mrs#download', None, 'mrs'),
    ('https://rules.test/api/rules', 'yaml', 'yaml'),
    ('https://rules.test/a.yaml', 'text', 'text'),
    ('https://rules.test/a.list', 'mrs', 'mrs'),
])
def test_rule_format_is_shared_by_generation_and_delivery(url, declared, expected):
    item = {'id': 'rule', 'name': 'Rules', 'itemType': 'ruleset',
            'behavior': 'domain', 'url': url, 'policy': 'DIRECT'}
    if declared:
        item['format'] = declared
    cfg = _config([item])
    generated = yaml.safe_load(generate_mihomo_config(cfg, preflight_providers=False))
    provider = generated['rule-providers']['Rules']
    assert provider['format'] == expected
    assert get_mihomo_ruleset_downloads(cfg)[0]['local_path'] == provider['path']
    surge = generate_surge_config(cfg)
    assert ('rule-set-format=yaml' in surge) == (expected == 'yaml')
    loon = generate_loon_config(cfg).split('[Remote Rule]\n', 1)[1]
    if expected in ('yaml', 'mrs'):
        assert loon.strip().startswith('# Loon 不支持')
    else:
        assert loon.strip().startswith(url)


def test_inline_yaml_format_survives_real_repository_hydration(tmp_path):
    from backend.common.config_repository import ProfileRepository
    repository = ProfileRepository(tmp_path)
    repository.save_shared({'rule_library': [{
        'id': 'lib', 'name': 'Inline', 'source_type': 'content', 'format': 'yaml',
        'behavior': 'domain', 'content': 'payload:\n  - +.example.test',
    }]})
    repository.save_profile('default', {'rule_configs': [{
        'id': 'selected', 'itemType': 'ruleset', 'library_rule_id': 'lib', 'policy': 'DIRECT',
    }]})
    cfg = repository.get_compat_config('default')
    generated = yaml.safe_load(generate_mihomo_config(cfg, preflight_providers=False))
    assert generated['rule-providers']['Inline']['format'] == 'yaml'
    assert get_mihomo_ruleset_downloads(cfg)[0]['local_path'].endswith('.yaml')
    assert 'rule-set-format=yaml' in generate_surge_config(cfg)
    assert '# Loon 不支持' in generate_loon_config(cfg)
