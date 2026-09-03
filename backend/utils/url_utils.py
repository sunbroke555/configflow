"""Helpers for rendering URLs in logs without exposing credentials."""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, unquote, unquote_plus, urlencode, urlsplit, urlunsplit

_REDACTED = "[REDACTED]"
_INVALID = "[INVALID URL REDACTED]"
_MAX_DECODE_PASSES = 8
_SENSITIVE_KEY_PARTS = (
    "token",
    "authorization",
    "auth",
    "key",
    "secret",
    "password",
    "passwd",
    "credential",
)
_BAD_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")


def _has_control(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def _decode_layers(value: str, *, plus: bool = False) -> str:
    decoder = unquote_plus if plus else unquote
    for _ in range(_MAX_DECODE_PASSES):
        if _has_control(value) or _BAD_PERCENT_ESCAPE.search(value):
            raise ValueError("unsafe encoded value")
        decoded = decoder(value)
        if decoded == value:
            return value
        value = decoded
    if decoder(value) != value:
        raise ValueError("too many encoding layers")
    if _has_control(value) or _BAD_PERCENT_ESCAPE.search(value):
        raise ValueError("unsafe decoded value")
    return value


def _is_sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", _decode_layers(key).lower())
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _contains_sensitive_expression(value: str) -> bool:
    for component in re.split(r"[?&;]", value):
        key, separator, _ = component.partition("=")
        if separator and _is_sensitive_key(key.strip()):
            return True
    return False


def _sanitize_nested_value(value: str, depth: int) -> str:
    decoded = _decode_layers(value, plus=True)
    if _contains_sensitive_expression(decoded):
        return _REDACTED
    if depth < 2 and ("://" in decoded or decoded.startswith("/")) and "?" in decoded:
        nested = _safe_url(decoded, depth + 1)
        return _REDACTED if _REDACTED in nested else nested
    return decoded


def _safe_url(value: str, depth: int = 0) -> str:
    if not isinstance(value, str) or not value or _has_control(value):
        raise ValueError("invalid URL")
    if _BAD_PERCENT_ESCAPE.search(value):
        raise ValueError("invalid percent escape")

    decoded = _decode_layers(value)
    parsed = urlsplit(decoded)
    if parsed.scheme in ("http", "https") and not parsed.hostname:
        raise ValueError("URL has no host")
    if not parsed.scheme and not decoded.startswith("/"):
        raise ValueError("URL is neither absolute nor relative")

    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    port = parsed.port
    netloc = hostname + (f":{port}" if port is not None else "")

    query = []
    for key, query_value in parse_qsl(parsed.query, keep_blank_values=True):
        decoded_key = _decode_layers(key)
        if _is_sensitive_key(decoded_key):
            query.append((decoded_key, _REDACTED))
        else:
            query.append((decoded_key, _sanitize_nested_value(query_value, depth)))

    # Fragments are commonly used for credentials by subscription providers.
    # Drop them entirely: decoding and selectively redacting nested fragments
    # is too easy to bypass with mixed encodings.
    fragment = ""

    return urlunsplit((parsed.scheme, netloc, parsed.path, urlencode(query, doseq=True), fragment))


def safe_url_for_log(url: object) -> str:
    """Return a log-safe URL, failing closed for malformed input.

    User information is removed. Sensitive query keys (including encoded and
    common compound variants) retain their names but have redacted values.
    Several percent-encoding layers are decoded before inspection so encoding
    cannot be used to bypass redaction.
    """
    try:
        return _safe_url(url)  # type: ignore[arg-type]
    except (TypeError, ValueError, UnicodeError):
        return _INVALID


def safe_exception_details(error: BaseException) -> str:
    """Return bounded exception metadata without rendering its message or URL."""
    exception_type = re.sub(r"[^A-Za-z0-9_.-]", "", type(error).__name__)[:80] or "Exception"
    details = f"exception_type={exception_type}"
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        details += f" status={status}"
    return details
