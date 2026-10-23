"""Router split marker, full TUN base YAML and legacy takeover detection."""
from copy import deepcopy

import pytest
import yaml

from backend.common import setup_flow
from backend.common.config_repository import ProfileRepository
from backend.common.linux_gateway import LinuxGatewayError, validate_gateway_selection
from backend.common.router_split import (
    FULL_TUN, RouterSplitError, check_generation, full_tun_base_yaml, full_tun_document, is_full_tun,
    is_full_tun_document, legacy_context, mode_from_context, read_marker,
    require_full_tun_generated, route_rule_set_ids, router_split_state,
)

MARKER = {'version': 1, 'takeover': 'full_tun', 'route_rule_set_ids': ['a', 'b', 'a'], 'origin': 'setup'}


def config(marker=MARKER, gateway=None, custom_config=None):
    mihomo = {'custom_config': full_tun_base_yaml('mixed-port: 7890\n') if custom_config is None else custom_config}
    if marker is not None:
        mihomo['router_split'] = deepcopy(marker)
    if gateway is not None:
        mihomo['linux_gateway'] = gateway
    return {'mihomo': mihomo}


def test_valid_marker_is_normalized_and_full_tun():
    state, marker = read_marker(config())
    assert state == 'valid'
    assert marker['route_rule_set_ids'] == ['a', 'b']
    assert is_full_tun(config()) and route_rule_set_ids(config()) == ['a', 'b']


def test_absent_marker():
    assert read_marker({})[0] == 'absent'
    assert read_marker(config(None)) == ('absent', None)
    assert not is_full_tun(config(None)) and route_rule_set_ids(config(None)) == []


@pytest.mark.parametrize('change', [
    {'version': True}, {'version': 2}, {'version': '1'}, {'extra': 1}, {'takeover': 'selective'},
    {'origin': 'manual'}, {'origin': 'migration'}, {'route_rule_set_ids': 'a'}, {'route_rule_set_ids': ['']},
    {'route_rule_set_ids': ['x\n']}, {'route_rule_set_ids': ['x' * 257]}, {'route_rule_set_ids': [1]},
    {'route_rule_set_ids': ['r'] * 1025}, {'migration_id': 'f' * 32},
])
def test_invalid_marker_fails_closed(change):
    marker = {**MARKER, **change}
    assert read_marker(config(marker)) == ('invalid', None)
    assert not is_full_tun(config(marker)) and route_rule_set_ids(config(marker)) == []
    with pytest.raises(RouterSplitError) as error:
        check_generation(config(marker))
    assert error.value.code == 'ROUTER_SPLIT_INVALID' and error.value.status == 409
    assert isinstance(error.value, LinuxGatewayError)


@pytest.mark.parametrize('marker', [
    {key: value for key, value in MARKER.items() if key != 'origin'}, None, [],
    {**MARKER, 'migrated_at': '2026-10-23T08:00:00+00:00'},
])
def test_incomplete_or_malformed_marker_is_invalid(marker):
    assert read_marker({'mihomo': {'router_split': marker}})[0] == 'invalid'


def test_generation_check_rejects_gateway_with_full_tun_only():
    check_generation(config(None, {'enabled': True, 'rule_set_ids': []}))
    check_generation(config(gateway={'enabled': False, 'rule_set_ids': []}))
    with pytest.raises(RouterSplitError) as error:
        check_generation(config(gateway={'enabled': True, 'rule_set_ids': []}))
    assert error.value.code == 'ROUTER_SPLIT_FULL_TUN'


def test_full_tun_document_on_empty_and_existing_tun():
    assert full_tun_document({}) == {'tun': FULL_TUN}
    assert list(full_tun_document({})['tun']) == list(FULL_TUN)
    source = {'tun': {'stack': 'gvisor', 'device': 'utun', 'route-address-set': ['x'], 'route-address': ['1.0.0.0/8'],
                      'inet4-route-address': [], 'inet6-route-address': [], 'route-exclude-address-set': ['y'],
                      'route-exclude-address': ['192.168.1.2/32'], 'dns-hijack': [], 'enable': False}}
    result = full_tun_document(source)
    assert result['tun'] == {'stack': 'gvisor', 'device': 'utun', 'route-exclude-address': ['192.168.1.2/32'],
                             'dns-hijack': ['any:53', 'tcp://any:53'], 'enable': True, 'auto-route': True,
                             'auto-redirect': True, 'auto-detect-interface': True}
    assert source['tun']['enable'] is False  # The input is not changed.
    assert full_tun_document(result) == result


def test_full_tun_document_rejects_filters_and_non_object_tun():
    with pytest.raises(LinuxGatewayError, match='tun.include-interface'):
        full_tun_document({'tun': {'include-interface': ['eth0']}})
    assert full_tun_document({'tun': {'include-interface': []}})['tun']['enable'] is True
    with pytest.raises(LinuxGatewayError, match='tun 必须是对象'):
        full_tun_document({'tun': ['enable']})


def test_full_tun_base_yaml_is_idempotent_and_uses_default_template():
    text = full_tun_base_yaml('')
    document = yaml.safe_load(text)
    assert document['tun'] == FULL_TUN and document['mixed-port'] == 7890
    assert full_tun_base_yaml(text) == text
    assert yaml.safe_load(full_tun_base_yaml('# 名称\nmode: global\n'))['mode'] == 'global'
    for broken in ('[1, 2]', 'a: [', 1):
        with pytest.raises(LinuxGatewayError):
            full_tun_base_yaml(broken)


@pytest.mark.parametrize('change, expected', [
    ({}, True),
    ({'tun': None}, False),
    ({'tun': 'on'}, False),
    ({'tun': {**FULL_TUN, 'enable': False}}, False),
    ({'tun': {**FULL_TUN, 'auto-route': 'true'}}, False),
    ({'tun': {key: value for key, value in FULL_TUN.items() if key != 'auto-redirect'}}, False),
    ({'tun': {**FULL_TUN, 'dns-hijack': ['tcp://any:53']}}, False),
    ({'tun': {**FULL_TUN, 'dns-hijack': 'any:53'}}, False),
    ({'tun': {**FULL_TUN, 'route-address-set': ['cf_gateway_fake_ip']}}, False),
    ({'tun': {**FULL_TUN, 'route-address': []}}, False),
    ({'tun': {**FULL_TUN, 'route-exclude-address': ['192.168.1.2/32'], 'device': 'Meta'}}, True),
    ({'x-configflow-linux-gateway': {'enabled': False}}, False),
])
def test_full_tun_document_truth_table(change, expected):
    document = {'tun': deepcopy(FULL_TUN), **change}
    assert is_full_tun_document(document) is expected
    if expected:
        require_full_tun_generated(document)
    else:
        with pytest.raises(ValueError, match='完整 TUN'):
            require_full_tun_generated(document)
    assert is_full_tun_document(None) is False


def test_rule_set_id_validation_is_shared_with_gateway_selection():
    from backend.common.linux_gateway import validate_rule_set_ids
    profile = {'mihomo': {'custom_config': 'mixed-port: 7890\n'}, 'rule_configs': [
        {'id': 'ip', 'itemType': 'ruleset', 'behavior': 'ipcidr', 'source_type': 'content', 'content': '1.1.1.0/24'},
        {'id': 'domain', 'itemType': 'ruleset', 'behavior': 'domain'}]}
    assert validate_rule_set_ids(profile, ['ip'], set()) == ['ip']
    assert validate_rule_set_ids(profile, ['domain', 'gone'], {'domain', 'gone'}) == ['domain', 'gone']
    for ids in (['domain'], ['gone']):
        with pytest.raises(LinuxGatewayError):
            validate_rule_set_ids(profile, ids, set())
        with pytest.raises(LinuxGatewayError):
            validate_gateway_selection(profile, {'enabled': True, 'rule_set_ids': ids})


@pytest.fixture
def repository(tmp_path):
    repository = ProfileRepository(tmp_path)
    for profile_id in ('home', 'other'):
        repository.create_profile({'id': profile_id, 'name': profile_id})
    repository.update_system_transaction(lambda system: system.update(agents=[
        {'id': 'mihomo-home', 'name': 'm', 'profile_id': 'home', 'service_type': 'mihomo'},
        {'id': 'oxidns-home', 'name': 'o', 'profile_id': 'home', 'service_type': 'oxidns'},
        {'id': 'mihomo-other', 'name': 'm2', 'profile_id': 'other', 'service_type': 'mihomo'}]))
    return repository


def session(repository, goal, profile_id='home', index=0, completed=False):
    record = {'id': f'{index:032x}', 'profile_id': profile_id, 'name': goal, 'goal': goal, 'step': 3,
              'max_step': 3, 'prepared': True, 'completed': completed}
    setup_flow.write(repository, record)
    return record


def gateway_profile(routers=()):
    return {'mihomo': {'custom_config': 'mixed-port: 7890\n', 'linux_gateway': {'enabled': True, 'rule_set_ids': ['r']}},
            'routers': {'items': [{'id': f'router-{agent}', 'proxy_gateway_agent_id': agent} for agent in routers]}}


def test_marker_wins_and_reports_issues(repository):
    session(repository, 'gateway')
    state = router_split_state(repository, 'home', config())
    assert state['mode'] == 'full_tun' and state['source'] == 'marker' and state['issues'] == []
    assert state['mihomo_agent_ids'] == ['mihomo-home']
    broken = config(gateway={'enabled': True, 'rule_set_ids': []}, custom_config='mixed-port: 7890\n')
    assert router_split_state(repository, 'home', broken)['issues'] == ['tun_missing', 'gateway_enabled']
    assert router_split_state(repository, 'home', config(custom_config='a: ['))['issues'] == ['tun_missing']
    assert router_split_state(repository, 'home', config({**MARKER, 'version': 0}))['mode'] == 'invalid'


def test_router_session_with_enabled_gateway_is_legacy(repository):
    record = session(repository, 'router')
    state = router_split_state(repository, 'home', gateway_profile())
    assert (state['mode'], state['source'], state['setup_session_id']) == ('legacy', 'setup', record['id'])
    # Without the enabled gateway nothing is taken over.
    assert router_split_state(repository, 'home', {'mihomo': {}})['mode'] is None
    assert router_split_state(repository, 'home', {'mihomo': {'linux_gateway': {'enabled': False}}})['mode'] is None


def test_gateway_session_wins_over_topology(repository):
    session(repository, 'gateway')
    state = router_split_state(repository, 'home', gateway_profile(['mihomo-home']))
    assert state['mode'] is None and state['router_ids'] == ['router-mihomo-home']


def test_topology_only_counts_this_profiles_mihomo(repository):
    state = router_split_state(repository, 'home', gateway_profile(['mihomo-home']))
    assert (state['mode'], state['source']) == ('legacy', 'topology')
    assert router_split_state(repository, 'home', gateway_profile(['mihomo-other']))['mode'] is None
    assert router_split_state(repository, 'home', gateway_profile(['oxidns-home']))['mode'] is None
    assert router_split_state(repository, 'home', gateway_profile())['mode'] is None


def test_latest_session_of_the_profile_decides(repository):
    session(repository, 'router', profile_id='other', index=1)
    assert router_split_state(repository, 'home', gateway_profile())['mode'] is None
    context = legacy_context(repository, 'home')
    assert context['session_goal'] is None and context['mihomo_agent_ids'] == ['mihomo-home']
    session(repository, 'router', index=2, completed=True)
    context = legacy_context(repository, 'home')
    assert (context['session_goal'], context['session_completed']) == ('router', True)
    assert mode_from_context(gateway_profile(), context) == 'legacy'
    # The decision inside a transaction reads only the locked profile and context.
    assert mode_from_context(config(), context) == 'full_tun'


def test_abandoned_sessions_no_longer_decide(repository):
    """An abandoned wizard is not the profile's purpose, as on the Setup page."""
    record = session(repository, 'router', index=3)
    setup_flow.write(repository, {**record, 'abandoned': True})
    context = legacy_context(repository, 'home')
    assert context['session_goal'] is None and context['session_id'] is None
    assert router_split_state(repository, 'home', gateway_profile())['mode'] is None
    # An abandoned gateway session no longer hides the topology either.
    gateway = session(repository, 'gateway', index=4)
    setup_flow.write(repository, {**gateway, 'abandoned': True})
    assert router_split_state(repository, 'home', gateway_profile(['mihomo-home']))['mode'] == 'legacy'


def test_base_yaml_with_its_own_full_tun_is_not_a_legacy_takeover(repository):
    """Turning the gateway off keeps the base YAML's full TUN, so nothing ends."""
    profile = gateway_profile(['mihomo-home'])
    profile['mihomo']['custom_config'] = full_tun_base_yaml('mixed-port: 7890\n')
    assert router_split_state(repository, 'home', profile)['mode'] is None
    session(repository, 'router', index=5)
    assert router_split_state(repository, 'home', profile)['mode'] is None
    # A TUN restricted to route addresses is not the full TUN.
    profile['mihomo']['custom_config'] = 'tun: {enable: true, auto-route: true, auto-redirect: true, dns-hijack: [any:53], route-address: [198.18.0.0/16]}\n'
    assert router_split_state(repository, 'home', profile)['mode'] == 'legacy'


def test_session_reads_never_wait_for_a_wizard_step(repository):
    """A step holding its session lock (a slow Agent check) must not block the editors."""
    import threading
    record = session(repository, 'router', index=6)
    # Not session records: skipped instead of failing every profile's editor.
    root = repository.data_dir / 'setup-sessions'
    (root / 'notes.json').write_text('{}', encoding='utf-8')
    (root / ('f' * 32 + '.json')).write_text('[]', encoding='utf-8')
    done = threading.Event()
    with setup_flow.locked(repository, record['id']):
        worker = threading.Thread(target=lambda: (legacy_context(repository, 'home'), done.set()))
        worker.start()
        assert done.wait(5), 'legacy_context waited for the session lock'
        worker.join()
    assert legacy_context(repository, 'home')['session_id'] == record['id']


def test_stored_profile_is_read_when_no_config_is_given(repository):
    repository.save_profile('home', {'mihomo': config()['mihomo']})
    assert router_split_state(repository, 'home')['mode'] == 'full_tun'


def test_clone_keeps_marker_but_not_routers(repository):
    repository.save_profile('home', {'mihomo': config()['mihomo'], 'routers': {'items': [
        {'id': 'r1', 'name': 'Main', 'type': 'ikuai', 'endpoint': 'http://192.0.2.1', 'username': 'admin',
         'password': 'p', 'proxy_gateway_agent_id': 'mihomo-home'}]}})
    repository.clone_profile('home', {'id': 'copy', 'name': 'copy'})
    clone = repository.get_profile('copy')
    assert clone['mihomo']['router_split'] == MARKER and clone['routers'] == {'items': []}
    assert router_split_state(repository, 'copy', clone)['mode'] == 'full_tun'


def test_exports_and_imports_keep_marker(repository):
    repository.save_profile('home', {'mihomo': config()['mihomo']})
    exported = repository.export_profile('home')
    repository.import_profile('other', {'mihomo': exported['mihomo']})
    assert repository.get_profile('other')['mihomo']['router_split'] == MARKER
    repository.import_all(repository.export_all())
    assert repository.get_profile('home')['mihomo']['router_split'] == MARKER
    redacted = repository.export_all(desensitize=True)['profiles']['home']
    assert redacted['mihomo']['router_split'] == MARKER and redacted['mihomo']['custom_config'] == ''
    state = router_split_state(repository, 'home', redacted)
    assert state['mode'] == 'full_tun' and state['issues'] == ['tun_missing']


@pytest.mark.parametrize('version, gateway, forwarding', [
    ('1.8.25-go', True, False), ('1.8.39-go', True, False), ('1.8.40-go', True, True), ('1.9.0', True, True),
    ('1.8.40-beta', False, False), ('', False, False), (None, False, False),
])
def test_forwarding_agent_version_gate(version, gateway, forwarding):
    from backend.agents.linux_gateway import FORWARDING_MIN_AGENT_VERSION, supports_forwarding, supports_gateway
    assert FORWARDING_MIN_AGENT_VERSION == '1.8.40-go'
    assert supports_gateway({'version': version}) is gateway
    assert supports_forwarding({'version': version}) is forwarding


def test_frontend_keeps_the_same_router_split_copy():
    """The editor falls back to its own copy only when a response has no message."""
    import re
    from pathlib import Path
    from backend.agents.linux_gateway import FORWARDING_MIN_AGENT_VERSION
    from backend.common.router_split import LEGACY_RESTORE_BLOCKED, MESSAGES
    source = (Path(__file__).resolve().parent.parent / 'frontend/src/lib/routerSplit.ts').read_text(encoding='utf-8')
    frontend = dict(re.findall(r"^\s+(ROUTER_SPLIT_[A-Z_]+): '([^']*)',$", source, re.MULTILINE))
    assert frontend == MESSAGES
    assert f"LEGACY_RESTORE_BLOCKED = '{LEGACY_RESTORE_BLOCKED}'" in source
    assert f"FORWARDING_AGENT_VERSION = '{FORWARDING_MIN_AGENT_VERSION}'" in source
