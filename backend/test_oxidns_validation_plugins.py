"""Plugin validation regressions audited against OxiDNS 1.6.1 factories."""
import copy
import json
import re
from pathlib import Path

import pytest

from backend.common.oxidns_plugin_constraints import collect_plugin_issues, validate_plugin_format

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = json.loads((ROOT / 'backend/schemas/oxidns/plugins.json').read_text())['plugins']

# Each type has at least one legal core shape, including intentionally empty
# defaults. Referenced plugins are provided by document() below.
VALID = {
    'sequence': [{'exec': 'accept'}], 'fallback': {'primary': 'upstream', 'secondary': 'upstream'},
    'cache': {}, 'hosts': {}, 'arbitrary': {'rules': ['example.test. 60 IN TXT "hello"']},
    'response': {'rcode': 'NOERROR', 'answers': ['{qname} 60 {qclass} A 192.0.2.1']},
    'redirect': {'rules': ['full:old.example new.example']}, 'client_ip_from_ecs': ['127.0.0.1/8', '::1'],
    'ecs_handler': {'preset': '', 'mask4': 0, 'mask6': 128}, 'forward_edns0opt': {'codes': []},
    'ttl': {'fix': 0, 'min': 300, 'max': 10},
    'ip_selector': {'probe_methods': 'none', 'cache': {'enabled': False, 'size': 0, 'ttl': 0, 'failure_ttl': 0}},
    'prefer_ipv4': {'cache': False, 'cache_ttl': 0}, 'prefer_ipv6': {'cache': False, 'cache_ttl': 0},
    'black_hole': {'mode': 'null'}, 'drop_resp': None, 'reverse_lookup': {'size': 0, 'ttl': 0},
    'query_summary': '', 'query_recorder': {'path': 'state/queries.sqlite'},
    'metrics_collector': {}, 'debug_print': '', 'sleep': {'duration': 0},
    'learn_domain': {'provider': 'dynamic', 'timeout': '', 'qtypes': ['A', '65']},
    'http_request': {'method': 'POST', 'url': 'https://example.test/hook', 'json': {}, 'form': {}, 'timeout': ''},
    'script': {'command': '/bin/true', 'args': [], 'env': {}, 'cwd': ''},
    'ipset': {}, 'nftset': {},
    'ros_address_list': {'address': '192.0.2.1:8728', 'username': 'admin', 'password': 'secret', 'address_list4': 'dns', 'fixed_ttl': 0, 'persistent': {}, 'wait_timeout': 1},
    'ros_route': {'address': '192.0.2.1:8728', 'username': 'admin', 'password': 'secret', 'routing_table': 'dns', 'gateway4': '192.0.2.2', 'distance': 0, 'wait_timeout': '1s'},
    'upgrade': {'timeout': '', 'bundle': 'auto'},
    'download': {'timeout': 0, 'downloads': [{'url': 'https://example.test/rules.txt', 'dir': 'rules', 'filename': ''}]},
    'reload_provider': ['$domains'], 'reload': None,
    'cron': {'timezone': '', 'jobs': [{'name': 'refresh', 'interval': '1m', 'executors': ['$upstream']}]},
    'qname': ['domain:example.test'], 'question': ['$adguard'], 'qtype': [1, 'AAAA', 65], 'qclass': 'IN 255',
    'client_ip': ['192.0.2.0/24'], 'ptr_ip': ['::1'], 'resp_ip': '192.0.2.0/24', 'cname': 'full:example.test',
    'rcode': [0, 'NXDOMAIN'], 'has_resp': None, 'has_wanted_ans': None, 'mark': ['0', '4294967295'],
    'env': ['NAME=', 'OTHER:value with spaces'], 'time': {'timezone': 'Asia/Shanghai', 'periods': [{'weekdays': ['mon', 7]}]},
    'random': ['0.5'], 'rate_limiter': {'qps': .5}, 'string_exp': 'qname suffix .example.test',
    'any_match': ['qtype A'], '_true': None, '_false': None,
    'domain_set': {'exps': [], 'sets': [], 'files': []},
    'dynamic_domain_set': {'path': 'state/dynamic.txt', 'bootstrap_rules': []},
    'geosite': {'file': 'rules/geosite.dat', 'selectors': ['cn@ads']},
    'adguard_rule': {'rules': [], 'files': []}, 'ip_set': {'ips': [], 'sets': [], 'files': []},
    'geoip': {'file': 'rules/geoip.dat', 'selectors': []},
}


def document(kind, args):
    return {'plugins': [
        {'tag': 'subject', 'type': kind, 'args': copy.deepcopy(args)},
        {'tag': 'upstream', 'type': 'forward', 'args': {'upstreams': [{'addr': '192.0.2.53'}]}},
        {'tag': 'domains', 'type': 'domain_set', 'args': {'exps': ['domain:example.test']}},
        {'tag': 'adguard', 'type': 'adguard_rule', 'args': {'rules': ['||ads.example^']}},
        {'tag': 'dynamic', 'type': 'dynamic_domain_set', 'args': {'path': 'state/learned.txt'}},
    ]}


def errors(doc):
    from backend.common.oxidns_validation import validate_native_config
    return [issue for issue in validate_native_config(doc) if issue.get('severity', 'error') == 'error']


def test_every_non_server_forward_catalog_type_has_schema_and_legal_fixture():
    catalog = (ROOT / 'frontend/src/lib/oxidnsSchema.ts').read_text()
    expected = set(re.findall(r"plugin\('([^']+)'", catalog)) - {'forward', 'udp_server', 'tcp_server', 'http_server', 'quic_server'}
    assert set(SCHEMAS) == expected == set(VALID)
    assert all(schema['x-kind'] in ('executor', 'matcher', 'provider') for schema in SCHEMAS.values())


@pytest.mark.parametrize('kind', sorted(VALID))
def test_all_plugin_types_accept_documented_legal_shapes_without_mutation(kind):
    doc = document(kind, VALID[kind])
    before = copy.deepcopy(doc)
    assert errors(doc) == []
    assert doc == before


@pytest.mark.parametrize('kind', sorted(k for k,v in SCHEMAS.items() if v.get('x-args-optional')))
def test_factory_default_args_may_be_omitted_or_null(kind):
    doc = document(kind, None)
    assert not errors(doc)
    del doc['plugins'][0]['args']
    assert not errors(doc)


@pytest.mark.parametrize('kind', sorted(k for k,v in SCHEMAS.items() if not v.get('x-args-optional')))
def test_required_args_reject_null_instead_of_writing_empty_object(kind):
    doc = document(kind, None)
    assert errors(doc)
    assert doc['plugins'][0]['args'] is None


INVALID = [
    ('cache', {'size': -1}), ('cache', {'size': True}), ('cache', {'lazy_cache_ttl': 0}),
    ('cache', {'dump_file': '', 'dump_interval': 0}), ('cache', {'min_positive_ttl': 20, 'max_positive_ttl': 10}),
    ('cache', {'negative_ttl_without_soa': 4294967296}), ('hosts', {'entries': None}),
    ('hosts', {'entries': ['example.test not-an-ip']}), ('arbitrary', {'rules': ['example.test. IN A 999.1.1.1']}),
    ('response', {'rcode': 16}), ('response', {'answers': ['{qname} 60 IN A ::1']}),
    ('redirect', {'rules': ['old.example new.example extra']}), ('client_ip_from_ecs', ['256.0.0.1']),
    ('ecs_handler', {'mask4': 33}), ('ecs_handler', {'mask6': -1}), ('ecs_handler', {'preset': 'not-ip'}),
    ('forward_edns0opt', {'codes': [65536]}), ('forward_edns0opt', '65536'), ('ttl', {}), ('ttl', 'tomorrow'),
    ('ip_selector', {'probe_methods': ['none', 'tcp:443']}), ('ip_selector', {'probe_methods': 'tcp:0'}),
    ('ip_selector', {'cache': {'enabled': True, 'size': 0}}), ('ip_selector', {'probe_timeout': 0}),
    ('prefer_ipv4', {'cache_ttl': 0}), ('prefer_ipv6', {'probe_executor': ''}),
    ('black_hole', {'mode': 'custom', 'ips': []}), ('black_hole', {'mode': 'nodata', 'ips': ['192.0.2.1']}),
    ('reverse_lookup', {'ttl': -1}), ('query_summary', {'msg': []}), ('metrics_collector', {'name': 1}),
    ('debug_print', {'msg': True}), ('sleep', {'duration': -1}), ('query_recorder', {'path': ''}),
    ('query_recorder', {'path': 'x.db', 'queue_size': 0}), ('learn_domain', {'provider': 'dynamic', 'qtypes': []}),
    ('learn_domain', {'provider': 'dynamic', 'qtypes': ['NOTATYPE']}),
    ('http_request', {'method': 'POST', 'url': 'https://example.test/', 'json': {}, 'body': 'data'}),
    ('http_request', {'method': 'POST', 'url': 'https://example.test/', 'json': {}, 'content_type': 'text/plain'}),
    ('http_request', {'method': 'GET', 'url': 'https://example.test/', 'headers': {'Bad Header': 'x'}}),
    ('script', {'command': ''}), ('script', {'command': '/bin/true', 'max_output_bytes': 0}),
    ('ipset', {'mask4': 33}), ('nftset', {'ipv4': {'table_family': 'wrong', 'table_name': 'x', 'set_name': 'x'}}),
    ('ros_address_list', {'address': '192.0.2.1:8728', 'username': 'u', 'password': 'p'}),
    ('ros_route', {'address': '192.0.2.1:8728', 'username': 'u', 'password': 'p', 'routing_table': 'dns', 'gateway4': 'gw', 'min_ttl': 4000}),
    ('upgrade', {'bundle': 'unknown'}), ('download', {'downloads': []}),
    ('download', {'downloads': [{'url': 'https://example.test/', 'dir': 'rules'}]}),
    ('reload_provider', ['domains']), ('cron', {'jobs': []}),
    ('cron', {'jobs': [{'name': 'x', 'interval': '1s', 'executors': ['$upstream']}]}),
    ('cron', {'jobs': [{'name': 'x', 'schedule': '70 * * * *', 'executors': ['$upstream']}]}),
    ('qname', []), ('question', ['example.test']), ('qtype', ['70000']), ('qclass', ['INVALID']), ('rcode', ['1.5']),
    ('client_ip', ['1.2.3.999']), ('ptr_ip', ['1.2.3.4/40']), ('resp_ip', ['bad']), ('cname', ['regexp:[']),
    ('mark', ['-1']), ('mark', [1]), ('env', ['=value']), ('time', {'periods': [{}]}),
    ('time', {'periods': [{'start': '12:00'}]}), ('time', {'periods': [{'start': '12:00', 'end': '12:00'}]}),
    ('random', ['2']), ('rate_limiter', {'qps': 0}), ('string_exp', 'qname zl value'), ('any_match', []),
    ('domain_set', {'exps': None}), ('dynamic_domain_set', {'path': 'x', 'bootstrap_rules': ['full:']}),
    ('geosite', {'file': 'x.dat', 'selectors': ['cn@']}), ('adguard_rule', {'rules': 'not-a-list'}),
    ('ip_set', {'ips': ['192.0.2.1/99']}), ('geoip', {}),
]


@pytest.mark.parametrize('kind,args', INVALID)
def test_invalid_plugin_values_are_rejected_without_destroying_draft(kind, args):
    doc = document(kind,args)
    before = copy.deepcopy(doc)
    assert errors(doc), (kind,args)
    assert doc == before


@pytest.mark.parametrize('kind', ['ip_selector','learn_domain','dynamic_domain_set','ros_address_list','ros_route','upgrade'])
def test_factory_deny_unknown_fields_is_not_silently_accepted(kind):
    doc=document(kind, VALID[kind])
    doc['plugins'][0]['args']['typo_parameter']=123
    assert errors(doc)
    assert doc['plugins'][0]['args']['typo_parameter']==123


def test_nullable_options_and_empty_containers_are_preserved_exactly():
    doc=document('cache',{'size':None,'dump_file':None,'ecs_in_key':None})
    doc['plugins'].extend([
        {'tag':'optional-record','type':'response','args': {'rcode':None,'answers':[]}},
        {'tag':'optional-provider','type':'domain_set','args':{}},
        {'tag':'optional-prefer','type':'prefer_ipv4','args': {'cache':None,'cache_ttl':None}},
    ])
    before=copy.deepcopy(doc)
    assert errors(doc)==[]
    assert doc==before


@pytest.mark.parametrize('raw', [
    '{qname} 60 {qclass} A 192.0.2.1',
    'example.test. 60 IN TXT "hello world"',
    'example.test. IN MX 10 mail.example.test.',
    'example.test. IN TYPE65400 \\# 2 0102',
    '$ORIGIN example.test.\nwww IN A 192.0.2.1',
])
def test_response_records_use_official_placeholder_and_zone_rules(raw):
    assert validate_plugin_format('dns-record',raw) is None


@pytest.mark.parametrize('raw', [
    'example.test. IN A ::1', 'example.test IN A 192.0.2.1',
    'example.test. IN TXT "{qname}"', '{qname} 60 IN TXT "{qclass}"',
    '{qname} 60 IN TXT "{qname}"', 'example.test. IN MX 10 relative',
    'example.test. IN TYPE65400 \\# 2 01',
    'example.test. IN A 192.0.2.1\nother.test. IN A 192.0.2.2',
    'example.test. IN HTTPS 1 . alpn=h2',
])
def test_invalid_response_record_templates(raw):
    assert validate_plugin_format('dns-record',raw)


def test_errors_do_not_echo_credentials_or_http_body():
    secret='CONFIDENTIAL_BODY_AND_PASSWORD'
    doc=document('http_request', {'method':'POST','url':'https://example.test/','body':secret,'json':{'password':secret}})
    assert secret not in json.dumps(errors(doc))


def test_ros_ignored_persistent_family_is_warning_only():
    args={**VALID['ros_route'],'persistent':{'ips':['::1','0.0.0.0/0']}}
    issues=collect_plugin_issues({'type':'ros_route','args':args},0,{})
    assert {issue['code'] for issue in issues}=={'ignored-family','ignored-default-route'}
    assert all(issue['severity']=='warning' for issue in issues)


@pytest.mark.parametrize('kind,args', [
    ('mark', ['9' * 5000]), ('ttl', '9' * 5000),
    ('forward_edns0opt', '9' * 5000),
    ('ip_selector', {'probe_methods': 'tcp:' + '9' * 5000}),
    ('cron', {'jobs': [{'name': 'huge', 'interval': '9' * 5000 + 's', 'executors': ['$upstream']}]}),
    ('cron', {'jobs': [{'name': 'huge', 'schedule': '*/' + '9' * 5000 + ' * * * *', 'executors': ['$upstream']}]}),
    ('cron', {'jobs': [{'name': 'huge', 'schedule': '0 0 ' + '9' * 5000 + 'W * *', 'executors': ['$upstream']}]}),
    ('http_request', {'url': 'https://example.test/', 'method': 'GET', 'timeout': '9' * 5000 + 'ms'}),
    ('arbitrary', {'rules': ['$GENERATE 0-' + '9' * 5000 + ' host$.test. A 192.0.2.1']}),
    ('response', {'answers': ['test. TYPE' + '9' * 5000 + ' \\# 0']}),
    ('response', {'answers': ['test. TYPE65000 \\# ' + '9' * 5000 + ' 01']}),
])
def test_oversized_decimal_tokens_report_errors_instead_of_raising(kind, args):
    assert errors(document(kind, args))


def test_long_leading_zero_decimal_strings_and_plain_strings_remain_legal():
    assert not errors(document('ttl', '0' * 5000 + '60'))
    assert not errors(document('mark', ['0' * 5000 + '1']))
    assert not errors(document('query_summary', '9' * 5000))


@pytest.mark.parametrize('raw', [
    '$INCLUDE /target-host/rules.zone',
    '$GENERATE 1-1 host$.test. A 192.0.2.1',
])
def test_response_zone_directives_are_preserved_for_target_file_initialization(raw):
    doc = document('response', {'answers': [raw]})
    assert not errors(doc)
    issues = collect_plugin_issues(doc['plugins'][0], 0, doc)
    assert any(issue['code'] == 'runtime-zone' and issue['severity'] == 'warning' for issue in issues)


def test_routeros_persistent_ips_accept_multiline_comments_without_mutation():
    args = {**VALID['ros_route'], 'persistent': {'ips': ['# example\n192.0.2.1\n192.0.2.0/24 # local']}}
    assert not errors(document('ros_route', args))


def test_schema_audit_follows_nested_union_fields_and_ignores_runtime_only_rust_fields():
    from scripts.audit_oxidns_schema import native_fields, schema_at
    schema = {'anyOf': [{'type': 'null'}, {'properties': {'items': {'type': 'array', 'items': {'properties': {'timeout': {'type': 'string'}}}}}}]}
    assert schema_at(schema, 'items[].timeout')
    assert not schema_at(schema, 'items[].new_parameter')
    source = 'struct Config {\n #[serde(rename = "async")]\n async_mode: bool,\n #[serde(default, skip)]\n runtime_only: bool,\n new_parameter: String,\n}'
    assert native_fields(source, 'Config') == {'async', 'new_parameter'}


def test_black_hole_bare_null_mode_is_warning():
    def issues(args):
        plugin = {'tag': 'bh', 'type': 'black_hole', 'args': args}
        return collect_plugin_issues(plugin, 0, {'plugins': [plugin]})
    assert issues({'mode': None, 'short_circuit': True}) == [{
        'path': ['plugins', 0, 'args', 'mode'], 'message': 'mode 为空时 OxiDNS 会按 nxdomain 处理；黑洞请写 "null"（带引号）',
        'code': 'null-mode', 'severity': 'warning'}]
    assert issues({'mode': 'null', 'short_circuit': True}) == []
    assert issues({'short_circuit': True}) == []
