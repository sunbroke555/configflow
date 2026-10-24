"""Duplicate scans keep complete results and expose when a response is ready."""
import logging

import pytest
from flask import Flask

from backend.common import config as config_module
from backend.common.config_repository import ProfileRepository
from backend.routes import register_blueprints
from backend.routes import rules


@pytest.fixture
def duplicate_app(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path)
    repository.save_shared({'rule_library': [
        {'id': 'a', 'name': 'A', 'source_type': 'content', 'content': '', 'enabled': True},
        {'id': 'b', 'name': 'B', 'source_type': 'content', 'content': '', 'enabled': True},
        {'id': 'empty', 'name': 'Empty', 'source_type': 'content', 'content': '', 'enabled': True},
        {'id': 'off', 'name': 'Off', 'source_type': 'content', 'content': 'DOMAIN-SUFFIX,example.com', 'enabled': False},
    ]})
    repository.save_profile('default', {'rule_configs': [
        {'id': 'direct', 'itemType': 'rule', 'rule_type': 'DOMAIN-SUFFIX', 'value': 'example.com', 'policy': 'DIRECT'},
        {'id': 'set-a', 'itemType': 'ruleset', 'library_rule_id': 'a', 'policy': 'DIRECT'},
        {'id': 'set-b', 'itemType': 'ruleset', 'library_rule_id': 'b', 'policy': 'Proxy'},
        {'id': 'empty', 'itemType': 'ruleset', 'library_rule_id': 'empty', 'policy': 'DIRECT'},
        {'id': 'off', 'itemType': 'ruleset', 'library_rule_id': 'off', 'policy': 'DIRECT'},
        {'id': 'disabled', 'itemType': 'rule', 'rule_type': 'DOMAIN-SUFFIX', 'value': 'example.com', 'enabled': False},
    ]})
    repository.write_shared_text('rules/A.list', '\n'.join([
        '# ignored', 'DOMAIN-SUFFIX,Example.com', 'DOMAIN-SUFFIX,example.com',
        'IP-CIDR6,2001:db8::/32,no-resolve', 'REGEX,^Foo$',
    ]))
    repository.write_shared_text('rules/B.list', '\n'.join([
        'domain:example.com', 'IP-CIDR6,2001:db8::/32', 'REGEX,^foo$',
    ]))
    config_module.set_repository(repository)
    monkeypatch.setattr('backend.common.auth.is_auth_enabled', lambda: False)
    app = Flask(__name__)
    register_blueprints(app)
    yield app, repository
    config_module.reset_config_context()


def test_duplicates_keep_all_occurrences_and_report_response_completion(duplicate_app, monkeypatch, caplog):
    app, _ = duplicate_app
    config_reads = []
    original_get_config = rules.get_config

    def read_config():
        config_reads.append(True)
        return original_get_config()

    monkeypatch.setattr(rules, 'get_config', read_config)
    with caplog.at_level(logging.INFO):
        response = app.test_client().post('/api/rules/find-duplicates')
    assert response.status_code == 200
    data = response.get_json()
    assert data['stats'] == {
        'rules_checked': 1, 'rulesets_checked': 2,
        'failed_rulesets': ['Empty'], 'duplicate_groups': 2,
    }
    domain, ipv6 = data['duplicates']
    assert (domain['rule_type'], domain['count'], domain['policy_conflict']) == ('DOMAIN-SUFFIX', 4, True)
    assert [item['priority'] for item in domain['occurrences']] == [1, 2, 2, 3]
    assert [item.get('line_no') for item in domain['occurrences']] == [None, 2, 3, 1]
    assert (ipv6['value'], ipv6['count'], ipv6['policy_conflict']) == ('2001:db8::/32', 2, True)
    # Cached sources reuse the request snapshot rather than resolving the full
    # configuration again for every source file.
    assert len(config_reads) == 1
    assert 'Duplicate scan ' in caplog.text
    assert 'scanned ruleset "A": 4 entries' in caplog.text
    assert 'matched: 2 duplicate groups' in caplog.text
    assert 'response ready: status=200 bytes=' in caplog.text


def test_cached_and_inline_sources_do_not_load_download_configuration(duplicate_app, monkeypatch):
    app, _ = duplicate_app

    def unexpected_config_read():
        pytest.fail('cached/inline content should not reload the configuration')

    monkeypatch.setattr(rules, 'get_config', unexpected_config_read)
    with app.test_request_context('/api/rules'):
        assert 'Example.com' in rules.get_ruleset_content({'name': 'A', 'source_type': 'content'})
        assert rules.get_ruleset_content({}, {
            'name': 'Inline', 'source_type': 'content', 'content': 'DOMAIN,example.com',
        }) == 'DOMAIN,example.com'


def test_compact_scan_keeps_first_case_stable_ties_and_complete_source_details(duplicate_app, monkeypatch):
    app, _ = duplicate_app
    config = {
        'rule_configs': [
            {'id': 'set-a', 'name': 'A', 'itemType': 'ruleset', 'library_rule_id': 'a', 'policy': 'DIRECT'},
            {'id': 'direct', 'itemType': 'rule', 'rule_type': 'DOMAIN', 'value': 'Conflict.Test', 'policy': 'DIRECT'},
            {'id': 'set-b', 'name': 'B', 'itemType': 'ruleset', 'library_rule_id': 'b', 'policy': 'Proxy'},
            {'id': 'disabled', 'itemType': 'rule', 'rule_type': 'DOMAIN', 'value': 'First.Test', 'enabled': False},
        ],
        'rule_library': [{'id': 'a'}, {'id': 'b'}],
    }
    contents = {
        'a': '\n'.join([
            '# skipped', '  - DOMAIN,First.Test # first spelling  ', 'DOMAIN,Second.Test',
            'REGEX,^Case$', 'REGEXP,^Case$', 'DOMAIN,Three.Test',
            'DOMAIN,three.test', 'DOMAIN,three.test',
            'DOMAIN,second.test', 'DOMAIN,first.test', 'DOMAIN-SUFFIX,first.test',
        ]),
        'b': 'DOMAIN,conflict.test\nREGEX,^case$\nREGEXP,^Case$\n',
    }
    monkeypatch.setattr(rules, 'get_config', lambda: config)
    monkeypatch.setattr(rules, 'get_ruleset_content', lambda _item, library, **_kwargs: contents[library['id']])
    data = app.test_client().post('/api/rules/find-duplicates').get_json()
    assert data['success'] is True
    assert data['stats'] == {'rules_checked': 1, 'rulesets_checked': 2, 'failed_rulesets': [], 'duplicate_groups': 5}
    assert [(group['rule_type'], group['value'], group['count'], group['policy_conflict'])
            for group in data['duplicates']] == [
        ('REGEXP', '^Case$', 2, True), ('DOMAIN', 'Conflict.Test', 2, True),
        ('DOMAIN', 'Three.Test', 3, False), ('DOMAIN', 'First.Test', 2, False),
        ('DOMAIN', 'Second.Test', 2, False),
    ]
    first = data['duplicates'][3]
    assert first['occurrences'] == [
        {'source_type': 'ruleset', 'source': 'A', 'rule_id': 'set-a', 'policy': 'DIRECT', 'priority': 1,
         'line': '- DOMAIN,First.Test # first spelling', 'line_no': 2},
        {'source_type': 'ruleset', 'source': 'A', 'rule_id': 'set-a', 'policy': 'DIRECT', 'priority': 1,
         'line': 'DOMAIN,first.test', 'line_no': 10},
    ]
    direct = data['duplicates'][1]['occurrences'][0]
    assert direct == {
        'source_type': 'rule', 'source': '直接配置的规则', 'rule_id': 'direct', 'policy': 'DIRECT',
        'priority': 2, 'line': 'DOMAIN,Conflict.Test',
    }


def test_repeated_library_references_keep_each_policy_and_priority(duplicate_app, monkeypatch):
    app, _ = duplicate_app
    config = {'rule_library': [{'id': 'a'}], 'rule_configs': [
        {'id': 'first', 'name': 'First', 'itemType': 'ruleset', 'library_rule_id': 'a', 'policy': 'DIRECT'},
        {'id': 'second', 'name': 'Second', 'itemType': 'ruleset', 'library_rule_id': 'a', 'policy': 'Proxy'},
    ]}
    monkeypatch.setattr(rules, 'get_config', lambda: config)
    monkeypatch.setattr(rules, 'get_ruleset_content', lambda *_args, **_kwargs: 'DOMAIN,shared.test')
    data = app.test_client().post('/api/rules/find-duplicates').get_json()
    group, = data['duplicates']
    assert group['count'] == 2 and group['policy_conflict'] is True
    assert [(item['rule_id'], item['source'], item['policy'], item['priority'], item['line_no'])
            for item in group['occurrences']] == [
        ('first', 'First', 'DIRECT', 1, 1), ('second', 'Second', 'Proxy', 2, 1),
    ]


@pytest.mark.parametrize('content', [
    '', '# comment\npayload:\n', '\n'.join(f'DOMAIN,node-{i}.test' for i in range(2000)),
], ids=['missing', 'comments', 'unique'])
def test_empty_and_unique_sources_return_no_duplicates(duplicate_app, monkeypatch, content):
    app, _ = duplicate_app
    config = {'rule_library': [{'id': 'a'}], 'rule_configs': [
        {'id': 'set-a', 'name': 'A', 'itemType': 'ruleset', 'library_rule_id': 'a'},
    ]}
    monkeypatch.setattr(rules, 'get_config', lambda: config)
    monkeypatch.setattr(rules, 'get_ruleset_content', lambda *_args, **_kwargs: content)
    data = app.test_client().post('/api/rules/find-duplicates').get_json()
    assert data['success'] is True and data['duplicates'] == []
    assert data['stats'] == {
        'rules_checked': 0, 'rulesets_checked': int(bool(content)),
        'failed_rulesets': [] if content else ['A'], 'duplicate_groups': 0,
    }


def test_duplicate_response_keeps_central_credential_redaction(duplicate_app, monkeypatch):
    app, repository = duplicate_app
    secret = 'duplicate-scan-private-capability'
    repository.save_system({'system_config': {'rule_proxy_token': secret}})
    config = {'rule_library': [], 'rule_configs': [
        {'id': str(i), 'itemType': 'rule', 'rule_type': 'DOMAIN', 'value': secret}
        for i in range(2)
    ]}
    monkeypatch.setattr(rules, 'get_config', lambda: config)
    response = app.test_client().post('/api/rules/find-duplicates')
    group, = response.get_json()['duplicates']
    assert group['value'] == '[REDACTED]'
    assert [item['line'] for item in group['occurrences']] == ['[REDACTED]', '[REDACTED]']
    assert secret not in response.get_data(as_text=True)
    assert config['rule_configs'][0]['value'] == secret


def test_dense_duplicate_group_releases_compact_positions_while_materializing():
    """A large duplicate group must not retain both full and compact records."""
    import gc
    import tracemalloc

    count = 20000
    gc.collect()
    tracemalloc.start()
    try:
        entry = rules._DuplicateOccurrences('Shared.Test', (
            'ruleset', 'A', 'set-a', 'DIRECT', 1, 'DOMAIN,Shared.Test', 1,
        ))
        for line_no in range(2, count + 1):
            entry.add(('ruleset', 'A', 'set-a', 'DIRECT', 1, 'DOMAIN,shared.test', line_no))
        result = entry.result('DOMAIN')
        del entry
        gc.collect()
        retained, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    # Retaining the old tuples and a second list used over 100 extra bytes per
    # result. Leave headroom for interpreter bookkeeping without accepting that
    # second representation of every position.
    assert peak - retained < count * 40
    assert result['count'] == count and result['value'] == 'Shared.Test'
    assert result['policy_conflict'] is False
    assert len(result['occurrences']) == count
    assert [item['line_no'] for item in result['occurrences']] == list(range(1, count + 1))
    assert result['occurrences'][0]['line'] == 'DOMAIN,Shared.Test'
    assert result['occurrences'][-1]['line'] == 'DOMAIN,shared.test'
