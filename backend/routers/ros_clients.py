"""LAN clients of a RouterOS router: ARP plus DHCP leases, with an active check.

Observed on RouterOS 7.24.5 (arp-timeout 30s, lease-time 30m):
- ARP ``stale`` entries never expire, so a stale entry alone proves nothing;
  the WAN side carries hundreds of ``failed`` entries, so only LAN interfaces count.
- Online DHCP clients renew at half the lease time and refresh ``last-seen``.
  Static-IP hosts have no dynamic lease; a static lease shows ``waiting`` while
  such a host is online; one MAC can own two addresses.
- ``/ping`` needs the group's ``test`` policy. An ARP ping on the entry's
  interface is answered even by hosts whose firewall drops ICMP. After an ICMP
  ping a stale entry sits in ``delay`` for ~5 s before turning reachable/failed.
"""
import ipaddress
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Iterable, List, Optional, Set, Tuple

from backend.routers.base import RouterError
from backend.routers.lan_clients import Reading, Sighting, ipv4, normalize_mac
from backend.routers.routeros import PingNotPermitted

ONLINE_ARP = frozenset({'reachable', 'delay', 'probe'})
GONE_ARP = frozenset({'failed', 'incomplete'})
FRESH_LEASE = 20 * 60          # a renewal this recent means the client is on the network
RENEW_MARGIN = 5 * 60          # grace after the renewal point (half the lease time)
PROBE_LIMIT = 32               # hosts actively checked per read
PROBE_WORKERS = 6
PROBE_TIMEOUT = 4              # seconds per ping request; one ping waits ~0.6 s
PRIVATE = tuple(ipaddress.ip_network(net) for net in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
NOTE_NO_TEST = '账号缺少 test 权限，无法确认静态 IP 设备是否在线'
_DURATION = re.compile(r'(\d+)(w|d|h|ms|m|s)')
_UNITS = {'w': 604800, 'd': 86400, 'h': 3600, 'm': 60, 's': 1, 'ms': 0}


def duration(value) -> Optional[int]:
    """RouterOS duration text ('2d1h36m57s', '15s', '00:30:00') in seconds; None for 'never'/unknown."""
    text = str(value or '').strip()
    if re.fullmatch(r'\d+:\d{2}:\d{2}', text):
        hours, minutes, seconds = (int(part) for part in text.split(':'))
        return hours * 3600 + minutes * 60 + seconds
    if not text or _DURATION.sub('', text):
        return None
    return sum(int(number) * _UNITS[unit] for number, unit in _DURATION.findall(text))


def _true(value) -> bool:
    return value is True or str(value).lower() in ('true', 'yes')


def _private(address: str) -> bool:
    try:
        return any(ipaddress.IPv4Address(address) in net for net in PRIVATE)
    except ValueError:
        return False


def lan_interfaces(servers: List[dict], addresses: List[dict], wan_members: Iterable[str] = ()) -> Set[str]:
    """Interfaces of enabled DHCP servers; without any, those with a static RFC1918 address.

    The fallback skips dynamic addresses (a DHCP-client WAN behind another
    router is RFC1918 too) and members of the interface list ``WAN``.
    """
    lan = {str(row['interface']) for row in servers if row.get('interface') and not _true(row.get('disabled'))}
    if lan:
        return lan
    return {str(row['interface']) for row in addresses
            if row.get('interface') and not _true(row.get('disabled')) and not _true(row.get('dynamic'))
            and not _true(row.get('invalid')) and _private(str(row.get('address') or '').split('/')[0])
            } - set(wan_members)


def _lease_time(row: dict, servers: Dict[str, int]) -> Optional[int]:
    own = duration(row.get('lease-time'))
    if own:
        return own
    server = str(row.get('server') or '')
    if server in servers:
        return servers[server]
    # A lease of server "all" (or no server field) is ambiguous unless every server agrees.
    values = set(servers.values())
    return values.pop() if len(values) == 1 else None


def classify(arp: List[dict], leases: List[dict], servers: List[dict],
             interfaces: Set[str]) -> Tuple[Dict[str, Sighting], List[Tuple[str, str]]]:
    """Presence from one read. Returns sightings and (ip, interface) pairs still to confirm."""
    entries: Dict[str, dict] = {}
    for row in arp:
        ip = ipv4(row.get('address'))
        if not ip or str(row.get('interface') or '') not in interfaces or _true(row.get('disabled')) \
                or _true(row.get('invalid')):
            continue
        status = str(row.get('status') or '').lower()
        previous = entries.get(ip)
        if previous is None or (status in ONLINE_ARP and previous['status'] not in ONLINE_ARP):
            entries[ip] = {'status': status, 'mac': normalize_mac(row.get('mac-address')),
                           'interface': str(row['interface']), 'comment': str(row.get('comment') or '').strip()}
    server_times = {str(row.get('name')): seconds for row in servers
                    if (seconds := duration(row.get('lease-time')))}
    server_interfaces = {str(row.get('name')): str(row.get('interface') or '') for row in servers}
    by_ip: Dict[str, dict] = {}
    for row in leases:
        ip = ipv4(row.get('active-address') or row.get('address'))
        if not ip or _true(row.get('disabled')):
            continue
        lease = {'status': str(row.get('status') or '').lower(), 'last_seen': duration(row.get('last-seen')),
                 'dynamic': _true(row.get('dynamic')), 'lease_time': _lease_time(row, server_times),
                 'mac': normalize_mac(row.get('active-mac-address') or row.get('mac-address')),
                 'name': str(row.get('comment') or '').strip() or str(row.get('host-name') or '').strip(),
                 'interface': server_interfaces.get(str(row.get('server') or ''), '')}
        previous = by_ip.get(ip)
        if previous is None or (lease['last_seen'] is not None
                                and (previous['last_seen'] is None or lease['last_seen'] < previous['last_seen'])):
            by_ip[ip] = lease
    sightings: Dict[str, Sighting] = {}
    candidates: List[Tuple[str, str]] = []
    for ip in set(entries) | set(by_ip):
        entry, lease = entries.get(ip), by_ip.get(ip)
        sighting = Sighting(ip=ip, mac=(entry or {}).get('mac') or (lease or {}).get('mac', ''),
                            hostname=(lease or {}).get('name') or (entry or {}).get('comment', ''),
                            sources={name for name, item in (('arp', entry), ('lease', lease)) if item},
                            last_seen=(lease or {}).get('last_seen'))
        status = (entry or {}).get('status', '')
        fresh = bool(lease and lease['status'] == 'bound' and lease['last_seen'] is not None
                     and lease['last_seen'] <= FRESH_LEASE)
        # Missing the renewal at half the lease time means the client left. A
        # static lease that never renewed may belong to a host with a static IP.
        missed = bool(lease and lease['last_seen'] is not None and lease['lease_time']
                      and (lease['dynamic'] or lease['status'] == 'bound')
                      and lease['last_seen'] > max(FRESH_LEASE, lease['lease_time'] // 2 + RENEW_MARGIN))
        if status in ONLINE_ARP:
            sighting.presence, sighting.basis = 'online', 'arp'
        elif fresh:
            sighting.presence, sighting.basis = 'online', 'lease'
        elif status in GONE_ARP:
            sighting.presence, sighting.basis = 'offline', 'arp'
        elif missed:
            sighting.presence, sighting.basis = 'offline', 'lease'
        elif entry:
            sighting.presence, sighting.basis = 'recent', 'stale'
            candidates.append((ip, entry['interface']))
        elif lease['status'] == 'bound':
            sighting.presence, sighting.basis = 'recent', 'stale'
            candidates.append((ip, lease['interface'] if lease['interface'] in interfaces else ''))
        else:
            sighting.presence, sighting.basis = 'offline', 'lease'
        sightings[ip] = sighting
    return sightings, candidates


def _ip_key(ip: str):
    return ipaddress.IPv4Address(ip)


def confirm(driver, sightings: Dict[str, Sighting], candidates: List[Tuple[str, str]], interfaces: Set[str],
            priority: Iterable[str] = (), deadline: Optional[float] = None) -> List[str]:
    """Ping unconfirmed hosts through the router and settle them; returns source notes.

    Saved addresses go first. Without the ``test`` policy, on timeout or past
    the cap, hosts stay ``recent``.
    """
    if not candidates:
        return []
    deadline = deadline if deadline is not None else time.monotonic() + 15
    preferred = set(priority)
    order = sorted(candidates, key=lambda item: (item[0] not in preferred, _ip_key(item[0])))
    chosen, skipped = order[:PROBE_LIMIT], order[PROBE_LIMIT:]
    denied = threading.Event()

    def check(candidate):
        ip, interface = candidate
        if denied.is_set() or time.monotonic() + 1 > deadline:
            return ip, None, ''
        timeout = max(1.0, min(PROBE_TIMEOUT, deadline - time.monotonic()))
        try:
            if interface:
                try:
                    return ip, driver.ping(ip, interface, timeout=timeout), 'arp'
                except PingNotPermitted:
                    raise
                except RouterError:
                    pass  # e.g. ARP ping refused on this interface type: fall back to ICMP
            return ip, driver.ping(ip, timeout=timeout), 'icmp'
        except PingNotPermitted:
            denied.set()
        except RouterError:
            pass
        return ip, None, ''

    with ThreadPoolExecutor(max_workers=min(PROBE_WORKERS, len(chosen))) as pool:
        results = list(pool.map(check, chosen))
    silent = [(ip, kind) for ip, answered, kind in results if answered is False]
    for ip, answered, _ in results:
        if answered:
            sightings[ip].presence, sightings[ip].basis = 'online', 'probe'
    if silent:
        try:
            now = {ipv4(row.get('address')): str(row.get('status') or '').lower() for row in driver._list('/ip/arp')
                   if str(row.get('interface') or '') in interfaces}
        except RouterError:
            now = {}
        for ip, kind in silent:
            status = now.get(ip, '')
            if status == 'reachable':
                sightings[ip].presence, sightings[ip].basis = 'online', 'probe'
            elif status in GONE_ARP or kind == 'arp':
                # No ARP reply to two ARP probes: the host is not on the LAN.
                sightings[ip].presence, sightings[ip].basis = 'offline', 'probe'
    notes = []
    if denied.is_set():
        notes.append(NOTE_NO_TEST)
    unsettled = sum(1 for _, answered, _ in results if answered is None)
    if skipped:
        notes.append(f'待确认的设备较多，只主动确认了 {len(chosen)} 台，其余 {len(skipped)} 台显示为近期见过')
    if unsettled and not denied.is_set():
        notes.append(f'{unsettled} 台设备未能及时确认，显示为近期见过')
    return notes


def read(driver, priority: Iterable[str] = (), deadline: Optional[float] = None) -> Reading:
    """One live read. /ip/arp is required; the other tables degrade to notes."""
    deadline = deadline if deadline is not None else time.monotonic() + 15
    reading = Reading()
    arp = driver._list('/ip/arp')

    def optional(path, label):
        try:
            return driver._list(path), True
        except RouterError as error:
            reading.notes.append(f'无法读取{label}：{error}')
            return [], False

    addresses, _ = optional('/ip/address', ' IP 地址')
    servers, _ = optional('/ip/dhcp-server', ' DHCP 服务')
    leases, _ = optional('/ip/dhcp-server/lease', ' DHCP 租约')
    interfaces = lan_interfaces(servers, [])
    if not interfaces:
        try:
            members = [str(row.get('interface') or '') for row in driver._list('/interface/list/member')
                       if str(row.get('list') or '').upper() == 'WAN']
        except RouterError:
            members = []
        interfaces = lan_interfaces([], addresses, members)
        if interfaces:
            reading.notes.append('未发现启用的 DHCP 服务，按带私有地址的接口（' + '、'.join(sorted(interfaces)) + '）判断局域网')
    if not interfaces:
        raise RouterError('无法确定局域网接口：没有启用的 DHCP 服务，也没有配置私有地址的接口')
    reading.own = {ip for row in addresses if (ip := ipv4(str(row.get('address') or '').split('/')[0]))}
    reading.sightings, candidates = classify(arp, leases, servers, interfaces)
    reading.notes += confirm(driver, reading.sightings, candidates, interfaces, priority, deadline)
    return reading
