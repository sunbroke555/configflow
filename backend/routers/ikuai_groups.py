"""ConfigFlow-owned iKuai IP groups (``route_object``) for proxy destinations.

The 端口分流 rules ``cf-mihomo``/``cf-mihomo-manual`` and the NAT bypass rule
ConfigFlow writes destinations into reference these groups instead of listing
every CIDR in the rule itself (``dst_addr.custom`` stays empty, always, however
few CIDRs there are). A rule listing ~5000 CIDRs exceeds iKuai's ~96 KiB request
limit and takes seconds to apply (4.0.314), and each change rewrote the whole
rule; a group edit takes effect at once without touching the rule.

Each role is one family of groups used by exactly one rule:

=====  ===========================  =============  ==================
role   rule                          group name     marker
=====  ===========================  =============  ==================
pa     端口分流 cf-mihomo            cf_pa_<n>      cf-mihomo
pm     端口分流 cf-mihomo-manual     cf_pm_<n>      cf-mihomo-manual
nat    the NAT bypass target rule    cf_nat_<n>     cf-mihomo-nat
=====  ===========================  =============  ==================

iKuai 4.0.314 keeps no comment on a group (``route_object`` accepts the field
and drops it; rows read back with ``tagname`` = ``group_name``), but it keeps
each entry's comment and the entry order. The marker is therefore the first
entry's comment. A group is ours only when both its name and its marker match;
anything else is the user's, never edited, deleted or referenced (a matching
name only reserves its number). The one exception is a group with our name
that lost its marker (the user edited the first entry) while that role's
managed rule still references it: it is still ours and the marker is written
back. Notes the user gives other entries stay with their entries. Groups hold
at most 2000 entries and are split by request bytes, notes included, so a
group write always fits in one request.

iKuai counts every reference to a group (``ref_count``), including ones from
features ConfigFlow does not read (ACL, 流控…), and refuses to delete a group
still referenced. A group with such references is never deleted or edited.
"""
import hashlib
import ipaddress
import json
import re
from typing import Any, Dict, List, Optional, Tuple

GROUP_MAX_ENTRIES = 2000
# iKuai refuses request bodies from about 98 300 bytes (4.0.314); keep a margin.
GROUP_MAX_BYTES = 92 * 1024
# Provisional until a rule referencing more groups is verified on a device.
MAX_RULE_GROUPS = 16
MAX_INDEX = 9999
ROLES = {'pa': 'cf-mihomo', 'pm': 'cf-mihomo-manual', 'nat': 'cf-mihomo-nat'}
ROLE_RULES = {'pa': '端口分流「cf-mihomo」', 'pm': '端口分流「cf-mihomo-manual」', 'nat': 'NAT 免转换'}
ROUTE_ROLES = ('pa', 'pm')
NAME = re.compile(r'^cf_(pa|pm|nat)_([1-9][0-9]{0,3})$')


# --- names, values and sizes ------------------------------------------------------------

def group_name(role: str, index: int) -> str:
    return f'cf_{role}_{index}'


def name_role(name: Any) -> Optional[Tuple[str, int]]:
    match = NAME.match(name) if isinstance(name, str) else None
    return (match[1], int(match[2])) if match else None


def owned_role(name: Any, comment: Any) -> Optional[Tuple[str, int]]:
    """``(role, n)`` for a group ConfigFlow owns: managed name AND that role's marker."""
    parsed = name_role(name)
    return parsed if parsed and comment == ROLES[parsed[0]] else None


def canon(value: Any) -> str:
    """Compare entries as addresses: iKuai may read ``1.1.1.1/32`` back as ``1.1.1.1``."""
    text = str(value).strip()
    try:
        if '-' in text:
            start, end = (ipaddress.IPv4Address(part.strip()) for part in text.split('-', 1))
            return f'{start}-{end}'
        return str(ipaddress.ip_network(text, strict=False))
    except ValueError:
        return text


def digest(values: List[str]) -> str:
    return hashlib.sha1('\n'.join(sorted({canon(value) for value in values})).encode()).hexdigest()[:16]


def payload(name: str, role: str, values: List[str], row_id: Any = None,
            notes: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """The route_object add/edit form: the first entry carries the marker.

    ``notes`` (by :func:`canon` address) are the user's own entry comments; the
    marker goes on an entry without one, moved to the front, so none is lost.
    """
    entries = [{'ip': value, 'comment': notes.get(canon(value), '') if notes else ''} for value in values]
    if entries:
        spare = next((position for position, entry in enumerate(entries) if not entry['comment']), 0)
        entries.insert(0, entries.pop(spare))
        entries[0]['comment'] = ROLES[role]
    return entry_payload(name, entries, row_id)


def entry_payload(name: str, entries: List[Dict[str, str]], row_id: Any = None) -> Dict[str, Any]:
    """The form writing exactly these entries (a rollback restores what it read)."""
    form = {'group_name': name, 'type': 0, 'group_value': [dict(entry) for entry in entries]}
    return {'id': row_id, **form} if row_id is not None else form


def entry_notes(group: Dict[str, Any]) -> Dict[str, str]:
    """The user's own entry comments: everything but our marker on the first entry."""
    notes = {}
    for position, entry in enumerate(group.get('entries') or []):
        comment = entry['comment']
        if comment and comment not in ROLES.values() and (position or not group['role'] or group.get('adopted')):
            notes.setdefault(canon(entry['ip']), comment)
    return notes


_BASE_BYTES: List[int] = []


def _base_bytes() -> int:
    if not _BASE_BYTES:
        from backend.routers.ikuai import encode_value, request_bytes
        # Worst case of every variable part: a 10-digit ID, the longest name and marker.
        worst = payload('cf_nat_9999', 'pm', [], 9999999999)
        marker = max(len(encode_value(comment, 'comment').encode()) for comment in ROLES.values())
        _BASE_BYTES.append(request_bytes('route_object', 'edit', worst) + marker)
    return _BASE_BYTES[0]


def _entry_bytes(value: str, note: str = '') -> int:
    from backend.routers.ikuai import encode_value
    # One list item plus its ", " separator, exactly as json.dumps writes it.
    return len(json.dumps(encode_value({'ip': value, 'comment': note})).encode()) + 2


def group_bytes(name: str, role: str, values: List[str], notes: Optional[Dict[str, str]] = None) -> int:
    from backend.routers.ikuai import request_bytes
    return request_bytes('route_object', 'edit', payload(name, role, values, 9999999999, notes))


# --- what the router has ------------------------------------------------------------------

def group_from_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """A decoded route_object row as the planner and driver see it."""
    name = str(row.get('group_name') or '')
    entries = [item for item in row.get('group_value') or [] if isinstance(item, dict)]
    # The marker: the first entry's comment (a group itself has no comment on iKuai).
    comment = str(entries[0].get('comment') or '') if entries else ''
    owned = owned_role(name, comment)
    kept = [{'ip': str(item.get('ip')), 'comment': str(item.get('comment') or '')}
            for item in entries if item.get('ip')]
    refs = row.get('ref_count')
    return {'id': row.get('id') if type(row.get('id')) is int else None, 'gid': str(row.get('group_id') or ''),
            'name': name, 'comment': comment, 'values': [entry['ip'] for entry in kept], 'entries': kept,
            'type': row.get('type', 0), 'refs': refs if type(refs) is int and refs > 0 else 0,
            'role': owned[0] if owned else None, 'index': owned[1] if owned else None, 'users': []}


def mark_owners(groups: List[Dict[str, Any]]) -> None:
    """A name used twice identifies neither group: neither is ours."""
    counts: Dict[str, int] = {}
    for group in groups:
        counts[group['name']] = counts.get(group['name'], 0) + 1
    for group in groups:
        if counts[group['name']] > 1:
            group['role'] = group['index'] = None
            group['dup'] = True


def hidden_refs(group: Dict[str, Any]) -> int:
    """References iKuai counts that no NAT or 端口分流 rule accounts for (ACL, 流控…)."""
    return max(0, group.get('refs', 0) - len(group['users']))


def _role_rule(role: str, rule: Dict[str, Any]) -> bool:
    """Whether ``rule`` is the managed rule a ``role`` family belongs to."""
    if role == 'nat':
        return rule['func'] == 'nat_rule' and rule['action'] == 'filter' and _is_managed(rule['comment'])
    return rule['func'] == 'stream_ipport' and rule['comment'] == ROLES[role]


def adopt(groups: List[Dict[str, Any]], rules: Dict[str, Dict[str, Any]]) -> None:
    """Our groups whose marker the user removed: our name, and that role's managed
    rule references them. Marked ``adopted``; the next release writes the marker back."""
    for group in groups:
        parsed = name_role(group['name'])
        if group['role'] or group.get('dup') or not parsed:
            continue
        if any(user['field'] == 'dst_addr' and _role_rule(parsed[0], rules[user['rule']])
               for user in group['users']):
            group['role'], group['index'], group['adopted'] = parsed[0], parsed[1], True


def resolve(groups: List[Dict[str, Any]], ref: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The group a rule reference points at: by gid; by name only when the reference has none."""
    gid = str(ref.get('gid') or '')
    if gid:
        return next((group for group in groups if group['gid'] == gid), None)
    named = [group for group in groups if group['name'] == str(ref.get('gp_name') or '')]
    return named[0] if len(named) == 1 else None


def classify(groups: List[Dict[str, Any]], ref: Dict[str, Any]) -> Tuple[str, Optional[Dict[str, Any]]]:
    """``owned`` (one of ours), ``stale`` (ours by name, gone) or ``foreign`` (the user's)."""
    group = resolve(groups, ref)
    if group is not None:
        return ('owned', group) if group['role'] else ('foreign', group)
    return ('stale', None) if name_role(ref.get('gp_name')) else ('foreign', None)


def foreign_refs(groups: List[Dict[str, Any]], field: Any) -> List[Dict[str, Any]]:
    objects = field.get('object') if isinstance(field, dict) else None
    return [ref for ref in objects if isinstance(ref, dict) and classify(groups, ref)[0] == 'foreign'] \
        if isinstance(objects, list) else []


def read_rules(groups: List[Dict[str, Any]], nat_rows, policy_rows) -> Dict[str, Dict[str, Any]]:
    """Each rule's literal destinations and group references, and who uses each group."""
    rules = {}
    for func, prefix, rows in (('nat_rule', 'nat', nat_rows), ('stream_ipport', 'policy', policy_rows)):
        for row in rows:
            key = f'{prefix}:{row.get("id", "")}'
            fields = {}
            for field in ('src_addr', 'dst_addr'):
                value = row.get(field) if isinstance(row.get(field), dict) else {}
                custom = value.get('custom') if isinstance(value.get('custom'), list) else []
                objects = value.get('object') if isinstance(value.get('object'), list) else []
                fields[field] = {'custom': [str(item) for item in custom if isinstance(item, str)],
                                 'objects': [ref for ref in objects if isinstance(ref, dict)]}
                for ref in fields[field]['objects']:
                    group = resolve(groups, ref)
                    if group is not None:
                        group['users'].append({'rule': key, 'field': field, 'comment': str(row.get('comment') or ''),
                                               'label': '端口分流' if prefix == 'policy' else 'NAT 规则'})
            rules[key] = {'func': func, 'id': str(row.get('id', '')), 'comment': str(row.get('comment') or ''),
                          'enabled': row.get('enabled') == 'yes', 'action': row.get('action'),
                          'custom': fields['dst_addr']['custom'], 'objects': fields['dst_addr']['objects'],
                          'src_objects': fields['src_addr']['objects']}
    adopt(groups, rules)
    return rules


# --- layout ---------------------------------------------------------------------------------

def _pack(slots: List[Dict[str, Any]], queue: List[str], position: int, size_of=_entry_bytes) -> int:
    """Fill each slot in order from ``queue[position:]``; return the new position."""
    for slot in slots:
        while position < len(queue):
            size = size_of(queue[position])
            if len(slot['values']) >= GROUP_MAX_ENTRIES or slot['size'] + size > GROUP_MAX_BYTES:
                break
            slot['values'].append(queue[position])
            slot['size'] += size
            position += 1
    return position


def layout(role: str, existing: List[Dict[str, Any]], want: List[str], taken: set,
           notes: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Place ``want`` into this role's groups, writing as little as possible.

    Entries stay where they are; new ones fill free space from the lowest number,
    then new groups. Emptied groups are deleted. Only a badly fragmented family
    is repacked, and then every entry moves to a group with a lower or equal number.
    ``notes`` are entry comments the groups keep, counted in each request's size.
    """
    base = _base_bytes()

    def size_of(value: str) -> int:
        return _entry_bytes(value, notes.get(canon(value), '')) if notes else _entry_bytes(value)

    wanted: Dict[str, str] = {}
    for value in want:
        wanted.setdefault(canon(value), value)
    placed = set()
    slots, fixed = [], []
    for group in sorted(existing, key=lambda item: item['index']):
        if group.get('frozen'):
            # Another rule uses it too: never edited or deleted here, only
            # referenced as it is when every entry is wanted.
            keys = [canon(value) for value in group['values']]
            if keys and len(set(keys)) == len(keys) and all(key in wanted and key not in placed for key in keys):
                placed.update(keys)
                fixed.append({'name': group['name'], 'index': group['index'], 'before': group['values'],
                              'new': False, 'values': list(group['values'])})
            continue
        kept = []
        for value in group['values']:
            key = canon(value)
            if key in wanted and key not in placed:
                placed.add(key)
                kept.append(value)
        slots.append({'name': group['name'], 'index': group['index'], 'before': group['values'], 'new': False,
                      'values': kept, 'size': base + sum(size_of(value) for value in kept)})
    pending = [value for key, value in wanted.items() if key not in placed]
    used = set(taken) | {slot['index'] for slot in slots} | {group['index'] for group in existing}
    error = ''

    def new_slots(queue, position):
        nonlocal error
        created = []
        while position < len(queue):
            index = next((number for number in range(1, MAX_INDEX + 1) if number not in used), None)
            if index is None:
                error = f'iKuai IP 分组编号 cf_{role}_1…cf_{role}_{MAX_INDEX} 已用尽'
                break
            used.add(index)
            slot = {'name': group_name(role, index), 'index': index, 'before': [], 'new': True,
                    'values': [], 'size': base}
            created.append(slot)
            position = _pack([slot], queue, position, size_of)
            if not slot['values']:
                error = f'iKuai IP 分组条目「{queue[position][:64]}」过长，无法写入'
                break
        return created

    position = _pack(slots, pending, 0, size_of)
    created = new_slots(pending, position)
    groups = [slot for slot in slots + created if slot['values']]
    fixed_keys = {canon(value) for slot in fixed for value in slot['values']}
    needed = _minimum([value for key, value in wanted.items() if key not in fixed_keys], base, size_of)
    if len(groups) > needed + 2:
        # Repack in number order, existing groups first: entries only move down.
        sequence = [value for slot in slots for value in slot['values']] + pending
        for slot in slots:
            slot['values'], slot['size'] = [], base
        for slot in created:
            used.discard(slot['index'])
        position = _pack(slots, sequence, 0, size_of)
        created = new_slots(sequence, position)
    final = sorted([slot for slot in slots + created + fixed if slot['values']], key=lambda slot: slot['index'])
    result = {
        'final': [(slot['name'], slot['values']) for slot in final],
        'adds': [(slot['name'], slot['values']) for slot in final if slot['new']],
        'updates': [(slot['name'], slot['values'], slot['before']) for slot in final
                    if not slot['new'] and slot['values'] != slot['before']],
        'deletes': [(slot['name'], slot['before']) for slot in slots if not slot['values']],
        'error': error,
    }
    if not error and len(final) > MAX_RULE_GROUPS:
        result['error'] = (f'iKuai IP 分组需要 {len(final)} 个，超过单条规则可引用的 {MAX_RULE_GROUPS} 个'
                           f'（每组最多 {GROUP_MAX_ENTRIES} 个网段），请减少代理网段（例如少选规则集）后重试')
    return result


def _minimum(values: List[str], base: int, size_of=_entry_bytes) -> int:
    slots: List[Dict[str, Any]] = []
    position = 0
    while position < len(values):
        slot = {'values': [], 'size': base}
        slots.append(slot)
        position = _pack([slot], values, position, size_of)
    return len(slots)


# --- planning ---------------------------------------------------------------------------------

def span(names: List[str]) -> str:
    """``cf_pa_1…cf_pa_3`` for consecutive groups, otherwise each name."""
    if not names:
        return '（无）'
    if len(names) == 1:
        return names[0]
    roles = {name_role(name)[0] for name in names}
    numbers = [name_role(name)[1] for name in names]
    if len(roles) == 1 and numbers == list(range(numbers[0], numbers[0] + len(numbers))):
        return f'{names[0]}…{names[-1]}'
    return '、'.join(names)


def _samples(values: List[str], limit: int = 5) -> str:
    shown = '、'.join(values[:limit])
    return shown + ('…' if len(values) > limit else '')


class GroupPlanner:
    """The group side of one release plan, shared by the routes and NAT stages."""

    def __init__(self, gstate: Dict[str, Any], warnings: List[str], blockers: List[str]):
        self.groups = gstate['groups']
        self.gstate = gstate
        self.rules = gstate['rules']
        self.warnings = warnings
        self.blockers = blockers
        self.finals: Dict[str, List[str]] = {}
        self.contents: Dict[str, Dict[str, List[str]]] = {}
        # Rule key -> the managed group names its destination will reference after this plan.
        self.rewritten: Dict[str, List[str]] = {}
        self.deleted_rules: set = set()
        self.migrations: List[Dict[str, Any]] = []
        self.route_rules: List[Dict[str, Any]] = []
        self.stage_checks: Dict[str, Dict[str, Any]] = {}
        self.marked: set = set()

    def notes(self, role: str) -> Dict[str, str]:
        """The user's entry comments in this role's groups; they follow their entries."""
        notes: Dict[str, str] = {}
        for group in self.owned(role):
            for key, note in entry_notes(group).items():
                notes.setdefault(key, note)
        return notes

    def mark_ops(self, role: str) -> List[Dict[str, Any]]:
        """Write the marker back on adopted groups first; later ops then find them ours."""
        ops = []
        notes = self.notes(role)
        for group in self.owned(role):
            if not group.get('adopted') or group['name'] in self.marked:
                continue
            self.marked.add(group['name'])
            ops.append({'kind': 'ikuai_group_update', 'name': group['name'], 'role': role, 'comment': ROLES[role],
                        'values': group['values'], 'expected': digest(group['values']), 'added': [], 'removed': [],
                        'mark': True, **_notes_for(notes, group['values']),
                        'summary': f'恢复 IP 分组「{group["name"]}」的 ConfigFlow 归属标记：第一个条目的备注不是'
                                   f'「{ROLES[role]}」（可能在 iKuai 中被改过），{ROLE_RULES[role]}仍在引用它，'
                                   f'改回标记，网段和其他备注不变'})
        return ops

    def owned(self, role: str) -> List[Dict[str, Any]]:
        return sorted((group for group in self.groups if group['role'] == role), key=lambda group: group['index'])

    def taken(self, role: str) -> set:
        return {parsed[1] for parsed in (name_role(group['name']) for group in self.groups)
                if parsed and parsed[0] == role}

    def refs(self, key: Optional[str]) -> Dict[str, Any]:
        return self.rules.get(key) or {'custom': [], 'objects': []} if key else {'custom': [], 'objects': []}

    def referenced_values(self, key: Optional[str]) -> List[str]:
        """Entries of every managed group the rule's destination references (any role)."""
        values = []
        for ref in self.refs(key)['objects']:
            kind, group = classify(self.groups, ref)
            if kind == 'owned':
                values.extend(group['values'])
        return values

    def shared(self, group: Dict[str, Any], subject: Optional[str]) -> bool:
        """Whether anything but ``subject``'s destination uses the group (and keeps it):
        another rule, or a feature ConfigFlow does not read."""
        return hidden_refs(group) > 0 or any(
            user['rule'] not in self.deleted_rules and (user['rule'] != subject or user['field'] != 'dst_addr')
            for user in group['users'])

    def family(self, role: str, want: List[str], subject: Optional[str]) -> Tuple[List[str], List[Dict[str, Any]]]:
        """Group adds and updates for one role; their final names.

        ``subject`` is the rule that will reference the family. A group some other
        rule uses as well is frozen: editing it would change that rule too.
        """
        existing = [{**group, 'frozen': self.shared(group, subject)} for group in self.owned(role)]
        notes = self.notes(role)
        result = layout(role, existing, want, self.taken(role), notes)
        if result['error']:
            self.blockers.append(result['error'])
        self.finals[role] = [name for name, _ in result['final']]
        self.contents[role] = dict(result['final'])
        user = ROLE_RULES[role]
        ops = self.mark_ops(role)
        for name, values in result['adds']:
            ops.append({'kind': 'ikuai_group_add', 'name': name, 'role': role, 'comment': ROLES[role], 'values': values,
                        **_notes_for(notes, values),
                        'summary': f'新建 IP 分组「{name}」（{user}使用，{len(values)} 个网段）'})
        for name, values, before in result['updates']:
            old, new = {canon(value) for value in before}, {canon(value) for value in values}
            added = [value for value in values if canon(value) not in old]
            removed = [value for value in before if canon(value) not in new]
            parts = []
            if added:
                parts.append(f'加入 {len(added)} 个')
            if removed:
                parts.append(f'移除 {len(removed)} 个')
            detail = '、'.join(parts) or '整理重复条目'
            examples = '；'.join(item for item in (f'加入 {_samples(added)}' if added else '',
                                                    f'移除 {_samples(removed)}' if removed else '') if item)
            ops.append({'kind': 'ikuai_group_update', 'name': name, 'role': role, 'comment': ROLES[role],
                        'values': values, 'expected': digest(before), 'added': added, 'removed': removed,
                        **_notes_for(notes, values),
                        'summary': f'更新 IP 分组「{name}」：{detail}网段（现 {len(values)} 个）'
                                   + (f'：{examples}' if examples else '')})
        return self.finals[role], ops

    def needs_rewrite(self, key: Optional[str], role: str, keep_foreign: bool) -> bool:
        if key is None or key not in self.rules:
            return True
        rule = self.rules[key]
        if rule['custom']:
            return True
        names = []
        for ref in rule['objects']:
            kind, group = classify(self.groups, ref)
            if kind == 'stale' or (kind == 'foreign' and not keep_foreign):
                return True
            if kind == 'owned':
                if group['role'] != role or ref.get('gp_name') != group['name']:
                    return True
                names.append(group['name'])
        return sorted(names) != sorted(self.finals.get(role, [])) or len(set(names)) != len(names)

    def dropped_foreign(self, key: Optional[str]) -> List[str]:
        return [str(ref.get('gp_name') or ref.get('gid') or '') for ref in self.refs(key)['objects']
                if classify(self.groups, ref)[0] == 'foreign']

    def migration(self, key: Optional[str], kind: str, rule: str) -> int:
        count = len(self.refs(key)['custom'])
        if count:
            self.migrations.append({'rule': rule, 'kind': kind, 'count': count})
        return count

    def cleanup(self, roles: Tuple[str, ...]) -> List[Dict[str, Any]]:
        """Delete our groups of these roles that nothing will reference; warn about the rest.

        Returns marker write-backs (before their deletes) and the deletes.
        """
        ops = []
        for role in roles:
            self._unmarked_hints(role)
            # Any adopted group no family wrote back yet (e.g. only a disabled rule uses it).
            ops.extend(self.mark_ops(role))
            final = set(self.finals.get(role, []))
            for group in self.owned(role):
                if group['name'] in final:
                    continue
                users = [user for user in group['users']
                         if user['rule'] not in self.deleted_rules
                         and not (user['field'] == 'dst_addr' and user['rule'] in self.rewritten
                                  and group['name'] not in self.rewritten[user['rule']])]
                hidden = hidden_refs(group)
                if users and not hidden and role == 'nat' and all(
                        user['field'] == 'dst_addr' and _role_rule('nat', self.rules[user['rule']]) for user in users):
                    # Another managed NAT bypass rule's destinations (e.g. one added while
                    # the target was disabled): ConfigFlow's own, kept as it is.
                    continue
                if users or hidden:
                    names = '、'.join(dict.fromkeys(f'{user["label"]}「{user["comment"] or user["rule"]}」'
                                                    for user in users))
                    owner = '规则' if all(_is_managed(user['comment']) for user in users) else '手工规则'
                    unseen = f'iKuai 中 ConfigFlow 读不到的配置（如 ACL、流控）引用 {hidden} 处'
                    if not hidden:
                        by = f'被{owner}{names}引用'
                    elif users:
                        by = f'被{owner}{names}引用，还被 {unseen}'
                    else:
                        by = f'还被 {unseen}'
                    self.warnings.append(f'ConfigFlow 的 IP 分组 {group["name"]} {by}，已保留，请先在 iKuai 中解除引用')
                    continue
                ops.append({'kind': 'ikuai_group_delete', 'name': group['name'], 'role': role,
                            'expected': digest(group['values']), 'count': len(group['values']),
                            'summary': f'删除不再使用的 IP 分组「{group["name"]}」（原 {len(group["values"])} 个网段）'})
        return ops

    def _unmarked_hints(self, role: str) -> None:
        """A group with our name but no marker that nothing uses: the user's, so left
        alone; but if ConfigFlow made it (its first entry edited), say how to get it back."""
        for group in self.groups:
            parsed = name_role(group['name'])
            if (parsed and parsed[0] == role and not group['role'] and not group.get('dup')
                    and not group['users'] and not hidden_refs(group)):
                self.warnings.append(
                    f'IP 分组「{group["name"]}」名称是 ConfigFlow 的格式，但第一个条目的备注不是「{ROLES[role]}」，'
                    f'按用户分组处理（不修改、不删除，只占用这个序号）。如果它是 ConfigFlow 建的，把第一个条目的备注'
                    f'改回「{ROLES[role]}」后重新预览即可清理；如果是你自己的分组，建议改名')

    def check(self, roles: Tuple[str, ...], rules: List[Dict[str, Any]], removed: List[str]) -> Dict[str, Any]:
        return {'groups': {name: digest(values) for role in roles
                           for name, values in self.contents.get(role, {}).items()},
                'rules': rules, 'removed': removed}

    def migration_warning(self) -> Optional[str]:
        if not self.migrations:
            return None
        rules = '和'.join(f'{item["kind"]}「{item["rule"]}」' for item in self.migrations)
        total = sum(item['count'] for item in self.migrations)
        families = '、'.join(dict.fromkeys(
            'cf_nat_*' if item['kind'] == 'NAT 免转换' else
            ('cf_pm_*' if item['rule'] == ROLES['pm'] else 'cf_pa_*') for item in self.migrations))
        return (f'将把 iKuai {rules}中直接填写的 {total} 个目的网段移入 ConfigFlow 管理的 IP 分组（{families}），'
                '规则改为引用分组。原因：单条规则直接填写约 5000 个网段就会超过 iKuai 单次请求上限，'
                '大规则写入还容易超时；改用分组后，以后增删网段只改分组内容，不再重写整条规则。'
                '本次发布失败会自动恢复原规则并删除新建的分组。')


def _notes_for(notes: Dict[str, str], values: List[str]) -> Dict[str, Any]:
    """``{'notes': ...}`` for the entries of ``values`` that carry one, else nothing."""
    picked = {key: notes[key] for key in (canon(value) for value in values) if key in notes} if notes else {}
    return {'notes': picked} if picked else {}


def _is_managed(comment: str) -> bool:
    from backend.routers.base import is_managed
    return is_managed(comment)


def nat_target(gstate: Dict[str, Any], managed: List[Any]):
    """The managed NAT bypass rule whose destinations live in the cf_nat groups.

    Stable across releases: the rule already referencing cf_nat groups; before
    any does, the one listing the most CIDRs (the rule earlier releases added
    to), the first on ties. Other managed rules keep what they list.
    """
    groups, rules = gstate['groups'], gstate['rules']

    def grouped(rule) -> bool:
        return any(kind == 'owned' and group['role'] == 'nat'
                   for kind, group in (classify(groups, ref)
                                       for ref in (rules.get(f'nat:{rule.id}') or {}).get('objects', [])))

    holder = next((rule for rule in managed if grouped(rule)), None)
    if holder is not None or not managed:
        return holder
    return max(managed, key=lambda rule: len((rules.get(f'nat:{rule.id}') or {}).get('custom') or []))


def report_section(state) -> Dict[str, Any]:
    """Pending migrations and unreferenced groups; either keeps the router out of sync."""
    from backend.routers.base import POLICY_AUTO_COMMENT, POLICY_MANUAL_COMMENT, is_managed, policy_route
    gstate = state.ikuai_groups
    migrations = []
    if state.policy_routes is not None:
        for comment in (POLICY_AUTO_COMMENT, POLICY_MANUAL_COMMENT):
            rule = policy_route(state, comment)
            custom = (gstate['rules'].get(f'policy:{rule.id}') or {}).get('custom') if rule else None
            if custom:
                migrations.append({'rule': comment, 'kind': '端口分流', 'count': len(custom)})
    target = nat_target(gstate, [rule for rule in state.nat_bypass if rule.enabled and is_managed(rule.comment)])
    custom = (gstate['rules'].get(f'nat:{target.id}') or {}).get('custom') if target else None
    if custom:
        migrations.append({'rule': target.comment, 'kind': 'NAT 免转换', 'count': len(custom)})
    orphans = [group['name'] for group in gstate['groups']
               if group['role'] and not group['users'] and not hidden_refs(group)]
    # Groups whose marker a release writes back: until then only the rule ties them to us.
    marks = [group['name'] for group in gstate['groups'] if group.get('adopted')]
    return {'migrations': migrations, 'orphans': orphans, 'marks': marks}


def verify(stage_checks: Dict[str, Any], state) -> None:
    """Read back the group structure one stage planned: contents, references, removals."""
    from backend.routers.base import RouterError
    gstate = state.ikuai_groups
    if gstate is None:
        raise RouterError('回读校验失败：无法读取 iKuai IP 分组')
    groups = gstate['groups']
    by_name: Dict[str, List[Dict[str, Any]]] = {}
    for group in groups:
        by_name.setdefault(group['name'], []).append(group)
    for name, expected in stage_checks['groups'].items():
        found = by_name.get(name, [])
        # Marked, not just adopted: every group the stage planned now carries the marker.
        if (len(found) != 1 or not found[0]['role'] or found[0].get('adopted')
                or digest(found[0]['values']) != expected):
            raise RouterError(f'回读校验失败：IP 分组「{name}」内容与计划不一致')
    for check in stage_checks['rules']:
        candidates = [rule for rule in gstate['rules'].values()
                      if rule['func'] == check['func'] and rule['enabled']
                      and (rule['id'] == check['id'] if check.get('id') else rule['comment'] == check['comment'])
                      and (check['func'] != 'nat_rule' or rule['action'] == 'filter')]
        if check['func'] == 'stream_ipport':
            candidates = candidates[:1]
        if not any(_rule_matches(groups, rule, check) for rule in candidates):
            label = '策略路由' if check['func'] == 'stream_ipport' else 'NAT 免转换规则'
            raise RouterError(f'回读校验失败：{label}「{check["comment"]}」没有按计划只引用 ConfigFlow 的 IP 分组')
    for name in stage_checks['removed']:
        if any(group['role'] for group in by_name.get(name, [])):
            raise RouterError(f'回读校验失败：应删除的 IP 分组「{name}」仍然存在')


def _rule_matches(groups, rule, check) -> bool:
    if rule['custom']:
        return False
    names = []
    for ref in rule['objects']:
        kind, group = classify(groups, ref)
        if kind == 'owned':
            if ref.get('gp_name') != group['name']:
                return False
            names.append(group['name'])
        elif kind == 'stale' or not check.get('keep_foreign'):
            return False
    return sorted(names) == sorted(check['groups'])
