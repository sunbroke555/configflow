"""Display-only client names shared by Mihomo and OxiDNS in one profile."""
from copy import deepcopy

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


def read_notes(config):
    notes = normalize_notes(config.get('device_notes', {}))
    # Existing DNS policy names remain editable through the original settings UI.
    for row in normalize_policies(config.get('oxidns', {}).get('device_policies', [])):
        if row['name']:
            notes[row['ip']] = row['name']
        else:
            notes.pop(row['ip'], None)
    return notes


def set_note(config, ip, name):
    notes = read_notes(config)
    if name:
        notes[ip] = name
    else:
        notes.pop(ip, None)
    config['device_notes'] = normalize_notes(notes)
    settings = deepcopy(config.get('oxidns', {}))
    for section in (settings, settings.get('template_settings', {})):
        if not isinstance(section, dict):
            continue
        for row in section.get('device_policies', []):
            if normalize_client_range(row['ip']) == ip:
                row['name'] = name
    # Do not create a DNS policy or regenerate the native configuration for a note.
    config['oxidns'] = settings


def prepare_policy_notes(config, rows):
    rows = normalize_policies(rows)
    notes = read_notes(config)
    previous = {row['ip'] for row in normalize_policies(config.get('oxidns', {}).get('device_policies', []))}
    for row in rows:
        if row['ip'] not in previous and not row['name']:
            row['name'] = notes.get(row['ip'], '')
        if row['name']:
            notes[row['ip']] = row['name']
        else:
            notes.pop(row['ip'], None)
    return rows, normalize_notes(notes)
