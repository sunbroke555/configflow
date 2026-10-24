"""Health checks through durable profile/shared-source and generated boundaries."""
from copy import deepcopy
import pytest
import yaml
from backend.common.config_repository import ProfileRepository, ProfileValidationError
from backend.converters.mihomo import generate_mihomo_config
from backend.utils.health_check import GROUP_DEFAULTS, PROVIDER_DEFAULTS


def seed(repo):
    def profile(p):
        p['proxy_groups'] = []
        p['rule_configs'] = []
        p['mihomo']['custom_config'] = 'mixed-port: 17899\nmode: rule\n'
    repo.update_profile_transaction('default', profile)
    def shared(s):
        s['subscriptions'] = [{'id':'s','name':'Shared', 'url':'https://example.test/sub', 'health_check':dict(PROVIDER_DEFAULTS)}]
        s['nodes'] = [{'id':'n','name':'Node', 'enabled':True, 'proxy_string':'http://127.0.0.1:19876'}]
        s['subscription_aggregations'] = [{'id':'a','name':'Aggregate','nodes':['n'],'subscriptions':[], 'health_check':{**PROVIDER_DEFAULTS,'lazy':False,'enable':False}}]
        s['rule_library'] = []
    repo.update_shared_transaction(shared)
    def groups(p):
        p['proxy_groups'] = [{'id':'auto','name':'Auto','type':'url-test','enabled':True,'subscriptions':['s'],'aggregations':['a'], **GROUP_DEFAULTS, 'tolerance':50}]
    repo.update_profile_transaction('default', groups)


def test_reopen_clone_export_import_and_shared_impact(tmp_path):
    repo = ProfileRepository(tmp_path / 'source')
    seed(repo)
    repo.clone_profile('default', {'id':'second','name':'Second'})
    assert {u['profile_id'] for u in repo.get_resource_usage('subscriptions','s')} == {'default','second'}
    def change(s):
        s['subscriptions'][0]['health_check'].update(lazy=False, interval=77)
    repo.update_shared_transaction(change)
    reopened = ProfileRepository(tmp_path / 'source')
    for profile in ('default','second'):
        cfg = reopened.get_compat_config(profile)
        assert cfg['subscriptions'][0]['health_check']['lazy'] is False
        assert cfg['subscriptions'][0]['health_check']['interval'] == 77
        assert cfg['proxy_groups'][0]['interval'] == 60
    exported = reopened.export_all()
    target = ProfileRepository(tmp_path / 'target')
    target.import_all(exported)
    assert target.get_compat_config('second')['subscriptions'] == reopened.get_compat_config('second')['subscriptions']
    before = target._store.fingerprint()
    invalid = deepcopy(exported)
    invalid['shared']['subscriptions'][0]['health_check']['lazy'] = 'false'
    with pytest.raises(ProfileValidationError): target.import_all(invalid)
    assert target._store.fingerprint() == before


def test_actual_generation_uses_subscription_and_aggregation_source_truth(tmp_path):
    repo = ProfileRepository(tmp_path)
    seed(repo)
    cfg = repo.get_compat_config('default')
    cfg['mihomo']['custom_config'] += 'proxy-providers:\n  Shared:\n    health-check:\n      interval: 999\n      lazy: true\n'
    before = deepcopy(cfg)
    generated = yaml.safe_load(generate_mihomo_config(cfg, preflight_providers=False))
    assert generated['proxy-providers']['Shared']['health-check'] == PROVIDER_DEFAULTS
    assert generated['proxy-providers']['Aggregate']['health-check']['enable'] is False
    assert generated['proxy-providers']['Aggregate']['health-check']['lazy'] is False
    group = generated['proxy-groups'][0]
    assert group['lazy'] is False
    assert group['timeout'] == 5000
    assert group['tolerance'] == 50
    assert group['expected-status'] == '204'
    assert cfg == before
