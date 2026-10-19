"""Independent OxiDNS strategy and compatibility regression tests."""
from copy import deepcopy
import re
import pytest
import yaml

from backend.common.oxidns_settings import (
    get_default_oxidns_settings, normalize_oxidns_settings, OxidnsSettingsError,
)
from backend.converters.oxidns import (
    generate_oxidns_config, get_oxidns_custom_files, get_oxidns_ruleset_downloads,
)
from backend.converters.mosdns import generate_mosdns_config


def document(settings=None, rules=None):
    return yaml.safe_load(generate_oxidns_config({'oxidns': settings or {}, 'rule_configs': rules or []}))


def plugins(settings=None, rules=None):
    return {p['tag']: p for p in document(settings, rules)['plugins']}


def rule(identity, value, kind='DOMAIN-SUFFIX'):
    return {'id': identity, 'itemType': 'rule', 'rule_type': kind, 'value': value, 'enabled': True}


def test_verified_defaults_independent_from_mosdns():
    original = {'mosdns': {'cache_lazy_ttl': 21600}, 'oxidns': {}}
    before = deepcopy(original)
    oxi = yaml.safe_load(generate_oxidns_config(original))
    assert original == before
    oxicache = next(p for p in oxi['plugins'] if p['tag'] == 'lazy_cache')['args']
    assert oxicache == {'size': 10240, 'lazy_cache_ttl': 300, 'short_circuit': True}
    assert oxi['runtime'] == {'worker_threads': 2}
    assert 'api' not in oxi and 'file' not in oxi['log']
    assert not any('query_summary' in str(p) for p in oxi['plugins'])
    mos = yaml.safe_load(generate_mosdns_config(original))
    assert next(p for p in mos['plugins'] if p['tag'] == 'lazy_cache')['args']['lazy_cache_ttl'] == 21600


def test_probe_cache_isolated_child_returns_to_ip_classification():
    p = plugins({'proxy_rules': ['ip']}, [rule('ip', '203.0.113.0/24', 'IP-CIDR')])
    assert p['local_probe_cache']['args'] == {'size': 2048, 'short_circuit': False}
    assert p['local_probe']['args'] == [
        {'exec': '$local_probe_cache'}, {'matches': ['!has_resp'], 'exec': '$forward_local'}, {'exec': 'return'}]
    main = p['sequence_main']['args']
    probe_index = main.index({'exec': 'jump local_probe'})
    assert main[probe_index + 1] == {'matches': ['resp_ip $rules_ip_1'], 'exec': 'goto ip_requery_proxy'}
    assert p['ip_requery_proxy']['args'][0] == {'exec': 'drop_resp'}
    assert not any('$lazy_cache' in str(entry) for entry in p['proxy_dns_seq']['args'])


def test_aggregation_preserves_alternating_rule_priority_and_regex_commas():
    rules = [rule('a', 'example.test'), rule('b', 'x.example.test', 'DOMAIN'),
             rule('c', 'exception.example.test'), rule('d', r'^n[0-9]{1,3}\.test$', 'DOMAIN-REGEX'),
             rule('e', '203.0.113.0/24', 'IP-CIDR'), rule('f', '203.0.113.1/32', 'IP-CIDR')]
    p = plugins({'direct_rules': ['a', 'b', 'd', 'e'], 'proxy_rules': ['c', 'f']}, rules)
    assert p['rules_domain_1']['args']['exps'] == ['domain:example.test', 'full:x.example.test']
    assert p['rules_domain_2']['args']['exps'] == ['domain:exception.example.test']
    assert p['rules_domain_3']['args']['exps'] == [r'regexp:^n[0-9]{1,3}\.test$']
    domains = [entry['exec'] for entry in p['sequence_main']['args'] if str(entry.get('matches', [''])[0]).startswith('qname $rules_')]
    assert domains == ['goto china_dns', 'goto proxy_dns_seq', 'goto china_dns']
    assert [p[f'rules_ip_{n}']['args']['ips'] for n in (1, 2)] == [['203.0.113.0/24'], ['203.0.113.1/32']]


def test_aggregation_can_be_disabled():
    p = plugins({'direct_rules': ['a', 'b'], 'aggregate_rules': False}, [rule('a', 'a.test'), rule('b', 'b.test')])
    assert 'rules_domain_2' in p


def test_download_paths_are_stable_ascii_and_names_cannot_escape():
    config = {'profile_id': 'home', 'system_config': {'rule_proxy_token': 'fixture-token'},
        'oxidns': {'direct_rulesets': ['a', 'b']},
        'rule_configs': [dict(id=i, name='../同名规则', itemType='ruleset', behavior='classical', url='https://rules.test/source') for i in ('a', 'b')]}
    downloads = get_oxidns_ruleset_downloads(config, base_url='https://config.test')
    assert len({d['local_path'] for d in downloads}) == 4
    assert all(re.fullmatch(r'./rules/rule_[a-f0-9]{16}_(domain|ip).txt', d['local_path']) for d in downloads)
    assert all('/profiles/home/mosdns/rule-proxy?' in d['url'] for d in downloads)
    assert [d['local_path'] for d in downloads] == [d['local_path'] for d in get_oxidns_ruleset_downloads(config)]
    p = yaml.safe_load(generate_oxidns_config(config))['plugins']
    assert all(len(item['tag']) <= 64 and item['tag'].isascii() for item in p)


def test_advanced_features_preserve_real_response_boundaries_and_runtime_state():
    s = get_default_oxidns_settings()
    for name in ('query_recorder', 'ip_selector', 'dynamic_domain_set'):
        s[name]['enabled'] = True
    s['dynamic_domain_set']['learn'] = True
    s['direct_rules'] = ['ip']
    s['default_forward'] = 'forward_local'
    p = plugins(s, [rule('ip', '203.0.113.0/24', 'IP-CIDR')])
    assert p['china_dns']['args'][:2] == [{'exec': '$ip_selector'}, {'exec': '$lazy_cache'}]
    assert '$ip_selector' not in str(p['proxy_dns_seq']) + str(p['local_probe'])
    assert {'exec': '$learn_classified_domain'} in p['direct_ip']['args']
    assert {'exec': '$learn_classified_domain'} not in p['default_direct_ip']['args']
    assert {'matches': ['has_resp'], 'exec': 'goto default_direct_ip'} in p['sequence_main']['args']
    assert '$learn_classified_domain' not in str(p['china_dns']) + str(p['proxy_dns_seq'])
    assert get_oxidns_custom_files({'oxidns': s}) == []
    assert p['dynamic_domains']['args']['path'].startswith('./state/')
    assert p['query_recorder']['args']['retention_days'] == 7


def test_adguard_uses_question_for_dnstype_before_cache_and_keeps_raw_rules():
    content = '||ads.test^$dnstype=A\n@@||good.ads.test^'
    s = {'adguard': {'enabled': True, 'content': content}}
    p = plugins(s)
    assert {'matches': ['question $adguard_rules'], 'exec': 'goto adguard_reject'} in p['sequence_main']['args']
    assert p['adguard_block']['args']['mode'] == 'nxdomain'
    assert get_oxidns_custom_files({'oxidns': s}) == [{'path': './rules/adguard-custom.txt', 'content': content}]


def test_api_auth_log_rotation_and_custom_plugin_extension():
    s = {'api': {'enabled': True, 'listen': '0.0.0.0:9199', 'username': 'test', 'password': 'fixture', 'webui_root': './webui'},
         'log': {'file': './oxidns.log'}, 'custom_config': 'plugins:\n- tag: extra\n  type: sequence\n  args: []\n'}
    doc = document(s)
    assert doc['api']['http']['auth'] == {'type': 'basic', 'username': 'test', 'password': 'fixture'}
    assert doc['api']['http']['webui']['root'] == './webui'
    assert doc['log']['rotation'] == {'type': 'daily', 'max_files': 7}
    assert doc['plugins'][-1]['tag'] == 'extra'


@pytest.mark.parametrize('value', [
    {'worker_threads': 0}, {'worker_threads': True}, {'cache_size': -1},
    {'direct_rules': ['a'], 'proxy_rules': ['a']},
    {'api': {'enabled': True, 'username': 'alone'}},
    {'dynamic_domain_set': {'enabled': True, 'args': {'path': './rules/overwrite.txt'}}},
    {'query_recorder': {'enabled': True, 'args': {'retention_days': 0}}},
    {'adguard': {'enabled': True}},
    {'cache_dump_enabled': True, 'cache_dump_file': '../escape'},
    {'ros_route': {'enabled': True}},
    {'rule_update': {'enabled': True, 'args': {'schedule': '* *'}}},
])
def test_invalid_settings_rejected(value):
    with pytest.raises(OxidnsSettingsError):
        normalize_oxidns_settings(value)


@pytest.mark.parametrize('custom', [
    'include: [external.yaml]', 'plugins: {}', 'plugins: [{tag: 国内, type: sequence}]',
    'plugins: [{tag: sequence_main, type: sequence}]', 'plugins: [{tag: qs.exec.reserved, type: sequence}]',
    'plugins: [{tag: invalid..tag, type: sequence}]', 'plugins: [',
])
def test_invalid_or_nonportable_custom_config_rejected(custom):
    with pytest.raises(OxidnsSettingsError):
        document({'custom_config': custom})


@pytest.mark.parametrize('scheduled', [False, True])
def test_native_rule_update_lists_converted_downloads_and_targeted_providers(scheduled):
    config = {'system_config': {'rule_proxy_token': 'fixture-token'},
        'oxidns': {'rule_update': {'enabled': scheduled}, 'direct_rulesets': ['a']},
        'rule_configs': [{'id': 'a', 'name': 'test', 'itemType': 'ruleset', 'behavior': 'classical', 'url': 'https://rules.test/list'}]}
    doc = yaml.safe_load(generate_oxidns_config(config, 'https://config.test'))
    p = {item['tag']: item for item in doc['plugins']}
    assert len(p['rules_download']['args']['downloads']) == 2
    assert p['rules_reload']['args'] == ['$rules_domain_1', '$rules_ip_1']
    assert p['rules_refresh']['args'] == [{'exec': '$rules_download'}, {'exec': '$rules_reload'}]
    if scheduled:
        assert p['rules_cron']['args']['jobs'][0]['executors'] == ['$rules_refresh']
        assert p['rules_cron']['args']['jobs'][0]['schedule'] == '30 3 * * *'
    else:
        assert 'rules_cron' not in p


def test_default_manual_update_chain_supports_adguard_subscriptions_without_cron():
    p = plugins({'adguard': {'enabled': True, 'urls': ['https://rules.test/adguard.txt']}})
    assert p['rules_download']['args']['downloads'][0]['url'] == 'https://rules.test/adguard.txt'
    assert p['rules_reload']['args'] == ['$adguard_rules']
    assert p['rules_refresh']['args'] == [{'exec': '$rules_download'}, {'exec': '$rules_reload'}]
    assert 'rules_cron' not in p


@pytest.mark.parametrize('settings,rules', [
    ({}, []),
    ({'direct_rules': ['a']}, [rule('a', 'example.test')]),
    ({'adguard': {'enabled': True, 'content': '||ads.test^'}}, []),
    ({}, [{'id': 'unused', 'itemType': 'ruleset', 'url': 'https://rules.test/unused'}]),
])
def test_no_downloads_produce_no_empty_update_chain(settings, rules):
    p = plugins(settings, rules)
    assert not {'rules_download', 'rules_reload', 'rules_refresh', 'rules_cron'} & p.keys()
    with pytest.raises(OxidnsSettingsError, match='定时更新需要至少一个规则订阅'):
        plugins({**settings, 'rule_update': {'enabled': True}}, rules)


@pytest.mark.parametrize('args', [
    {'timeout': 'not-a-duration'}, {'timeout': False},
    {'socks5': 'missing-port'}, {'socks5': False},
    {'outbound': '   '}, {'outbound': []},
])
def test_manual_download_options_are_validated_with_scheduling_disabled(args):
    with pytest.raises(OxidnsSettingsError):
        plugins({'adguard': {'enabled': True, 'urls': ['https://rules.test/adguard.txt']},
                 'rule_update': {'enabled': False, 'args': args}})


def test_manual_download_keeps_zero_timeout_and_ignores_inactive_schedule_fields():
    p = plugins({'adguard': {'enabled': True, 'urls': ['https://rules.test/adguard.txt']},
                 'rule_update': {'enabled': False, 'args': {
                     'timeout': 0, 'socks5': '127.0.0.1:1080', 'outbound': '',
                     'schedule': 'unfinished', 'timezone': 'not-a-timezone'}}})
    assert p['rules_download']['args']['timeout'] == 0
    assert p['rules_download']['args']['socks5'] == '127.0.0.1:1080'
    assert 'outbound' not in p['rules_download']['args']
    assert 'rules_cron' not in p


@pytest.mark.parametrize('prefix', ['rules', 'custom'])
def test_custom_update_chain_is_preserved_without_adding_another_or_enabling_cron(prefix):
    settings = {'adguard': {'enabled': True, 'urls': ['https://rules.test/adguard.txt']}}
    path = get_oxidns_ruleset_downloads({'oxidns': settings})[0]['local_path']
    custom = [
        {'tag': f'{prefix}_download', 'type': 'download', 'args': {'timeout': '19s', 'downloads': [
            {'url': 'https://rules.test/adguard.txt', 'dir': './rules', 'filename': path.rsplit('/', 1)[-1]}]}},
        {'tag': f'{prefix}_reload', 'type': 'reload_provider', 'args': ['$adguard_rules']},
        {'tag': f'{prefix}_refresh', 'type': 'sequence', 'args': [
            {'exec': f'${prefix}_download'}, {'exec': f'${prefix}_reload'}]},
    ]
    settings['custom_config'] = yaml.safe_dump({'plugins': custom})
    p = plugins(settings)
    assert [item for item in p.values() if item['type'] in ('download', 'reload_provider')] == custom[:2]
    assert p[f'{prefix}_refresh'] == custom[2]
    assert 'rules_cron' not in p
    if prefix != 'rules':
        assert not {'rules_download', 'rules_reload', 'rules_refresh'} & p.keys()
    with pytest.raises(OxidnsSettingsError, match='已有自定义下载或重载插件'):
        plugins({**settings, 'rule_update': {'enabled': True}})


@pytest.mark.parametrize('custom', [
    {'tag': 'custom_download', 'type': 'download', 'args': {'downloads': [
        {'url': 'https://rules.test/custom.txt', 'dir': './rules', 'filename': 'custom.txt'}]}},
    {'tag': 'custom_reload', 'type': 'reload_provider', 'args': ['$adguard_rules']},
    {'tag': 'rules_refresh', 'type': 'sequence', 'args': [{'exec': 'accept'}]},
])
def test_custom_maintenance_executors_do_not_receive_an_implicit_standard_chain(custom):
    settings = {'adguard': {'enabled': True, 'urls': ['https://rules.test/adguard.txt']},
                'custom_config': yaml.safe_dump({'plugins': [custom]})}
    p = plugins(settings)
    assert p[custom['tag']] == custom
    assert not ({'rules_download', 'rules_reload', 'rules_refresh', 'rules_cron'} - {custom['tag']}) & p.keys()
    with pytest.raises(OxidnsSettingsError, match='已有自定义下载或重载插件'):
        plugins({**settings, 'rule_update': {'enabled': True}})


def native_rule_fixture():
    config = {'system_config': {'server_domain': 'https://config.test', 'rule_proxy_token': 'fixture-token'},
              'rule_configs': [{'id': 'a', 'itemType': 'ruleset', 'behavior': 'domain', 'url': 'https://rules.test/list'}],
              'oxidns': {'direct_rulesets': ['a'], 'rule_update': {'enabled': False}}}
    path = get_oxidns_ruleset_downloads(config)[0]['local_path']
    native = {'runtime': {'worker_threads': 3}, 'future': {'keep': [False, None, 0]}, 'plugins': [
        {'tag': 'domains', 'type': 'domain_set', 'args': {'files': [path]}}]}
    config['oxidns'].update(config_mode='native', native_config='# saved native draft\n' + yaml.safe_dump(native))
    return config, native


def include_native_plugins(config, native):
    config['oxidns']['additional_files'] = [{'path': 'plugins.yaml', 'encoding': 'text',
                                            'content': yaml.safe_dump({'plugins': native['plugins']})}]
    config['oxidns']['native_config'] = yaml.safe_dump({**native, 'include': ['plugins.yaml'], 'plugins': []})


@pytest.mark.parametrize('included', [False, True])
def test_native_generation_adds_manual_capability_without_changing_saved_draft(included):
    config, native = native_rule_fixture()
    if included:
        include_native_plugins(config, native)
    before = deepcopy(config)
    rendered = generate_oxidns_config(config)
    generated = yaml.safe_load(rendered)
    p = {item['tag']: item for item in generated['plugins']}
    assert set(p) == {'domains', 'rules_download', 'rules_reload', 'rules_refresh'}
    assert p['domains'] == native['plugins'][0]
    assert p['rules_reload']['args'] == ['$domains']
    assert p['rules_refresh']['args'] == [{'exec': '$rules_download'}, {'exec': '$rules_reload'}]
    assert len(p['rules_download']['args']['downloads']) == 1
    assert generated['runtime'] == native['runtime'] and generated['future'] == native['future']
    assert config == before
    assert generate_oxidns_config(config) == rendered
    deployed = deepcopy(config)
    deployed['oxidns']['native_config'] = rendered
    assert generate_oxidns_config(deployed) == rendered


@pytest.mark.parametrize('timeout', ['17s', 'unfinished'])
def test_native_generation_restores_and_validates_parked_download_options(timeout):
    config, _ = native_rule_fixture()
    config['oxidns']['_common_disabled'] = {
        'rules_download': {'tag': 'rules_download', 'type': 'download', 'args': {'timeout': timeout, 'downloads': []}},
        'rules_reload': {'tag': 'rules_reload', 'type': 'reload_provider', 'args': ['$withdrawn', '$domains']},
        'rules_refresh': {'tag': 'rules_refresh', 'type': 'sequence', 'args': []},
        'rules_cron': {'tag': 'rules_cron', 'type': 'cron', 'args': {'jobs': []}},
    }
    before = deepcopy(config)
    if timeout == 'unfinished':
        with pytest.raises(OxidnsSettingsError, match='timeout'):
            generate_oxidns_config(config)
    else:
        p = {item['tag']: item for item in yaml.safe_load(generate_oxidns_config(config))['plugins']}
        assert p['rules_download']['args']['timeout'] == timeout
        assert len(p['rules_download']['args']['downloads']) == 1
        assert p['rules_reload']['args'] == ['$domains']
        assert 'rules_cron' not in p
    assert config == before


@pytest.mark.parametrize('included', [False, True])
@pytest.mark.parametrize('prefix', ['rules', 'custom'])
def test_native_generation_keeps_existing_update_chain_without_adding_another(prefix, included):
    config, native = native_rule_fixture()
    native['plugins'].extend([
        {'tag': f'{prefix}_download', 'type': 'download', 'args': {'downloads': [
            {'url': 'https://rules.test/list', 'dir': 'rules', 'filename': 'custom.txt'}]}},
        {'tag': f'{prefix}_reload', 'type': 'reload_provider', 'args': ['$domains']},
        {'tag': f'{prefix}_refresh', 'type': 'sequence', 'args': [
            {'exec': f'${prefix}_download'}, {'exec': f'${prefix}_reload'}]},
    ])
    config['oxidns']['native_config'] = yaml.safe_dump(native)
    if included:
        include_native_plugins(config, native)
    before = deepcopy(config)
    assert yaml.safe_load(generate_oxidns_config(config)) == native
    assert config == before


@pytest.mark.parametrize('tag', ['rules_download', 'rules_reload', 'rules_refresh'])
def test_native_generation_does_not_claim_existing_standard_tags(tag):
    config, native = native_rule_fixture()
    native['plugins'].append({'tag': tag, 'type': 'sequence', 'args': [{'exec': 'return'}]})
    include_native_plugins(config, native)
    before = deepcopy(config)
    assert yaml.safe_load(generate_oxidns_config(config)) == native
    assert config == before


@pytest.mark.parametrize('unmanaged_file', [False, True])
def test_native_generation_without_referenced_downloads_does_not_add_a_chain(unmanaged_file):
    config, native = native_rule_fixture()
    native['plugins'][0]['args'] = {'files': ['rules/private.txt']} if unmanaged_file else {'exps': ['domain:example.test']}
    config['oxidns']['native_config'] = yaml.safe_dump(native)
    if unmanaged_file:
        config['oxidns']['additional_files'] = [{'path': 'rules/private.txt', 'encoding': 'text', 'content': 'domain:example.test\n'}]
    before = deepcopy(config)
    assert yaml.safe_load(generate_oxidns_config(config)) == native
    assert config == before


def test_explicit_domain_rule_wins_over_old_learned_classification():
    p = plugins({'direct_rules': ['explicit'], 'dynamic_domain_set': {'enabled': True, 'route': 'proxy'}},
                [rule('explicit', 'business.test')])
    main = p['sequence_main']['args']
    explicit = {'matches': ['qname $rules_domain_1'], 'exec': 'goto china_dns'}
    learned = {'matches': ['qname $dynamic_domains'], 'exec': 'goto proxy_dns_seq'}
    assert main.index(explicit) < main.index(learned)


@pytest.mark.parametrize('upstream', ['- addr: [', '- wrong: 1', '[false]'])
def test_invalid_upstream_yaml_is_not_silently_dropped(upstream):
    with pytest.raises(OxidnsSettingsError):
        document({'local_dns': upstream})


def test_empty_fallback_disables_backup_without_inheriting_local_upstreams():
    local = '- addr: udp://192.0.2.10:5353\n- addr: tls://dns.test\n  dial_addr: 192.0.2.11\n'
    p = plugins({'local_dns': local})
    assert not {'forward_fallback', 'fallback_dns', 'fallback'} & p.keys()
    assert p['proxy_dns_seq']['args'] == [{'exec': 'goto remote_dns'}]
    assert p['forward_local']['args']['upstreams'][1]['dial_addr'] == '192.0.2.11'
    override = plugins({'local_dns': local, 'fallback_dns': 'udp://192.0.2.12'})
    assert override['forward_fallback']['args']['upstreams'] == [{'addr': 'udp://192.0.2.12'}]


def test_default_cache_dump_lives_in_unmanaged_state_directory():
    p = plugins({'cache_dump_enabled': True})
    assert p['lazy_cache']['args']['dump_file'] == './state/cache.dump'
    assert get_oxidns_custom_files({'oxidns': {'cache_dump_enabled': True}}) == []


@pytest.mark.parametrize('lazy_ttl', [0, 1, 300])
def test_lazy_cache_ttl_zero_disables_lazy_without_disabling_cache(lazy_ttl):
    p = plugins({'cache_enabled': True, 'cache_lazy_ttl': lazy_ttl,
                 'cache_size': 512, 'cache_dump_enabled': True})
    cache = p['lazy_cache']['args']
    assert cache['size'] == 512
    assert cache['short_circuit'] is True
    assert cache['dump_file'] == './state/cache.dump'
    assert cache['dump_interval'] == get_default_oxidns_settings()['cache_dump_interval']
    assert {'exec': '$lazy_cache'} in p['china_dns']['args']
    if lazy_ttl == 0:
        assert 'lazy_cache_ttl' not in cache
    else:
        assert cache['lazy_cache_ttl'] == lazy_ttl


@pytest.mark.parametrize('content', [
    'router.test 192.0.2.10',
    'router.test 2001:db8::10',
    'full:router.test 192.0.2.10 192.0.2.11 2001:db8::10',
    'domain:service.test 2001:db8::1 192.0.2.12 2001:db8::2',
    'keyword:router 192.0.2.1 192.0.2.1',
    r'regexp:^api[0-9]{1,3}\.test$ 192.0.2.10 2001:db8::10',
    '# comment\n\n  full:router.test\t192.0.2.10\t2001:DB8::10 # dual stack\n',
    'full:router.test 192.0.2.1\r\nfull:router.test 192.0.2.2\r\n',
    '# temporarily no hosts\n',
])
def test_native_hosts_preserves_ipv6_all_addresses_rule_order_and_comments(content):
    config = {'oxidns': {'custom_hosts': content}}
    before = deepcopy(config)
    files = get_oxidns_custom_files(config)
    assert files == [{'path': './rules/custom_hosts.txt', 'content': content}]
    p = plugins(config['oxidns'])
    assert p['custom_hosts']['args']['files'] == [files[0]['path']]
    assert p['sequence_main']['args'].index({'exec': '$custom_hosts'}) < p['sequence_main']['args'].index({'exec': 'goto proxy_dns_seq'})
    assert config == before


@pytest.mark.parametrize(('content', 'expected'), [
    ('192.0.2.10 router.test', 'router.test 192.0.2.10'),
    ('2001:db8::10 router.test', 'router.test 2001:db8::10'),
    ('192.0.2.10 router.test alias.test # aliases\n',
     'router.test 192.0.2.10 # aliases\nalias.test 192.0.2.10\n'),
    ('2001:db8::10 router.test alias.test\r\n',
     'router.test 2001:db8::10\r\nalias.test 2001:db8::10\r\n'),
    ('192.0.2.10 domain:router.test', 'domain:router.test 192.0.2.10'),
    ('192.0.2.10 router.test alias.test', 'router.test 192.0.2.10\nalias.test 192.0.2.10'),
    ('router.test 192.0.2.1 2001:db8::1\n2001:db8::2 legacy.test',
     'router.test 192.0.2.1 2001:db8::1\nlegacy.test 2001:db8::2'),
])
def test_legacy_hosts_ip_first_rows_and_aliases_convert_without_truncation(content, expected):
    config = {'oxidns': {'custom_hosts': content}}
    assert get_oxidns_custom_files(config) == [{'path': './rules/custom_hosts.txt', 'content': expected}]
    document(config['oxidns'])


@pytest.mark.parametrize('invalid', [
    'router.test',
    'router.test not-an-address',
    'router.test 192.0.2.1 bad-address',
    'router.test 999.999.999.999',
    'router.test [2001:db8::1]',
    'router.test fe80::1%eth0',
    'router.test 192.0.2.1,2001:db8::1',
    '192.0.2.1 router.test 2001:db8::1',
    '192.0.2.1 999.999.999.999',
    '2001:db8::1 https://router.test',
    '192.0.2.1 full:',
])
def test_invalid_or_ambiguous_hosts_fail_in_preview_and_download_instead_of_dropping_data(invalid):
    config = {'oxidns': {'custom_hosts': '# first line\n' + invalid}}
    for render in (generate_oxidns_config, get_oxidns_custom_files):
        with pytest.raises(OxidnsSettingsError, match='Hosts 第 2 行'):
            render(config)
