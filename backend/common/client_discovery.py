"""Device discovery for the 「客户端设备」 page.

Reads every RouterOS / iKuai main router of a profile live and concurrently,
then joins what they see with the current saved rows ({ip, name, mode}).
Router snapshots can be reused in memory. See doc/oxidns-device-policies.md.
"""
import ipaddress
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime
from typing import Dict, Iterable, List, Optional
from urllib.parse import urlparse

from backend.common.client_discovery_cache import DiscoveryCache, Snapshot
from backend.common.client_devices import list_devices
from backend.routers import ikuai_clients, ros_clients
from backend.routers.base import RouterError
from backend.routers.lan_clients import Reading, Sighting, better, ipv4
from backend.routers.store import routers_of
from backend.utils.logger import get_logger

ROUTER_TIMEOUT = 20      # seconds for one router: reads plus active checks
READERS = {'routeros': ros_clients.read, 'ikuai': ikuai_clients.read}
UNSUPPORTED = '需要在当前配置接入 RouterOS 或 iKuai 才能发现设备'
logger = get_logger(__name__)


def open_driver(router):
    from backend.routes.routers import _driver
    return _driver(router)


def _read(router: dict, priority: frozenset, deadline: float) -> Reading:
    driver = open_driver(router)
    try:
        reading = READERS[router['type']](driver, priority=priority, deadline=deadline)
        from backend.common.client_fixed_ip import enrich
        enrich(driver, reading, router.get('id') or '')
        return reading
    finally:
        driver.close()


def _read_all(routers: List[dict], priority: frozenset, timeout: float) -> List[tuple]:
    """[(router, Reading | None, error)] in router order; a slow router never blocks the others."""
    deadline = time.monotonic() + timeout - 2
    pool = ThreadPoolExecutor(max_workers=len(routers), thread_name_prefix='client-discovery')
    futures = [pool.submit(_read, router, priority, deadline) for router in routers]
    wait(futures, timeout=timeout)
    pool.shutdown(wait=False, cancel_futures=True)
    results = []
    for router, future in zip(routers, futures):
        if not future.done():
            results.append((router, None, f'读取超时（{timeout} 秒），请检查路由器负载和网络'))
            continue
        try:
            results.append((router, future.result(), ''))
        except RouterError as error:
            results.append((router, None, str(error)))
        except Exception as error:  # a parsing bug must not hide the other routers
            logger.exception('Client discovery failed for router %s', router.get('id'))
            results.append((router, None, f'读取失败：{error.__class__.__name__}'))
    return results


def gateway_host(router: dict) -> str:
    """IPv4 of the router's proxy gateway Agent; smart_sources never routes it as a source."""
    agent_id = router.get('proxy_gateway_agent_id')
    if not agent_id or not isinstance(agent_id, str):
        return ''
    from backend.common.agent_manager import get_agent_manager
    agent = get_agent_manager().get_agent_by_id(agent_id)
    return ipv4(agent.get('host')) if agent else ''


def _endpoint_ip(router: dict) -> str:
    try:
        return ipv4(urlparse(str(router.get('endpoint') or '')).hostname)
    except ValueError:
        return ''


def _network(value: str):
    try:
        return ipaddress.ip_network(value, strict=False)
    except ValueError:
        return None


def discover(config: dict, agent_hosts: Iterable[str] = (), timeout: Optional[float] = None, *,
             cache: Optional[DiscoveryCache] = None, profile_id: str = '', refresh: bool = False) -> dict:
    """Discovery payload for one profile's config (see doc/oxidns-device-policies.md)."""
    devices = list_devices(config)['devices']
    routers = [router for router in routers_of(config) if router.get('type') in READERS]
    networks = {row['ip']: _network(row['ip']) for row in devices}
    saved_hosts = {str(net.network_address) for net in networks.values()
                   if net is not None and net.version == 4 and net.num_addresses == 1}
    result = {'checked_at': datetime.now().astimezone().isoformat(timespec='seconds'), 'sources': [],
              'devices': [], 'discovered': [], 'cached': False, 'stale': False, 'refresh_error': ''}
    if not routers:
        if cache is not None:
            cache.invalidate(profile_id)
        result.update(state='unsupported', message=UNSUPPORTED)
        result['devices'] = [{**row, **_saved(networks[row['ip']]), 'discoverable': False} for row in devices]
        return result

    def load():
        readings = _read_all(routers, frozenset(saved_hosts), timeout or ROUTER_TIMEOUT)
        return Snapshot([(reading, error) for _, reading, error in readings], result['checked_at'])

    if cache is None:
        snapshot = load()
    else:
        snapshot, metadata = cache.read(profile_id, routers, load, refresh)
        result.update(metadata)
    result['checked_at'] = snapshot.checked_at
    readings = [(router, reading, error) for router, (reading, error) in zip(routers, snapshot.readings)]
    merged: Dict[str, Sighting] = {}
    excluded = {ip for ip in (ipv4(host) for host in agent_hosts) if ip}
    routers_at: Dict[str, str] = {}       # own address of a router that was just read -> its id
    done: List[Reading] = []
    for router, reading, error in readings:
        source = {'id': router.get('id'), 'name': router.get('name') or router.get('id'), 'type': router['type'],
                  'ok': reading is not None, 'error': error, 'notes': list(reading.notes) if reading else []}
        if router['type'] == 'routeros':
            # Smart IPv4 rows are the sources of RouterOS policy routing when it is enabled.
            source['policy_routing'] = bool(router.get('ros_policy_rule_sets'))
            if source['policy_routing']:
                source['gateway'] = gateway_host(router)
        result['sources'].append(source)
        endpoint = {ip for ip in [_endpoint_ip(router)] if ip}
        excluded |= endpoint
        if reading is None:
            continue
        done.append(reading)
        excluded |= reading.own
        for ip in endpoint | reading.own:
            routers_at.setdefault(ip, router.get('id') or '')
        for ip, sighting in reading.sightings.items():
            sighting.router_id = router.get('id') or ''
            merged[ip] = better(merged.get(ip), sighting)
    ok = [source['ok'] for source in result['sources']]
    complete = all(ok)
    result['state'] = 'ok' if complete else ('partial' if any(ok) else 'failed')
    result['message'] = '' if complete else ('部分路由器读取失败，未出现的设备显示为未知' if any(ok)
                                              else '路由器读取失败，暂时无法发现设备')
    evidence = Evidence(merged, routers_at, done if complete else None)

    def listable(ip: str) -> bool:
        # A failed ARP entry without MAC or lease is only an address someone tried to reach.
        sighting = merged.get(ip)
        return ip not in excluded and sighting is not None and bool(sighting.mac or sighting.sources - {'arp'})
    # discoverable: the row would be listed under discovered if it were not saved (UI: a removed row returns there).
    result['devices'] = [{**row, **_saved(networks[row['ip']], evidence), 'discoverable': row['ip'] in saved_hosts
                          and listable(row['ip'])} for row in devices]
    # Only rows with a mode set a policy; OxiDNS and RouterOS smart_sources apply the most specific one.
    covering = sorted(((net, row) for row in devices if row['mode'] and (net := networks[row['ip']]) is not None
                       and net.version == 4 and net.num_addresses > 1), key=lambda item: -item[0].prefixlen)
    for ip in sorted(merged, key=ipaddress.IPv4Address):
        sighting = merged[ip]
        if ip in saved_hosts or not listable(ip):
            continue
        address = ipaddress.IPv4Address(ip)
        network = next(({'ip': row['ip'], 'name': row['name'], 'mode': row['mode']}
                        for net, row in covering if address in net), None)
        result['discovered'].append({'ip': ip, **sighting.public(), 'network': network})
    return result


class Evidence:
    """What the routers said, for the saved rows.

    ``readings`` is None unless every router was read: then a saved address
    that no router lists is offline if it lies in a LAN subnet some router
    scanned completely, and unknown ('outside') otherwise.
    """

    def __init__(self, merged: Dict[str, Sighting], routers_at: Dict[str, str], readings: Optional[List[Reading]]):
        self.merged, self.routers_at, self.readings = merged, routers_at, readings


def _saved(net, evidence: Optional[Evidence] = None) -> dict:
    empty = {'mac': '', 'hostname': '', 'sources': [], 'basis': '', 'last_seen': None, 'router_id': ''}
    if net is None or net.version == 6:
        return {**empty, 'presence': 'unknown'}
    if net.num_addresses > 1:
        return {**empty, 'presence': 'network'}
    if evidence is None:
        return {**empty, 'presence': 'unknown'}
    ip = str(net.network_address)
    sighting = evidence.merged.get(ip)
    if ip in evidence.routers_at:
        # The router itself is never in its own ARP or lease table; it was just read.
        return {**(sighting.public() if sighting else empty), 'presence': 'online', 'basis': 'router',
                'router_id': evidence.routers_at[ip]}
    if sighting:
        return sighting.public()
    if evidence.readings is None:
        return {**empty, 'presence': 'unknown'}
    if any(reading.covers(ip) for reading in evidence.readings):
        return {**empty, 'presence': 'offline', 'basis': 'absent'}
    if all(reading.complete for reading in evidence.readings):
        return {**empty, 'presence': 'unknown', 'basis': 'outside'}
    return {**empty, 'presence': 'unknown'}
