"""Invalid rule syntax is rejected atomically at every handwritten save entry."""
from copy import deepcopy

import pytest

from backend.test_rule_compose_route import space, HEADERS, compose, values  # noqa: F401


@pytest.mark.parametrize('kind,value', [
    ('DOMAIN-SHUFFIX', 'tmdb.org'), ('domain-shuffix', 'tmdb.org'),
    ('DOMAIN', ''), ('DOMAIN-SUFFIX', '+.tmdb.org'), ('DOMAIN', '999.999.999.999'),
    ('IP-CIDR', '+.tmdb.org'), ('IP-CIDR', '192.0.2.1/33'), ('IP-CIDR6', ':::/64'),
    ('DST-PORT', 'abc'), ('SRC-PORT', '65536'),
])
@pytest.mark.parametrize('entry', ['create', 'edit', 'compose', 'batch'])
def test_invalid_type_or_value_does_not_write_any_entry(space, kind, value, entry):
    client, repo = space
    before = deepcopy(repo.export_all())
    if entry == 'create':
        result = client.post('/api/rules', headers=HEADERS, json={
            'id': 'new', 'itemType': 'rule', 'rule_type': kind, 'value': value, 'policy': 'DIRECT'})
    elif entry == 'edit':
        result = client.put('/api/rules/inline', headers=HEADERS, json={'rule_type': kind, 'value': value})
    elif entry == 'compose':
        result = compose(client, values(f'{kind},{value}'))
    else:
        result = client.post('/api/rules/batch', headers=HEADERS, json={
            'rule_type': kind, 'domains': [value], 'policy': 'DIRECT'})
    assert result.status_code == 400, result.get_json()
    assert result.get_json()['message']
    assert repo.export_all() == before


def test_batch_validation_is_atomic_and_legacy_metadata_edits_remain_possible(space):
    client, repo = space
    before = deepcopy(repo.export_all())
    result = client.post('/api/rules/batch', headers=HEADERS, json={
        'rule_type': 'IP-CIDR', 'domains': ['192.0.2.0/24', '+.tmdb.org'], 'policy': 'DIRECT'})
    assert result.status_code == 400 and '2' in result.get_json()['message']
    assert repo.export_all() == before
    repo.update_profile_transaction('other', lambda profile: profile['rule_configs'][1].update(rule_type='DOMAIN-SHUFFIX'))
    result = client.put('/api/rules/inline', headers=HEADERS, json={'enabled': False, 'remark': '待修复'})
    assert result.status_code == 200, result.get_json()
    item = next(item for item in repo.get_profile('other')['rule_configs'] if item['id'] == 'inline')
    assert item['rule_type'] == 'DOMAIN-SHUFFIX' and not item['enabled']
    assert client.put('/api/rules/inline', headers=HEADERS, json={
        'rule_type': 'DOMAIN-SUFFIX', 'value': 'tmdb.org'}).status_code == 200


@pytest.mark.parametrize('kind,value', [
    ('MATCH', ''), ('DOMAIN-SUFFIX', 'tmdb.org'), ('IP-CIDR', '2001:db8::/32'),
    ('IP-CIDR6', '192.0.2.0/24'), ('DOMAIN-REGEX', r'^node[0-9]{1,3}\.test$'),
    ('DST-PORT', '80/443/8000-9000'), ('PROCESS-NAME', 'browser'),
])
def test_supported_single_rule_syntax_remains_available(space, kind, value):
    client, repo = space
    result = client.post('/api/rules', headers=HEADERS, json={
        'id': 'new', 'itemType': 'rule', 'rule_type': kind, 'value': value, 'policy': 'DIRECT'})
    assert result.status_code == 200, result.get_json()
    assert repo.get_profile('other')['rule_configs'][0]['value'] == value


@pytest.mark.parametrize('behavior,content', [
    ('ipcidr', '+.tmdb.org'), ('ipcidr', 'DOMAIN-SUFFIX,tmdb.org'),
    ('ipcidr', '192.0.2.1'), ('ipcidr', 'IP-CIDR,192.0.2.0/24'),
    ('ipcidr', '2001:db8::/129'), ('domain', '192.0.2.0/24'),
    ('domain', '999.999.999.999'), ('classical', 'DOMAIN-SHUFFIX,tmdb.org'),
    ('classical', 'DOMAIN-SUFFIX,'), ('classical', 'IP-CIDR,+.tmdb.org'),
    ('domain-shuffix', 'tmdb.org'), ('ip-cidr', '192.0.2.0/24'),
])
@pytest.mark.parametrize('entry', ['create', 'edit', 'draft', 'compose'])
def test_ruleset_behavior_content_mismatch_is_rejected_at_save(space, behavior, content, entry):
    client, repo = space
    rule = {'id': 'bad', 'name': 'Bad', 'source_type': 'content',
            'behavior': behavior, 'format': 'text', 'content': content}
    if entry == 'draft':
        # Simulate a legacy source to exercise revision-checked draft updates.
        repo.update_shared_transaction(lambda shared: shared['rule_library'][0].update(
            behavior=behavior, format='text', content='# old source'))
    before = deepcopy(repo.export_all())
    if entry == 'create':
        result = client.post('/api/rule-library', json=rule)
    elif entry == 'edit':
        result = client.put('/api/rule-library/ads', json=rule)
    elif entry == 'draft':
        state = client.get('/api/rule-library/ads/draft-content').get_json()
        result = client.put('/api/rule-library/ads/draft-content', json={
            'revision': state['revision'], 'content': content})
    else:
        result = compose(client, {'kind': 'new_library', 'library': rule})
    assert result.status_code == 400, result.get_json()
    assert repo.export_all() == before


def test_ipcidr_check_reports_each_bad_line_and_valid_yaml_saves_unchanged(space):
    client, repo = space
    content = '# nets\n192.0.2.0/24\n+.tmdb.org\n:::/64\n'
    result = client.post('/api/rule-library/check-content', json={'behavior': 'ipcidr', 'content': content})
    assert result.status_code == 200
    assert result.json['content'] == content
    assert [item['line'] for item in result.json['errors']] == [3, 4]
    valid = '"payload": ["192.0.2.0/24", "2001:db8::/32"]\n'
    rule = {'id': 'nets', 'name': 'Nets', 'source_type': 'content', 'behavior': 'ipcidr', 'format': 'yaml', 'content': valid}
    assert client.post('/api/rule-library', json=rule).status_code == 200
    assert next(item for item in repo.get_shared()['rule_library'] if item['id'] == 'nets')['content'] == valid
    import yaml
    delivered = client.get('/api/rules/local/Nets?target=mihomo').data.decode()
    assert yaml.safe_load(delivered) == yaml.safe_load(valid)
    assert not yaml.compose(delivered).value[0][1].flow_style


@pytest.mark.parametrize('kind,payload', [
    ('DOMAIN-REGEX', r'^node[0-9]{1,3}\.test$'),
    ('AND', '((DOMAIN,tmdb.org),(NETWORK,tcp))'),
])
def test_compose_preserves_commas_inside_regex_or_logical_payloads(space, kind, payload):
    client, repo = space
    result = compose(client, values(f'{kind},{payload}'))
    assert result.status_code == 200, result.get_json()
    rule = result.get_json()['data']['rules'][0]
    assert rule['rule_type'] == kind and rule['value'] == payload
    assert not result.get_json()['warnings']


def test_pasted_match_has_no_payload_and_uses_selected_policy(space):
    client, repo = space
    result = compose(client, values('MATCH,REJECT'))
    assert result.status_code == 200, result.get_json()
    item = result.json['data']['rules'][0]
    assert item['rule_type'] == 'MATCH' and item['value'] == '' and item['policy'] == 'DIRECT'
    assert any('REJECT' in warning for warning in result.json['warnings'])


@pytest.mark.parametrize('kind,value', [
    ('DST-PORT', '80,443'), ('DOMAIN-KEYWORD', 'foo,bar'), ('PROCESS-NAME', 'app,DIRECT'),
])
@pytest.mark.parametrize('entry', ['create', 'edit', 'batch'])
def test_direct_value_commas_cannot_be_emitted_as_target_fields(space, kind, value, entry):
    client, repo = space
    before = deepcopy(repo.export_all())
    if entry == 'create':
        result = client.post('/api/rules', headers=HEADERS, json={
            'id': 'new', 'itemType': 'rule', 'rule_type': kind, 'value': value, 'policy': 'DIRECT'})
    elif entry == 'edit':
        result = client.put('/api/rules/inline', headers=HEADERS, json={'rule_type': kind, 'value': value})
    else:
        result = client.post('/api/rules/batch', headers=HEADERS, json={
            'rule_type': kind, 'domains': [value], 'policy': 'DIRECT'})
    assert result.status_code == 400, result.get_json()
    assert '逗号' in result.get_json()['message']
    assert repo.export_all() == before
