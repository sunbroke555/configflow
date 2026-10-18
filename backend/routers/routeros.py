"""RouterOS v7 driver using the REST API (``/rest``, RouterOS 7.1+).

Reads and writes go through REST. Each operation records inverse changes and
verifies the written fields; compound NAT changes compensate partial failures.
"""
import ipaddress
import re
from typing import Any, Dict, List
from urllib.parse import quote

import requests

from backend.routers.base import (
    DhcpDnsEntry, DhcpDnsState, NatBypassEntry, RouteEntry, RouterDriver, RouterError,
    RouterInfo, RouterRollbackError, RouterState,
)
from backend.routers.dhcp_dns import checked_dns_target
from backend.routers.report import _networks

TIMEOUT = 10
PERMISSION_HELP = (
    '请用管理员登录 WinBox，在 System → Users 找到 ConfigFlow 使用的账号并确认 Group；'
    '在 Groups 中为该账号的专用用户组启用 read、write、api、rest-api，重点检查 write。'
    '保存后重新生成发布预览；连接测试成功不代表已有写入权限。'
)
ROUTES = '/ip/route'
NAT = '/ip/firewall/nat'
DHCP_NETWORKS = '/ip/dhcp-server/network'
DHCP_SERVERS = '/ip/dhcp-server'
DHCP_LEASES = '/ip/dhcp-server/lease'
DHCP_OPTIONS = '/ip/dhcp-server/option'
DHCP_OPTION_SETS = '/ip/dhcp-server/option/sets'
DHCP_MATCHERS = '/ip/dhcp-server/matcher'
# /ip/route also returns operational attributes which cannot be written back.
ROUTE_FIELDS = {
    'dst-address', 'gateway', 'routing-table', 'pref-src', 'blackhole',
    'check-gateway', 'distance', 'scope', 'target-scope', 'vrf-interface',
    'suppress-hw-offload', 'disabled', 'comment',
}
NAT_READ_ONLY = {'.id', 'dynamic', 'invalid', 'bytes', 'packets'}
ROUTE_EVIDENCE_FIELDS = ROUTE_FIELDS | {'.id', 'immediate-gw', 'active', 'inactive', 'connect', 'ecmp', 'invalid'}


class MissingResource(RouterError):
    pass


class UncertainWrite(RouterError):
    """The request may have reached RouterOS even though its reply was lost."""


class RouterOSDriver(RouterDriver):
    vendor = 'routeros'
    supports_write = True

    def __init__(self, endpoint: str, username: str, password: str, verify_tls: bool = True):
        self.endpoint = endpoint.rstrip('/')
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.verify = verify_tls
        self.session.auth = (username, password)

    def close(self):
        self.session.close()

    def _request(self, method: str, path: str, values=None) -> Any:
        try:
            kwargs = {'timeout': TIMEOUT, 'allow_redirects': False}
            if values is not None:
                kwargs['json'] = values
            response = getattr(self.session, method.lower())(f'{self.endpoint}/rest{path}', **kwargs)
        except requests.RequestException as error:
            error_type = RouterError if method == 'GET' else UncertainWrite
            raise error_type(f'无法连接 RouterOS：{error.__class__.__name__}') from None
        if response.status_code == 401:
            raise RouterError('RouterOS 认证失败，请核对 ConfigFlow 中的用户名、密码和账号允许的来源地址；'
                              '在 WinBox → System → Users / Groups 检查用户组的 api、rest-api 权限')
        if response.status_code == 403:
            raise RouterError('RouterOS 拒绝访问，请先确认所用 HTTP/HTTPS 服务允许 REST API。' + PERMISSION_HELP)
        if response.status_code == 404 and method == 'GET':
            raise MissingResource('RouterOS 未提供 REST API（HTTPS 需要 7.1+ 并启用 www-ssl；HTTP 需要 7.9+ 并启用 www）')
        if response.status_code == 404 and method == 'DELETE':
            return {}  # already absent during compensation
        if not 200 <= response.status_code < 300:
            try:
                body = response.json()
            except ValueError:
                body = {}
            detail = ': '.join(str(body[key]) for key in ('message', 'detail') if body.get(key)) if isinstance(body, dict) else ''
            error_type = UncertainWrite if method != 'GET' and response.status_code >= 500 else RouterError
            message = (f'RouterOS {method} {path} 返回 HTTP {response.status_code}'
                       + (f'（{detail}）' if detail else ''))
            if re.search(r'\bnot enough permissions\b', detail, re.IGNORECASE):
                message = f'RouterOS 账号权限不足。{PERMISSION_HELP} 原始错误：{message}'
            raise error_type(message)
        if response.status_code == 204:
            return {}
        try:
            return response.json()
        except ValueError:
            error_type = RouterError if method == 'GET' else UncertainWrite
            raise error_type('RouterOS 返回的不是 JSON') from None

    def _get(self, path: str) -> Any:
        return self._request('GET', path)

    def _list(self, path: str) -> List[Dict[str, Any]]:
        rows = self._get(path)
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise RouterError(f'RouterOS {path} 返回的列表格式无法识别')
        return rows

    def probe(self) -> RouterInfo:
        resource = self._get('/system/resource')
        if not isinstance(resource, dict):
            raise RouterError('RouterOS 系统信息格式无法识别')
        version = str(resource.get('version') or '')
        match = re.match(r'(\d+)\.(\d+)', version)
        if not match or (int(match.group(1)), int(match.group(2))) < (7, 1):
            raise RouterError(f'需要 RouterOS 7.1+，当前版本 {version or "未知"}')
        capabilities = []
        for path, capability in (('/ip/route', 'routes'), ('/ip/firewall/nat', 'nat_bypass'),
                                 ('/ip/firewall/address-list', 'address_sets')):
            try:
                self._list(path)
                capabilities.append(capability)
            except RouterError:
                pass
        return RouterInfo(vendor='routeros', version=version,
                          model=str(resource.get('board-name') or ''), capabilities=capabilities)

    def read_state(self) -> RouterState:
        state = RouterState()
        for row in self._list('/ip/firewall/address-list'):
            if _truthy(row.get('disabled')):
                continue
            state.address_sets.setdefault(str(row.get('list') or ''), []).append(str(row.get('address') or ''))
        routes = self._list(ROUTES)
        nats = self._list(NAT)
        context = {
            'routes': [{key: value for key, value in row.items() if key in ROUTE_EVIDENCE_FIELDS}
                       for row in routes],
            'rules': [{key: value for key, value in row.items() if key not in {'bytes', 'packets'}}
                      for row in nats],
            'routing_rules': None, 'mangle': None, 'vrfs': None,
            'interface_lists': None, 'interface_members': None,
        }
        scoped = [row for row in nats if row.get('chain') == 'srcnat' and not _truthy(row.get('disabled'))
                  and (row.get('out-interface') or row.get('out-interface-list'))]
        if scoped:
            # Main-table routes alone cannot prove an egress when policy routing
            # or VRFs can select another path. Unavailable evidence stays unknown.
            for key, path in (('routing_rules', '/routing/rule'), ('mangle', '/ip/firewall/mangle'),
                              ('vrfs', '/ip/vrf')):
                try:
                    context[key] = [{name: value for name, value in row.items()
                                     if name not in {'bytes', 'packets'}} for row in self._list(path)]
                except RouterError:
                    pass
            if any(row.get('out-interface-list') for row in scoped):
                for key, path in (('interface_lists', '/interface/list'),
                                  ('interface_members', '/interface/list/member')):
                    try:
                        context[key] = self._list(path)
                    except RouterError:
                        pass
        state.nat_context = context
        for row in routes:
            dst = str(row.get('dst-address') or '')
            try:
                dst = str(ipaddress.ip_network(dst, strict=False))
            except ValueError:
                continue
            active = _truthy(row['active']) if 'active' in row else None
            if active is None and 'inactive' in row:
                active = not _truthy(row['inactive'])
            if _truthy(row.get('inactive')) or _truthy(row.get('invalid')):
                active = False
            try:
                distance = int(row['distance'])
            except (KeyError, TypeError, ValueError):
                distance = None
            state.routes.append(RouteEntry(
                id=str(row.get('.id', '')), dst=dst, gateway=str(row.get('gateway') or ''),
                enabled=not _truthy(row.get('disabled')), comment=str(row.get('comment') or ''),
                table=str(row.get('routing-table') or 'main'), active=active, distance=distance))
        for row in nats:
            if str(row.get('comment') or '').startswith('cf-ros-policy-'):
                continue
            if row.get('chain') != 'srcnat' or row.get('action') != 'accept':
                continue
            dst = [str(row['dst-address'])] if row.get('dst-address') else []
            if row.get('dst-address-list'):
                dst.extend(state.address_sets.get(str(row['dst-address-list']), []))
            src = [str(row['src-address'])] if row.get('src-address') else []
            if row.get('src-address-list'):
                src.extend(state.address_sets.get(str(row['src-address-list']), []))
            state.nat_bypass.append(NatBypassEntry(
                id=str(row.get('.id', '')), dst=dst, src=src,
                enabled=not _truthy(row.get('disabled')), comment=str(row.get('comment') or ''),
                attributes={key: value for key, value in row.items()
                            if key not in NAT_READ_ONLY and not key.startswith('.')}))
        manifest = getattr(self, 'ros_policy_manifest', None)
        if manifest and isinstance(context.get('mangle'), list):
            from backend.routers.ros_policy import config_row, MANGLE
            proven = {entry['id'] for entry in manifest.get('resources', []) if entry['path'] == MANGLE
                      and any(row.get('.id') == entry['id'] and config_row(row) == config_row(entry['config'])
                              for row in context['mangle'])}
            context['mangle'] = [row for row in context['mangle'] if row.get('.id') not in proven]
        return state

    def read_dhcp_dns(self) -> DhcpDnsState:
        """Read network DNS and the option layers which can override option 6.

        Server/matcher overrides are deliberately treated as potentially global:
        mapping pools, relays and matchers to clients is outside this operation.
        Fixed-address lease overrides can be scoped to their actual subnet.
        """
        networks = self._list(DHCP_NETWORKS)
        servers = self._list(DHCP_SERVERS)
        leases = self._list(DHCP_LEASES)
        options = self._list(DHCP_OPTIONS)
        option_sets = self._list(DHCP_OPTION_SETS)
        matchers = self._list(DHCP_MATCHERS)
        active_servers = [row for row in servers if not _truthy(row.get('disabled'))]
        global_blockers = []
        if not active_servers:
            global_blockers.append('未发现启用的 DHCP 服务，无法确认网段 DNS 会被下发')
        for server in active_servers:
            label = f'DHCP 服务 {server.get("name") or server.get(".id") or "未知"}'
            if not server.get('name') or _truthy(server.get('invalid')):
                global_blockers.append(f'{label} 信息不完整或无效，无法确认 DNS 下发')
            radius = str(server.get('use-radius', 'no')).lower()
            if radius not in ('no', 'false', 'accounting'):
                global_blockers.append(f'{label} 启用了 RADIUS 或其状态未知，可能覆盖 DHCP DNS')
            global_blockers.extend(_dhcp_overrides(server, label, options, option_sets))
        disabled_servers = {row.get('name') for row in servers if _truthy(row.get('disabled'))}
        for matcher in matchers:
            if _truthy(matcher.get('disabled')) or matcher.get('server') in disabled_servers:
                continue
            label = f'DHCP 匹配器 {matcher.get("name") or matcher.get(".id") or "未知"}'
            global_blockers.extend(_dhcp_overrides(
                {'dhcp-option-set': matcher.get('option-set', '')}, label, options, option_sets))

        # Ordinary dynamic leases carry no option override and must not make
        # preview fingerprints change as clients renew or acquire addresses.
        override_leases = [row for row in leases if not _truthy(row.get('disabled'))
                           and (_dhcp_names(row.get('dhcp-option', ''))
                                or _dhcp_names(row.get('dhcp-option-set', ''))
                                or _truthy(row.get('radius')))]
        entries = []
        seen_ids = set()
        for row in networks:
            row_id = str(row.get('.id') or '')
            if not row_id or row_id in seen_ids:
                raise RouterError('RouterOS DHCP 网段缺少唯一对象 ID')
            seen_ids.add(row_id)
            try:
                network = ipaddress.IPv4Network(str(row.get('address') or ''), strict=False)
            except ValueError:
                raise RouterError('RouterOS DHCP 网段地址无法识别') from None
            attributes = _dhcp_network_attributes(row)
            blockers = list(global_blockers)
            if _truthy(row.get('dynamic')) or _truthy(row.get('invalid')):
                blockers.append('该 DHCP 网段为动态或无效对象，不能自动修改 DNS')
            blockers.extend(_dhcp_overrides(row, f'DHCP 网段 {network}', options, option_sets))
            for lease in override_leases:
                if lease.get('server') in disabled_servers:
                    continue
                try:
                    address = ipaddress.IPv4Address(str(lease.get('address') or ''))
                except ValueError:
                    address = None  # a pool or unknown scope may include this network
                if address and not address.is_unspecified and address not in network:
                    continue
                label = f'DHCP 租约 {lease.get("address") or lease.get(".id") or "未知"}'
                if _truthy(lease.get('radius')):
                    blockers.append(f'{label} 由 RADIUS 管理，可能覆盖 DHCP DNS')
                blockers.extend(_dhcp_overrides(lease, label, options, option_sets))
            try:
                dns = [str(ipaddress.IPv4Address(value)) for value in _dhcp_names(attributes['dns-server'])]
            except ValueError:
                raise RouterError(f'RouterOS DHCP 网段 {network} 的 DNS 地址无法识别') from None
            if _truthy(attributes['dns-none']):
                dns = []
            entries.append(DhcpDnsEntry(
                id=row_id, network=str(network), dns=dns,
                enabled=not _truthy(row.get('disabled')), attributes=attributes,
                blockers=list(dict.fromkeys(blockers))))

        def fields(rows, names):
            return [{key: row[key] for key in names if key in row} for row in rows]

        evidence = {
            'networks': [{**_dhcp_network_attributes(row), **{key: row[key]
                          for key in ('.id', 'dynamic', 'invalid') if key in row}} for row in networks],
            'servers': fields(servers, ('.id', 'name', 'disabled', 'invalid', 'dynamic', 'interface',
                                       'address-pool', 'relay', 'use-radius', 'dhcp-option', 'dhcp-option-set')),
            'leases': fields(override_leases, ('.id', 'address', 'server', 'dynamic', 'radius',
                                              'dhcp-option', 'dhcp-option-set')),
            'options': fields(options, ('.id', 'name', 'code', 'value', 'force')),
            'option_sets': fields(option_sets, ('.id', 'name', 'options')),
            'matchers': fields(matchers, ('.id', 'name', 'disabled', 'server', 'code', 'matching-type',
                                          'value', 'option-set', 'address-pool')),
        }
        return DhcpDnsState(entries=entries, evidence=evidence)


    def _row(self, path: str, row_id: str) -> Dict[str, Any]:
        for row in self._list(path):
            if str(row.get('.id')) == str(row_id):
                return row
        raise RouterError(f'RouterOS {path} 中找不到 id={row_id}，可能已被手动删除')

    @staticmethod
    def _payload(path: str, row: Dict[str, Any]) -> Dict[str, Any]:
        if _truthy(row.get('dynamic')):
            raise RouterError('RouterOS 动态对象不能接管、修改或删除')
        if path == ROUTES:
            return {key: value for key, value in row.items() if key in ROUTE_FIELDS}
        if path == DHCP_NETWORKS:
            return _dhcp_network_attributes(row)
        # Preserve *all* NAT matchers, including negated source lists, interface
        # lists, ports, connection marks and IPsec policy. An allowlist could
        # silently broaden a rule when a firmware adds a new matcher.
        return {key: value for key, value in row.items()
                if key not in NAT_READ_ONLY and not key.startswith('.')}

    def _perform(self, change: Dict[str, Any], undo: List[Dict[str, Any]], *, restoring: bool = False):
        """Journal an inverse before writes where the affected ID is known."""
        method, path = change['method'], change['path']
        values = dict(change.get('values') or {})
        if method == 'RESTORE':
            # DELETE may have succeeded before a timeout. Recreate only if its
            # original ID is absent; never duplicate a row after a rejected delete.
            rows = self._list(path)
            if any(str(row.get('.id')) == str(change['id']) for row in rows):
                return {'path': path, 'id': str(change['id']),
                        'values': {key: value for key, value in values.items() if key != 'place-before'}}
            return self._perform({'method': 'PUT', 'path': path, 'values': values}, undo)
        if method == 'PUT':
            before = {str(row.get('.id')) for row in self._list(path)}
            try:
                row = self._request(method, path, values)
                row_id = str(row.get('.id') or '') if isinstance(row, dict) else ''
                if not row_id:
                    raise UncertainWrite(f'RouterOS {path} 已提交但没有返回对象 ID')
            except UncertainWrite as error:
                # Recover the exact newly created row after a lost/malformed
                # response. Do not delete a pre-existing or ambiguous match.
                try:
                    candidates = [row for row in self._list(path)
                                  if str(row.get('.id')) not in before and _matches(row, values)]
                except RouterError as read_error:
                    raise RouterRollbackError(f'{error}；无法确认本次写入结果，回滚未完成：{read_error}') from error
                if len(candidates) == 1:
                    undo.append({'method': 'DELETE', 'path': path, 'id': str(candidates[0]['.id'])})
                else:
                    raise RouterRollbackError(f'{error}；无法唯一确认新增对象，回滚未完成，请检查路由器') from error
                raise
            undo.append({'method': 'DELETE', 'path': path, 'id': row_id})
            return {'path': path, 'id': row_id, 'values': values}

        row_id = str(change['id'])
        resource = f'{path}/{quote(row_id, safe="*")}'
        try:
            rows = self._list(path)
        except RouterError:
            if (restoring and path == DHCP_NETWORKS and method == 'PATCH'
                    and values and set(values) <= {'dns-server', 'dns-none'}):
                # The saved DNS fields and known ID permit a best-effort undo
                # even when reads fail after the original PATCH. _restore still
                # requires readback; an unreachable read must not claim success.
                self._request(method, resource, values)
                return {'path': path, 'id': row_id, 'values': values}
            raise
        row = next((row for row in rows if str(row.get('.id')) == row_id), None)
        if row is None and method == 'DELETE':
            # A create response supplied an exact ID. Even if a list read is
            # stale, delete that ID rather than leaving an unverified rule behind.
            self._request(method, f'{path}/{quote(row_id, safe="*")}')
            return {'path': path, 'id': row_id, 'absent': True}
        if row is None:
            raise RouterError(f'RouterOS {path} 中找不到 id={row_id}')
        original = self._payload(path, row)
        if method == 'PATCH':
            if _matches(row, values):
                return {'path': path, 'id': row_id, 'values': values}
            previous = {key: original.get(key, '') for key in values}
            undo.append({'method': 'PATCH', 'path': path, 'id': row_id, 'values': previous})
            self._request(method, resource, values)
            return {'path': path, 'id': row_id, 'values': values}
        if method == 'DELETE':
            if path == NAT:
                index = rows.index(row)
                if index + 1 < len(rows):
                    original['place-before'] = rows[index + 1]['.id']
            undo.append({'method': 'RESTORE', 'path': path, 'id': row_id, 'values': original})
            self._request(method, resource)
            return {'path': path, 'id': row_id, 'absent': True}
        raise RouterError(f'不支持的 RouterOS 写入方法 {method}')

    def _verify_change(self, check: Dict[str, Any]):
        rows = self._list(check['path'])
        row = next((row for row in rows if str(row.get('.id')) == check['id']), None)
        if check.get('absent'):
            if row is not None:
                raise RouterError('RouterOS 回读校验失败：应删除的对象仍然存在')
            return
        if row is None:
            raise RouterError('RouterOS 回读校验失败：没有找到已写入的对象')
        for key, value in check['values'].items():
            if key == 'place-before':
                target = next((item for item in rows if str(item.get('.id')) == str(value)), None)
                if target is None or rows.index(row) >= rows.index(target):
                    raise RouterError('RouterOS 回读校验失败：NAT 规则顺序不正确')
            elif _comparable(key, row.get(key, '')) != _comparable(key, value):
                raise RouterError(f'RouterOS 回读校验失败：{check["id"]} 的 {key} 与提交值不一致')

    def _restore(self, changes: List[Dict[str, Any]]):
        failures, undo = [], []
        for change in changes:
            try:
                check = self._perform(change, undo, restoring=True)
                self._verify_change(check)
            except RouterError as error:
                failures.append(str(error))
        if failures:
            raise RouterRollbackError('RouterOS 回滚未完成：' + '；'.join(failures))
        return {'kind': 'ros_restore', 'changes': list(reversed(undo))}

    def _transaction(self, changes: List[Dict[str, Any]]) -> Dict[str, Any]:
        undo = []
        try:
            for change in changes:
                check = self._perform(change, undo)
                self._verify_change(check)
        except RouterError as error:
            try:
                self._restore(list(reversed(undo)))
            except RouterError as rollback_error:
                raise RouterRollbackError(f'{error}；{rollback_error}') from error
            raise
        return {'kind': 'ros_restore', 'changes': list(reversed(undo))}

    def apply(self, op: Dict[str, Any]) -> Dict[str, Any]:
        kind = op['kind']
        if kind in ('ros_policy_replace', 'ros_policy_restore'):
            from backend.routers import ros_policy_write
            return (ros_policy_write.apply if kind == 'ros_policy_replace' else ros_policy_write.restore)(self, op)
        if kind == 'ros_restore':
            return self._restore(op['changes'])
        if kind == 'dhcp_dns_update':
            dns = op.get('dns')
            if (not isinstance(dns, list) or not 1 <= len(dns) <= 2
                    or any(not isinstance(value, str) for value in dns)):
                raise RouterError('DHCP DNS 必须包含一至两个 IPv4 地址')
            try:
                dns = [str(ipaddress.IPv4Address(value)) for value in dns]
            except (ValueError, TypeError):
                raise RouterError('DHCP DNS 地址不是有效 IPv4 地址') from None
            scope = op.get('scope')
            if (not isinstance(scope, dict)
                    or any(not isinstance(scope.get(key), str) or not scope[key]
                           for key in ('id', 'network', 'gateway'))
                    or scope['id'] != str(op.get('id') or '')):
                raise RouterError('RouterOS DHCP DNS 缺少完整且一致的目标范围，请重新预览')
            # Reuse the latest complete snapshot instead of looking up only the
            # old ID: another overlapping network may have appeared since the
            # release stage checked forwarding prerequisites.
            entry = checked_dns_target(scope, self.read_dhcp_dns())
            if entry.attributes != op.get('expected'):
                raise RouterError('RouterOS DHCP 网段在预览后发生变化，请重新预览')
            return self._transaction([{'method': 'PATCH', 'path': DHCP_NETWORKS, 'id': entry.id,
                                       'values': {'dns-server': ','.join(dns), 'dns-none': 'false'}}])
        if kind == 'route_add':
            values = {'dst-address': op['cidr'], 'gateway': op['gateway'],
                      'comment': op['comment'], 'disabled': 'false'}
            return self._transaction([{'method': 'PUT', 'path': ROUTES, 'values': values}])
        if kind == 'route_delete':
            return self._transaction([{'method': 'DELETE', 'path': ROUTES, 'id': op['id']}])
        if kind == 'route_comment':
            return self._transaction([{'method': 'PATCH', 'path': ROUTES, 'id': op['id'],
                                       'values': {'comment': op['comment']}}])
        if kind == 'nat_update':
            row = self._row(NAT, op['id'])
            original = self._payload(NAT, row)
            if 'expected' in op and original != op['expected']:
                raise RouterError('RouterOS NAT 规则在预览后发生变化，请重新预览')
            if original.get('chain') != 'srcnat' or original.get('action') != 'accept':
                raise RouterError('RouterOS 目标规则已不是 NAT 免转换规则，请重新预览')
            changes = [{'method': 'PATCH', 'path': NAT, 'id': op['id'],
                        'values': {'comment': op['comment']}}]
            for cidr in dict.fromkeys(op.get('add_dst') or []):
                values = {**original, 'dst-address': cidr, 'comment': op['comment'],
                          'place-before': row['.id']}
                values.pop('dst-address-list', None)
                if op.get('out_interface'):
                    values['out-interface'] = op['out_interface']
                changes.append({'method': 'PUT', 'path': NAT, 'values': values})
            return self._transaction(changes)
        if kind == 'nat_add':
            rows = self._list(NAT)
            position = {'place-before': rows[0]['.id']} if rows else {}
            # Use explicit source CIDRs instead of mutating a shared address list.
            sources = [str(net) for net in ipaddress.collapse_addresses(_networks(op['src']))]
            if op['src'] and not sources:
                raise RouterError('RouterOS NAT 源地址范围无法识别')
            changes = []
            for source in sources or ['']:
                for cidr in dict.fromkeys(op['dst']):
                    values = {'chain': 'srcnat', 'action': 'accept', 'disabled': 'false',
                              'dst-address': cidr, 'comment': op['comment'], **position}
                    if source:
                        values['src-address'] = source
                    if op.get('out_interface'):
                        values['out-interface'] = op['out_interface']
                    changes.append({'method': 'PUT', 'path': NAT, 'values': values})
            return self._transaction(changes)
        raise RouterError(f'不支持的操作 {kind}')


def _comparable(key: str, value: Any) -> str:
    text = str(value).lower() if isinstance(value, bool) else str(value)
    if key == 'dns-none' and text == '':
        return 'false'  # absent is RouterOS's documented default
    if key in ('disabled', 'blackhole', 'log', 'suppress-hw-offload', 'dns-none'):
        if text in ('true', 'yes'):
            return 'true'
        if text in ('false', 'no'):
            return 'false'
    if key in ('src-address', 'dst-address', 'address'):
        try:
            return str(ipaddress.ip_network(text, strict=False))
        except ValueError:
            pass
    return text


def _matches(row: Dict[str, Any], values: Dict[str, Any]) -> bool:
    return all(_comparable(key, row.get(key, '')) == _comparable(key, value)
               for key, value in values.items() if key != 'place-before')


def _truthy(value: Any) -> bool:
    return value is True or str(value).lower() in ('true', 'yes')


def _dhcp_names(value: Any) -> List[str]:
    if isinstance(value, str):
        values = value.split(',')
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        values = value
    else:
        raise RouterError('RouterOS DHCP 选项或 DNS 列表格式无法识别')
    return [item.strip() for item in values if item.strip() and item.strip() != 'none']


def _dhcp_network_attributes(row: Dict[str, Any]) -> Dict[str, Any]:
    attributes = {key: value for key, value in row.items()
                  if not key.startswith('.') and key not in ('dynamic', 'invalid')}
    attributes.setdefault('dns-server', '')
    attributes.setdefault('dns-none', 'false')
    suppression = attributes['dns-none']
    if not isinstance(suppression, bool) and suppression not in ('true', 'false', 'yes', 'no'):
        raise RouterError('RouterOS DHCP dns-none 状态无法识别')
    return attributes


def _dhcp_overrides(row, label, options, option_sets) -> List[str]:
    """A custom option 6 at any supported level can supersede network DNS.

    RouterOS priority is RADIUS, lease, server, network. Keep all manual option
    objects intact and block rather than claiming a network-only write wins.
    """
    blockers = []
    references = _dhcp_names(row.get('dhcp-option', ''))
    for name in _dhcp_names(row.get('dhcp-option-set', '')):
        matches = [item for item in option_sets if name in (item.get('name'), item.get('.id'))]
        if len(matches) != 1 or 'options' not in matches[0]:
            blockers.append(f'{label} 的选项集 {name} 无法完整读取，可能覆盖 DHCP DNS')
            continue
        references.extend(_dhcp_names(matches[0]['options']))
    for name in dict.fromkeys(references):
        matches = [item for item in options if name in (item.get('name'), item.get('.id'))]
        if len(matches) != 1:
            blockers.append(f'{label} 的选项 {name} 无法唯一识别，可能覆盖 DHCP DNS')
            continue
        raw_code = matches[0].get('code')
        code = None
        if isinstance(raw_code, int) and not isinstance(raw_code, bool):
            code = raw_code
        elif isinstance(raw_code, str):
            if raw_code == 'vendor-specific':
                code = 43
            elif re.fullmatch(r'[0-9]+', raw_code):
                try:
                    code = int(raw_code)
                except ValueError:
                    pass  # oversized or otherwise unparseable evidence
        if code == 6:
            blockers.append(f'{label} 使用自定义 DNS 选项 6（{name}），不能只修改网段 DNS')
        elif code is None or not 1 <= code <= 254:
            blockers.append(f'{label} 的选项 {name} 编号未知，可能覆盖 DHCP DNS')
    return blockers
