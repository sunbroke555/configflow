"""RouterOS NAT bypass as one rule over two ConfigFlow address lists.

One ``srcnat accept`` rule, first in the NAT table, selects the client sources
(``cf-mihomo-nat-src``: the gateway's /24 without the gateway) and the proxy
destinations that need it (``cf-mihomo-nat-dst``, one member per CIDR). A CIDR
coming or going changes one list member, never the rule, so the table stays one
rule long however many CIDRs are proxied.

No manifest records these objects. Ownership is the rule's exact shape: its
comment, its two list names, no other matchers, the only such rule, ahead of
every rule that may translate, and no other rule selecting the lists. Members
are ours by their comment; anything else in the lists is kept and never written.

Earlier releases wrote one rule per source fragment and destination (comment
``cf-mihomo``). A preview offers to merge them into the lists whenever the
device holds any, even with nothing else to change: thousands of rules slow
every new connection down, and auto-sync cannot update them. The release a
person starts writes and proves the lists and the rule first, then removes
the old rules in batches; any failure restores them in their original order.
Auto-sync never migrates. Devices without such rules and without a missing
exemption keep exactly the earlier plan and reads (``plan`` returns None).
"""
import copy
import ipaddress
from itertools import groupby
from urllib.parse import quote

from backend.routers.base import RouterError, RouterRollbackError, is_managed, managed_comment
from backend.routers.report import _networks
from backend.routers.routeros_nat import _interface, client_source_networks

COMMENT = 'cf-mihomo-nat'
SRC_LIST = 'cf-mihomo-nat-src'
DST_LIST = 'cf-mihomo-nat-dst'
NAMES = (SRC_LIST, DST_LIST)
# What the per-destination writer (RouterOSDriver nat_add) used as comment.
LEGACY_COMMENT = 'cf-mihomo'

NAT = '/ip/firewall/nat'
ADDRESSES = '/ip/firewall/address-list'
MANGLE = '/ip/firewall/mangle'
FILTER = '/ip/firewall/filter'
RAW = '/ip/firewall/raw'

# Fields the rule may carry; the same evidence preserves_sources accepts, minus
# literal addresses and interface lists.
RULE_FIELDS = frozenset({'.id', 'chain', 'action', 'disabled', 'comment', 'log', 'log-prefix', 'bytes', 'packets',
                         'dynamic', 'invalid', 'src-address-list', 'dst-address-list', 'out-interface'})
LEGACY_FIELDS = frozenset({'chain', 'action', 'disabled', 'comment', 'log', 'log-prefix', 'dynamic',
                           'src-address', 'dst-address', 'out-interface'})
REMOVE_BATCH = 500
SETTLE_READS = 8
CHANGED = 'NAT 免转换规则或地址列表在预览后发生变化，请重新预览'
WHY_MERGE = '逐条规则过多会拖慢每个新连接的源 NAT 匹配，自动同步也无法更新逐条规则'


def _truthy(value):
    return value is True or str(value).lower() in ('true', 'yes', '1')


def _enabled(row):
    return not _truthy(row.get('disabled'))


def _key(value):
    """One member or CIDR as RouterOS compares it: 1.2.3.4 and 1.2.3.4/32 are one entry."""
    text = str(value or '').strip()
    try:
        return str(ipaddress.IPv4Network(text, strict=False))
    except ValueError:
        return text


def _config(row):
    from backend.routers.ros_policy import config_row
    return config_row(row)


def _resource(path, row):
    return {'path': path, 'id': row['.id'], 'config': _config(row)}


def _sample(values):
    values = list(values)
    return '、'.join(values[:5]) + (f' 等 {len(values)} 个' if len(values) > 5 else '')


def is_rule(row):
    """The ConfigFlow list rule by identity, whatever else was changed on it."""
    return (row.get('comment') == COMMENT and row.get('src-address-list') == SRC_LIST
            and row.get('dst-address-list') == DST_LIST)


def references(rows):
    """(id, list name) of every row selecting either list, negated or not."""
    found = []
    for row in rows:
        for key in ('src-address-list', 'dst-address-list'):
            name = str(row.get(key) or '').lstrip('!')
            if name in NAMES:
                found.append((str(row.get('.id')), name))
                break
    return found


def candidate(row):
    """A NAT row that may be the list rule: its comment and a selector of either list.

    The comment alone is not ownership: adopting a hand-made exemption
    commented ``mihomo-nat`` writes this very comment onto a rule that
    selects no list, and that rule stays an ordinary adopted one.
    """
    return row.get('comment') == COMMENT and bool(references([row]))


def inspect_rule(rows, mangle_references=(), firewall_references=()):
    """Find the rule in an ordered NAT table: ``{'rule', 'problem', 'out_interface'}``.

    ``rule`` is None when there is none. A problem means the device holds
    something only a person can sort out; nothing is then written. Mangle
    references are (id, list name); filter and raw ones (path, id, list name).
    """
    found = [row for row in rows if candidate(row)]
    result = {'rule': None, 'problem': '', 'out_interface': ''}
    if len(found) > 1:
        result['problem'] = (f'发现多条 ConfigFlow NAT 免转换列表规则（{COMMENT}），无法确认归属，'
                             '请删除多余规则后重新预览')
        return result
    own = str(found[0].get('.id')) if found else None
    others = [(f'{NAT}/{row_id}', name) for row_id, name in references(rows) if row_id != own]
    others += [(f'{MANGLE}/{row_id}', name) for row_id, name in mangle_references]
    others += [(f'{path}/{row_id}', name) for path, row_id, name in firewall_references]
    if others:
        result['problem'] = f'其他规则 {others[0][0]} 正在引用 ConfigFlow 的免转换地址列表 {others[0][1]}，请先解除引用'
        return result
    if not found:
        return result
    row = found[0]
    row_id = row.get('.id')
    changed = sorted(set(row) - RULE_FIELDS)
    if row.get('chain') != 'srcnat':
        changed.append('chain')
    if row.get('action') != 'accept':
        changed.append('action')
    if row.get('src-address-list') != SRC_LIST:
        changed.append('src-address-list')
    if row.get('dst-address-list') != DST_LIST:
        changed.append('dst-address-list')
    if _truthy(row.get('log')) or row.get('log-prefix'):
        changed.append('log')
    egress = str(row.get('out-interface') or '')
    if egress and (egress.startswith('!') or _interface(egress) is None):
        changed.append('out-interface')
    if changed:
        result['problem'] = (f'ConfigFlow 的 NAT 免转换规则 {row_id} 被修改（{"、".join(changed)}），'
                             '不会自动覆盖，请恢复或删除后重新预览')
        return result
    if not _enabled(row):
        result['problem'] = f'ConfigFlow 的 NAT 免转换规则 {row_id} 已停用，请启用或删除后重新预览'
        return result
    if _truthy(row.get('dynamic')) or _truthy(row.get('invalid')):
        result['problem'] = f'ConfigFlow 的 NAT 免转换规则 {row_id} 是动态或无效规则，请检查后重新预览'
        return result
    for other in rows[:rows.index(row)]:
        # Not even a return ahead of it: only rules that let packets through.
        if (_enabled(other) and other.get('chain') in ('srcnat', None, '')
                and other.get('action') not in ('accept', 'log', 'passthrough')):
            result['problem'] = (f'ConfigFlow 的 NAT 免转换规则 {row_id} 位于源 NAT 转换规则 {other.get(".id")} 之后，'
                                 '请移到最前或删除后重新预览')
            return result
    result.update(rule=row, out_interface=egress)
    return result


def _members(name, rows):
    """(ours, foreign, problems) of one list; dynamic or disabled members are never maintained."""
    ours, foreign, problems = [], [], []
    for row in rows:
        if _truthy(row.get('dynamic')) or row.get('timeout'):
            problems.append(f'地址列表 {name} 含动态条目，可能被其他规则写入，无法安全维护')
        elif not _enabled(row):
            problems.append(f'地址列表 {name} 含停用条目 {row.get("address")}，请启用或删除后重新预览')
        elif row.get('comment') == COMMENT:
            ours.append(row)
        else:
            foreign.append(row)
    return ours, foreign, list(dict.fromkeys(problems))


def _evidence(lists):
    return {kind: [{'.id': row.get('.id'), **_config(row)} for row in lists.get(kind) or []]
            for kind in ('src', 'dst')}


def _legacy_shape(row, excluded, out_interface):
    """Exactly what the per-destination writer produced, nothing added since."""
    return (row.get('comment') == LEGACY_COMMENT and str(row.get('.id')) not in excluded
            and row.get('chain') == 'srcnat' and row.get('action') == 'accept' and _enabled(row)
            and not _truthy(row.get('dynamic')) and not _truthy(row.get('invalid'))
            and not set(_config(row)) - LEGACY_FIELDS and not _truthy(row.get('log')) and not row.get('log-prefix')
            and bool(row.get('src-address')) and bool(row.get('dst-address'))
            and str(row.get('out-interface') or '') in ('', out_interface))


def _legacy(rows, sources, items, desired, skipped, excluded, out_interface, routed):
    """Per-destination rules the earlier writer made, by their exact shape:
    ``(merged, their destinations, stale, retained)``.

    Only a destination with one rule for every current source fragment, and
    nothing else on them, is taken over: what an adopted hand-made rule (also
    ``cf-…``) could not look like by chance. A desired CIDR that needs an
    exemption moves into the lists. A destination overlapping no desired CIDR
    and nothing else still sent to mihomo (``routed``; None when that is
    unknown) is stale: earlier releases never removed these rules when a CIDR
    stopped being proxied, so traffic to it left the WAN untranslated. Anything
    in between (a wider or narrower destination, a CIDR skipped for a route
    conflict, not required or adopted, still routed to mihomo) is left as it
    is; stale-looking groups kept only for a route come back as ``retained``.
    """
    wanted = {_key(item['cidr']): item['cidr'] for item in items if item['status'] in ('synced', 'missing')}
    source_keys = {_key(source) for source in sources}
    blocked = _networks(skipped)
    proxied = _networks(desired)
    position = {id(row): index for index, row in enumerate(rows)}
    groups = {}
    for row in rows:
        if _legacy_shape(row, excluded, out_interface) and _key(row['src-address']) in source_keys:
            groups.setdefault(_key(row['dst-address']), []).append(row)
    selected, destinations, stale, retained = [], [], [], []
    for key, group in groups.items():
        if ({_key(row['src-address']) for row in group} != source_keys
                or len({str(row.get('out-interface') or '') for row in group}) != 1):
            continue
        try:
            network = ipaddress.IPv4Network(key)
        except ValueError:
            continue
        if key in wanted:
            if not any(network.overlaps(other) for other in blocked):
                selected.extend(group)
                destinations.append(wanted[key])
        elif not any(network.overlaps(other) for other in proxied):
            if routed is not None and not any(network.overlaps(other) for other in routed):
                stale.extend(group)
            else:
                retained.extend(group)
    for found in (selected, stale, retained):
        found.sort(key=lambda row: position[id(row)])
    return ([_resource(NAT, row) for row in selected], destinations, [_resource(NAT, row) for row in stale],
            [_resource(NAT, row) for row in retained])


def _via(gateway, value):
    """Whether a RouterOS route gateway (``a``, ``a%iface``, ``a@table``, ECMP ``a,b``) names this next hop."""
    return any(part.split('%')[0].split('@')[0].strip() == gateway for part in str(value or '').split(','))


def still_routed(report, state, gateway):
    """Destinations that keep reaching mihomo after this release besides the
    desired CIDRs, as networks; None when the routes cannot be read.

    Every enabled route to the gateway counts, in any table and however it
    came there (public DNS routed for TUN DNS hijack, a person's own route),
    except the managed routes this release deletes. The source policy's own
    default route stands for its destinations. A destination ConfigFlow no
    longer proxies may still be sent to mihomo this way, and its old
    exemption then still keeps the client's address.
    """
    context = state.nat_context or {}
    rows = context.get('routes')
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return None
    deleting = {str(extra['route_id']) for extra in report['routes'].get('extra') or []}
    check = (state.ros_policy_plan or {}).get('check') or {}
    previous = check.get('previous') or {}
    policy_ids = {str(item['id']) for item in previous.get('resources', []) if item.get('path') == '/ip/route'}
    policy = [cidr for spec in (check.get('spec'), previous.get('spec')) for cidr in (spec or {}).get('destinations', [])]
    everything = ipaddress.IPv4Network('0.0.0.0/0')
    routed = []
    for row in rows:
        if not _enabled(row) or not _via(gateway, row.get('gateway')) or str(row.get('.id')) in deleting:
            continue
        if str(row.get('.id')) in policy_ids:
            routed.extend(_networks(policy))
            continue
        networks = _networks([str(row.get('dst-address') or '')])
        routed.extend(networks if networks or ':' in str(row.get('dst-address') or '') else [everything])
    for entry in (state.policy_routes or []) + (state.retired_policy_routes or []):
        if entry.enabled and _via(gateway, entry.gateway):
            routed.extend(_networks(entry.dst) if entry.dst else [everything])
    for row in context.get('mangle') or []:
        if _enabled(row) and row.get('action') == 'route' and _via(gateway, row.get('route-dst')):
            routed.append(everything)
    return routed


def _adopt_ops(items, rules):
    """Hand-made covering rules are re-commented only, never cloned or widened."""
    adopt_ids = []
    for item in items:
        if item['status'] == 'adoptable':
            adopt_ids.extend(rule_id for rule_id in item['rule_ids']
                             if rule_id not in adopt_ids and not is_managed(rules[rule_id].comment))
    ops = []
    for rule_id in adopt_ids:
        rule = rules[rule_id]
        comment = managed_comment(rule.comment)
        ops.append({'kind': 'nat_update', 'id': rule.id, 'add_dst': [], 'comment': comment,
                    'summary': f'接管 NAT 免转换规则「{rule.comment or rule.id}」（备注 → {comment}）',
                    'expected': rule.attributes})
    return ops


def _refs(lists):
    return (lists or {}).get('mangle_refs') or (), (lists or {}).get('firewall_refs') or ()


def plan(report, state, gateway, skipped):
    """The list-based NAT plan, or None when the earlier per-rule plan stays.

    None only while no list rule exists (nor anything named like it), no CIDR
    is missing an exemption and no per-destination rule of earlier releases
    can be merged: not required, adopt-only and unknown plans keep their
    earlier operations, fingerprint and reads (the lists are not read).
    """
    rows = state.nat_context.get('rules') or []
    items = [item for item in report['nat_bypass']['items'] if item['cidr'] not in skipped]
    found = inspect_rule(rows, *_refs(state.nat_lists))
    missing = [item['cidr'] for item in items if item['status'] == 'missing']
    check = (state.ros_policy_plan or {}).get('check') or {}
    moving = {(item['path'], item['id']) for item in check.get('migrations', [])}
    sources = [str(network) for network in client_source_networks(gateway)]
    desired = [item['cidr'] for item in report['nat_bypass']['items']]
    legacy, legacy_destinations, stale, retained = _legacy(
        rows, sources, items, desired, skipped, {item_id for path, item_id in moving if path == NAT},
        found['out_interface'], still_routed(report, state, gateway))
    if found['rule'] is None and not found['problem'] and not missing and not legacy:
        return None
    result = {'ops': [], 'blockers': [], 'warnings': [], 'evidence': None, 'check': None, 'exempt': []}
    lists = lists_of(state)
    if lists is None:
        result['blockers'].append('无法读取 NAT 免转换地址列表，请重新预览')
        return result
    found = inspect_rule(rows, *_refs(lists))
    result['evidence'] = _evidence(lists)
    if lists.get('mangle_refs') is None:
        result['blockers'].append('无法读取 Mangle，不能确认 NAT 免转换地址列表未被其他规则引用')
    if lists.get('firewall_refs') is None:
        result['blockers'].append('无法读取防火墙 filter 或 raw 规则，不能确认 NAT 免转换地址列表未被其他规则引用')
    if found['problem']:
        result['blockers'].append(found['problem'])
    rule = found['rule']
    src_ours, src_foreign, problems = _members(SRC_LIST, lists.get('src') or [])
    dst_ours, dst_foreign, more = _members(DST_LIST, lists.get('dst') or [])
    result['blockers'].extend(problems + more)
    for name, foreign in ((SRC_LIST, src_foreign), (DST_LIST, dst_foreign)):
        if foreign and rule is None:
            result['blockers'].append(f'地址列表 {name} 已存在非 ConfigFlow 条目，ConfigFlow 不会接管同名列表')
        elif foreign:
            result['warnings'].append(f'地址列表 {name} 中有 {len(foreign)} 条不是 ConfigFlow 写入的条目，保留不动')
    if result['blockers']:
        return result
    if retained:
        kept = list(dict.fromkeys(item['config'].get('dst-address', '') for item in retained))
        result['warnings'].append(
            f'保留 {len(retained)} 条旧的逐条 NAT 免转换规则（备注 {LEGACY_COMMENT}，目的 {_sample(kept)}）：'
            '这些网段已不在代理网段中，但仍有路由把它们送往 mihomo，删除会让这部分流量被源 NAT、丢失客户端 IP')

    nat_rules = {entry.id: entry for entry in state.nat_bypass if entry.enabled and (NAT, entry.id) not in moving}
    rule_id = str(rule['.id']) if rule is not None else None
    egress = found['out_interface']
    covered = [item['cidr'] for item in items if item['status'] == 'synced' and rule_id in item['rule_ids']]
    targets = {}
    for cidr in missing + covered + legacy_destinations:
        targets.setdefault(_key(cidr), cidr)
    desired = {_key(cidr) for cidr in desired}
    # A CIDR skipped for a route conflict keeps whatever protects it today.
    held = _networks(skipped)
    withdraw = rule is not None and not targets and not any(
        network.overlaps(other) for row in dst_ours for network in _networks([row.get('address', '')]) for other in held)
    if withdraw:
        stale = []  # withdrawing touches the list rule only

    def split(ours, foreign, keep):
        """(ours to remove, keys present afterwards, keys of ours kept): duplicates and
        members no longer wanted go; members the source policy migrates are its own."""
        present, removing, kept = {_key(row.get('address')) for row in foreign}, [], set()
        for row in ours:
            if (ADDRESSES, row['.id']) in moving:
                continue
            key = _key(row.get('address'))
            if withdraw or key not in keep or key in present:
                removing.append(_resource(ADDRESSES, row))
            else:
                present.add(key)
                kept.add(key)
        return removing, present, kept

    source_keys = {_key(source): source for source in sources}
    src_remove, src_present, src_kept = split(src_ours, src_foreign, source_keys)
    dst_remove, dst_present, dst_kept = split(dst_ours, dst_foreign, desired)
    src_add = [] if withdraw else [value for key, value in source_keys.items() if key not in src_present]
    dst_add = [value for key, value in targets.items() if key not in dst_present]
    create = rule is None and bool(targets)
    ours_after = {'src': sorted(src_kept | {_key(value) for value in src_add}),
                  'dst': sorted(dst_kept | {_key(value) for value in dst_add})}

    result['ops'] = _adopt_ops(items, nat_rules)
    if create or withdraw or src_add or src_remove or dst_add or dst_remove or legacy or stale:
        op = {'kind': 'nat_list_sync', 'comment': COMMENT, 'src_list': SRC_LIST, 'dst_list': DST_LIST,
              'rule': None if rule is None else _resource(NAT, rule), 'out_interface': egress,
              'replace_rule': False, 'sources': sources, 'destinations': list(targets.values()),
              'src_add': src_add, 'src_remove': src_remove, 'dst_add': dst_add, 'dst_remove': dst_remove,
              'legacy': legacy, 'stale': stale, 'withdraw': withdraw}
        merged = {_key(cidr) for cidr in legacy_destinations}
        op['summary'] = _summary(op, gateway, create, [cidr for key, cidr in targets.items() if key not in merged])
        result['ops'].append(op)
    result['check'] = {'rule': rule_id, 'out_interface': egress, 'sources': sources,
                       'destinations': list(targets.values()),
                       'removed': [item['id'] for item in legacy + stale],
                       'members': ours_after, 'withdraw': withdraw}

    # Return routing covers what stays exempt after this stage.
    gone = {item['id'] for item in legacy + stale}
    removed = {_key(item['config'].get('address')) for item in dst_remove}
    exempt = []
    for entry in nat_rules.values():
        if entry.id in gone or (entry.id == rule_id and withdraw):
            continue
        exempt.extend(cidr for cidr in entry.dst if entry.id != rule_id or _key(cidr) not in removed)
    exempt += list(targets.values())
    exempt += [item['values']['dst-address'] for item in check.get('replacements', []) if item['path'] == NAT]
    result['exempt'] = exempt
    return result


def _summary(op, gateway, create, fresh=None):
    """One line: what changes, and for old per-destination rules why they merge."""
    if op['withdraw']:
        return f'删除 NAT 免转换规则「{COMMENT}」及其地址列表：已没有需要免转换的代理网段'
    parts, head = [], ''
    stale = op.get('stale') or []
    if create:
        target = (f'源地址列表 {SRC_LIST}（{gateway} 所在 /24 除 {gateway} 外，{len(op["sources"])} 段）'
                  f'→ 目的地址列表 {DST_LIST}（{len(op["destinations"])} 个网段），放在所有源 NAT 转换规则之前')
        if op['legacy']:
            head = f'将 {len(op["legacy"])} 条 NAT 免转换规则合并为 1 条「{COMMENT}」：{target}'
            if fresh:
                parts.append(f'其中新增 {len(fresh)} 个网段（{_sample(fresh)}）')
            parts.append(f'{WHY_MERGE}；先建立并回读校验新规则，再删除旧规则（备注 {LEGACY_COMMENT}）')
        else:
            head = f'新增 NAT 免转换规则「{COMMENT}」：{target}'
    else:
        changes = []
        if op['dst_add']:
            changes.append(f'新增 {len(op["dst_add"])} 个网段（{_sample(op["dst_add"])}）')
        if op['dst_remove']:
            changes.append(f'移除 {len(op["dst_remove"])} 个不再代理的网段'
                           f'（{_sample(item["config"].get("address", "") for item in op["dst_remove"])}）')
        head = f'NAT 免转换目的列表 {DST_LIST}：' + '、'.join(changes) if changes else ''
        if op['src_add'] or op['src_remove']:
            parts.append(f'源地址列表 {SRC_LIST} 随网关 {gateway} 更新：'
                         f'新增 {len(op["src_add"])} 段、移除 {len(op["src_remove"])} 段')
        if op['legacy']:
            parts.append(f'将 {len(op["legacy"])} 条旧的逐条 NAT 免转换规则（备注 {LEGACY_COMMENT}）与「{COMMENT}」'
                         f'合并为 1 条：{WHY_MERGE}；先回读校验列表规则的覆盖，再删除旧规则')
    if create and op['dst_remove']:
        parts.append(f'移除 {len(op["dst_remove"])} 个不再代理的残留网段')
    if stale:
        destinations = list(dict.fromkeys(item['config'].get('dst-address', '') for item in stale))
        parts.append(f'删除 {len(stale)} 条残留的旧逐条免转换规则（备注 {LEGACY_COMMENT}，目的 {_sample(destinations)}）：'
                     '这些网段已不再代理，保留它们会让去往这些网段的流量经 WAN 出去时不做源 NAT')
    return '；'.join(part for part in [head] + parts if part)


def removals_first(nat_ops, route_ops, gateway):
    """Move list removals ahead of the route deletions: ``(nat_ops, route_ops)``.

    A CIDR no longer proxied loses its route in the routes stage. Were its
    member removed only afterwards, the list rule (which exempts every egress
    whenever an exemption is needed at all) would meanwhile let its traffic
    leave the WAN with a LAN source. Removed first, it is only translated on
    its way to mihomo for a moment, the order auto-sync writes too. Stale old
    per-destination rules go early for the same reason, with or without the
    list rule: their destinations reach mihomo only through routes this
    release deletes, if at all. Withdrawing the rule moves whole; additions
    and merges stay after the routes.
    """
    if not any(op['kind'] == 'route_delete' for op in route_ops):
        return nat_ops, route_ops
    early, rest = [], []
    for op in nat_ops:
        if op['kind'] != 'nat_list_sync' or op['replace_rule']:
            rest.append(op)
            continue
        if op['withdraw']:
            early.append(op)
            continue
        # Without the rule its members exempt nothing: they may go any time.
        pruning = op['dst_remove'] if op['rule'] is not None else []
        if not (pruning or op['stale']):
            rest.append(op)
            continue
        prune = {**copy.deepcopy(op), 'src_add': [], 'src_remove': [], 'dst_add': [], 'legacy': [],
                 'destinations': [], 'dst_remove': copy.deepcopy(pruning)}
        prune['summary'] = _summary(prune, gateway, False) + '；在删除对应路由之前执行'
        early.append(prune)
        op = {**op, 'dst_remove': [] if pruning else op['dst_remove'], 'stale': []}
        if (op['rule'] is None or op['src_add'] or op['src_remove'] or op['dst_add'] or op['dst_remove']
                or op['legacy']):
            op['summary'] = _summary(op, gateway, op['rule'] is None,
                                     [cidr for cidr in op['destinations']
                                      if _key(cidr) not in {_key(item['config'].get('dst-address'))
                                                            for item in op['legacy']}])
            rest.append(op)
    return rest, early + route_ops


def restrict_egress(op, check, interface):
    """Withdrawing source policy: the list rule may only exempt the LAN egress.

    Narrowing to the one interface mihomo is reached through cannot cut any
    proxy path; it only lets traffic that never reaches mihomo be translated.
    Returns a blocker, or '' when the operation now writes that egress.
    """
    if op['withdraw'] or op['out_interface'] == interface:
        return ''
    if op['rule'] is not None and op['out_interface']:
        return (f'ConfigFlow 的 NAT 免转换规则限定出口 {op["out_interface"]}，'
                f'与当前 mihomo 出口 {interface} 不一致，请先核对')
    if op['rule'] is not None:
        op['replace_rule'] = True
        check['removed'].append(op['rule']['id'])
    op['out_interface'] = check['out_interface'] = interface
    op['summary'] += f'；限定出口 {interface}：撤销来源策略期间，未走代理的流量不会被免转换'
    return ''


def policy_migrations(row, addresses, exclusions):
    """Members of the list rule that source policy takes over, as migrations.

    Only members ConfigFlow wrote are removed; the NAT plan adds back any CIDR
    that still needs an exemption. A foreign member cannot be split safely.
    """
    from backend.routers.ros_policy import network_overlap
    overlaps = network_overlap(exclusions)
    result = []
    for item in addresses:
        if item.get('list') != row.get('dst-address-list') or not _enabled(item):
            continue
        networks = _networks([item.get('address', '')])
        if not networks:
            raise RouterError('受管 NAT 的目的地址无法安全拆分，请先核对：' + str(item.get('.id')))
        if not any(overlaps(str(network)) for network in networks):
            continue
        if item.get('comment') != COMMENT or _truthy(item.get('dynamic')):
            raise RouterError('列表型 NAT 免转换中有非 ConfigFlow 条目与策略目的重叠，无法自动拆分，请先核对：'
                              + str(item.get('.id')))
        result.append(_resource(ADDRESSES, item))
    return result


# --- reads -----------------------------------------------------------------

def eager(names, driver):
    """Whether read_state reads the two lists with the others: a NAT or Mangle
    rule selects them, or this driver wrote them (read-back and rollback)."""
    return bool(set(NAMES) & set(names)) or bool(getattr(driver, 'nat_list_touched', False))


def state_lists(driver, nats, rows_by_name, mangle):
    """The two lists and every rule outside NAT selecting them (None: unreadable).

    Filter and raw are read only while the lists or the rule exist, so
    devices without them keep the reads they had before. A rule selecting a
    list before ConfigFlow first creates it is still refused when writing.
    """
    from backend.routers import ros_address_lists
    src, dst = rows_by_name.get(SRC_LIST, []), rows_by_name.get(DST_LIST, [])
    for name, rows in ((SRC_LIST, src), (DST_LIST, dst)):
        ros_address_lists.expect(driver, name, rows)
    firewall = []
    if src or dst or any(candidate(row) for row in nats):
        try:
            for path in (FILTER, RAW):
                firewall.extend((path, row_id, name) for row_id, name in references(driver._list(path)))
        except RouterError:
            firewall = None
    return {'src': src, 'dst': dst, 'mangle_refs': None if mangle is None else references(mangle),
            'firewall_refs': firewall}


def reader(driver, nats, mangle):
    """Read the lists later, for a plan that turns out to need them."""
    def read():
        from backend.routers import ros_address_lists
        return state_lists(driver, nats, ros_address_lists.read(driver, list(NAMES)), mangle)
    return read


def lists_of(state):
    """The state's lists, read now when read_state left them for later (None: unreadable)."""
    read = getattr(state, 'nat_lists_reader', None)
    if state.nat_lists is None and read is not None:
        state.nat_lists_reader = None
        try:
            state.nat_lists = read()
        except RouterError:
            state.nat_lists = None
    return state.nat_lists


def _assessed(driver, rows):
    """The NAT rows the plan judged: proven DNS guard hairpins never matter here."""
    manifest = getattr(driver, 'dns_guard_manifest', None)
    if not manifest:
        return rows
    from backend.routers.dns_guard import owned_hairpins
    hairpins = owned_hairpins(manifest, rows)
    return [row for row in rows if str(row.get('.id')) not in hairpins]


def _read_lists(driver):
    """Both lists; within this session they are proven by digest afterwards."""
    from backend.routers import ros_address_lists
    lists = ros_address_lists.read(driver, list(NAMES))
    for name in NAMES:
        ros_address_lists.expect(driver, name, lists[name])
    return lists


def _foreign_references(driver, rule_ids, nat_rows=None):
    """(path, id, name) of every other firewall row selecting either list."""
    found = []
    for path in (NAT, MANGLE, FILTER, RAW):
        rows = nat_rows if path == NAT and nat_rows is not None else driver._list(path)
        found.extend((path, row_id, name) for row_id, name in references(rows)
                     if not (path == NAT and row_id in rule_ids))
    return found


def nat_resources(driver):
    """Auto-sync baseline: the rule and its source members, or None without a rule."""
    rows = _assessed(driver, driver._list(NAT))
    found = inspect_rule(rows)
    if found['problem']:
        raise RouterError(found['problem'])
    if found['rule'] is None:
        return None
    lists = _read_lists(driver)
    return {'rule': _resource(NAT, found['rule']),
            'src': [_resource(ADDRESSES, row) for row in lists[SRC_LIST] if row.get('comment') == COMMENT]}


def member_update(plan, baseline):
    """The one NAT change auto-sync may write, or None.

    Only destination members of the list rule enrolled by a manual release:
    the rule (ID, configuration, egress) and its source members must match
    the baseline. Creating, replacing or withdrawing the rule, migrating or
    removing old rules, source or adoption changes still need a manual
    release. Removals the plan put in the routes stage (``removals_first``)
    are merged in.
    """
    enrolled = (baseline or {}).get('nat_list')
    stages = {stage['key']: stage['ops'] for stage in plan['stages']}
    ops = [op for op in stages.get('routes', []) if op['kind'] == 'nat_list_sync'] + stages.get('nat_bypass', [])
    if not ops or not enrolled or any(op['kind'] != 'nat_list_sync' for op in ops):
        return None
    for op in ops:
        if (op['rule'] is None or op['replace_rule'] or op['withdraw'] or op['legacy'] or op.get('stale')
                or op['src_add']
                or op['src_remove'] or op['rule']['id'] != enrolled['rule']['id']
                or _config(op['rule']['config']) != _config(enrolled['rule']['config'])
                or op['out_interface'] != str(enrolled['rule']['config'].get('out-interface') or '')):
            return None
    return {**ops[-1], 'dst_add': [value for op in ops for value in op['dst_add']],
            'dst_remove': [item for op in ops for item in op['dst_remove']]}


def migration(plan):
    """Old per-destination rules a plan merges or removes: (count, stale count)."""
    ops = [inner for stage in plan['stages'] for op in stage['ops'] for inner in op.get('prepare_static', [op])
           if inner['kind'] == 'nat_list_sync']
    return (sum(len(op['legacy']) for op in ops if op.get('legacy')),
            sum(len(op['stale']) for op in ops if op.get('stale')))


def entries(name, addresses):
    return [{'list': name, 'address': address, 'comment': COMMENT, 'disabled': 'false'} for address in addresses]


# --- writes ----------------------------------------------------------------

def _check_unchanged(driver, op, full, rows, lists):
    """Everything the preview decided on is still exactly there."""
    keep = {op['rule']['id']} if op['rule'] else set()
    foreign = _foreign_references(driver, keep, full)
    if foreign:
        path, row_id, name = foreign[0]
        raise RouterError(f'其他防火墙规则 {path}/{row_id} 正在引用 {name}，未做任何修改')
    found = inspect_rule(rows)
    if found['problem']:
        raise RouterError(found['problem'])
    if op['rule'] is None:
        if found['rule'] is not None:
            raise RouterError(CHANGED)
    elif (found['rule'] is None or str(found['rule']['.id']) != op['rule']['id']
          or _config(found['rule']) != _config(op['rule']['config'])):
        raise RouterError(CHANGED)
    by_id = {str(row.get('.id')): row for row in rows}
    for item in op['legacy'] + op.get('stale', []):
        row = by_id.get(item['id'])
        if row is None or _config(row) != _config(item['config']):
            raise RouterError(CHANGED)
    for kind, name in (('src', SRC_LIST), ('dst', DST_LIST)):
        members = {row['.id']: row for row in lists[name]}
        ours, others, problems = _members(name, lists[name])
        if problems:
            raise RouterError(problems[0])
        if others and op['rule'] is None:
            raise RouterError(f'地址列表 {name} 已存在非 ConfigFlow 条目，ConfigFlow 不会接管同名列表')
        for item in op[kind + '_remove']:
            row = members.get(item['id'])
            if row is None or _config(row) != _config(item['config']):
                raise RouterError(CHANGED)
        present = {_key(row.get('address')) for row in lists[name]}
        if any(_key(value) in present for value in op[kind + '_add']):
            raise RouterError(CHANGED)


def _settle(driver, row_id):
    """A rule just written reads invalid=true for about half a second on RouterOS."""
    import time
    from backend.routers.ros_policy_write import _read
    for attempt in range(SETTLE_READS):
        row = _read(driver, NAT, row_id)
        if row is None:
            raise RouterError('新的 NAT 免转换规则回读时不存在，已回滚')
        if not _truthy(row.get('invalid')):
            return
        if attempt + 1 < SETTLE_READS:
            time.sleep(0.25)
    raise RouterError('新的 NAT 免转换规则未生效（invalid），已回滚')


def _create_rule(driver, op, journal, ids):
    from backend.routers.ros_policy_write import _create
    values = {'chain': 'srcnat', 'action': 'accept', 'src-address-list': SRC_LIST, 'dst-address-list': DST_LIST,
              'comment': COMMENT, 'disabled': 'false'}
    if op['out_interface']:
        values['out-interface'] = op['out_interface']
    # _create places it before the first rule and checks that order on read-back.
    return _create(driver, NAT, values, journal, ids)['id']


def _prove(driver, op, rule_id):
    """Before old rules go: with them gone, the list rule alone still covers every pair."""
    from backend.routers.routeros_nat import preserves_sources
    legacy = {item['id'] for item in op['legacy'] + op.get('stale', [])}
    rows = [row for row in _assessed(driver, driver._list(NAT)) if str(row.get('.id')) not in legacy]
    found = inspect_rule(rows)
    lists = _read_lists(driver)
    context = {'address_sets': {name: [str(row.get('address') or '') for row in lists[name] if _enabled(row)]
                                for name in NAMES}}
    sources = [ipaddress.IPv4Network(value) for value in op['sources']]
    destinations = [ipaddress.IPv4Network(value) for value in op['destinations']]
    if (found['problem'] or found['rule'] is None or str(found['rule']['.id']) != rule_id
            or not preserves_sources(rows, sources, destinations, op['out_interface'] or None, context)):
        raise RouterError('新的 NAT 免转换规则回读后无法证明覆盖全部客户端与目的网段，已回滚'
                          + (f'：{found["problem"]}' if found['problem'] else ''))


def delete_rules(driver, items, journal):
    """Delete many NAT rules: one table read guards them all, removes go in batches.

    Each restore step records its original position and the next rule that
    stays, so a rollback puts them back in the same order.
    """
    from backend.routers.ros_policy_write import _writable
    rows = driver._list(NAT)
    order = [str(row.get('.id')) for row in rows]
    position = {row_id: index for index, row_id in enumerate(order)}
    by_id = {str(row.get('.id')): row for row in rows}
    for item in items:
        row = by_id.get(item['id'])
        if row is None or _config(row) != _config(item['config']):
            raise RouterError(f'NAT 规则在操作期间发生变化，保留现场：{NAT}/{item["id"]}')
    deleting = {item['id'] for item in items}
    steps = []
    for item in items:
        values = _writable(NAT, by_id[item['id']])
        index = position[item['id']]
        following = next((row_id for row_id in order[index + 1:] if row_id not in deleting), None)
        step = {'action': 'restore', **item, 'values': values, 'order': index}
        if following:
            values['place-before'] = following
            step['following_order'] = position[following]
        steps.append(step)
    journal.extend(steps)
    ids = [item['id'] for item in items]
    try:
        for start in range(0, len(ids), REMOVE_BATCH):
            driver._request('POST', NAT + '/remove', {'.id': ','.join(ids[start:start + REMOVE_BATCH])})
    except RouterError:
        # A remove stops at its first failure; delete what is left one by one.
        present = {str(row.get('.id')) for row in driver._list(NAT)}
        for row_id in ids:
            if row_id in present:
                driver._request('DELETE', NAT + '/' + quote(row_id, safe='*'))
    if deleting & {str(row.get('.id')) for row in driver._list(NAT)}:
        raise RouterError('旧的 NAT 免转换规则删除后仍然存在')


def restore_rules(driver, steps, ids, remapped, failures):
    """Recreate deleted NAT rules in their original order with console batches.

    Rules still present must be unchanged. An absent rule goes back before
    whichever came first originally: the next rule of the same deletion that
    is still there, or the rule outside it that followed it (possibly
    recreated under a new ID), which must then still exist. Absent rules
    sharing an anchor go in their original order. What a batch cannot prove
    is restored one by one.
    """
    from bisect import bisect_right
    from backend.routers import routeros_batch
    from backend.routers.ros_policy_write import _restore_step
    rows = driver._list(NAT)
    by_id = {str(row.get('.id')): row for row in rows}
    batched = sorted((step for step in steps if 'order' in step), key=lambda step: step['order'])
    present = [step for step in batched if step['id'] in by_id]
    orders = [step['order'] for step in present]
    pending = []
    for step in batched:
        current = by_id.get(step['id'])
        if current is not None:
            if _config(current) != _config(step['config']):
                failures.append(f'NAT 规则在操作期间发生变化，保留现场：{NAT}/{step["id"]}')
            continue
        values = copy.deepcopy(step['values'])
        index = bisect_right(orders, step['order'])
        sibling = present[index] if index < len(present) else None
        following = values.get('place-before')
        if following:
            following = values['place-before'] = remapped.get((NAT, following), following)
        # The rule outside the deletion wins when it sat closer: a sibling
        # further down would put the rules in between behind the restored ones.
        if sibling is not None and not (following in by_id and step.get('following_order') is not None
                                        and step['following_order'] < sibling['order']):
            values['place-before'] = sibling['id']
        if values.get('place-before') and values['place-before'] not in by_id:
            failures.append('原规则位置已改变，不能安全恢复顺序')
            continue
        pending.append(({**step, 'values': values}, values))
    undo, handled = [], 0
    if len(pending) >= routeros_batch.BATCH_MIN:
        try:
            handled = routeros_batch.put_nat(driver, [{'method': 'PUT', 'path': NAT, 'values': values}
                                                      for _, values in pending], undo)
        except RouterError as error:
            failures.append(str(error))
            return
    for (step, _), entry in zip(pending[:handled], undo):
        remapped[(NAT, step['id'])] = entry['id']
        ids[NAT].add(entry['id'])
    # Anchors are rules that existed before this restore, so what the batch
    # left goes one by one in the same order; other steps keep their place.
    for step in [step for step, _ in pending[handled:]] + [step for step in steps if 'order' not in step]:
        try:
            _restore_step(driver, step, ids, remapped)
        except RouterError as error:
            failures.append(str(error))


def _ids(driver, rows, lists):
    from collections import defaultdict
    ids = defaultdict(set)
    ids[NAT].update(str(row.get('.id')) for row in rows)
    ids[ADDRESSES].update(row['.id'] for name in NAMES for row in lists[name])
    return ids


def apply(driver, op):
    from backend.routers import ros_address_lists
    from backend.routers.ros_policy_write import _create_addresses, _delete, _delete_addresses
    ros_address_lists.invalidate(driver)  # destructive: never trust an earlier proof
    full = driver._list(NAT)
    rows = _assessed(driver, full)
    lists = _read_lists(driver)
    _check_unchanged(driver, op, full, rows, lists)
    driver.nat_list_touched = True
    ids = _ids(driver, full, lists)
    journal, keep = [], [op['rule']['id']] if op['rule'] else []
    try:
        if op['withdraw']:
            _delete(driver, op['rule'], journal)
            for kind in ('dst', 'src'):
                if op[kind + '_remove']:
                    _delete_addresses(driver, op[kind + '_remove'], journal)
            return {'kind': 'nat_list_restore', 'steps': list(reversed(journal)), 'keep': keep}
        # Add before remove: removing first would translate new connections
        # of clients both ranges cover until the new fragments are in.
        if op['src_add']:
            _create_addresses(driver, entries(SRC_LIST, op['src_add']), journal, ids)
        if op['src_remove']:
            _delete_addresses(driver, op['src_remove'], journal)
        rule_id = op['rule']['id'] if op['rule'] else None
        if op['replace_rule']:
            # Narrow first: no member is added while the rule exempts every egress.
            rule_id = _create_rule(driver, op, journal, ids)
            keep.append(rule_id)
            _delete(driver, op['rule'], journal)
        if op['dst_add']:
            _create_addresses(driver, entries(DST_LIST, op['dst_add']), journal, ids)
        if op['rule'] is None and op['destinations']:
            rule_id = _create_rule(driver, op, journal, ids)
            keep.append(rule_id)
        if rule_id is not None:
            _settle(driver, rule_id)
        # Stale rules alone, ahead of the routes and before any list rule
        # exists, protect no desired CIDR: there is nothing to prove.
        if rule_id is not None and (op['legacy'] or op.get('stale')):
            _prove(driver, op, rule_id)
        if op['legacy'] or op.get('stale'):
            order = {str(row.get('.id')): index for index, row in enumerate(full)}
            delete_rules(driver, sorted(op['legacy'] + op.get('stale', []), key=lambda item: order[item['id']]),
                         journal)
        # Narrowing comes last, once the new state is in place.
        if op['dst_remove']:
            _delete_addresses(driver, op['dst_remove'], journal)
    except RouterError as error:
        try:
            restore(driver, {'kind': 'nat_list_restore', 'steps': list(reversed(journal)), 'keep': keep})
        except RouterError as rollback_error:
            raise RouterRollbackError(f'{error}；{rollback_error}') from error
        raise
    return {'kind': 'nat_list_restore', 'steps': list(reversed(journal)), 'keep': keep}


def restore(driver, op):
    """Undo a list update: members back, old rules back in order, new rule gone."""
    from backend.routers import ros_address_lists
    from backend.routers.ros_policy_write import run_steps
    ros_address_lists.invalidate(driver)
    driver.nat_list_touched = True
    failures, configs = [], {}
    remapped = {(path, old): new for path, old, new in op.get('remapped') or []}
    rows = driver._list(NAT)
    lists = _read_lists(driver)
    ids = _ids(driver, rows, lists)
    # Members and rules this release wrote may only go while nothing else
    # selects the lists; otherwise keep them for inspection.
    rule_ids = {step['id'] for step in op['steps'] if step.get('path') == NAT} | set(op.get('keep') or [])
    delete_safe = True
    try:
        foreign = _foreign_references(driver, rule_ids)
        if foreign:
            delete_safe = False
            failures.append(f'其他防火墙规则 {foreign[0][0]}/{foreign[0][1]} 正在引用 {foreign[0][2]}，保留这些对象')
    except RouterError as error:
        delete_safe = False
        failures.append(str(error))
    # What this release wrote goes only once everything it took away is back:
    # deleting the new rule beside old rules that could not return would
    # leave the masquerade translating every client. Keep it for inspection.
    kept = False
    for deleting, run in groupby(op['steps'], key=lambda step: step['action'] == 'delete'):
        if deleting and failures and delete_safe:
            kept = True
        run_steps(driver, list(run), ids, remapped, configs, failures, delete_safe and not failures, nat_batch=True)
    if kept:
        failures.append('原有免转换对象未能全部恢复，已保留新的 NAT 免转换规则和地址列表条目，请检查后处理')
    if failures:
        raise RouterRollbackError('NAT 免转换回滚未完成：' + '；'.join(failures))
    return {}


# --- verification ----------------------------------------------------------

def verify(check, state):
    """Read-back of the NAT stage beyond per-CIDR coverage: shape, removals, members."""
    rows = (state.nat_context or {}).get('rules') or []
    lists = lists_of(state)
    if lists is None:
        raise RouterError('回读校验失败：无法读取 NAT 免转换地址列表')
    # As strict as the preview: an unreadable table could hide a rule
    # selecting the lists, which every member change would silently alter.
    if lists.get('mangle_refs') is None:
        raise RouterError('回读校验失败：无法读取 Mangle，不能确认 NAT 免转换地址列表未被其他规则引用')
    if lists.get('firewall_refs') is None:
        raise RouterError('回读校验失败：无法读取防火墙 filter 或 raw 规则，不能确认 NAT 免转换地址列表未被其他规则引用')
    found = inspect_rule(rows, lists['mangle_refs'], lists['firewall_refs'])
    present = {str(row.get('.id')) for row in rows}
    if check['withdraw']:
        if found['rule'] is not None or any(candidate(row) for row in rows):
            raise RouterError('回读校验失败：应删除的 NAT 免转换规则仍然存在')
    else:
        if found['problem']:
            raise RouterError('回读校验失败：' + found['problem'])
        if check['destinations'] and found['rule'] is None:
            raise RouterError('回读校验失败：没有找到 NAT 免转换规则')
        if found['rule'] is not None and found['out_interface'] != check['out_interface']:
            raise RouterError('回读校验失败：NAT 免转换规则的出口限制与计划不一致')
    if set(check['removed']) & present:
        raise RouterError('回读校验失败：应删除的旧 NAT 免转换规则仍然存在')
    for kind, name in (('src', SRC_LIST), ('dst', DST_LIST)):
        ours = {_key(row.get('address')) for row in lists.get(kind) or []
                if row.get('comment') == COMMENT and _enabled(row)}
        expected = set() if check['withdraw'] else set(check['members'][kind])
        if ours != expected:
            raise RouterError(f'回读校验失败：地址列表 {name} 的 ConfigFlow 条目与计划不一致')
        if not check['withdraw']:
            present_keys = {_key(row.get('address')) for row in lists.get(kind) or [] if _enabled(row)}
            wanted_values = check['sources'] if kind == 'src' else check['destinations']
            if any(_key(value) not in present_keys for value in wanted_values):
                raise RouterError(f'回读校验失败：地址列表 {name} 缺少计划中的网段')
