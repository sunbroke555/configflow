"""LAN clients of an iKuai 4.x router.

``monitor_lanip`` (终端监控) lists the clients that sent traffic through the
router in roughly the last two minutes: those are online. ``dhcp_lease`` only
adds addresses to discover. Field names follow the 4.x web API as published by
jakeslee/ikuai (v4/action/monitor.go): ip_addr, mac, hostname, termname,
comment, interface, uptime. Parsing is tolerant on purpose: rows without an
IPv4 address are skipped and unknown fields ignored, so captured firmware
responses can replace the fixtures without code changes.
"""
from typing import List, Optional

from backend.routers.base import RouterError
from backend.routers.ikuai import decode_value
from backend.routers.lan_clients import Reading, Sighting, ipv4, normalize_mac

PAGE = 1000
MAX_PAGES = 10
NOTE_TRAFFIC = '按 2 分钟内经过路由器的流量判断在线'


def rows(driver, func_name: str) -> List[dict]:
    """All rows of a ``show`` list, page by page (``TYPE: data,total``)."""
    result: List[dict] = []
    for page in range(MAX_PAGES):
        results = driver.call(func_name, {'TYPE': 'data,total', 'limit': f'{page * PAGE},{PAGE}'})
        data = results.get('data')
        data = [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []
        result.extend(data)
        try:
            total = int(results.get('total'))
        except (TypeError, ValueError):
            total = None
        if len(data) < PAGE or (total is not None and len(result) >= total):
            break
    return result


def _text(row: dict, key: str) -> str:
    value = row.get(key)
    return str(decode_value(value, key)).strip() if isinstance(value, (str, int)) else ''


def _name(row: dict) -> str:
    return next((text for key in ('comment', 'termname', 'hostname') if (text := _text(row, key))), '')


def _address(row: dict) -> str:
    return next((ip for key in ('ip_addr', 'ip', 'addr') if (ip := ipv4(row.get(key)))), '')


def parse_terminal(row: dict) -> Optional[Sighting]:
    ip = _address(row)
    if not ip:
        return None
    return Sighting(ip=ip, mac=normalize_mac(row.get('mac')), hostname=_name(row), sources={'online'},
                    presence='online', basis='terminal')


def parse_lease(row: dict) -> Optional[Sighting]:
    ip = _address(row)
    if not ip:
        return None
    # Not in the terminal monitor: no traffic in the last ~2 minutes.
    return Sighting(ip=ip, mac=normalize_mac(row.get('mac')), hostname=_name(row), sources={'lease'},
                    presence='offline', basis='absent')


def read(driver, priority=(), deadline=None) -> Reading:
    reading = Reading(notes=[NOTE_TRAFFIC])
    for row in rows(driver, 'monitor_lanip'):
        sighting = parse_terminal(row)
        if sighting:
            current = reading.sightings.get(sighting.ip)
            if current:
                current.mac = current.mac or sighting.mac
                current.hostname = current.hostname or sighting.hostname
            else:
                reading.sightings[sighting.ip] = sighting
    try:
        leases = rows(driver, 'dhcp_lease')
    except RouterError as error:
        reading.notes.append(f'无法读取 DHCP 租约：{error}')
        leases = []
    for row in leases:
        sighting = parse_lease(row)
        if not sighting:
            continue
        current = reading.sightings.get(sighting.ip)
        if current:
            current.sources.add('lease')
            current.mac = current.mac or sighting.mac
            current.hostname = current.hostname or sighting.hostname
        else:
            reading.sightings[sighting.ip] = sighting
    return reading
