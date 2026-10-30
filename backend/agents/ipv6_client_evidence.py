"""Correlate a fresh LAN request across the client, ROS and Mihomo.

Kernel route lookup is useful context, but only a matching tracked packet on
the linked router proves that this request actually reached that router.
"""
import ipaddress
import time
from urllib.parse import urlencode, urlsplit

from backend.agents import ipv6_readiness as readiness
from backend.routers.base import RouterError


def _ip(value):
    try:
        return str(ipaddress.ip_address(value))
    except (ValueError, TypeError):
        return None


def _port(value):
    try:
        port = int(value)
        return port if 1 <= port <= 65535 else None
    except (ValueError, TypeError):
        return None


def _endpoint(row, direction):
    address = row.get(direction + '-address')
    if _ip(address):
        return _ip(address), _port(row.get(direction + '-port'))
    # Older RouterOS versions combine address and port in the address field.
    if isinstance(address, str) and ':' in address:
        host, port = address.rsplit(':', 1)
        return _ip(host.strip('[]')), _port(port)
    return None, None


def _row_matches(row, source, sport, destination, port):
    if (str(row.get('protocol')).lower() == 'tcp'
            and _endpoint(row, 'src') == (source, sport)
            and _endpoint(row, 'dst') == (destination, port)):
        try:
            return int(row.get('orig-packets', 0)) + int(row.get('orig-fasttrack-packets', 0)) > 0
        except (ValueError, TypeError):
            pass
    return False


def observe(config, source, test_url, stop, ready):
    """Retain fresh packets while HTTP runs, before asymmetric flows expire.

Only the target TCP port and necessary tuple/counter columns are read. Rows
are bounded and discarded after correlation; other clients are never exposed.
"""
    target = urlsplit(test_url)
    port = target.port or (443 if target.scheme == 'https' else 80)
    fields = '.id,protocol,src-address,src-port,dst-address,dst-port,orig-packets,orig-fasttrack-packets'
    paths = [prefix + '/firewall/connection?' + urlencode({
        'protocol': 'tcp', 'dst-port': port, '.proplist': fields}) for prefix in ('/ip', '/ipv6')]
    drivers, result, baseline = [], {}, {}
    deadline = time.monotonic() + 30
    try:
        for router in readiness._linked(config, source['id'])[:4]:
            if router.get('type') != 'routeros':
                continue
            try:
                driver = readiness._driver(router)
                drivers.append((router['id'], driver))
                result[router['id']] = {}
                for path in paths:
                    try:
                        rows = driver._list(path, timeout=2)
                        baseline[(router['id'], path)] = {r.get('.id') for r in rows[:256]}
                    except RouterError:
                        baseline[(router['id'], path)] = None
            except RouterError:
                continue
        ready.set()
        while not stop.is_set() and time.monotonic() < deadline:
            for ident, driver in drivers:
                for path in paths:
                    previous = baseline.get((ident, path))
                    if previous is None:
                        continue
                    try:
                        for row in driver._list(path, timeout=2)[:256]:
                            key = (path, row.get('.id'))
                            if row.get('.id') and row['.id'] not in previous and len(result[ident]) < 256:
                                result[ident][key] = row
                    except RouterError:
                        pass
            stop.wait(.4)
    finally:
        ready.set()
        for _, driver in drivers:
            driver.close()
    return {key: list(rows.values()) for key, rows in result.items()}


def _tracked(driver, request, port, observed=()):
    source = _ip(request.get('source_ip'))
    destination = _ip(request.get('address'))
    sport = _port(request.get('source_port'))
    if not source or not destination or not sport:
        return False
    if any(_row_matches(row, source, sport, destination, port) for row in observed):
        return True
    path = '/ipv6/firewall/connection' if ':' in source else '/ip/firewall/connection'
    queries = [dict(protocol='tcp', **{'src-address': source, 'src-port': sport}),
               dict(protocol='tcp', **{'src-address': f'[{source}]:{sport}' if ':' in source else f'{source}:{sport}'})]
    for query in queries:
        try:
            rows = driver._list(path + '?' + urlencode(query))
        except RouterError:
            continue
        for row in rows:
            if _row_matches(row, source, sport, destination, port):
                return True
    return False


def evaluate(config, source, report, trace, test_url, observed=None):
    def check(key, label, confirmed, success, missing, details=None):
        return {'id': key, 'label': label, 'status': 'ok' if confirmed else 'unknown',
                'message': success if confirmed else missing, **({'details': details} if details else {})}

    target = urlsplit(test_url)
    port = target.port or (443 if target.scheme == 'https' else 80)
    host = str(report.get('target_host') or '').lower().rstrip('.')
    requests = [r for r in report.get('requests', []) if isinstance(r, dict) and r.get('status') == 'ok'][:8]
    expected_host = str(target.hostname or '').lower().rstrip('.')
    matches = []
    for request in requests:
        candidates = [m for m in trace.get('matches', []) if isinstance(m, dict)
                      and _ip(m.get('source_ip')) == _ip(request.get('source_ip'))
                      and _ip(request.get('source_ip'))
                      and _port(m.get('source_port')) == _port(request.get('source_port'))
                      and _port(request.get('source_port'))
                      and str(m.get('host') or '').lower().rstrip('.') == host == expected_host
                      and str(trace.get('target_host') or '').lower().rstrip('.') == expected_host
                      and m.get('network') == 'tcp' and _port(m.get('destination_port')) == port
                      and m.get('proxy_name') and m.get('proxy_type')
                      and m.get('chains') and not any(str(p).upper() in ('DIRECT', 'REJECT', 'REJECT-DROP', 'PASS')
                                                     for p in m['chains'])]
        matches.append(candidates)
    complete_http = bool(requests) and len(requests) == len(report.get('requests', []))
    proxied = complete_http and all(matches)
    transport = proxied and all(any(m.get('ipv6_transport_verified') is True for m in group) for group in matches)
    checks = [check('client_mihomo_path', '客户端经过 Mihomo', proxied,
                    '本次成功请求均对应 Mihomo 中的新代理连接',
                    '未取得与本次客户端源地址、端口和网站对应的代理连接，不能确认分流路径'),
              check('client_ipv6_transport', '客户端连接使用 IPv6 节点', transport,
                    '本次客户端连接的实际节点远端与 Mihomo 持有的 IPv6 节点连接相符',
                    '尚未将本次客户端请求与实际 IPv6 节点连接对应；网站可访问不能单独证明这一项')]
    confirmed, details = set(), []
    for router in readiness._linked(config, source['id'])[:8]:
        if router.get('type') != 'routeros':
            continue
        driver = None
        try:
            driver = readiness._driver(router)
            for index, request in enumerate(requests):
                if _tracked(driver, request, port, (observed or {}).get(router['id'], [])):
                    confirmed.add(index)
                    details.append(f"{router.get('name') or router['id']} · {request.get('family')} · 已找到本次请求的连接记录")
        except RouterError:
            pass
        finally:
            if driver:
                driver.close()
    checks.append(check('client_ros_path', '客户端经过 ROS', complete_http and len(confirmed) == len(requests),
                        '关联 ROS 的连接记录与本次客户端请求一一对应',
                        '未在关联 ROS 中找到本次请求的完整记录；可能已过期、未启用跟踪或走了其他路径，暂不能确认', details))
    return checks
