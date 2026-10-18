"""Small, sanitized timelines for long operations, separate from recovery state."""
from datetime import datetime, timezone
import re

from .diagnostics import failure_text

EVENT_LIMIT = 80
MESSAGE_LIMIT = 1024
_STATUS = re.compile(r'^[a-z][a-z0-9_]{0,63}$')
_URL = re.compile(r'[a-zA-Z][a-zA-Z0-9+.-]*://[^\s<>"\'\\]+')


def event_message(value, secrets=()):
    if isinstance(value, str):
        value = _URL.sub('[下载地址已隐藏]', value)
    return failure_text(value, secrets, limit=MESSAGE_LIMIT)


def event_time(value=None):
    if value is None:
        moment = datetime.now(timezone.utc)
    elif isinstance(value, str):
        if len(value) > 64:
            return None
        try:
            moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except (ValueError, OverflowError):
            return None
        if moment.tzinfo is None:
            return None
    else:
        return None
    try:
        return moment.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')
    except (ValueError, OverflowError):
        return None


def clean_events(value, secrets=(), statuses=None):
    if not isinstance(value, list):
        return []
    result = []
    for entry in value[-EVENT_LIMIT:]:
        if not isinstance(entry, dict):
            continue
        status = entry.get('status')
        at = event_time(entry.get('at')) if isinstance(entry.get('at'), str) else None
        if (not isinstance(status, str) or not _STATUS.fullmatch(status) or not at
                or statuses is not None and status not in statuses):
            continue
        event = {'at': at, 'status': status}
        message = event_message(entry.get('message'), secrets)
        if message:
            event['message'] = message
        if not result or _content(result[-1]) != _content(event):
            result.append(event)
    return result


def _content(event):
    return event['status'], event.get('message', '')


def append_event(events, status, message=None, *, at=None, secrets=()):
    result = clean_events(events, secrets)
    event = clean_events([{'at': at or event_time(), 'status': status, 'message': message}], secrets)
    if event and (not result or _content(result[-1]) != _content(event[0])):
        result.append(event[0])
    return result[-EVENT_LIMIT:]


def merge_events(events, incoming, *, secrets=(), statuses=None):
    """Merge an Agent journal without replaying already-observed stages each poll."""
    previous = clean_events(events, secrets, statuses)
    received = clean_events(incoming, secrets, statuses)
    if not received:
        return previous
    # A full worker journal replaces our earlier bare observations of its stages.
    # Keep explicit panel diagnostics (such as connection uncertainty) intact.
    reported_statuses = {event['status'] for event in received}
    previous = [event for event in previous if event.get('message') or event['status'] not in reported_statuses]
    merged = {(event['at'], *_content(event)): event for event in [*previous, *received]}
    ordered = sorted(merged.values(), key=lambda event: event['at'])
    return clean_events(ordered, secrets, statuses)
