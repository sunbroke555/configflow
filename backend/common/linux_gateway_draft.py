"""Read-only gateway trials and recoverable, explicitly saved profile drafts.

This layer never writes generated YAML back to either service's base settings.
The caller commits settings and their recovery point in one profile transaction.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import re
import secrets
from urllib.parse import urlsplit, urlunsplit

import yaml

from backend.common.config_repository import ProfileInUse
from backend.common.linux_gateway import (
    LinuxGatewayError, direct_overlap_warnings, gateway_settings, validate_gateway_selection,
)
from backend.common.linux_gateway_scheme import SCHEME_LABELS
from backend.converters.mihomo import generate_mihomo_config

RESTORE_KEY = 'linux_gateway_restore_point'


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def check_revision(expected, revision):
    if type(expected) is not int or expected < 0:
        raise LinuxGatewayError('缺少有效配置版本，请重新打开 Linux 网关设置')
    if expected != revision:
        raise ProfileInUse('配置或共享资源已更新，请重新打开 Linux 网关设置并预览')


def restore_point(config):
    point = config.get('mihomo', {}).get(RESTORE_KEY)
    if (isinstance(point, dict) and point.get('version') == 1
            and isinstance(point.get('id'), str) and point['id']
            and isinstance(point.get('created_at'), str)
            and all(isinstance(point.get(key), dict) for key in ('mihomo', 'oxidns'))
            and RESTORE_KEY not in point['mihomo']):
        return point
    return None


def restore_summary(config):
    point = restore_point(config)
    return {'id': point['id'], 'created_at': point['created_at']} if point else None


def prepare_settings(config, value, takeover=None):
    """Validate dialog settings; ``takeover(config)`` gives router-scheme IDs."""
    if isinstance(value, dict):
        value = dict(value)
        if value.get('scheme') is None:
            # Older clients omit the scheme; never let a save clear it.
            value.pop('scheme', None)
            stored = gateway_settings(config).get('scheme')
            if stored:
                value['scheme'] = stored
        if value.get('scheme') == 'router' and takeover is not None:
            # Router scheme mirrors 上游路由; a client list cannot diverge.
            value['rule_set_ids'] = takeover(config)['rule_set_ids']
    candidate = deepcopy(config)
    candidate.setdefault('mihomo', {})['linux_gateway'] = validate_gateway_selection(config, value)
    return candidate


def _without_scheme(config):
    return {key: value for key, value in gateway_settings(config).items() if key != 'scheme'}


def prepare_restore(config, takeover=None):
    """Bring back the trial snapshot; ``takeover(config)`` gives router-scheme IDs."""
    point = restore_point(config)
    if not point:
        raise LinuxGatewayError('没有可用的试用前草稿快照')
    candidate = deepcopy(config)
    for key in ('mihomo', 'oxidns'):
        candidate[key] = deepcopy(point[key])
    # Restore the snapshot as a whole, scheme included: switching the scheme is
    # itself a trial that a restore must be able to undo. A restored router
    # scheme follows the current 上游路由 IP rule sets.
    restored = gateway_settings(candidate)
    if restored.get('scheme') == 'router' and takeover is not None:
        candidate['mihomo']['linux_gateway'] = {**restored, 'rule_set_ids': takeover(config)['rule_set_ids']}
    return candidate


def remember_original(config, candidate):
    # Choosing a scheme alone changes no takeover, so it is not a gateway trial.
    if _without_scheme(config) == _without_scheme(candidate):
        return
    if RESTORE_KEY in config.get('mihomo', {}):
        if not restore_point(config):
            raise LinuxGatewayError('原有网关恢复快照无效，请先检查配置备份')
        return
    candidate['mihomo'][RESTORE_KEY] = {
        'version': 1, 'id': secrets.token_hex(16),
        'created_at': datetime.now(timezone.utc).isoformat(),
        'mihomo': deepcopy(config.get('mihomo', {})),
        'oxidns': deepcopy(config.get('oxidns', {})),
    }


def _safe(value, key=''):
    # Opaque editor documents can contain embedded credentials. Show a change
    # fingerprint instead of their contents; preview/editor endpoints disclose
    # only snapshot identity and time, never the stored recovery documents.
    if key in ('custom_config', 'native_config', 'template_base_config',
               'legacy_native_config', 'additional_files'):
        return f'内容摘要 {_digest(value)[:12]}'
    if any(part in key.lower() for part in ('password', 'secret', 'token', 'authorization', 'private-key', 'private_key', 'uuid')):
        return '[已隐藏]'
    if isinstance(value, dict):
        return {k: _safe(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_safe(v, key) for v in value]
    if isinstance(value, str) and '://' in value:
        try:
            parts = urlsplit(value)
            host = parts.netloc.rsplit('@', 1)[-1]
            return urlunsplit((parts.scheme, host, parts.path, '…' if parts.query else '', ''))
        except ValueError:
            return '[地址已隐藏]'
    return value


def _display(value, key):
    text = yaml.safe_dump(_safe(value, key), allow_unicode=True, sort_keys=False).removesuffix('...\n').strip()
    return text if len(text) <= 800 else text[:800] + '\n…'


def _changes(before, after, prefix=''):
    missing = object()
    result = []
    for key in sorted(set(before) | set(after)):
        if key == RESTORE_KEY:
            continue
        left, right = before.get(key, missing), after.get(key, missing)
        if left == right:
            continue
        path = f'{prefix}.{key}' if prefix else str(key)
        if isinstance(left, dict) and isinstance(right, dict):
            result.extend(_changes(left, right, path))
        else:
            result.append({'path': path,
                           'before': '未设置' if left is missing else _display(left, str(key)),
                           'after': '移除' if right is missing else _display(right, str(key))})
    return result


def preview_draft(config, candidate, revision, action, base_url=''):
    warnings = []
    try:
        after = yaml.safe_load(generate_mihomo_config(candidate, base_url, preflight_providers=False))
    except (ValueError, yaml.YAMLError, TypeError) as exc:
        raise LinuxGatewayError(f'无法生成目标 Mihomo 配置：{exc}') from exc
    try:
        before = yaml.safe_load(generate_mihomo_config(config, base_url, preflight_providers=False))
        changes = _changes(before, after, 'Mihomo')
    except (ValueError, yaml.YAMLError, TypeError):
        warnings.append('当前 Mihomo 草稿无法生成；保存目标设置后可修复此问题。')
        changes = [{'path': 'Mihomo', 'before': '当前配置无法生成', 'after': '目标配置生成通过'}]
    warnings.extend(direct_overlap_warnings(candidate))
    # The scheme never reaches Mihomo; show it so the diff is not empty.
    left, right = (gateway_settings(item).get('scheme') for item in (config, candidate))
    if left != right:
        changes.append({'path': 'Linux 网关.使用方式', 'before': SCHEME_LABELS.get(left, '未设置'),
                        'after': SCHEME_LABELS.get(right, '未设置')})
    if action == 'restore':
        # Also reveal edits made since the snapshot, including native DNS
        # changes, without rendering/downloading any OxiDNS rule assets.
        changes += _changes(config.get('mihomo', {}), candidate.get('mihomo', {}), 'Mihomo 草稿')
        changes += _changes(config.get('oxidns', {}), candidate.get('oxidns', {}), 'OxiDNS 草稿')
    # Heartbeats and backup status are global runtime data, not draft inputs.
    # They must not invalidate an otherwise current configuration preview.
    inputs = [{key: value for key, value in item.items() if key not in ('agents', 'backup')}
              for item in (config, candidate)]
    return {
        'success': True, 'action': action, 'config_revision': revision,
        'preview_token': _digest([*inputs, revision, action, base_url]),
        'changes': changes, 'warnings': warnings,
        'restore_point': restore_summary(config),
    }


def check_preview(body, preview):
    token = body.get('preview_token')
    if (not isinstance(token, str) or not re.fullmatch(r'[0-9a-f]{64}', token)
            or not secrets.compare_digest(token, preview['preview_token'])):
        raise ProfileInUse('配置预览已变化或未确认，请重新预览后保存')
