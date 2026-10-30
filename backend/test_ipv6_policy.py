"""Ordered ROS diagnostics must prove a path, never infer safety from rule names."""
import pytest

from backend.routers.ipv6_policy import POLICY_TABLES, evaluate_chain, policy_checks
from backend.routers import ipv6_readiness as ros
from backend.routers.base import RouterError


def snapshot():
    result = {key: [] for key in (*ros.READINESS_TABLES, *POLICY_TABLES)}
    result['routes'] = [
        {'.id': '*D', 'dst-address': '::/0', 'gateway': 'fe80::1%wan', 'active': 'true'},
        {'.id': '*L', 'dst-address': '2001:db8:1::/64', 'gateway': 'lan', 'active': 'true'},
        {'.id': '*F', 'dst-address': 'fc00::/64', 'gateway': 'fdda::3%lan', 'active': 'true'},
    ]
    result['ipv4_routes'] = [
        {'.id': '*4D', 'dst-address': '0.0.0.0/0', 'gateway': 'wan', 'active': 'true'},
        {'.id': '*4L', 'dst-address': '192.168.0.0/24', 'gateway': 'lan', 'active': 'true'},
        {'.id': '*4F', 'dst-address': '198.18.0.0/16', 'gateway': '192.168.0.3%lan', 'active': 'true'},
    ]
    result['interfaces'] = [{'name': 'lan', 'dynamic': 'false'}, {'name': 'wan', 'dynamic': 'true'}]
    return result


def packet(**overrides):
    return {'src-address': '2001:db8:1::3', 'dst-address': '2001:db8:2::10', 'protocol': 'tcp',
            'dst-port': 443, 'connection-state': 'new', 'in-interface': 'lan', 'out-interface': 'wan', **overrides}


def rule(action, **fields):
    return {'chain': 'forward', 'action': action, **fields}


def evaluate(rules, query=None, table='filter', family=6, **extra):
    state = snapshot()
    state.update(extra)
    state[('ipv4_' if family == 4 else '') + table] = rules
    return evaluate_chain(state, family, table, 'srcnat' if table == 'nat' else 'forward', query or packet())


def test_first_terminating_match_wins_and_reports_id_comment():
    rules = [rule('accept', **{'.id': '*1', 'src-address': '2001:db8:1::/64', 'comment': 'LAN permit'}), rule('drop')]
    report = evaluate(rules)
    assert report['decision'] == 'allow'
    assert '*1' in report['evidence'][0] and 'LAN permit' in report['evidence'][0]
    assert evaluate(list(reversed(rules)))['decision'] == 'drop'


@pytest.mark.parametrize('fields', [{'disabled': 'true'}, {'invalid': 'true'}])
def test_disabled_or_invalid_rules_are_not_effective(fields):
    assert evaluate([rule('drop', **fields), rule('accept')])['decision'] == 'allow'


def test_default_accept_is_explicitly_scoped_to_rule_table():
    report = evaluate([])
    assert report['decision'] == 'allow'
    assert '默认放行' in report['evidence'][0]
    assert evaluate([rule('return')])['decision'] == 'allow'


def test_jump_return_resumes_caller_and_accept_terminates_caller():
    rules = [rule('jump', **{'jump-target': 'inspect'}), rule('drop'),
             rule('return', chain='inspect')]
    assert evaluate(rules)['decision'] == 'drop'
    rules[-1]['action'] = 'accept'
    assert evaluate(rules)['decision'] == 'allow'
    rules[-1] = rule('log', chain='inspect')
    assert evaluate(rules)['decision'] == 'drop'


@pytest.mark.parametrize('rules', [
    [rule('jump', **{'jump-target': 'missing'})],
    [rule('jump', **{'jump-target': 'inspect'}), rule('jump', chain='inspect', **{'jump-target': 'forward'})],
])
def test_missing_and_cyclic_jump_targets_are_unknown(rules):
    assert evaluate(rules)['decision'] == 'unknown'


def test_unknown_condition_is_not_silently_ignored():
    assert evaluate([rule('drop', **{'layer7-protocol': 'custom'}), rule('accept')])['decision'] == 'unknown'
    assert evaluate([rule('drop', protocol='udp', **{'layer7-protocol': 'custom'}), rule('accept')])['decision'] == 'allow'


def test_unknown_matching_branch_can_only_merge_identical_outcomes():
    assert evaluate([rule('accept', **{'connection-state': 'established'}), rule('accept')], packet(**{'connection-state': None}))['decision'] == 'allow'
    assert evaluate([rule('accept', **{'connection-state': 'established'}), rule('drop')], packet(**{'connection-state': None}))['decision'] == 'unknown'


def test_address_lists_and_negation_use_members_not_names():
    lists = [{'list': 'trusted', 'address': '2001:db8:1::/64'}, {'list': 'trusted', 'address': '::/0', 'disabled': 'true'}]
    assert evaluate([rule('accept', **{'src-address-list': 'trusted'}), rule('drop')], address_lists=lists)['decision'] == 'allow'
    assert evaluate([rule('drop', **{'src-address-list': '!trusted'}), rule('accept')], address_lists=lists)['decision'] == 'allow'
    assert evaluate([rule('accept', **{'src-address-list': 'unresolved'}), rule('drop')], address_lists=[{'list': 'unresolved', 'address': 'example.test'}])['decision'] == 'unknown'


def test_partial_prefix_overlap_does_not_sample_one_fake_address():
    assert evaluate([rule('accept', **{'dst-address': 'fc00::/80'}), rule('drop')], packet(**{'dst-address': 'fc00::/64'}))['decision'] == 'unknown'
    assert evaluate([rule('accept', **{'dst-address': 'fc00::/7'}), rule('drop')], packet(**{'dst-address': 'fc00::/64'}))['decision'] == 'allow'


def test_interface_list_include_exclude_then_explicit_member_order():
    definitions = [{'name': 'base'}, {'name': 'blocked'}, {'name': 'chosen', 'include': 'base', 'exclude': 'blocked'}]
    members = [{'list': 'base', 'interface': 'lan'}, {'list': 'blocked', 'interface': 'lan'}]
    rules = [rule('accept', **{'in-interface-list': 'chosen'}), rule('drop')]
    assert evaluate(rules, interface_lists=definitions, interface_members=members)['decision'] == 'drop'
    members.append({'list': 'chosen', 'interface': 'lan'})
    assert evaluate(rules, interface_lists=definitions, interface_members=members)['decision'] == 'allow'
    assert evaluate([rule('drop', **{'in-interface-list': '!static'}), rule('accept')])['decision'] == 'allow'


def test_missing_interface_list_and_read_errors_remain_unknown():
    assert evaluate([rule('accept', **{'in-interface-list': 'absent'}), rule('drop')])['decision'] == 'unknown'
    assert evaluate([rule('accept', **{'src-address-list': 'known'}), rule('drop')], _errors={'address_lists'})['decision'] == 'unknown'
    assert evaluate([], _errors={'filter'})['decision'] == 'unknown'


def test_protocol_ports_and_icmp_types_are_checked():
    assert evaluate([rule('accept', protocol='6', **{'dst-port': '80,443,8000-8099'}), rule('drop')])['decision'] == 'allow'
    icmp = packet(protocol='icmpv6', **{'icmp-type': 2, 'icmp-code': 0})
    assert evaluate([rule('accept', protocol='ipv6-icmp', **{'icmp-options': '2:0'}), rule('drop')], icmp)['decision'] == 'allow'
    assert evaluate([rule('accept', protocol='icmpv6', **{'icmp-options': '134:0'}), rule('drop')], icmp)['decision'] == 'drop'


def test_nat_exemption_must_precede_translation_and_match_actual_exit():
    exempt = rule('accept', chain='srcnat', **{'.id': '*E', 'dst-address': '198.18.0.0/16', 'out-interface': 'lan'})
    masquerade = rule('masquerade', chain='srcnat', **{'.id': '*M'})
    query = packet(**{'src-address': '192.168.0.10', 'dst-address': '198.18.0.0/16', 'out-interface': 'lan'})
    assert evaluate([exempt, masquerade], query, 'nat', 4)['decision'] == 'allow'
    report = evaluate([masquerade, exempt], query, 'nat', 4)
    assert report['decision'] == 'translate' and '*M' in report['evidence'][0]
    assert evaluate([rule('masquerade', chain='srcnat', **{'out-interface': 'wan'})], query, 'nat', 4)['decision'] == 'allow'


def inspect_policy(state=None, context=None, errors=()):
    state = state or snapshot()
    host = {'addresses': [{'address': '2001:db8:1::3', 'usable': True}, {'address': 'fdda::3', 'usable': True}]}
    configuration = {'fake_ipv6_enabled': True, 'fake_ipv6_range': 'fc00::/64'}
    default_context = {'node_targets': [{'address': '2001:db8:2::10', 'port': 443}],
                       'clients': [{'address': '2001:db8:1::20'}, {'address': '192.168.0.20'}],
                       'fake_ipv4_range': '198.18.0.0/16'}
    return policy_checks(state, errors, {'host': '192.168.0.3'}, host, configuration, 'lan', default_context if context is None else context)


def by_id(checks, key):
    return next(c for c in checks if c['id'] == key)


def test_unrelated_drop_does_not_make_matching_node_path_warning():
    state = snapshot()
    state['filter'] = [rule('drop', protocol='udp'), rule('accept')]
    checks = inspect_policy(state)
    assert by_id(checks, 'node_0_0')['decision'] == 'allow'
    assert by_id(checks, 'node_0_0_return')['decision'] == 'allow'


def test_raw_drop_and_mangle_marks_are_not_hidden_by_filter_accept():
    state = snapshot()
    state['filter'] = [rule('accept')]
    state['raw'] = [rule('drop', chain='prerouting', **{'.id': '*R', 'src-address': '2001:db8:1::3'})]
    report = by_id(inspect_policy(state), 'node_0_0')
    assert report['decision'] == 'drop' and any('*R' in e for e in report['details'])
    assert report['status'] == 'warning' and '源地址候选' in report['message']
    state['raw'] = []
    state['mangle'] = [rule('mark-routing', chain='prerouting', **{'new-routing-mark': 'vpn'})]
    assert by_id(inspect_policy(state), 'node_0_0')['decision'] == 'unknown'


def test_policy_routes_and_missing_extra_tables_do_not_claim_main_path():
    state = snapshot()
    state['routing_rules'] = [{'src-address': '2001:db8:1::/64', 'action': 'lookup', 'table': 'vpn'}]
    assert by_id(inspect_policy(state), 'node_0_0')['decision'] == 'unknown'
    state['routing_rules'] = []
    assert by_id(inspect_policy(state, errors={'raw'}), 'node_0_0')['decision'] == 'unknown'


def test_no_client_or_literal_node_never_invents_endpoints():
    checks = inspect_policy(context={'node_targets': [{'address': 'node.example.test', 'port': 443}], 'fake_ipv4_range': '198.18.0.0/16'})
    assert by_id(checks, 'node_policy')['status'] == 'unknown'
    assert by_id(checks, 'fake6_policy')['status'] == 'unknown'
    assert by_id(checks, 'fake4_policy')['status'] == 'unknown'
    assert by_id(checks, 'firewall')['status'] == 'unknown'
    assert not any(c['id'] == 'node_0_0' for c in checks)


def test_nat_return_and_same_lan_return_are_not_claimed_verified():
    state = snapshot()
    state['nat'] = [rule('masquerade', chain='srcnat', **{'out-interface': 'wan'})]
    checks = inspect_policy(state)
    assert by_id(checks, 'node_0_0')['decision'] == 'translate'
    assert by_id(checks, 'node_0_0_return')['status'] == 'unknown'
    assert by_id(checks, 'fake6_0')['decision'] == 'allow'
    assert by_id(checks, 'fake4_0')['decision'] == 'allow'
    assert by_id(checks, 'fake4_0_return_routing')['status'] == 'unknown'


@pytest.mark.parametrize('action,decision,status', [('accept', 'allow', 'ok'), ('drop', 'drop', 'warning')])
def test_registered_client_address_candidate_is_not_reported_as_observed_flow(action, decision, status):
    state = snapshot()
    state['ipv4_filter'] = [rule(action, **{'.id': '*CLIENT', 'src-address': '192.168.0.20'})]
    context = {'clients': [{'address': '192.168.0.20', 'source_candidate': True,
                            'source_note': '验收客户端登记地址候选，尚未确认实际源地址'}],
               'fake_ipv4_range': '198.18.0.0/16'}
    checks = inspect_policy(state, context)
    report = by_id(checks, 'fake4_0')
    assert report['decision'] == decision and report['status'] == status
    assert '地址候选' in report['label']
    assert any('尚未确认实际源地址' in detail for detail in report['details'])
    assert any('*CLIENT' in detail for detail in report['details'])
    assert by_id(checks, 'fake4_0_return_routing')['status'] == 'unknown'
    assert not any(c['id'] == 'fake4_policy' for c in checks)


def test_fake_ip_exit_interface_alone_does_not_prove_correct_mihomo_gateway():
    state = snapshot()
    state['ipv4_routes'][-1]['gateway'] = '192.168.0.99%lan'
    report = by_id(inspect_policy(state), 'fake4_0')
    assert report['decision'] == 'unknown'
    assert '下一跳' in report['message']


def test_input_control_classes_are_separate_from_forward_rules():
    state = snapshot()
    state['filter'] = [rule('drop'), rule('accept', chain='input', protocol='icmpv6'),
                       rule('accept', chain='input', protocol='udp', **{'dst-port': 546}), rule('drop', chain='input')]
    checks = inspect_policy(state)
    assert by_id(checks, 'node_0_0')['decision'] == 'drop'
    assert by_id(checks, 'ra_input')['decision'] == 'allow'
    assert by_id(checks, 'dhcpv6_input')['decision'] == 'allow'
    assert by_id(checks, 'icmpv6_2_input')['decision'] == 'allow'


def test_added_policy_read_failure_does_not_disable_existing_fake_route_plan():
    class Driver:
        def _list(self, path):
            if path == '/ipv6/firewall/raw':
                raise RouterError('not permitted')
            if path == '/ip/address':
                return [{'address': '192.168.0.1/24', 'interface': 'lan'}]
            if path == '/ipv6/address':
                return [{'address': 'fdda::1/64', 'interface': 'lan'}]
            return []

        def _get(self, path):
            return {}

    result = ros.inspect(Driver(), {'id': 'router'}, {'host': '192.168.0.3'},
                         {'addresses': [{'address': 'fdda::3', 'usable': True}]},
                         {'fake_ipv6_enabled': True, 'fake_ipv6_range': 'fc00::/64', 'tun_enabled': True})
    assert result['plan']['gateway'] == 'fdda::3%lan'
    assert by_id(result['checks'], 'ra_input')['status'] == 'unknown'


def test_only_address_lists_referenced_by_rules_are_read():
    reads = []

    class Driver:
        def _list(self, path):
            reads.append(path)
            if path == '/ipv6/firewall/filter':
                return [rule('accept', **{'src-address-list': 'Trusted LAN'})]
            return []

        def _get(self, path):
            return {}

    ros.inspect(Driver(), {'id': 'router'}, {'host': '192.168.0.3'}, {}, {})
    assert '/ipv6/firewall/address-list?list=Trusted%20LAN' in reads
    assert '/ipv6/firewall/address-list' not in reads
    assert '/ip/firewall/address-list' not in reads
