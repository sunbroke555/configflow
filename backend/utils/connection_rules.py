"""Append a connection destination or its selected subnet to a shared IP rule set."""
import ipaddress


from backend.common.config import get_repository, update_shared_config_transaction
from backend.common.config_repository import ProfileValidationError
from backend.common.oxidns_device_policy import normalize_client_range
from backend.common.oxidns_settings import OxidnsSettingsError
from backend.utils import domain_discovery_service as discovery
from backend.utils.rule_matcher import parse_rule_line
from backend.utils.rule_utils import save_rule_to_local
from backend.utils.ruleset_content import read_rule_payload as _content_lines, append_rule_payload as _append


def ip_rule_targets(config):
    result = []
    for item in config.get('rule_library', []):
        if item.get('source_type') != 'content' or item.get('behavior', 'classical') not in ('classical', 'ipcidr'):
            continue
        info = discovery.ruleset_info(config, item['id'])
        references = info['references']
        effective = [ref for ref in references if ref['enabled'] and not ref['after_match']]
        # A rule set's first applicable reference determines its policy.
        first = next(iter(effective or references), None)
        policy = first['policy'] if first else None
        targets = (['direct', 'proxy'] if first is None else ['direct'] if policy == 'DIRECT'
                   else ['proxy'] if policy and policy not in ('REJECT', 'REJECT-DROP', 'PASS') else [])
        result.append({key: info[key] for key in ('id', 'name', 'behavior')} | {
            'problem': None, 'active': bool(effective) and item.get('enabled', True), 'targets': targets,
        })
    return result


def _destination_network(value, rule_type):
    try:
        network = ipaddress.ip_network(normalize_client_range(value), strict=False)
        if rule_type != ('IP-CIDR' if network.version == 4 else 'IP-CIDR6'):
            raise ValueError()
        return network
    except (ValueError, OxidnsSettingsError):
        raise ProfileValidationError('请选择有效的目标 IP 或网段，IPv4 使用 IP-CIDR，IPv6 使用 IP-CIDR6。') from None



def apply_ip(profile_id, ruleset_id, target, value, rule_type, before_write):
    destination = _destination_network(value, rule_type)
    added, skipped, updated = [], [], []
    info = None

    def append(shared):
        nonlocal info
        before_write()
        # Binding, format and current policy are checked again under the write lock.
        config = get_repository().get_compat_config(profile_id)
        config['rule_library'] = shared.get('rule_library', [])
        info = next((item for item in ip_rule_targets(config) if item['id'] == ruleset_id and target in item['targets']), None)
        if info is None:
            raise ProfileValidationError('所选规则集不支持 IP、不可编辑或策略已改变，请重新打开后选择。')
        item = next(item for item in shared['rule_library'] if item['id'] == ruleset_id)
        content = item.get('content') or ''
        lines, payload = _content_lines(content)
        for raw in lines:
            parsed = parse_rule_line(raw)
            if parsed is None or parsed[0] not in ('IP', 'IP-CIDR', 'IP-CIDR6'):
                continue
            try:
                network = ipaddress.ip_network(parsed[1], strict=False)
            except ValueError:
                continue
            if network.version == destination.version and destination.subnet_of(network):
                skipped.append({'value': str(destination), 'target': target, 'reason': 'exists'})
                return
        cidr = str(destination)
        line = cidr if info['behavior'] == 'ipcidr' else f'{rule_type},{cidr},no-resolve'
        item['content'] = _append(content, line, payload)
        updated.append(item)
        added.append({'value': cidr, 'rule_type': rule_type, 'target': target,
                      'ruleset': item['name'], 'ruleset_id': ruleset_id})

    update_shared_config_transaction(append)
    for item in updated:
        save_rule_to_local(item)
    warnings = [] if info['active'] else ['此规则集尚未在当前配置中启用，或位于兜底规则之后；请在策略规则中设置并发布后生效。']
    return {'added': added, 'skipped': skipped, 'warnings': warnings}
