"""Bounded, readable failure details shared by Agent response paths."""
import re
from datetime import datetime
from urllib.parse import quote, quote_plus

from backend.utils.url_utils import safe_url_for_log

DETAIL_LIMIT = 16 * 1024
DNS_CHECK_LIMIT = 32
DNS_NAME_LIMIT = 128
DNS_MESSAGE_LIMIT = 2048
DNS_FIX_LIMIT = 1024
_ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
_URL = re.compile(r'[a-zA-Z][a-zA-Z0-9+.-]*://[^\s<>"\'\\]+')
_AUTH = re.compile(r'(?i)\b(Bearer|Basic)\s+[^\s,"\'<>]+')
_SECRET_KEY = r'(?:password|passwd|passphrase|secret|token|api[_-]?key|private[_-]?key|access[_-]?key|authorization|credential)'
_BLOCK_SECRET = re.compile(
    rf'''(?ix)^(?P<indent>[ \t]*)(?P<item>-\s+)?["']?{_SECRET_KEY}["']?\s*:[ \t]*[|>][0-9+-]*[ \t]*(?:\#.*)?$''')
_PEM = re.compile(r'-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|$)', re.S)
_SECRET = re.compile(
    rf'''(?ix)(["']?\b{_SECRET_KEY}["']?\s*[:=][ \t]*)
    (?:(?:Bearer|Basic)\s+)?(?:"(?:\\.|[^"\\])*"|'(?:''|\\.|[^'\\])*'|[^\s,;}}&]+)''')


def _hide_block_secrets(text):
    """Hide a YAML block value without swallowing the next diagnostic line."""
    lines = text.splitlines(keepends=True)
    result = []
    index = 0
    while index < len(lines):
        line = lines[index]
        match = _BLOCK_SECRET.match(line.rstrip('\r\n'))
        index += 1
        if not match:
            result.append(line)
            continue
        result.append(line[:line.index(':') + 1] + ' [REDACTED]\n')
        indent = len(match['indent']) + len(match['item'] or '')
        while index < len(lines):
            child = lines[index]
            if child.strip() and len(child) - len(child.lstrip(' \t')) <= indent:
                break
            index += 1
    return ''.join(result)


def _safe_diagnostic_url(match):
    # Proxy URI payloads can contain complete encoded credentials rather than a host.
    if match.group().split(':', 1)[0].lower() not in ('http', 'https', 'tcp', 'udp', 'tls', 'quic', 'h3'):
        return '[REDACTED URL]'
    return safe_url_for_log(match.group())


def failure_text(value, secrets=(), limit=DETAIL_LIMIT):
    if not isinstance(value, str):
        return ''
    text = _ANSI.sub('', value)
    text = ''.join(char for char in text if char in '\n\t' or ord(char) >= 32 and ord(char) != 127)
    text = _hide_block_secrets(_PEM.sub('[PRIVATE KEY REDACTED]', text))
    for secret in sorted({s for s in secrets if isinstance(s, str) and s}, key=len, reverse=True):
        for variant in {secret, quote(secret, safe=''), quote_plus(secret)}:
            text = text.replace(variant, '[REDACTED]')
    text = _URL.sub(_safe_diagnostic_url, text)
    text = _AUTH.sub(lambda match: match.group(1) + ' [REDACTED]', text)
    text = _SECRET.sub(lambda match: match.group(1) + '[REDACTED]', text).strip()
    encoded = text.encode('utf-8', errors='replace')
    if len(encoded) > limit:
        suffix = '\n[输出过长，已截断]'
        text = encoded[:limit - len(suffix.encode())].decode('utf-8', errors='ignore') + suffix
    return text


def dns_report_fields(payload, secrets=()):
    """Keep bounded, safe DNS results without inventing a result for old Agents."""
    result = {}
    checks = payload.get('dns_checks')
    if isinstance(checks, list):
        safe_checks = []
        incomplete = len(checks) > DNS_CHECK_LIMIT
        for check in checks[:DNS_CHECK_LIMIT]:
            if (not isinstance(check, dict)
                    or check.get('level') not in ('pass', 'warn', 'error')
                    or not isinstance(check.get('name'), str)
                    or not isinstance(check.get('message'), str)):
                incomplete = True
                continue
            name = failure_text(check['name'], secrets, limit=DNS_NAME_LIMIT)
            message = failure_text(check['message'], secrets, limit=DNS_MESSAGE_LIMIT)
            if not name or not message:
                incomplete = True
                continue
            safe = {'name': name, 'level': check['level'], 'message': message}
            if isinstance(check.get('fix'), str):
                fix = failure_text(check['fix'], secrets, limit=DNS_FIX_LIMIT)
                if fix:
                    safe['fix'] = fix
            safe_checks.append(safe)
        if incomplete:
            # A partial report must not look like an exhaustive all-pass result.
            safe_checks = safe_checks[:DNS_CHECK_LIMIT - 1]
            safe_checks.append({'name': 'DNS 报告完整性', 'level': 'warn',
                                'message': '部分 DNS 检测结果格式无效或数量过多，未完整显示。',
                                'fix': '请减少重复监听配置，或检查 Agent 返回的 DNS 检测结果。'})
        result['dns_checks'] = safe_checks
    checked_at = payload.get('dns_checked_at')
    if isinstance(checked_at, str) and len(checked_at) <= 64 and 'T' in checked_at:
        try:
            parsed = datetime.fromisoformat(checked_at.replace('Z', '+00:00'))
        except ValueError:
            pass
        else:
            if parsed.tzinfo is not None:
                result['dns_checked_at'] = checked_at
    return result


def response_failure(response, *, fallback, secrets=()):
    """Keep a legacy client's explanation even when its HTTP status is non-200."""
    try:
        payload = response.json()
    except ValueError:
        payload = None
    result = {'success': False}
    if isinstance(payload, dict):
        for key in ('message', 'error', 'error_details', 'rollback_error', 'rollback_error_details', 'failed_stage'):
            if isinstance(payload.get(key), str):
                result[key] = failure_text(payload[key], secrets)
    if not result.get('message'):
        result['message'] = result.get('error') or fallback
    # Plain text is useful for older clients; HTML proxy error pages are not diagnostics.
    if not isinstance(payload, dict) and response.headers.get('Content-Type', '').split(';')[0] == 'text/plain':
        detail = failure_text(response.text, secrets)
        if detail:
            result['error_details'] = detail
    if response.status_code >= 400:
        result['http_status'] = response.status_code
    return result
