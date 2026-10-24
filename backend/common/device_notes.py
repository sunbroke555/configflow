"""Display-only client names shared by Mihomo and OxiDNS in one profile.

``device_notes`` is the only place a name is written. Policy rows in the OxiDNS
settings may still carry names saved by older versions; they are a fallback for
addresses that ``device_notes`` does not mention, and an empty stored name hides
such a legacy name. Renaming a client therefore never changes the OxiDNS
settings, their revisions or anything a router sync compares.
"""
from hashlib import sha256
import json

from backend.common.oxidns_device_policy import normalize_client_range, normalize_policies
from backend.common.oxidns_settings import OxidnsSettingsError

MAX_NOTES = 2048


def normalize_name(value):
    if not isinstance(value, str) or len(value) > 80 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise OxidnsSettingsError('设备备注最多 80 个字符，不能包含换行或控制字符。')
    return value.strip()


def normalize_notes(value):
    if not isinstance(value, dict) or len(value) > MAX_NOTES:
        raise OxidnsSettingsError(f'设备备注必须为对象，最多 {MAX_NOTES} 条。')
    result = {}
    for address, name in value.items():
        ip = normalize_client_range(address)
        if ip in result:
            raise OxidnsSettingsError(f'设备备注地址重复：{ip}')
        result[ip] = normalize_name(name)
    return result


def _legacy_names(config):
    rows = normalize_policies(config.get('oxidns', {}).get('device_policies', []))
    return {row['ip']: row['name'] for row in rows if row['name']}


def stored_names(config):
    """Every address with a known name, including empty names that hide a legacy one."""
    return {**_legacy_names(config), **normalize_notes(config.get('device_notes', {}))}


def read_notes(config):
    return {ip: name for ip, name in stored_names(config).items() if name}


def _assign(config, notes, ip, name):
    if name:
        notes[ip] = name
    elif ip in _legacy_names(config):
        notes[ip] = ''
    else:
        notes.pop(ip, None)


def set_note(config, ip, name):
    notes = normalize_notes(config.get('device_notes', {}))
    _assign(config, notes, ip, name)
    config['device_notes'] = normalize_notes(notes)


def policy_names_revision(config):
    """Names shown next to the stored policy rows, for editors that save them."""
    names = read_notes(config)
    rows = normalize_policies(config.get('oxidns', {}).get('device_policies', []))
    text = json.dumps([[row['ip'], names.get(row['ip'], '')] for row in rows],
                      ensure_ascii=False, separators=(',', ':'))
    return sha256(text.encode()).hexdigest()


def prepare_policy_notes(config, rows):
    rows = normalize_policies(rows)
    names = read_notes(config)
    notes = normalize_notes(config.get('device_notes', {}))
    previous = {row['ip'] for row in normalize_policies(config.get('oxidns', {}).get('device_policies', []))}
    for row in rows:
        if row['ip'] not in previous and not row['name']:
            row['name'] = names.get(row['ip'], '')
        _assign(config, notes, row['ip'], row['name'])
    kept = {row['ip'] for row in rows}
    for ip, name in _legacy_names(config).items():
        if ip in kept:
            continue
        if notes.get(ip) == '':
            # The legacy name leaves with its row, so nothing is left to hide.
            notes.pop(ip)
        elif ip not in notes and len(notes) < MAX_NOTES:
            # Keep a removed row's legacy name for Mihomo and DNS logs while there is room.
            notes[ip] = name
    return rows, normalize_notes(notes)


def save_policy_rows(config, settings, rows, base_url=''):
    """Write names to device_notes; touch the OxiDNS settings only for address or mode changes."""
    from backend.common.oxidns_device_policy import apply_policies

    rows, notes = prepare_policy_notes(config, rows)
    config['device_notes'] = notes
    current = normalize_policies(settings.get('device_policies', []))
    if [(row['ip'], row['mode']) for row in rows] == [(row['ip'], row['mode']) for row in current]:
        return settings
    return apply_policies(settings, config, rows, base_url)
