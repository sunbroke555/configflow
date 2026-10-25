"""LAN clients of an iKuai 4.x router.

``monitor_lanip`` (终端监控) lists the clients that sent traffic through the
router in roughly the last two minutes: those are online. ``dhcp_lease`` only
adds addresses to discover. ``lan`` gives the router's own LAN addresses and
subnets: only inside them does a missing address count as offline.

Shapes verified on 4.0.314 (fixtures: backend/testdata/ikuai_clients_40314.json):
  - ``show`` with ``TYPE: data,total`` and ``limit: "<offset>,<count>"`` returns
    ``{"data": [...], "total": N}``; without TYPE only ``data`` comes back.
  - monitor_lanip rows: ip_addr, mac, interface, comment, termname, uptime
    ("YYYY-MM-DD HH:MM:SS"), static_status ... ``hostname`` is present only for
    DHCP clients; static-IP rows have no such key.
  - dhcp_lease rows: ip_addr, mac, hostname, termname, interface, start_time and
    end_time (router epoch; start_time moves on every renewal), timeout (seconds
    left), static_status, status.
  - lan rows: ``ip_mask`` like "192.168.77.1/255.255.255.0".
Rows without an IPv4 address are skipped and unknown fields ignored.
"""
import ipaddress
from typing import List, Optional

from backend.routers.base import RouterError
from backend.routers.ikuai import decode_value
from backend.routers.lan_clients import Reading, Sighting, ipv4, normalize_mac

PAGE = 1000
MAX_PAGES = 10
NOTE_TRAFFIC = '按 2 分钟内经过路由器的流量判断在线'
NOTE_NO_LAN = '无法读取路由器的局域网地址，没有出现的已保存设备显示为未知'


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
    return ipv4(row.get('ip_addr'))


def parse_terminal(row: dict) -> Optional[Sighting]:
    ip = _address(row)
    if not ip:
        return None
    return Sighting(ip=ip, mac=normalize_mac(row.get('mac')), hostname=_name(row), sources={'online'},
                    presence='online', basis='terminal')


def _int(row: dict, key: str) -> Optional[int]:
    value = row.get(key)
    return value if type(value) is int else None


def parse_lease(row: dict) -> Optional[Sighting]:
    ip = _address(row)
    if not ip:
        return None
    # Seconds since the lease was granted or renewed, from the router's own
    # clock only: (end_time - start_time) is the lease length, timeout what is left.
    start, end, left = _int(row, 'start_time'), _int(row, 'end_time'), _int(row, 'timeout')
    last_seen = end - start - left if None not in (start, end, left) and end - start >= left >= 0 else None
    # Not in the terminal monitor: no traffic in the last ~2 minutes.
    return Sighting(ip=ip, mac=normalize_mac(row.get('mac')), hostname=_name(row), sources={'lease'},
                    presence='offline', basis='absent', last_seen=last_seen)


def lan_addresses(driver) -> Optional[List[ipaddress.IPv4Interface]]:
    """The router's LAN interfaces (``lan`` ip_mask), or None when ``lan`` cannot be read."""
    try:
        results = driver.call('lan', {'TYPE': 'data,total', 'limit': f'0,{PAGE}'})
    except RouterError:
        return None
    addresses = []
    for row in results.get('data') or []:
        text = str(row.get('ip_mask') or '') if isinstance(row, dict) else ''
        for item in text.replace(',', ' ').split():
            try:
                addresses.append(ipaddress.IPv4Interface(item))
            except ValueError:
                continue
    return addresses


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
            if current.last_seen is None:
                current.last_seen = sighting.last_seen
        else:
            reading.sightings[sighting.ip] = sighting
    lan = lan_addresses(driver)
    if lan is None:
        # Without the LAN subnets a saved address missing from the monitor may simply be elsewhere.
        reading.complete = False
        reading.notes.append(NOTE_NO_LAN)
    reading.own = {str(item.ip) for item in lan or []}
    reading.lan = {item.network for item in lan or []}
    return reading
