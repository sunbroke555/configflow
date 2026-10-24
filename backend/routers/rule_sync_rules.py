"""Bounded, strict classification of sources used by automatic IPv4 routing.

Domain rules in classical sources are valid but never resolved to addresses.
Unlike the permissive converter used by manual previews, an automatic job must
not interpret an HTML error page or a malformed payload as an empty IP list.
"""
import hashlib
import ipaddress
import json
import re

import yaml

from backend.routers.base import RouterError
from backend.routers.store import static_rule_sets

MAX_BYTES = 4 * 1024 * 1024
MAX_LINES = 100_000
MAX_IPS = 20_000
DOMAIN_TYPES = {'DOMAIN', 'DOMAIN-SUFFIX', 'DOMAIN-KEYWORD', 'DOMAIN-REGEX', 'DOMAIN-WILDCARD'}
OTHER_TYPES = {'GEOIP', 'GEOSITE', 'IP-ASN', 'SRC-IP-CIDR', 'SRC-IP-CIDR6', 'SRC-PORT', 'DST-PORT',
               'PROCESS-NAME', 'PROCESS-PATH', 'PROCESS-NAME-REGEX', 'PROCESS-PATH-REGEX',
               'NETWORK', 'IN-TYPE', 'IN-NAME', 'IN-USER', 'IN-PORT', 'UID', 'DSCP', 'AND', 'OR', 'NOT'}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def classify(content, behavior='classical'):
    if not isinstance(content, str) or len(content.encode('utf-8')) > MAX_BYTES:
        raise RouterError('规则内容过大或不是文本（最多 4 MiB）')
    text = content.lstrip('\ufeff').strip()
    if not text or text.startswith(('<', '{', '[')):
        raise RouterError('规则内容为空或不是支持的规则格式')
    if behavior not in ('classical', 'ipcidr', 'domain'):
        raise RouterError('自动同步暂不支持此规则格式')
    lines = text.splitlines()
    if any(re.match(r'^payload\s*:', line) for line in lines):
        try:
            document = yaml.safe_load(text)
        except yaml.YAMLError:
            raise RouterError('规则 YAML 无法解析') from None
        if not isinstance(document, dict) or not isinstance(document.get('payload'), list):
            raise RouterError('规则 YAML 必须包含 payload 列表')
        lines = document['payload']
    if len(lines) > MAX_LINES:
        raise RouterError('规则条目超过 100000 条，停止自动同步')
    networks, domains, ignored, invalid, examples = set(), 0, 0, 0, []
    total = 0
    for index, raw in enumerate(lines, 1):
        if not isinstance(raw, str):
            invalid += 1
            if len(examples) < 3:
                examples.append(index)
            continue
        line = raw.strip()
        if not line or line.startswith(('#', '//')):
            continue
        total += 1
        kind, value, network = '', line, None
        if ',' in line:
            kind, value = line.split(',', 1)
            kind = kind.strip().upper()
            value = value.strip()
            if kind in {'IP-CIDR', 'IP-CIDR6'}:
                value = value.split(',', 1)[0].strip()
        is_domain = kind in DOMAIN_TYPES
        if not kind and line.startswith(('domain:', 'full:', 'keyword:', 'regexp:')):
            is_domain, value = True, line.split(':', 1)[1].strip()
        if not kind and not is_domain:
            try:
                network = ipaddress.ip_network(value, strict=False)
            except ValueError:
                # Plain domain lists: reject arbitrary prose, URLs and broken CIDRs.
                is_domain = bool(re.fullmatch(r'(?:\+\.|\*\.|\.)?(?:[\w-]+\.)+[\w-]+\.?', value)) and not bool(re.fullmatch(r'[\d.]+', value))
        elif kind in {'IP-CIDR', 'IP-CIDR6'}:
            try:
                network = ipaddress.ip_network(value, strict=False)
                if (kind == 'IP-CIDR6') != (network.version == 6):
                    raise ValueError()
            except ValueError:
                network = None
        else:
            network = None
        if is_domain and value and behavior != 'ipcidr':
            domains += 1
            continue
        if not kind and not is_domain:
            try:
                network = ipaddress.ip_network(value, strict=False)
            except ValueError:
                network = None
        if network is not None and behavior != 'domain' and (not kind or kind in {'IP-CIDR', 'IP-CIDR6'}):
            if isinstance(network, ipaddress.IPv4Network) and network.prefixlen > 0 and network.is_global:
                networks.add(str(network))
                if len(networks) > MAX_IPS:
                    raise RouterError('有效 IPv4 超过 20000 条，请使用手动发布')
            else:
                ignored += 1
            continue
        if kind in OTHER_TYPES and value and behavior == 'classical':
            ignored += 1
            continue
        invalid += 1
        if len(examples) < 3:
            examples.append(index)
    if not total:
        raise RouterError('规则不包含任何有效条目')
    if invalid:
        detail = '纯 IP 规则集含有域名或无效条目' if behavior == 'ipcidr' else '规则包含无法安全识别的条目'
        raise RouterError(f'{detail}：{invalid} 条（第 {"、".join(map(str, examples))} 行）')
    return {'version': hashlib.sha256(content.encode()).hexdigest(), 'ipv4': sorted(networks),
            'ip_count': len(networks), 'domain_count': domains, 'ignored_count': ignored, 'invalid_count': 0}


def selected_sources(config, router):
    from backend.routers.desired import rule_source_issue
    selected = list(dict.fromkeys(static_rule_sets(router) + (router.get('ros_policy_rule_sets') or [])))
    if len(selected) > 128:
        raise RouterError('自动同步每台路由器最多支持 128 个规则集')
    library = {item['id']: item for item in config.get('rule_library', [])}
    rules = {item['id']: item for item in config.get('rule_configs', []) if item.get('itemType') == 'ruleset'}
    result = {}
    for rule_id in selected:
        item = rules.get(rule_id)
        source = library.get((item or {}).get('library_rule_id'))
        issue = rule_source_issue(item, source, require_library=True)
        if issue:
            raise RouterError(f'{issue}，请重新手动预览发布')
        result[rule_id] = (item, source)
    if not result:
        raise RouterError('没有选择需要同步的 IP 规则集')
    return result


def load_sources(config, router, download=None):
    from pathlib import Path
    from backend.utils.rule_utils import rule_cache_read_path
    sources = selected_sources(config, router)
    contents, records, fetched, size = {}, {}, {}, 0
    for rule_id, (item, source) in sources.items():
        name = str(source.get('name') or rule_id)
        key = source['id']
        if key not in fetched:
            if download is not None and source.get('source_type', 'url') == 'url':
                fetched[key] = download(source, config)
            else:
                path = Path(rule_cache_read_path(source, config.get('rule_library', [])))
                try:
                    with path.open('rb') as handle:
                        raw = handle.read(MAX_BYTES + 1)
                    if len(raw) > MAX_BYTES:
                        raise RouterError(f'规则集「{name}」超过 4 MiB，停止自动同步')
                    fetched[key] = raw.decode('utf-8-sig')
                except FileNotFoundError:
                    fetched[key] = source.get('content', '') if source.get('source_type') == 'content' else ''
                except UnicodeError:
                    raise RouterError(f'规则集「{name}」不是 UTF-8 文本') from None
            size += len(fetched[key].encode())
            if size > 16 * 1024 * 1024:
                raise RouterError('所选规则内容总量超过 16 MiB，停止自动同步')
        content = fetched[key]
        try:
            if source.get('source_type', 'url') == 'url':
                from backend.utils.ruleset_content import validate_remote_rule_content, RemoteRuleValidationError
                try:
                    validate_remote_rule_content(content, source, config_data=config)
                except RemoteRuleValidationError as error:
                    raise RouterError(str(error)) from None
            record = classify(content, source.get('behavior') or item.get('behavior') or 'domain')
        except RouterError as error:
            raise RouterError(f'规则集「{name}」：{error}') from None
        contents[rule_id] = content
        records[rule_id] = {**record, 'name': name, 'source_type': source.get('source_type', 'url'),
                            'library_id': key}
        if sum(value['ip_count'] for value in records.values()) > MAX_IPS:
            raise RouterError('所选规则集的有效 IPv4 合计超过 20000 条，请使用手动发布')
    return contents, records


def loader(contents):
    return lambda item, source: contents.get(item['id'], '')


def ip_loader(records):
    """Use the classified IP set, without reparsing remote YAML for each consumer."""
    values = {key: '\n'.join(item['ipv4']) or '# no routable IPv4' for key, item in records.items()}
    return loader(values)


def check_updates(previous, current):
    for rule_id, record in current.items():
        old = previous.get(rule_id)
        if old is None:
            raise RouterError('规则集选择有变化，请先手动预览发布')
        if record['source_type'] != 'url' and record['version'] != old['version']:
            raise RouterError(f'本地规则集「{record["name"]}」已修改，请手动预览发布')
        if old['ip_count'] > 0 and record['ip_count'] == 0:
            raise RouterError(f'规则集「{record["name"]}」的有效 IPv4 从 {old["ip_count"]} 条变为 0，保留 ROS 原状态，请人工确认')
