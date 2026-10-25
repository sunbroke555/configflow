"""iKuai 4.x driver.

iKuai has no public API; this uses the same endpoints as its web UI:
``POST /Action/login`` then ``POST /Action/call`` with ``func_name``/``action``.
Only 4.x is supported: responses are ``{"code": 0, "results": {...}}``.
Write payloads mirror the web UI forms, including its string encoding.
"""
import base64
import copy
import hashlib
import ipaddress
import json
from contextlib import contextmanager
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urljoin, urlsplit

import requests

from backend.routers.base import (
    POLICY_PRIORITY, DhcpDnsEntry, DhcpDnsState, NatBypassEntry, PolicyRouteEntry, RouteEntry, RouterDriver,
    RouterError, RouterInfo, RouterRollbackError, RouterState,
)
from backend.routers import ikuai_groups
from backend.routers.dhcp_dns import checked_dns_target

TIMEOUT = 10
PAGE_SIZE = 1000
# Writing a rule back on rollback costs iKuai about 1.6 ms per literal entry (4.0.314).
RESTORE_SLOW_ENTRIES = 1000
RESTORE_SECONDS_PER_ENTRY = 0.003
# 4.0.314 answers an empty 200 to larger request bodies (measured: list-sized
# bodies up to 98267 bytes were accepted, 98308 bytes and more were refused).
MAX_REQUEST_BYTES = 96 * 1024
LIST_LABELS = {'static_rt': '静态路由', 'static_rt_table': '当前路由表', 'route_object': 'IP 分组',
               'nat_rule': 'NAT 规则', 'stream_ipport': '端口分流'}

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


def _call_error(func_name: str, data: Dict[str, Any]) -> str:
    """Explain a failed /Action/call.

    Codes as observed on 4.0.314 (many replies also carry ``"Result": 10000``):
    1001 no read permission and 1002 no write permission for the account's
    user group (with ``Label`` naming the feature); 1003 ``Access denied`` for a
    func_name this firmware does not have (restricted accounts get 1001 for
    those instead); 1008 session expired; 3001 invalid action or parameter.
    """
    code = data.get('code')
    message = data.get('message') if isinstance(data.get('message'), str) else ''
    label = data.get('Label') if isinstance(data.get('Label'), str) and data.get('Label') else ''
    feature = f'「{label}」（{func_name}）' if label else f' {func_name} '
    if code == 1001:
        return (f'iKuai 账号没有{feature}的访问权限（错误码 1001）。'
                '请在 iKuai 的用户组中授予读取权限，或改用管理员账号。')
    if code == 1002:
        return (f'iKuai 账号没有{feature}的执行权限（错误码 1002）。'
                '请在 iKuai 的用户组中授予修改权限，或改用管理员账号。')
    if code == 1003:
        return (f'iKuai 不提供 {func_name} 接口（错误码 1003{"：" + message if message else ""}）。'
                '当前固件可能不支持这项功能，请确认 iKuai 版本。')
    if code == 1008:
        return 'iKuai 登录会话已失效（错误码 1008），重新登录后仍未恢复，请稍后重试。'
    if code is None:
        return f'iKuai 调用 {func_name} 返回格式无法识别'
    return f'iKuai 调用 {func_name} 失败：{message or "未知错误"}（错误码 {code}）'


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
        # Inside batch(): one static_rt snapshot and the routes added since.
        self._batch = None

    def close(self):
        self.session.close()

    def _post(self, path: str, payload: Dict[str, Any], timeout: Optional[float] = None) -> Dict[str, Any]:
        url = f'{self.endpoint}{path}'
        timeout = timeout or TIMEOUT
        try:
            response = self.session.post(url, json=payload,
                                         timeout=timeout, allow_redirects=False)
        except requests.exceptions.SSLError:
            raise RouterError('iKuai HTTPS 连接失败：请检查证书是否可信、是否过期，'
                              '以及管理地址和 HTTPS 端口是否正确。') from None
        except requests.ConnectTimeout:
            raise RouterError(f'连接 iKuai 超时（{TIMEOUT} 秒）：请检查管理地址、端口，'
                              '以及 ConfigFlow 所在主机到路由器的网络是否畅通。') from None
        except requests.Timeout:
            raise RouterError(f'等待 iKuai 响应超时（{timeout:g} 秒）：请检查网络连接和路由器负载，稍后重试。') from None
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
            content = getattr(response, 'content', None)
            if isinstance(content, bytes) and not content.strip():
                # The management page itself is never empty; iKuai drops oversized requests this way.
                raise RouterError('iKuai 没有返回任何内容：请求可能过大或被 iKuai 拒绝') from None
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
        # 4.x answers every login with "Result": 10000, failures included
        # (4.0.314: {"Result":10000,"code":1005,"message":"账号或密码错误"}); only
        # ``code`` tells them apart. A reply without ``code`` is 3.x style.
        failed = data.get('code') != 0 if 'code' in data else data.get('Result') != 10000
        if failed:
            detail = data.get('message') or data.get('ErrMsg') or '账号或密码错误'
            code = f'（错误码 {data["code"]}）' if 'code' in data else ''
            raise RouterError(f'iKuai 登录失败：{detail}{code}')
        self._logged_in = True

    def call(self, func_name: str, param: Dict[str, Any], action: str = 'show') -> Dict[str, Any]:
        self._login()
        payload = {'func_name': func_name, 'action': action, 'param': param}
        data = self._post('/Action/call', payload)
        if data.get('code') == 1008:
            # Session expired (the router restarted, or another login replaced it): log in once more.
            self._logged_in = False
            self._login()
            data = self._post('/Action/call', payload)
        code = data.get('code')
        if code == 0 and isinstance(data.get('results'), dict):
            return data['results']
        if 'code' not in data and ('Data' in data or 'Result' in data):
            raise RouterError('检测到 iKuai 3.x 的响应格式，只支持 iKuai 4.x')
        raise RouterError(_call_error(func_name, data))

    def _list_all(self, func_name: str, prefix: str = '', ids: bool = True,
                  incomplete: str = '') -> List[Dict[str, Any]]:
        """Every row, page by page; fail closed on truncation, drift between pages or unclear IDs."""
        label = LIST_LABELS.get(func_name, func_name)
        incomplete = incomplete or f'iKuai {label}列表读取不完整或读取期间发生变化，请重试'
        rows, total, offset, seen = [], None, 0, set()
        while total is None or offset < total:
            result = self.call(func_name, {'TYPE': f'{prefix}total,{prefix}data', 'limit': f'{offset},{PAGE_SIZE}'})
            count, page = result.get(f'{prefix}total'), result.get(f'{prefix}data')
            if (type(count) is not int or count < 0 or not isinstance(page, list)
                    or (total is not None and count != total)
                    or len(page) != min(PAGE_SIZE, max(0, count - offset))):
                raise RouterError(incomplete)
            total = count
            for row in page:
                if not ids:
                    if not isinstance(row, dict):
                        raise RouterError(incomplete)
                elif (not isinstance(row, dict) or type(row.get('id')) is not int or row['id'] < 1
                      or row['id'] in seen):
                    raise RouterError(f'iKuai {label}列表缺少唯一有效 ID，无法安全编辑')
                else:
                    seen.add(row['id'])
                rows.append(row)
            offset += len(page)
            if not total:
                break
        return rows

    def probe(self) -> RouterInfo:
        verinfo = self.call('sysstat', {'TYPE': 'verinfo'}).get('verinfo') or {}
        version = str(verinfo.get('version') or '')
        if not version.startswith('4.'):
            raise RouterError(f'只支持 iKuai 4.x，当前版本 {version or "未知"}')
        capabilities = []
        for func, capability in (('static_rt', 'routes'), ('nat_rule', 'nat_bypass'), ('route_object', 'address_sets'),
                                 ('stream_ipport', 'policy_routes')):
            try:
                self.call(func, {'TYPE': 'total,data', 'limit': '0,1'})
                capabilities.append(capability)
            except RouterError:
                pass
        return RouterInfo(vendor='ikuai', version=version,
                          model=str(verinfo.get('verstring') or ''), capabilities=capabilities)

    def read_state(self) -> RouterState:
        state = RouterState()
        for row in self._list_all('static_rt'):
            try:
                network = ipaddress.ip_network(f'{row.get("dst_addr")}/{row.get("netmask")}', strict=False)
            except ValueError:
                continue
            state.routes.append(RouteEntry(
                id=str(row.get('id', '')), dst=str(network), gateway=str(row.get('gateway') or ''),
                enabled=row.get('enabled') == 'yes', comment=unquote(str(row.get('comment') or ''))))
        # IDs are optional here: only an edit or delete of one of our groups needs one.
        groups = [ikuai_groups.group_from_row(decode_value(row)) for row in self._list_all('route_object', ids=False)]
        ikuai_groups.mark_owners(groups)
        for group in groups:
            state.address_sets[group['name']] = group['values']
        by_gid = {group['gid']: group['values'] for group in groups if group['gid']}
        nat_rows = self._list_all('nat_rule')
        for row in nat_rows:
            if row.get('action') != 'filter':
                continue
            state.nat_bypass.append(NatBypassEntry(
                id=str(row.get('id', '')), dst=_addresses(row.get('dst_addr'), state.address_sets, by_gid),
                src=_addresses(row.get('src_addr'), state.address_sets, by_gid),
                enabled=row.get('enabled') == 'yes', comment=unquote(str(row.get('comment') or ''))))
        # Static routes are destination-only: the gateway's own (DIRECT) traffic
        # to a proxy CIDR would bounce between router and gateway forever. Proxy
        # CIDRs are therefore routed by 端口分流 rules that exclude the gateway.
        state.policy_routes = []
        policy_rows = [decode_value(row) for row in self._list_all('stream_ipport')]
        for row in policy_rows:
            state.policy_routes.append(PolicyRouteEntry(
                id=str(row.get('id', '')), dst=_addresses(row.get('dst_addr'), state.address_sets, by_gid),
                gateway=str(row.get('nexthop') or '') if row.get('type') == POLICY_NEXTHOP else '',
                enabled=row.get('enabled') == 'yes', comment=str(row.get('comment') or ''),
                src=_addresses(row.get('src_addr'), state.address_sets, by_gid),
                src_invert=row.get('src_addr_inv') == 1, dst_invert=row.get('dst_addr_inv') == 1,
                protocol=str(row.get('protocol') or ''),
                priority=row.get('prio') if type(row.get('prio')) is int else None))
        state.ikuai_groups = {'groups': groups, 'rules': ikuai_groups.read_rules(
            groups, [decode_value(row) for row in nat_rows], policy_rows)}
        state.active_routes = self._active_routes()
        return state

    def _active_routes(self) -> Optional[List[RouteEntry]]:
        """当前路由表: what the kernel forwards with, including routes no config explains."""
        try:
            rows = self._list_all('static_rt_table', ids=False)
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
        # Vendor include/sqlite.sh prints literal [] for an empty JSON query.
        rows = self._list_all(func, prefix, ids=False,
                              incomplete=f'iKuai {func} 配置读取不完整，无法安全下发 DHCP DNS')
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

    def _write(self, func_name: str, action: str, param: Dict[str, Any],
               timeout: Optional[float] = None) -> Dict[str, Any]:
        self._login()
        payload = {'func_name': func_name, 'action': action, 'param': encode_value(param)}
        data = self._post('/Action/call', payload, timeout)
        if data.get('code') == 1008:
            # As in call(): an expired or replaced session ran nothing, so one retry is safe.
            self._logged_in = False
            self._login()
            data = self._post('/Action/call', payload, timeout)
        if data.get('code') != 0:
            if data.get('code') in (1001, 1002, 1003, 1008):
                raise _Rejected(_call_error(func_name, data))
            details = '；'.join(f'{item.get("field")}: {item.get("msg")}' for item in data.get('details') or []
                               if isinstance(item, dict))
            raise _Rejected(f'iKuai {func_name} {action} 失败：{data.get("message") or data.get("code")}'
                            + (f'（{details}）' if details else ''))
        return data.get('results') if isinstance(data.get('results'), dict) else {}

    @contextmanager
    def batch(self, rollback: bool = False):
        """One release stage or rollback: read static_rt once instead of around every write.

        Deletes take their rows from one complete snapshot, kept in step with
        our own writes; edits still read the latest row so a concurrent change
        by hand is never overwritten. Consecutive new routes are confirmed by
        one readback before the next other write (a stage may delete what they
        replace) and at the end. A rollback attempts every undo even after a
        failed re-add, so it confirms only at the end. Nothing carries over.
        """
        if self._batch is not None:
            yield
            return
        self._batch = {'rows': None, 'stale': set(), 'added': [], 'added_since_read': False,
                       'rollback': rollback,
                       # IP groups: one snapshot, the names we changed since, what we wrote.
                       'groups': None, 'groups_stale': set(), 'groups_written': {}}
        try:
            yield
            self._confirm_added(self._batch['added'])
        finally:
            self._batch = None

    def _snapshot(self, fresh_tags: bool = False) -> Dict[str, Dict[str, Any]]:
        batch = self._batch
        if (batch['rows'] is None or (fresh_tags and batch['added_since_read'])):
            batch['rows'] = {str(row['id']): row for row in self._list_all('static_rt')}
            batch['stale'], batch['added_since_read'] = set(), False
        return batch['rows']

    def _static_write(self, action: str, param: Dict[str, Any], undo: Optional[Dict[str, Any]] = None):
        """Write static_rt and keep the batch snapshot true to what we changed."""
        batch = self._batch
        try:
            self._write('static_rt', action, param)
        except _Rejected:
            raise
        except RouterError as error:
            if batch is not None:
                batch['rows'] = None
            if undo is not None:
                # The reply was lost, not refused: the route may exist.
                error.undo = {**undo, 'confirmed': False}
            raise
        if batch is None or batch['rows'] is None:
            return
        if action == 'del':
            batch['rows'].pop(str(param['id']), None)
        elif action == 'edit':
            batch['stale'].add(str(param['id']))
        else:
            batch['added_since_read'] = True

    def _row(self, func_name: str, row_id: Any, fresh: bool = False) -> Dict[str, Any]:
        if func_name == 'static_rt' and self._batch is not None:
            if fresh or str(row_id) in self._batch['stale']:
                self._batch['rows'] = None
            row = self._snapshot().get(str(row_id))
        else:
            row = next((row for row in self._list_all(func_name) if str(row.get('id')) == str(row_id)), None)
        if row is None:
            raise RouterError(f'iKuai {func_name} 中找不到 id={row_id}，可能已被手动删除')
        return decode_value(row)

    def _find_id(self, func_name: str, match) -> str:
        for row in self._list_all(func_name):
            if match(decode_value(row)):
                return str(row.get('id'))
        raise RouterError(f'iKuai {func_name} 已提交但读不回新对象')

    @staticmethod
    def _matches(row: Dict[str, Any], match: Dict[str, Any]) -> bool:
        return all(row.get(key) == value for key, value in match.items())

    def _confirm_added(self, added: List[Dict[str, Any]]) -> None:
        """Prove each route added in this batch exists, with one complete readback."""
        if not added:
            return
        listed = self._list_all('static_rt')
        if self._batch is not None:
            # A complete read: it also serves as the snapshot for the writes that follow.
            self._batch['rows'] = {str(row['id']): row for row in listed}
            self._batch['stale'], self._batch['added_since_read'] = set(), False
        rows = {}
        for row in map(decode_value, listed):
            rows.setdefault(row.get('tagname'), []).append(row)
        missing = []
        for undo in added:
            tagged = rows.get(undo['match']['tagname'], [])
            if len(tagged) != 1 or not self._matches(tagged[0], undo['match']):
                missing.append(undo)
                # A complete read without the (unique) name proves nothing was created.
                undo['confirmed'] = bool(tagged)
        if missing:
            raise RouterError('iKuai static_rt 已提交但读不回新对象：'
                              + '、'.join(undo['cidr'] for undo in missing))

    def _new_id(self, func_name: str, undo: Dict[str, Any]) -> str:
        """The ID of an object just added; on failure the error carries its undo."""
        try:
            tagged = [row for row in map(decode_value, self._list_all(func_name))
                      if row.get('tagname') == undo['match']['tagname']]
        except RouterError as error:
            error.undo = undo
            raise
        if len(tagged) == 1 and self._matches(tagged[0], undo['match']):
            return str(tagged[0]['id'])
        error = RouterError(f'iKuai {func_name} 已提交但读不回新对象')
        error.undo = {**undo, 'confirmed': bool(tagged)}
        raise error

    def _add(self, func_name: str, payload: Dict[str, Any], match: Dict[str, Any],
             timeout: Optional[float] = None) -> Dict[str, Any]:
        """Add a named object; return the undo that finds it again by its unique name.

        iKuai rejects a duplicate name (code 3001, 4.0.314), so the name
        identifies the new object even when its ID is never read back.
        """
        undo = {'kind': 'add_undo', 'func': func_name, 'match': {'tagname': payload['tagname'], **match},
                'confirmed': True}
        if func_name == 'static_rt':
            self._static_write('add', payload, undo)
            return undo
        try:
            self._write(func_name, 'add', payload, timeout)
        except _Rejected:
            raise
        except RouterError as error:
            error.undo = {**undo, 'confirmed': False}
            raise
        return undo

    def _revert_add(self, op: Dict[str, Any]) -> Dict[str, Any]:
        func_name, match = op['func'], op['match']
        rows = (self._snapshot(fresh_tags=True).values() if func_name == 'static_rt' and self._batch is not None
                else self._list_all(func_name))
        tagged = [row for row in map(decode_value, rows) if row.get('tagname') == match['tagname']]
        if not tagged:
            if op['confirmed']:
                raise RouterError(f'iKuai {func_name} 中找不到本次新增的对象（名称 {match["tagname"]}），'
                                  '可能已被手动修改或删除，请检查')
            return {}
        if len(tagged) != 1 or not self._matches(tagged[0], match):
            raise RouterError(f'iKuai {func_name} 中名称为 {match["tagname"]} 的对象已被修改，未自动删除，请检查')
        if func_name == 'static_rt':
            self._static_write('del', {'id': tagged[0]['id']})
        else:
            self._write(func_name, 'del', {'id': tagged[0]['id']})
        return {}

    def _route_payload(self, row: Dict[str, Any], **changes) -> Dict[str, Any]:
        payload = {key: row.get(key) for key in ROUTE_FIELDS if key in row}
        payload.update(changes)
        payload['netmask'] = str(payload.get('netmask') or '').split(' ')[0]
        return payload

    @staticmethod
    def _nat_payload(row: Dict[str, Any], **changes) -> Dict[str, Any]:
        payload = {key: row.get(key) for key in NAT_FIELDS if key in row}
        for key in ('src_addr', 'dst_addr', 'src_port', 'dst_port'):
            payload[key] = _address_field(payload.get(key))
        payload.update(changes)
        return payload

    @staticmethod
    def _new_nat(src: List[str], dst: List[str], comment: str, refs: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """A NAT bypass rule; with ``refs`` its destination is those IP groups, ``dst`` only names it."""
        target = {'custom': dst, 'object': []} if refs is None else {'custom': [], 'object': refs}
        return IKuaiDriver._nat_payload({}, enabled='yes', tagname=_tagname('nat', ','.join(dst)), action='filter',
                                        protocol='any', iinterface='any', ointerface='any',
                                        src_addr={'custom': src, 'object': []}, dst_addr=target,
                                        nat_addr='', nat_port='', src_addr_inv=0, dst_addr_inv=0, comment=comment)

    @staticmethod
    def _policy_payload(row: Dict[str, Any], **changes) -> Dict[str, Any]:
        """A complete 端口分流 form: omitted fields would reset the rule's other settings."""
        payload = {key: row.get(key) for key in POLICY_FIELDS if key in row}
        for key in ('src_addr', 'dst_addr', 'src_port', 'dst_port', 'time'):
            payload[key] = _address_field(payload.get(key))
        payload.update(changes)
        return payload

    @staticmethod
    def _new_policy(gateway: str, dst: List[str], comment: str,
                    refs: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        return IKuaiDriver._policy_payload({}, **IKuaiDriver._policy_target(gateway, dst, comment, refs),
                                           tagname=_tagname('pr', comment), interface='', iface_band=0,
                                           time=POLICY_ALWAYS, mode=0, area_code='', dst_type=0)

    @staticmethod
    def _policy_target(gateway: str, dst: List[str], comment: str,
                       refs: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """The fields that make a rule route ``dst`` to the gateway for every source but itself.

        With ``refs`` the destination is those IP groups and nothing is listed literally.
        """
        target = {'custom': list(dst), 'object': []} if refs is None else {'custom': [], 'object': list(refs)}
        return {'enabled': 'yes', 'type': POLICY_NEXTHOP, 'nexthop': gateway, 'protocol': 'any', 'prio': POLICY_PRIORITY,
                'src_addr': {'custom': [gateway], 'object': []}, 'src_addr_inv': 1,
                'dst_addr': target, 'dst_addr_inv': 0, 'comment': comment}

    def _add_policy(self, payload: Dict[str, Any], timeout: Optional[float] = None) -> str:
        undo = self._add('stream_ipport', payload, {'comment': payload['comment']}, timeout)
        return self._new_id('stream_ipport', undo)

    def _edit(self, func_name: str, payload: Dict[str, Any], undo: Dict[str, Any],
              timeout: Optional[float] = None) -> None:
        """Edit a whole rule; a lost reply carries the idempotent undo (the full old row)."""
        try:
            self._write(func_name, 'edit', payload, timeout)
        except _Rejected:
            raise
        except RouterError as error:
            error.undo = undo
            raise

    def _target_refs(self, op: Dict[str, Any], field: Any) -> Optional[List[Dict[str, Any]]]:
        """The destination references an op writes: the user's groups it keeps, then ours."""
        if 'groups' not in op:
            return None
        refs = self._cf_refs(op['groups'])
        if op.get('keep_foreign'):
            refs = ikuai_groups.foreign_refs(self._group_rows(), _address_field(field)) + refs
        return refs

    def _apply_policy(self, op: Dict[str, Any]) -> Dict[str, Any]:
        kind = op['kind']
        if kind == 'policy_route_add':
            refs = self._target_refs(op, None)
            rule_id = self._add_policy(self._new_policy(op['gateway'], op['dst'], op['comment'], refs))
            return {'kind': 'policy_route_delete', 'id': rule_id}
        if kind == 'policy_route_readd':
            payload = self._policy_payload(self._rebind(op['record']))
            payload.pop('id', None)
            return {'kind': 'policy_route_delete', 'id': self._add_policy(payload, _restore_timeout(payload))}
        if kind == 'policy_route_delete':
            row = self._row('stream_ipport', op['id'])
            self._write('stream_ipport', 'del', {'id': row['id']})
            return {'kind': 'policy_route_readd', 'record': row}
        row = self._row('stream_ipport', op['id'] if kind == 'policy_route_update' else op['record']['id'])
        timeout = None
        if kind == 'policy_route_update':
            refs = self._target_refs(op, row.get('dst_addr'))
            payload = self._policy_payload(row, **self._policy_target(op['gateway'], op['dst'], op['comment'], refs))
        else:
            payload = self._policy_payload(self._rebind(op['record']))
            timeout = _restore_timeout(payload)
        undo = {'kind': 'policy_route_restore', 'record': row}
        self._edit('stream_ipport', payload, undo, timeout)
        return undo

    # --- IP groups (route_object) -------------------------------------------------

    def _group_rows(self, names=(), fresh: bool = False) -> List[Dict[str, Any]]:
        """Every IP group. In a batch one complete read serves until a group it names changes."""
        batch = self._batch
        if (batch is not None and not fresh and batch['groups'] is not None
                and not set(names) & batch['groups_stale']):
            return batch['groups']
        groups = [ikuai_groups.group_from_row(decode_value(row))
                  for row in self._list_all('route_object', ids=False)]
        ikuai_groups.mark_owners(groups)
        if batch is not None:
            batch['groups'], batch['groups_stale'] = groups, set()
        return groups

    def _group_changed(self, name: str) -> None:
        if self._batch is not None:
            self._batch['groups_stale'].add(name)

    def _named_groups(self, name: str, fresh: bool = False) -> List[Dict[str, Any]]:
        return [group for group in self._group_rows([name], fresh) if group['name'] == name]

    @staticmethod
    def _group_role(op: Dict[str, Any]) -> str:
        role = op.get('role')
        if role not in ikuai_groups.ROLES or not ikuai_groups.owned_role(op.get('name'), ikuai_groups.ROLES[role]):
            raise RouterError('iKuai IP 分组操作无效，请重新预览')
        return role

    def _owned_group(self, op: Dict[str, Any], fresh: bool = False) -> Dict[str, Any]:
        role = self._group_role(op)
        found = self._named_groups(op['name'], fresh)
        if len(found) != 1 or found[0]['role'] != role:
            raise RouterError(f'iKuai IP 分组「{op["name"]}」不存在或不属于 ConfigFlow，请重新预览')
        if found[0]['id'] is None:
            raise RouterError(f'iKuai IP 分组「{op["name"]}」缺少有效 ID，无法安全编辑')
        return found[0]

    def _group_users(self, group: Dict[str, Any]) -> List[str]:
        """Every NAT and 端口分流 rule that references the group, read now."""
        users = []
        for func, label in (('nat_rule', 'NAT 规则'), ('stream_ipport', '端口分流')):
            for row in map(decode_value, self._list_all(func)):
                for field in ('src_addr', 'dst_addr'):
                    for ref in _address_field(row.get(field))['object']:
                        if isinstance(ref, dict) and ikuai_groups.resolve([group], ref) is group:
                            users.append(f'{label}「{row.get("comment") or row.get("id")}」')
        return list(dict.fromkeys(users))

    def _group_write(self, action: str, name: str, param: Dict[str, Any], undo: Optional[Dict[str, Any]]) -> None:
        try:
            self._write('route_object', action, param)
        except _Rejected:
            raise
        except RouterError as error:
            if undo is not None:
                # The reply was lost, not refused: the change may have been made.
                error.undo = undo
            raise
        finally:
            self._group_changed(name)

    def _cf_refs(self, names: List[str]) -> List[Dict[str, Any]]:
        """References to our groups by their current gid, proving each one we wrote holds what we wrote."""
        groups = self._group_rows(names)
        written = self._batch['groups_written'] if self._batch is not None else {}
        refs, missing = [], []
        for name in names:
            found = [group for group in groups if group['name'] == name]
            parsed = ikuai_groups.name_role(name)
            if (len(found) != 1 or not parsed or found[0]['role'] != parsed[0] or not found[0]['gid']
                    or (name in written and ikuai_groups.digest(found[0]['values']) != written[name]['expected'])):
                missing.append(name)
                if name in written and not found:
                    # A complete read without the (unique) name: the add never happened.
                    written[name]['confirmed'] = False
                continue
            refs.append({'type': 0, 'gid': found[0]['gid'], 'gp_name': name})
        if missing:
            raise RouterError(f'iKuai IP 分组 {"、".join(missing)} 已提交但读不回预期内容，无法引用')
        return refs

    def _rebind(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """A saved rule with references to our groups pointed at their current gid.

        A rollback may delete a group and create it again under a new gid; the
        user's own references are written back exactly as they were.
        """
        record = copy.deepcopy(record)
        names = [ref.get('gp_name') for field in ('src_addr', 'dst_addr')
                 for ref in _address_field(record.get(field))['object']
                 if isinstance(ref, dict) and ikuai_groups.name_role(ref.get('gp_name'))]
        if not names:
            return record
        groups = self._group_rows(names)
        for field in ('src_addr', 'dst_addr'):
            value = record.get(field)
            for ref in (value.get('object') if isinstance(value, dict) and isinstance(value.get('object'), list) else []):
                if not isinstance(ref, dict) or not ikuai_groups.name_role(ref.get('gp_name')):
                    continue
                current = ikuai_groups.resolve(groups, ref)
                if current is not None and current['name'] == ref['gp_name']:
                    continue
                # Gone, or its gid now belongs to another group (iKuai may reuse a freed ID).
                found = [group for group in groups if group['name'] == ref['gp_name'] and group['role']]
                if len(found) == 1:
                    ref['gid'] = found[0]['gid']
                elif current is None:
                    raise RouterError(f'iKuai IP 分组 {ref["gp_name"]} 不存在，无法恢复规则引用')
        return record

    def _apply_group(self, op: Dict[str, Any]) -> Dict[str, Any]:
        kind = op['kind']
        name = op.get('name')
        if kind == 'ikuai_group_add':
            role = self._group_role(op)
            if self._named_groups(name):
                raise RouterError(f'iKuai IP 分组「{name}」已存在，请重新预览')
            undo = {'kind': 'ikuai_group_undo', 'name': name, 'role': role,
                    'expected': ikuai_groups.digest(op['values']), 'confirmed': True}
            self._group_write('add', name, ikuai_groups.payload(name, role, op['values']), {**undo, 'confirmed': False})
            if self._batch is not None:
                self._batch['groups_written'][name] = undo
            return undo
        if kind == 'ikuai_group_update':
            group = self._owned_group(op)
            if ikuai_groups.digest(group['values']) != op['expected']:
                raise RouterError(f'iKuai IP 分组「{name}」在预览后发生变化，请重新预览')
            undo = {'kind': 'ikuai_group_restore', 'name': name, 'role': group['role'], 'values': group['values'],
                    'previous': op['expected'], 'expected': ikuai_groups.digest(op['values'])}
            self._group_write('edit', name, ikuai_groups.payload(name, group['role'], op['values'], group['id']), undo)
            if self._batch is not None:
                self._batch['groups_written'][name] = {'expected': undo['expected']}
            return undo
        if kind == 'ikuai_group_delete':
            group = self._owned_group(op, fresh=True)
            if ikuai_groups.digest(group['values']) != op['expected']:
                raise RouterError(f'iKuai IP 分组「{name}」在预览后发生变化，请重新预览')
            users = self._group_users(group)
            if users:
                raise RouterError(f'iKuai IP 分组「{name}」仍被{"、".join(users)}引用，未删除')
            undo = {'kind': 'ikuai_group_readd', 'name': name, 'role': group['role'], 'values': group['values']}
            self._group_write('del', name, {'id': group['id']}, undo)
            return undo
        if kind == 'ikuai_group_undo':
            role = self._group_role(op)
            found = self._named_groups(name, fresh=True)
            if not found:
                if op['confirmed']:
                    raise RouterError(f'iKuai 中找不到本次新增的 IP 分组「{name}」，可能已被手动修改或删除，请检查')
                return {}
            group = found[0]
            if (len(found) != 1 or group['role'] != role
                    or ikuai_groups.digest(group['values']) != op['expected']):
                raise RouterError(f'iKuai IP 分组「{name}」已被修改，未自动删除，请检查')
            if group['id'] is None:
                raise RouterError(f'iKuai IP 分组「{name}」缺少有效 ID，未自动删除，请检查')
            users = self._group_users(group)
            if users:
                raise RouterError(f'iKuai IP 分组「{name}」仍被{"、".join(users)}引用，未自动删除')
            self._group_write('del', name, {'id': group['id']}, None)
            return {}
        if kind == 'ikuai_group_restore':
            group = self._owned_group(op, fresh=True)
            current = ikuai_groups.digest(group['values'])
            if current == op['previous']:
                return {}
            if current != op['expected']:
                raise RouterError(f'iKuai IP 分组「{name}」已被修改，保留现状，请检查')
            self._group_write('edit', name, ikuai_groups.payload(name, group['role'], op['values'], group['id']), None)
            return {}
        if kind == 'ikuai_group_readd':
            role = self._group_role(op)
            found = self._named_groups(name, fresh=True)
            if found:
                if (len(found) == 1 and found[0]['role'] == role
                        and ikuai_groups.digest(found[0]['values']) == ikuai_groups.digest(op['values'])):
                    return {}
                raise RouterError(f'iKuai IP 分组「{name}」已被其他配置占用，无法恢复，请检查')
            undo = {'kind': 'ikuai_group_undo', 'name': name, 'role': role,
                    'expected': ikuai_groups.digest(op['values']), 'confirmed': True}
            self._group_write('add', name, ikuai_groups.payload(name, role, op['values']), {**undo, 'confirmed': False})
            return undo
        raise RouterError(f'不支持的操作 {kind}')

    def _add_route(self, cidr: str, gateway: str, comment: str) -> Dict[str, Any]:
        network = ipaddress.IPv4Network(cidr)
        undo = self._add('static_rt', {
            'enabled': 'yes', 'tagname': _tagname('rt', cidr), 'ip_type': '4', 'interface': 'auto',
            'dst_addr': str(network.network_address), 'gateway': gateway, 'netmask': str(network.netmask),
            'prio': 1, 'comment': comment}, {'dst_addr': str(network.network_address),
                                             'netmask': str(network.netmask), 'gateway': gateway, 'comment': comment})
        undo['cidr'] = cidr
        if self._batch is not None:
            # Confirmed with every other new route when the batch ends.
            self._batch['added'].append(undo)
            return undo
        try:
            self._confirm_added([undo])
        except RouterError as error:
            error.undo = undo
            raise
        return undo

    def apply(self, op: Dict[str, Any]) -> Dict[str, Any]:
        kind = op['kind']
        batch = self._batch
        if kind != 'route_add' and batch is not None and batch['added'] and not batch['rollback']:
            # As when each add was read back at once: nothing else is written (e.g. the
            # 端口分流 rule or covered route a new route replaces) until the new routes exist.
            added, batch['added'] = batch['added'], []
            self._confirm_added(added)
        if kind in ('dhcp_dns_update', 'dhcp_dns_restore'):
            return self._dhcp_apply(op)
        if kind.startswith('ikuai_group_'):
            return self._apply_group(op)
        if kind in ('policy_route_add', 'policy_route_update', 'policy_route_restore',
                    'policy_route_delete', 'policy_route_readd'):
            return self._apply_policy(op)
        if kind == 'add_undo':
            return self._revert_add(op)
        if kind == 'route_add':
            return self._add_route(op['cidr'], op['gateway'], op['comment'])
        if kind == 'route_delete':
            row = self._row('static_rt', op['id'])
            self._static_write('del', {'id': row['id']})
            network = ipaddress.ip_network(f'{row["dst_addr"]}/{row["netmask"]}', strict=False)
            return {'kind': 'route_add', 'cidr': str(network), 'gateway': row.get('gateway'),
                    'comment': row.get('comment') or ''}
        if kind in ('route_comment', 'route_restore'):
            # Edits send the whole row: read it just before writing, never from the snapshot.
            row = self._row('static_rt', op['id'] if kind == 'route_comment' else op['record']['id'], fresh=True)
            payload = (self._route_payload(row, comment=op['comment']) if kind == 'route_comment'
                       else self._route_payload(op['record']))
            self._static_write('edit', payload)
            return {'kind': 'route_restore', 'record': row}
        if kind == 'nat_update':
            row = self._row('nat_rule', op['id'])
            dst = _address_field(row.get('dst_addr'))
            refs = self._target_refs(op, dst)
            if refs is None:
                dst['custom'] = dst['custom'] + [cidr for cidr in op.get('add_dst') or [] if cidr not in dst['custom']]
            else:
                # Destinations move into our groups; the user's groups and the sources stay.
                dst = {'custom': [], 'object': refs}
            undo = {'kind': 'nat_restore', 'record': row}
            self._edit('nat_rule', self._nat_payload(row, dst_addr=dst, comment=op['comment']), undo)
            return undo
        if kind == 'nat_restore':
            row = self._row('nat_rule', op['record']['id'])
            payload = self._nat_payload(self._rebind(op['record']))
            undo = {'kind': 'nat_restore', 'record': row}
            self._edit('nat_rule', payload, undo, _restore_timeout(payload))
            return undo
        if kind == 'nat_add':
            payload = self._new_nat(op['src'], op['dst'], op['comment'], self._target_refs(op, None))
            undo = self._add('nat_rule', payload, {'action': 'filter', 'comment': op['comment']})
            return {'kind': 'nat_delete', 'id': self._new_id('nat_rule', undo)}
        if kind == 'nat_readd':
            payload = self._nat_payload(self._rebind(op['record']))
            payload.pop('id', None)
            undo = self._add('nat_rule', payload, {'action': 'filter', 'comment': payload.get('comment')},
                             _restore_timeout(payload))
            return {'kind': 'nat_delete', 'id': self._new_id('nat_rule', undo)}
        if kind == 'nat_delete':
            row = self._row('nat_rule', op['id'])
            self._write('nat_rule', 'del', {'id': row['id']})
            # The whole rule, group references included.
            return {'kind': 'nat_readd', 'record': row}
        raise RouterError(f'不支持的操作 {kind}')


class _Rejected(RouterError):
    """iKuai answered and refused the write: nothing was changed."""


def request_bytes(func_name: str, action: str, param: Dict[str, Any]) -> int:
    """The JSON body _write sends (requests serialises ``json=`` the same way)."""
    return len(json.dumps({'func_name': func_name, 'action': action, 'param': encode_value(param)}).encode())


def _placeholder_refs(names: List[str]) -> List[Dict[str, Any]]:
    return [{'type': 0, 'gid': 'IPGP99999', 'gp_name': name} for name in names]


def oversized_requests(ops: List[Dict[str, Any]]) -> List[str]:
    """Blockers for writes whose single request iKuai would refuse as too large.

    Destinations go into IP groups split to fit one request, so this is a
    defence: it sizes the payload the driver would send for each op.
    """
    blockers = []
    for op in ops:
        kind = op['kind']
        if kind in ('ikuai_group_add', 'ikuai_group_update'):
            func_name, label, count = 'route_object', 'IP 分组', len(op['values'])
            payload = ikuai_groups.payload(op['name'], op['role'], op['values'])
            name = op['name']
        elif kind in ('policy_route_add', 'policy_route_update'):
            func_name, label, name = 'stream_ipport', '端口分流', op['comment']
            refs = _placeholder_refs(op['groups']) if 'groups' in op else None
            count = len(op['groups']) if refs is not None else len(op['dst'])
            payload = IKuaiDriver._new_policy(op['gateway'], op['dst'], op['comment'], refs)
        elif kind in ('nat_add', 'nat_update'):
            # An update also keeps the rule's own entries; counting only the
            # added ones may miss an oversize, but never blocks one that fits.
            func_name, label, name = 'nat_rule', 'NAT 免转换规则', op['comment']
            dst = op['dst'] if kind == 'nat_add' else op.get('add_dst') or []
            refs = _placeholder_refs(op['groups']) if 'groups' in op else None
            count = len(op['groups']) if refs is not None else len(dst)
            payload = IKuaiDriver._new_nat(op.get('src') or [], dst, op['comment'], refs)
        else:
            continue
        action = 'add' if kind.endswith('_add') else 'edit'
        if action == 'edit':
            payload['id'] = 0
        size = request_bytes(func_name, action, payload)
        if size > MAX_REQUEST_BYTES:
            unit = '个 IP 分组引用' if kind not in ('ikuai_group_add', 'ikuai_group_update') and 'groups' in op else '个网段'
            blockers.append(f'iKuai {label}「{name}」需要一次写入 {count} {unit}，请求约 {size // 1024} KB，'
                            f'超过 iKuai 单次请求上限（约 {MAX_REQUEST_BYTES // 1024} KB），发布会被拒绝。'
                            '请减少代理网段（例如少选规则集）后重试')
    return blockers


def _restore_timeout(payload: Dict[str, Any]) -> Optional[float]:
    """More time to write back a rule that lists many entries literally (a rollback of a migration)."""
    count = sum(len(_address_field(payload.get(field))['custom']) for field in ('src_addr', 'dst_addr'))
    return TIMEOUT + RESTORE_SECONDS_PER_ENTRY * count if count > RESTORE_SLOW_ENTRIES else None


def _tagname(kind: str, key: str) -> str:
    """iKuai requires a 1-15 character name; derive a stable one, e.g. ``cf_rt_1a2b3c4d``."""
    return f'cf_{kind}_{hashlib.sha1(key.encode()).hexdigest()[:15 - len(kind) - 4]}'


def _addresses(value: Any, address_sets: Dict[str, List[str]],
               by_gid: Optional[Dict[str, List[str]]] = None) -> List[str]:
    """Flatten iKuai ``{"custom": [...], "object": [...]}`` into concrete CIDRs/ranges.

    A reference resolves by its group ID; only one without an ID falls back to
    the name. A reference to a group that no longer exists adds nothing, so a
    coverage check fails closed instead of reading a same-named group.
    """
    if not isinstance(value, dict):
        return []
    result = [str(item) for item in value.get('custom') or [] if isinstance(item, str)]
    objects = value.get('object')
    for ref in objects if isinstance(objects, list) else []:
        if not isinstance(ref, dict):
            continue
        gid = str(ref.get('gid') or '')
        if gid and by_gid is not None:
            result.extend(by_gid.get(gid, []))
        else:
            result.extend(address_sets.get(str(ref.get('gp_name')), []))
    return result
