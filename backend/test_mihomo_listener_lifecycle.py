"""Managed group edits keep native inbound references valid and explicit."""
from copy import deepcopy

import pytest
import yaml
from flask import Flask

from backend.common import config
from backend.common.config_repository import ProfileRepository
from backend.converters.mihomo import generate_mihomo_config
from backend.routes import register_blueprints
from backend.utils.mihomo_listeners import ListenerReferenceError, relink_group_listeners


@pytest.fixture
def environment(tmp_path):
    previous = config._repository
    repo = ProfileRepository(tmp_path)
    config.set_repository(repo)
    repo.update_profile_transaction('default', lambda profile: profile.update(
        proxy_groups=[{'id': 'g', 'name': 'Pinned', 'type': 'select', 'manual_nodes': ['DIRECT']}],
        rule_configs=[],
        mihomo={'custom_config': yaml.safe_dump({
            'dns': {'enable': False}, 'future-setting': {'keep': False},
            'listeners': [
                {'name': 'pinned', 'type': 'mixed', 'listen': '127.0.0.1', 'port': 17890,
                 'proxy': 'Pinned', 'rule': 'lan-rules', 'future-field': ['preserved']},
                {'name': 'direct', 'type': 'mixed', 'port': 17891, 'proxy': 'DIRECT'},
            ],
            'sub-rules': {'lan-rules': ['MATCH,DIRECT']},
        })},
    ))
    app = Flask(__name__)
    register_blueprints(app)
    yield repo, app.test_client()
    config.set_repository(previous)


@pytest.mark.parametrize('action', ['delete', 'disable', 'rename'])
def test_group_edit_relinks_only_fixed_inbounds_in_same_transaction(environment, action):
    repo, client = environment
    before = yaml.safe_load(repo.get_profile('default')['mihomo']['custom_config'])
    response = (client.delete('/api/proxy-groups/g') if action == 'delete' else
                client.put('/api/proxy-groups/g', json={'enabled': False} if action == 'disable' else
                           {'name': 'Renamed'}))
    assert response.status_code == 200, response.json
    assert response.json['listener_changes'] == ['listeners[0].proxy']
    assert ('恢复各自的规则路由' if action != 'rename' else '固定策略名称') in response.json['message']
    profile = repo.get_compat_config('default')
    actual = yaml.safe_load(profile['mihomo']['custom_config'])
    expected = deepcopy(before)
    if action == 'rename':
        expected['listeners'][0]['proxy'] = 'Renamed'
    else:
        expected['listeners'][0].pop('proxy')
    assert actual == expected
    emitted = yaml.safe_load(generate_mihomo_config(profile, preflight_providers=False))
    assert emitted['listeners'] == expected['listeners']
    assert emitted['sub-rules'] == before['sub-rules']


def test_other_references_still_block_deletion_without_changing_listener(environment):
    repo, client = environment
    repo.update_profile_transaction('default', lambda profile: profile['rule_configs'].append(
        {'id': 'rule', 'itemType': 'rule', 'rule_type': 'MATCH', 'policy': 'Pinned'}))
    before = repo.path.read_bytes()
    assert client.delete('/api/proxy-groups/g').status_code == 409
    assert repo.path.read_bytes() == before


@pytest.mark.parametrize('source', [
    'listeners: [', 'listeners: {proxy: Pinned}',
    'listeners: [{name: custom, proxy: "${FIXED_POLICY}"}]',
])
def test_uncertain_native_config_never_partially_deletes_group(environment, source):
    repo, client = environment
    repo.update_profile_transaction('default', lambda profile: profile['mihomo'].update(custom_config=source))
    before = repo.path.read_bytes()
    response = client.delete('/api/proxy-groups/g')
    assert response.status_code == 400
    assert repo.path.read_bytes() == before


@pytest.mark.parametrize('target', ['Missing', '${FIXED_POLICY}', ' Pinned '])
def test_generation_rejects_missing_listener_target_without_dialer_topology(environment, target):
    repo, _ = environment
    profile = repo.get_compat_config('default')
    document = yaml.safe_load(profile['mihomo']['custom_config'])
    document['listeners'][0]['proxy'] = target
    profile['mihomo']['custom_config'] = yaml.safe_dump(document)
    with pytest.raises(ListenerReferenceError, match=r'listeners\[0\]\.proxy'):
        generate_mihomo_config(profile, preflight_providers=False)


@pytest.mark.parametrize('target', ['', None, 'DIRECT', 'GLOBAL', 'Pinned'])
def test_generation_keeps_valid_or_unpinned_listener_targets(environment, target):
    repo, _ = environment
    profile = repo.get_compat_config('default')
    document = yaml.safe_load(profile['mihomo']['custom_config'])
    document['listeners'][0]['proxy'] = target
    profile['mihomo']['custom_config'] = yaml.safe_dump(document)
    assert yaml.safe_load(generate_mihomo_config(profile, preflight_providers=False))['listeners'][0]['proxy'] == target


def test_listener_edit_does_not_change_shared_yaml_alias_or_unrelated_document():
    source = 'shared: &base {name: pinned, type: mixed, proxy: Pinned}\nlisteners: [*base]\n'
    profile = {'mihomo': {'custom_config': source}}
    assert relink_group_listeners(profile, 'Pinned') == ['listeners[0].proxy']
    document = yaml.safe_load(profile['mihomo']['custom_config'])
    assert document['shared']['proxy'] == 'Pinned'
    assert 'proxy' not in document['listeners'][0]
    unrelated = {'mihomo': {'custom_config': '# keep this comment\nlisteners: []\n'}}
    original = deepcopy(unrelated)
    assert relink_group_listeners(unrelated, 'Pinned') == []
    assert unrelated == original
