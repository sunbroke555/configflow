"""Vendor-neutral LAN client sightings read live from a main router.

Readers (``ros_clients``, ``ikuai_clients``) turn one router's tables into
``Sighting`` objects keyed by IPv4 address. Nothing here is persisted.

``presence`` is one of:
  online   the router saw the host just now (ARP / fresh DHCP renewal / active check / iKuai terminal)
  recent   the router knows the host but could not confirm it is still there
  offline  the router has evidence the host is gone
  unknown  no evidence either way

``basis`` says which evidence decided it, for UI hints:
  arp       ARP neighbour state (reachable/delay/probe, or failed/incomplete)
  lease     DHCP renewal timing (renewed within 20 minutes, or missed its renewal)
  probe     active check through the router (answered, or no answer)
  terminal  iKuai terminal monitor (traffic through the router in the last ~2 minutes)
  stale     only an unconfirmed ARP entry or lease remains
  absent    every router was read and none lists the address
"""
import ipaddress
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

RANK = {'online': 3, 'recent': 2, 'offline': 1, 'unknown': 0}
SOURCE_ORDER = ('arp', 'lease', 'online')


@dataclass
class Sighting:
    ip: str
    mac: str = ''
    hostname: str = ''
    sources: Set[str] = field(default_factory=set)   # 'arp' | 'lease' | 'online'
    presence: str = 'unknown'
    basis: str = ''
    last_seen: Optional[int] = None                    # seconds since the last DHCP contact
    router_id: str = ''

    def public(self) -> dict:
        return {'mac': self.mac, 'hostname': self.hostname,
                'sources': [item for item in SOURCE_ORDER if item in self.sources],
                'presence': self.presence, 'basis': self.basis, 'last_seen': self.last_seen,
                'router_id': self.router_id}


@dataclass
class Reading:
    sightings: Dict[str, Sighting] = field(default_factory=dict)
    own: Set[str] = field(default_factory=set)         # the router's own addresses
    notes: List[str] = field(default_factory=list)


def ipv4(value) -> str:
    """Canonical IPv4 host text, or '' for anything else (IPv6, CIDR, garbage)."""
    try:
        return str(ipaddress.IPv4Address(str(value or '').strip()))
    except ValueError:
        return ''


def normalize_mac(value) -> str:
    text = str(value or '').strip().upper().replace('-', ':')
    parts = text.split(':')
    if len(parts) != 6 or any(len(part) not in (1, 2) for part in parts):
        return ''
    try:
        return ':'.join(f'{int(part, 16):02X}' for part in parts)
    except ValueError:
        return ''


def better(current: Optional[Sighting], candidate: Sighting) -> Sighting:
    """Merge two sightings of one address; the stronger presence decides."""
    if current is None:
        return candidate
    strong, weak = (candidate, current) if RANK[candidate.presence] > RANK[current.presence] else (current, candidate)
    strong.sources |= weak.sources
    strong.mac = strong.mac or weak.mac
    strong.hostname = strong.hostname or weak.hostname
    if strong.last_seen is None:
        strong.last_seen = weak.last_seen
    return strong
