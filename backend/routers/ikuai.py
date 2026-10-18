"""iKuai 4.x driver.

iKuai has no public API; this uses the same endpoints as its web UI:
``POST /Action/login`` then ``POST /Action/call`` with ``func_name``/``action``.
Only 4.x is supported: responses are ``{"code": 0, "results": {...}}``.
Write payloads mirror the web UI forms, including its string encoding.
"""
import base64
import hashlib
import ipaddress
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urljoin, urlsplit

import requests

from backend.routers.base import (
    POLICY_PRIORITY, DhcpDnsEntry, DhcpDnsState, NatBypassEntry, PolicyRouteEntry, RouteEntry, RouterDriver,
    RouterError, RouterInfo, RouterRollbackError, RouterState,
)
from backend.routers.dhcp_dns import checked_dns_target

TIMEOUT = 10
LIST_LIMIT = '0,1000'

# Escapes used by the web UI besides '%' (escaped first) and whitespace (-> %20).
_ENCODE = [('"', '%22'), ('`', '%60'), ('$', '%24'), ('&', '%26'), (';', '%3B'),
           ('\\', '%5C'), ('|', '%7C'), ("'", '%27')]
ROUTE_FIELDS = ('id', 'enabled', 'tagname', 'ip_type', 'interface', 'dst_addr', 'gateway', 'netmask', 'prio', 'comment')
NAT_FIELDS = ('id', 'enabled', 'tagname', 'comment', 'action', 'protocol', 'iinterface', 'ointerface',
              'src_addr', 'dst_addr', 'src_port', 'dst_port', 'nat_addr', 'nat_port', 'src_addr_inv', 'dst_addr_inv')
# 端口分流 (stream_ipport) as submitted by the 4.0.308 web UI form
# (static/js/index-BkqBAzZn.js): type 1 = next-hop gateway, 0 = WAN line.
POLICY_FIELDS = ('id', 'enabled', 'tagname', 'interface', 'src_addr', 'dst_addr', 'iface_band', 'comment', 'time',
                 'prio', 'mode', 'type', 'nexthop', 'area_code', 'src_port', 'dst_port', 'protocol', 'dst_type',
                 'src_addr_inv', 'dst_addr_inv')
POLICY_NEXTHOP = 1
# The form's default schedule: every day, all day.
POLICY_ALWAYS = {'custom': [{'type': 'weekly', 'weekdays': '1234567', 'start_time': '00:00', 'end_time': '23:59'}],
                 'object': []}

# Verified against the original vendor UI AND backend/schema in official firmware
# 4.0.120-beta Build202603191336 (downloaded, extracted, never executed):
# https://patch.ikuai8.com/3.x/patch/iKuai8_x64_4.0.120_beta_Build202603191336.bin
# usr/ikuai/script/dhcp_server.sh: edit(), __show_data(), __check_param_add();
# etc/defaults/config_db.conf: dhcp_server; the backend explicitly allows dns2=''.
# This is a full edit form: omitting fields would reset unrelated DHCP settings.
DHCP_OPTIONS = (15, 28, 43, 60, 66, 67, 80, 119, 125, 128, 138, 121)
DHCP_INT_FIELDS = ('lease', 'delay', 'check_addr_valid', 'check_relay_only') + tuple(
    f'opt_type{number}' for number in DHCP_OPTIONS)
DHCP_FIELDS = ('id', 'enabled', 'tagname', 'interface', 'phy_ifnames', 'addr_pool', 'exclude_pool',
               'netmask', 'gateway', 'next_server', 'dns1', 'dns2', 'domain', 'wins1', 'wins2') + (
    DHCP_INT_FIELDS) + tuple(f'opt{number}' for number in DHCP_OPTIONS)
DHCP_STATIC_FIELDS = ('id', 'enabled', 'tagname', 'interface', 'mac', 'gateway', 'dns1', 'dns2',
                      'ip_addr', 'comment')


def encode_value(value: Any, key: str = '') -> Any:
    """Encode strings like the iKuai UI (``$("encode", form)``) before add/edit calls."""
    if isinstance(value, dict):
        return {k: encode_value(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [encode_value(item) for item in value]
    if not isinstance(value, str):
        return value
    text = value.strip() if key == 'comment' else value
    text = text.replace('%', '%25')
    if key == 'comment':
        text = text.replace('\r\n', '%0A').replace('\n', '%0A').replace('\r', '%0A')
    text = ''.join(' ' if char.isspace() else char for char in text).replace(' ', '%20')
    for raw, encoded in _ENCODE:
        text = text.replace(raw, encoded)
    return text


def decode_value(value: Any, key: str = '') -> Any:
    if isinstance(value, dict):
        return {k: decode_value(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [decode_value(item) for item in value]
    if not isinstance(value, str):
        return value
    text = value.replace('%0A', '\n').replace('%0a', '\n') if key == 'comment' else value
    text = text.replace('%20', ' ')
    for raw, encoded in _ENCODE:
        text = text.replace(encoded, raw).replace(encoded.lower(), raw)
    return text.replace('%2F', '/').replace('%2f', '/').replace('%25', '%')


def _address_field(value: Any) -> Dict[str, Any]:
    """iKuai returns empty address lists as ``{}``; the forms send ``[]``."""
    value = value if isinstance(value, dict) else {}
    return {key: value.get(key) if isinstance(value.get(key), list) else [] for key in ('custom', 'object')}


def _origin(url: str) -> str:
    """Only show the management origin; redirects can contain login secrets."""
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        if parsed.scheme not in ('http', 'https') or not host:
            return ''
        host = f'[{host}]' if ':' in host else host
        port = parsed.port
        if port is not None and port != (443 if parsed.scheme == 'https' else 80):
            host = f'{host}:{port}'
        return f'{parsed.scheme}://{host}'
    except ValueError:
        return ''


def _http_error(response: requests.Response, url: str) -> str:
    status = response.status_code
    code = f'（HTTP {status}）'
    if status in (301, 302, 303, 307, 308):
        location = response.headers.get('Location', '')
        try:
            target = _origin(urljoin(url, location)) if location else ''
        except ValueError:
            target = ''
        if target and target != _origin(url):
            return (f'iKuai 要求跳转到 {target}{code}。'
                    '请核对浏览器打开的管理地址，在「编辑路由器」中修改地址后重试。')
        return (f'iKuai 将请求转到了其他页面{code}。请在浏览器打开管理页面，'
                '核对最终地址的协议和端口；若地址无误，请检查管理入口或反向代理的跳转设置。')
    reasons = {
        401: '身份验证未通过，请检查用户名、密码，以及管理入口是否另有登录验证',
        403: '拒绝访问，请检查账号的管理员权限和管理页面的访问限制',
        404: '未找到管理接口，请检查管理地址和端口，并确认使用的是 iKuai 4.x',
        405: '管理接口不接受此请求，请检查管理地址，以及反向代理是否允许接口请求',
        408: '等待请求超时，请检查网络连接后重试',
        429: '请求过于频繁，请稍后再试',
        502: '管理入口未能连接后端服务，请检查 iKuai 管理服务或反向代理',
        503: '管理服务暂时不可用，请稍后再试',
        504: '管理入口等待后端响应超时，请检查 iKuai 管理服务或反向代理',
    }
    reason = reasons.get(status)
    if reason is None:
        reason = ('管理服务发生错误，请确认浏览器能否正常打开管理页面，稍后重试'
                  if status >= 500 else '请求未成功，请检查管理地址、端口和管理入口设置')
    return f'iKuai {reason}{code}。'


class IKuaiDriver(RouterDriver):
    vendor = 'ikuai'
    supports_write = True

    def __init__(self, endpoint: str, username: str, password: str, verify_tls: bool = True):
        self.endpoint = endpoint.rstrip('/')
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.verify = verify_tls
        self._logged_in = False

    def close(self):
        self.session.close()

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f'{self.endpoint}{path}'
        try:
            response = self.session.post(url, json=payload,
                                         timeout=TIMEOUT, allow_redirects=False)
        except requests.exceptions.SSLError:
            raise RouterError('iKuai HTTPS 连接失败：请检查证书是否可信、是否过期，'
                              '以及管理地址和 HTTPS 端口是否正确。') from None
        except requests.ConnectTimeout:
            raise RouterError(f'连接 iKuai 超时（{TIMEOUT} 秒）：请检查管理地址、端口，'
                              '以及 ConfigFlow 所在主机到路由器的网络是否畅通。') from None
        except requests.Timeout:
            raise RouterError(f'等待 iKuai 响应超时（{TIMEOUT} 秒）：请检查网络连接和路由器负载，稍后重试。') from None
        except requests.ConnectionError:
            raise RouterError('无法连接 iKuai 管理服务：请检查地址、端口和域名解析，'
                              '确认管理服务已开启，并允许 ConfigFlow 所在主机访问。') from None
        except requests.RequestException:
            raise RouterError('与 iKuai 通信失败：请检查管理地址、端口和网络连接，稍后重试。') from None
        if response.status_code != 200:
            raise RouterError(_http_error(response, url))
        try:
            data = response.json()
        except ValueError:
            raise RouterError('iKuai 返回的不是 JSON，请确认地址指向管理页面') from None
        if not isinstance(data, dict):
            raise RouterError('iKuai 返回格式无法识别')
        return data

    def _login(self):
        if self._logged_in:
            return
        payload = {
            'username': self.username,
            'passwd': hashlib.md5(self.password.encode()).hexdigest(),
            'pass': base64.b64encode(f'salt_11{self.password}'.encode()).decode(),
        }
        data = self._post('/Action/login', payload)
        if data.get('Result') != 10000 and data.get('code') != 0:
            raise RouterError(f'iKuai 登录失败：{data.get("message") or "账号或密码错误"}')
        self._logged_in = True

    def call(self, func_name: str, param: Dict[str, Any], action: str = 'show') -> Dict[str, Any]:
        self._login()
        data = self._post('/Action/call', {'func_name': func_name, 'action': action, 'param': param})
        if data.get('code') == 1003:
            raise RouterError(f'iKuai 拒绝访问 {func_name}（需要管理员账号）')
        if data.get('code') != 0 or not isinstance(data.get('results'), dict):
            if 'Data' in data or 'Result' in data:
                raise RouterError('检测到 iKuai 3.x 的响应格式，只支持 iKuai 4.x')
            raise RouterError(f'iKuai 调用 {func_name} 失败：{data.get("message") or data.get("code")}')
        return data['results']

    def _list(self, func_name: str) -> List[Dict[str, Any]]:
        results = self.call(func_name, {'TYPE': 'total,data', 'limit': LIST_LIMIT})
        rows = results.get('data') or []
        return [row for row in rows if isinstance(row, dict)]

    def probe(self) -> RouterInfo:
        verinfo = self.call('sysstat', {'TYPE': 'verinfo'}).get('verinfo') or {}
        version = str(verinfo.get('version') or '')
        if not version.startswith('4.'):
            raise RouterError(f'只支持 iKuai 4.x，当前版本 {version or "未知"}')
        capabilities = []
        for func, capability in (('static_rt', 'routes'), ('nat_rule', 'nat_bypass'), ('route_object', 'address_sets'),
                                 ('stream_ipport', 'policy_routes')):
            try:
                self._list(func)
                capabilities.append(capability)
            except RouterError:
                pass
        return RouterInfo(vendor='ikuai', version=version,
                          model=str(verinfo.get('verstring') or ''), capabilities=capabilities)

    def read_state(self) -> RouterState:
        state = RouterState()
        for row in self._list('static_rt'):
            try:
                network = ipaddress.ip_network(f'{row.get("dst_addr")}/{row.get("netmask")}', strict=False)
            except ValueError:
                continue
            state.routes.append(RouteEntry(
                id=str(row.get('id', '')), dst=str(network), gateway=str(row.get('gateway') or ''),
                enabled=row.get('enabled') == 'yes', comment=unquote(str(row.get('comment') or ''))))
        groups = {str(row.get('group_id')): row for row in self._list('route_object')}
        for row in groups.values():
            state.address_sets[str(row.get('group_name') or row.get('tagname'))] = [
                str(item.get('ip')) for item in row.get('group_value') or [] if isinstance(item, dict) and item.get('ip')]
        for row in self._list('nat_rule'):
            if row.get('action') != 'filter':
                continue
            state.nat_bypass.append(NatBypassEntry(
                id=str(row.get('id', '')), dst=_addresses(row.get('dst_addr'), state.address_sets),
                src=_addresses(row.get('src_addr'), state.address_sets),
                enabled=row.get('enabled') == 'yes', comment=unquote(str(row.get('comment') or ''))))
        # Static routes are destination-only: the gateway's own (DIRECT) traffic
        # to a proxy CIDR would bounce between router and gateway forever. Proxy
        # CIDRs are therefore routed by 端口分流 rules that exclude the gateway.
        state.policy_routes = []
        for row in self._list('stream_ipport'):
            row = decode_value(row)
            state.policy_routes.append(PolicyRouteEntry(
                id=str(row.get('id', '')), dst=_addresses(row.get('dst_addr'), state.address_sets),
                gateway=str(row.get('nexthop') or '') if row.get('type') == POLICY_NEXTHOP else '',
                enabled=row.get('enabled') == 'yes', comment=str(row.get('comment') or ''),
                src=_addresses(row.get('src_addr'), state.address_sets),
                src_invert=row.get('src_addr_inv') == 1, dst_invert=row.get('dst_addr_inv') == 1,
                protocol=str(row.get('protocol') or ''),
                priority=row.get('prio') if type(row.get('prio')) is int else None))
        state.active_routes = self._active_routes()
        return state

    def _active_routes(self) -> Optional[List[RouteEntry]]:
        """当前路由表: what the kernel forwards with, including routes no config explains."""
        try:
            rows = self._list('static_rt_table')
        except RouterError:
            return None
        routes = []
        for row in rows:
            try:
                network = ipaddress.ip_network(f'{row.get("dst_addr")}/{row.get("netmask")}', strict=False)
            except ValueError:
                continue
            routes.append(RouteEntry(id=f'active:{row.get("id", "")}', dst=str(network),
                                     gateway=str(row.get('gateway') or ''), enabled=True,
                                     comment=f'{row.get("interface") or ""} prio {row.get("prio")}'))
        return routes

    def _dhcp_list(self, func: str, prefix: str = '') -> List[Dict[str, Any]]:
        results = self.call(func, {'TYPE': f'{prefix}total,{prefix}data', 'limit': LIST_LIMIT})
        rows, total = results.get(f'{prefix}data'), results.get(f'{prefix}total')
        # Vendor include/sqlite.sh prints literal [] for an empty JSON query.
        if (not isinstance(rows, list) or type(total) is not int or total != len(rows)
                or any(not isinstance(row, dict) for row in rows)):
            raise RouterError(f'iKuai {func} 配置读取不完整，无法安全下发 DHCP DNS')
        return [decode_value(row) for row in rows]

    def read_dhcp_dns(self) -> DhcpDnsState:
        rows = self._dhcp_list('dhcp_server')
        # Vendor dhcp_lease.sh dumps these DNS fields into ik_dhcpd_static.conf;
        # per-client overrides must not silently defeat a network-level change.
        bindings = self._dhcp_list('dhcp_static', 'static_')
        state = DhcpDnsState(evidence={
            'servers': [{key: value for key, value in row.items() if key != 'available'} for row in rows],
            'static': [{key: value for key, value in row.items()
                        if key not in ('termname', 'hostname', 'ip_addr_int')} for row in bindings],
        })
        for row in rows:
            blockers = []
            missing = set(DHCP_FIELDS) - row.keys()
            unknown = row.keys() - set(DHCP_FIELDS) - {'status', 'available'}
            if missing or unknown:
                blockers.append('iKuai DHCP 字段与已验证的 4.x 格式不兼容，无法保留完整配置'
                                + f'（缺少：{",".join(sorted(missing)) or "无"}；未知：{",".join(sorted(unknown)) or "无"}）')
            for key in DHCP_FIELDS:
                if key not in row:
                    continue
                value = row[key]
                valid = (type(value) is int if key in DHCP_INT_FIELDS or key == 'id'
                         else isinstance(value, str))
                if not valid:
                    blockers.append(f'iKuai DHCP 字段 {key} 类型无法识别')
            if row.get('enabled') not in ('yes', 'no'):
                blockers.append('iKuai DHCP 启用状态无法识别')
            if row.get('status') != 1 or type(row.get('status')) is not int:
                blockers.append('iKuai DHCP 服务未生效或无法确认运行状态')
            if row.get('check_relay_only') != 0:
                blockers.append('iKuai DHCP 仅响应中继请求，无法确认该网段客户端的 DNS 覆盖')
            if row.get('phy_ifnames') != 'all':
                blockers.append('iKuai DHCP 限制了物理接口，无法确认完整网段的 DNS 覆盖')
            network = ''
            try:
                start, end = str(row.get('addr_pool') or '').split('-')
                start, end = ipaddress.IPv4Address(start), ipaddress.IPv4Address(end)
                subnet = ipaddress.IPv4Network(f'{start}/{row.get("netmask")}', strict=False)
                if end not in subnet or start > end or ipaddress.IPv4Address(row.get('gateway')) not in subnet:
                    raise ValueError
                network = str(subnet)
            except (ValueError, TypeError):
                blockers.append('iKuai DHCP 地址池、掩码或网关无法确定唯一网段')
            dns = []
            for field in ('dns1', 'dns2'):
                value = row.get(field)
                if value == '':
                    continue
                try:
                    address = str(ipaddress.IPv4Address(value))
                    if address not in dns:
                        dns.append(address)
                except (ValueError, TypeError):
                    blockers.append('iKuai DHCP DNS 包含无法识别的自动或继承配置')
            for binding in bindings:
                if binding.get('enabled') == 'no':
                    continue
                try:
                    if not isinstance(binding.get('ip_addr'), str):
                        raise ValueError
                    address = ipaddress.IPv4Address(binding.get('ip_addr'))
                except (ValueError, TypeError):
                    blockers.append('iKuai 静态 DHCP 绑定缺少有效地址，无法排除 DNS 覆盖')
                    continue
                if network and address not in subnet:
                    continue
                if (binding.get('enabled') != 'yes' or not all(key in binding for key in DHCP_STATIC_FIELDS)
                        or any(not isinstance(binding.get(key), str) for key in DHCP_STATIC_FIELDS if key != 'id')):
                    blockers.append('iKuai 静态 DHCP 绑定信息不完整，无法排除 DNS 覆盖')
                elif binding['dns1'] or binding['dns2']:
                    blockers.append('该网段存在静态 DHCP 绑定的独立 DNS，请先移除绑定中的 DNS 覆盖')
                if binding.keys() - set(DHCP_STATIC_FIELDS) - {'termname', 'hostname', 'ip_addr_int'}:
                    blockers.append('iKuai 静态 DHCP 绑定含未验证字段，无法排除 DNS 覆盖')
            state.entries.append(DhcpDnsEntry(
                id=str(row.get('id', '')), network=network, dns=dns,
                enabled=row.get('enabled') != 'no',
                dns_explicit=bool(row.get('dns1') and row.get('dns2')),
                attributes={key: row[key] for key in DHCP_FIELDS if key in row},
                blockers=list(dict.fromkeys(blockers))))
        return state

    @staticmethod
    def _dhcp_entry(state: DhcpDnsState, row_id: Any) -> DhcpDnsEntry:
        matches = [entry for entry in state.entries if entry.id == str(row_id)]
        if len(matches) != 1:
            raise RouterError('iKuai DHCP 目标配置不存在或标识不唯一')
        return matches[0]

    def _dhcp_edit_verified(self, payload: Dict[str, Any]) -> None:
        self._write('dhcp_server', 'edit', payload)
        actual = self._dhcp_entry(self.read_dhcp_dns(), payload['id'])
        if actual.attributes != payload or actual.blockers:
            raise RouterError('iKuai DHCP DNS 回读校验失败：DNS 或其他 DHCP 配置与提交值不一致')

    def _dhcp_apply(self, op: Dict[str, Any]) -> Dict[str, Any]:
        if op['kind'] == 'dhcp_dns_restore':
            payload = op.get('record')
            if not isinstance(payload, dict) or set(payload) != set(DHCP_FIELDS):
                raise RouterError('iKuai DHCP DNS 撤销记录不完整')
            # A rollback already has a complete before-image and known ID. It
            # must remain possible when the failed release cannot read state.
            self._dhcp_edit_verified(payload)
            return {'kind': 'dhcp_dns_restore', 'record': payload}
        state = self.read_dhcp_dns()
        if op['kind'] == 'dhcp_dns_update':
            scope = op.get('scope')
            if (not isinstance(scope, dict)
                    or any(not isinstance(scope.get(key), str) or not scope[key]
                           for key in ('id', 'network', 'gateway'))):
                raise RouterError('iKuai DHCP DNS 操作缺少已预览网段')
            entry = checked_dns_target(scope, state)
            if entry.id != str(op.get('id')) or entry.attributes != op.get('expected'):
                raise RouterError('iKuai DHCP 配置已变化，请重新预览')
            target = op.get('dns')
            try:
                if (not isinstance(target, list) or len(target) != 1
                        or not isinstance(target[0], str)):
                    raise ValueError
                address = ipaddress.IPv4Address(target[0])
                if address.is_unspecified or address.is_multicast or address.is_loopback or int(address) == 0xffffffff:
                    raise ValueError
            except (ValueError, TypeError):
                raise RouterError('iKuai DHCP DNS 需要单个有效 IPv4 地址') from None
            # The UI requires both fields. Set both explicitly to the same DNS:
            # no blank value can inherit a firmware-specific fallback resolver.
            # read_dhcp_dns deduplicates the effective server list.
            payload = {**entry.attributes, 'dns1': str(address), 'dns2': str(address)}
        before = entry.attributes
        try:
            self._dhcp_edit_verified(payload)
        except RouterError as error:
            try:
                try:
                    actual = self._dhcp_entry(self.read_dhcp_dns(), payload['id'])
                    changed = actual.attributes != before
                except RouterError:
                    # An unreachable read does not imply that edit failed. The
                    # known ID and complete before-image still permit rollback.
                    changed = True
                if changed:
                    self._dhcp_edit_verified(before)
            except RouterError as rollback_error:
                raise RouterRollbackError(f'{error}；DHCP DNS 局部撤销失败：{rollback_error}') from None
            raise
        return {'kind': 'dhcp_dns_restore', 'record': before}


    # --- writes -------------------------------------------------------------

    def _write(self, func_name: str, action: str, param: Dict[str, Any]) -> Dict[str, Any]:
        self._login()
        data = self._post('/Action/call', {'func_name': func_name, 'action': action, 'param': encode_value(param)})
        if data.get('code') != 0:
            details = '；'.join(f'{item.get("field")}: {item.get("msg")}' for item in data.get('details') or []
                               if isinstance(item, dict))
            raise RouterError(f'iKuai {func_name} {action} 失败：{data.get("message") or data.get("code")}'
                              + (f'（{details}）' if details else ''))
        return data.get('results') if isinstance(data.get('results'), dict) else {}

    def _row(self, func_name: str, row_id: Any) -> Dict[str, Any]:
        for row in self._list(func_name):
            if str(row.get('id')) == str(row_id):
                return decode_value(row)
        raise RouterError(f'iKuai {func_name} 中找不到 id={row_id}，可能已被手动删除')

    def _find_id(self, func_name: str, match) -> str:
        for row in self._list(func_name):
            if match(decode_value(row)):
                return str(row.get('id'))
        raise RouterError(f'iKuai {func_name} 已提交但读不回新对象')

    def _route_payload(self, row: Dict[str, Any], **changes) -> Dict[str, Any]:
        payload = {key: row.get(key) for key in ROUTE_FIELDS if key in row}
        payload.update(changes)
        payload['netmask'] = str(payload.get('netmask') or '').split(' ')[0]
        return payload

    def _nat_payload(self, row: Dict[str, Any], **changes) -> Dict[str, Any]:
        payload = {key: row.get(key) for key in NAT_FIELDS if key in row}
        for key in ('src_addr', 'dst_addr', 'src_port', 'dst_port'):
            payload[key] = _address_field(payload.get(key))
        payload.update(changes)
        return payload

    def _policy_payload(self, row: Dict[str, Any], **changes) -> Dict[str, Any]:
        """A complete 端口分流 form: omitted fields would reset the rule's other settings."""
        payload = {key: row.get(key) for key in POLICY_FIELDS if key in row}
        for key in ('src_addr', 'dst_addr', 'src_port', 'dst_port', 'time'):
            payload[key] = _address_field(payload.get(key))
        payload.update(changes)
        return payload

    def _new_policy(self, gateway: str, dst: List[str], comment: str) -> Dict[str, Any]:
        return self._policy_payload({}, **self._policy_target(gateway, dst, comment), tagname=_tagname('pr', comment),
                                    interface='', iface_band=0, time=POLICY_ALWAYS, mode=0,
                                    area_code='', dst_type=0)

    @staticmethod
    def _policy_target(gateway: str, dst: List[str], comment: str) -> Dict[str, Any]:
        """The fields that make a rule route ``dst`` to the gateway for every source but itself."""
        return {'enabled': 'yes', 'type': POLICY_NEXTHOP, 'nexthop': gateway, 'protocol': 'any', 'prio': POLICY_PRIORITY,
                'src_addr': {'custom': [gateway], 'object': []}, 'src_addr_inv': 1,
                'dst_addr': {'custom': list(dst), 'object': []}, 'dst_addr_inv': 0, 'comment': comment}

    def _add_policy(self, payload: Dict[str, Any]) -> str:
        self._write('stream_ipport', 'add', payload)
        return self._find_id('stream_ipport', lambda row: row.get('tagname') == payload['tagname']
                             and row.get('comment') == payload['comment'])

    def _apply_policy(self, op: Dict[str, Any]) -> Dict[str, Any]:
        kind = op['kind']
        if kind == 'policy_route_add':
            rule_id = self._add_policy(self._new_policy(op['gateway'], op['dst'], op['comment']))
            return {'kind': 'policy_route_delete', 'id': rule_id}
        if kind == 'policy_route_readd':
            payload = self._policy_payload(op['record'])
            payload.pop('id', None)
            return {'kind': 'policy_route_delete', 'id': self._add_policy(payload)}
        if kind == 'policy_route_delete':
            row = self._row('stream_ipport', op['id'])
            self._write('stream_ipport', 'del', {'id': row['id']})
            return {'kind': 'policy_route_readd', 'record': row}
        row = self._row('stream_ipport', op['id'] if kind == 'policy_route_update' else op['record']['id'])
        payload = (self._policy_payload(row, **self._policy_target(op['gateway'], op['dst'], op['comment']))
                   if kind == 'policy_route_update' else self._policy_payload(op['record']))
        self._write('stream_ipport', 'edit', payload)
        return {'kind': 'policy_route_restore', 'record': row}

    def _add_route(self, cidr: str, gateway: str, comment: str) -> str:
        network = ipaddress.IPv4Network(cidr)
        self._write('static_rt', 'add', {
            'enabled': 'yes', 'tagname': _tagname('rt', cidr), 'ip_type': '4', 'interface': 'auto',
            'dst_addr': str(network.network_address), 'gateway': gateway, 'netmask': str(network.netmask),
            'prio': 1, 'comment': comment})
        return self._find_id('static_rt', lambda row: row.get('dst_addr') == str(network.network_address)
                             and row.get('netmask') == str(network.netmask) and row.get('gateway') == gateway
                             and row.get('comment') == comment)

    def apply(self, op: Dict[str, Any]) -> Dict[str, Any]:
        kind = op['kind']
        if kind in ('dhcp_dns_update', 'dhcp_dns_restore'):
            return self._dhcp_apply(op)
        if kind in ('policy_route_add', 'policy_route_update', 'policy_route_restore',
                    'policy_route_delete', 'policy_route_readd'):
            return self._apply_policy(op)
        if kind == 'route_add':
            route_id = self._add_route(op['cidr'], op['gateway'], op['comment'])
            return {'kind': 'route_delete', 'id': route_id}
        if kind == 'route_delete':
            row = self._row('static_rt', op['id'])
            self._write('static_rt', 'del', {'id': row['id']})
            network = ipaddress.ip_network(f'{row["dst_addr"]}/{row["netmask"]}', strict=False)
            return {'kind': 'route_add', 'cidr': str(network), 'gateway': row.get('gateway'),
                    'comment': row.get('comment') or ''}
        if kind in ('route_comment', 'route_restore'):
            row = self._row('static_rt', op['id'] if kind == 'route_comment' else op['record']['id'])
            payload = (self._route_payload(row, comment=op['comment']) if kind == 'route_comment'
                       else self._route_payload(op['record']))
            self._write('static_rt', 'edit', payload)
            return {'kind': 'route_restore', 'record': row}
        if kind == 'nat_update':
            row = self._row('nat_rule', op['id'])
            dst = _address_field(row.get('dst_addr'))
            dst['custom'] = dst['custom'] + [cidr for cidr in op.get('add_dst') or [] if cidr not in dst['custom']]
            self._write('nat_rule', 'edit', self._nat_payload(row, dst_addr=dst, comment=op['comment']))
            return {'kind': 'nat_restore', 'record': row}
        if kind == 'nat_restore':
            row = self._row('nat_rule', op['record']['id'])
            self._write('nat_rule', 'edit', self._nat_payload(op['record']))
            return {'kind': 'nat_restore', 'record': row}
        if kind == 'nat_add':
            self._write('nat_rule', 'add', self._nat_payload({}, enabled='yes', tagname=_tagname('nat', ','.join(op['dst'])),
                                                             action='filter',
                                                             protocol='any', iinterface='any', ointerface='any',
                                                             src_addr={'custom': op['src'], 'object': []},
                                                             dst_addr={'custom': op['dst'], 'object': []},
                                                             nat_addr='', nat_port='', src_addr_inv=0,
                                                             dst_addr_inv=0, comment=op['comment']))
            rule_id = self._find_id('nat_rule', lambda row: row.get('action') == 'filter'
                                    and row.get('comment') == op['comment'])
            return {'kind': 'nat_delete', 'id': rule_id}
        if kind == 'nat_delete':
            row = self._row('nat_rule', op['id'])
            self._write('nat_rule', 'del', {'id': row['id']})
            return {'kind': 'nat_add', 'src': _address_field(row.get('src_addr'))['custom'],
                    'dst': _address_field(row.get('dst_addr'))['custom'], 'comment': row.get('comment') or ''}
        raise RouterError(f'不支持的操作 {kind}')


def _tagname(kind: str, key: str) -> str:
    """iKuai requires a 1-15 character name; derive a stable one, e.g. ``cf_rt_1a2b3c4d``."""
    return f'cf_{kind}_{hashlib.sha1(key.encode()).hexdigest()[:15 - len(kind) - 4]}'


def _addresses(value: Any, address_sets: Dict[str, List[str]]) -> List[str]:
    """Flatten iKuai ``{"custom": [...], "object": [...]}`` into concrete CIDRs/ranges."""
    if not isinstance(value, dict):
        return []
    result = [str(item) for item in value.get('custom') or [] if isinstance(item, str)]
    objects = value.get('object')
    for ref in objects if isinstance(objects, list) else []:
        if isinstance(ref, dict):
            result.extend(address_sets.get(str(ref.get('gp_name')), []))
    return result
