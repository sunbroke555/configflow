"""cf-pd prefix guard: parameter derivation, template recognition and settings."""
from pathlib import Path

import pytest

from backend.routers import ipv6_pd_guard as pd
from backend.routers.base import RouterError
from backend.routers.store import RouterValidationError, normalize, public_view

PROD = (Path(__file__).parent / 'testdata' / 'cf-pd-watch-prod.rsc').read_text()
CLIENT = {'.id': '*0', 'interface': 'pppoe-out1', 'pool-name': 'ipv6pool', 'pool-prefix-length': '60',
          'request': 'prefix', 'disabled': 'false', 'status': 'bound', 'prefix': '2001:db8:a000::/60, 23h'}
FACTORY = {'.id': '*1', 'interface': 'pppoe-out1', 'pool-name': 'v6pool', 'request': 'prefix', 'disabled': 'true'}
LAN = {'.id': '*4', 'address': '2001:db8:a000::1/64', 'from-pool': 'ipv6pool', 'interface': 'lan4',
       'advertise': 'true', 'disabled': 'false'}
ULA = {'.id': '*5', 'address': 'fdda::1/64', 'interface': 'lan4', 'advertise': 'true', 'disabled': 'true'}


def live(**changes):
    value = {'clients': [CLIENT, FACTORY], 'addresses': [LAN, ULA], 'scripts': [], 'schedulers': [],
             'ipv4_addresses': None, 'last': [], 'prefixes': None}
    value.update(changes)
    return value


def test_template_is_the_production_script_byte_for_byte():
    assert pd.watch_source('lan4', 'ipv6pool') == PROD
    assert pd.parameters(PROD) == {'lan': 'lan4', 'pool': 'ipv6pool', 'hold': '3600'}


def test_derives_pool_lan_and_hook_client_skipping_the_disabled_factory_client():
    result = pd.derive(live(), '')
    assert result['spec'] == {'lan': 'lan4', 'pool': 'ipv6pool', 'client': '*0',
                              'client_interface': 'pppoe-out1', 'hold': 3600}
    assert result['warnings'] == []


@pytest.mark.parametrize('changes, reason', [
    ({'clients': [FACTORY]}, '没有启用并请求前缀'),
    ({'clients': [CLIENT, {**CLIENT, '.id': '*2', 'interface': 'pppoe-out2'}]}, '多个启用的 IPv6 DHCP 客户端'),
    ({'clients': [{**CLIENT, 'request': 'address'}]}, '没有启用并请求前缀'),
    ({'clients': [{**CLIENT, 'pool-name': ''}]}, '没有设置 pool-name'),
    ({'addresses': [ULA]}, '没有分配给任何 LAN'),
    ({'addresses': [{**LAN, 'disabled': 'true'}]}, '没有分配给任何 LAN'),
    ({'addresses': [LAN, {**LAN, '.id': '*6', 'address': '2001:db8:a000:1::1/64'}]}, '多条来自地址池'),
    ({'addresses': [{**LAN, 'address': '2001:db8:a000::1/62'}]}, '只支持 /64'),
    ({'addresses': [{**LAN, 'advertise': 'false'}]}, '没有开启 advertise'),
    ({'addresses': [{**LAN, 'interface': 'lan"4'}]}, '无法安全使用的字符'),
])
def test_unclear_setups_stop_with_a_reason(changes, reason):
    with pytest.raises(RouterError, match=reason):
        pd.derive(live(**changes), '')


def test_a_pool_shared_by_several_lans_follows_the_mihomo_lan_and_names_the_others():
    second = {**LAN, '.id': '*7', 'address': '2001:db8:a000:1::1/64', 'interface': 'guest'}
    ipv4 = [{'address': '192.168.10.1/24', 'interface': 'lan4'}, {'address': '192.168.20.1/24', 'interface': 'guest'}]
    result = pd.derive(live(addresses=[LAN, second], ipv4_addresses=ipv4), '192.168.10.3')
    assert result['spec']['lan'] == 'lan4'
    assert 'guest' in result['warnings'][0] and '不受前缀变化保护' in result['warnings'][0]
    with pytest.raises(RouterError, match='分给了多个 LAN'):
        pd.derive(live(addresses=[LAN, second], ipv4_addresses=ipv4), '192.168.30.3')
    with pytest.raises(RouterError, match='分给了多个 LAN'):
        pd.derive(live(addresses=[LAN, second], ipv4_addresses=ipv4), '')


def test_script_recognition_ignores_comments_and_whitespace_but_not_parameters():
    spec = {'lan': 'lan4', 'pool': 'ipv6pool'}
    reformatted = '# installed by hand\r\n\r\n' + '\n'.join('    ' + line for line in PROD.split('\n'))
    assert pd._script_state({'source': reformatted, 'policy': 'test,read,write'}, spec)[0] == 'ok'
    state, detail = pd._script_state({'source': PROD.replace('"lan4"', '"lan3"'), 'policy': pd.POLICY}, spec)
    assert state == 'update' and 'LAN 为 lan3，应为 lan4' in detail
    state, detail = pd._script_state({'source': PROD.replace('hold 3600', 'hold 1800'), 'policy': pd.POLICY}, spec)
    assert state == 'update' and '1800' in detail
    state, detail = pd._script_state({'source': PROD.replace(':log info', ':log debug'), 'policy': pd.POLICY}, spec)
    assert state == 'update' and '模板版本不同' in detail
    assert pd._script_state({'source': ':log info hello', 'policy': pd.POLICY}, spec)[0] == 'conflict'
    assert pd._script_state(None, spec)[0] == 'missing'


def test_hook_states_never_overwrite_foreign_content():
    assert pd._hook_state({'interface': 'x'})[0] == 'missing'  # RouterOS omits an empty script
    assert pd._hook_state({'script': pd.HOOK})[0] == 'ok'
    assert pd._hook_state({'script': '/system script run cf-pd-watch'})[0] == 'update'
    assert pd._hook_state({'script': ':log info other\n' + pd.HOOK})[0] == 'shared'
    state, detail = pd._hook_state({'interface': 'pppoe-out1', 'script': ':log info other'})
    assert state == 'conflict' and '不能覆盖' in detail and pd.HOOK in detail


def test_scheduler_states():
    good = {'name': pd.POLL, 'on-event': pd.POLL_EVENT, 'interval': '30s', 'start-time': 'startup',
            'policy': 'read,write,test', 'disabled': 'false'}
    assert pd._scheduler_state(good)[0] == 'ok'
    assert pd._scheduler_state({**good, 'interval': '1m', 'disabled': 'true'})[0] == 'update'
    assert pd._scheduler_state({**good, 'on-event': '/log info x'})[0] == 'conflict'


def test_settings_are_routeros_only_and_public():
    base = {'name': 'r', 'type': 'routeros', 'endpoint': 'https://192.0.2.1', 'username': 'a', 'password': 'b'}
    router = normalize({**base, 'ipv6_pd_guard': {'enabled': True}})
    assert router['ipv6_pd_guard'] == {'enabled': True}
    assert public_view(router)['ipv6_pd_guard'] == {'enabled': True}
    assert public_view(normalize(base))['ipv6_pd_guard'] == {'enabled': False}
    with pytest.raises(RouterValidationError, match='仅支持 RouterOS'):
        normalize({**base, 'type': 'ikuai', 'ipv6_pd_guard': {'enabled': True}})
    with pytest.raises(RouterValidationError, match='布尔值'):
        normalize({**base, 'ipv6_pd_guard': {'enabled': 'yes'}})
    owned = {**router, 'ipv6_pd_guard_state': {'version': 1}}
    with pytest.raises(RouterValidationError, match='前缀变化保护'):
        normalize({'endpoint': 'https://192.0.2.9'}, owned)
