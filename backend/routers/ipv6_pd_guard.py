"""Opt-in RouterOS guard against a stale IPv6 LAN prefix after a PD change (cf-pd).

When the ISP hands out a new delegated prefix after a reboot, RouterOS forgets
the old one and clients keep using it as a preferred source address. The cf-pd
script records the current LAN /64, advertises the previous /64 with
preferred-lifetime=0 for an hour when it changes, and revives a prefix that is
current again. A dhcp-client hook runs it 10x1s after each PD event; a 30 s
scheduler is the fallback.

Three objects make up an installation: the ``cf-pd-watch`` script, the
``cf-pd-poll`` scheduler and the hook in the ``script`` field of the one
prefix-requesting dhcp-client. Parameters are derived from the router; nothing
is typed in.

Ownership: objects written by ConfigFlow are recorded in
``ipv6_pd_guard_state`` (IDs only) and withdrawn when the switch is turned off,
together with the runtime state the script leaves (``cf-pd-last`` and its
deprecated prefix advertisements). An existing manual installation that matches
the template is used as it is ("沿用") and never recorded or removed. Once a
preview-confirmed release rewrites it, ConfigFlow owns it.

An unclear router setup skips this stage only; the rest of the release goes on.
"""
import copy
import ipaddress
import re
from typing import Any, Dict, List, Optional

from backend.routers.base import RouterError, RouterRollbackError
from backend.routers.ipv6_readiness import _lan, enabled as _enabled, yes

SCRIPTS = '/system/script'
SCHEDULER = '/system/scheduler'
CLIENTS = '/ipv6/dhcp-client'
ADDRESSES = '/ipv6/address'
IPV4_ADDRESSES = '/ip/address'
LISTS = '/ipv6/firewall/address-list'
PREFIXES = '/ipv6/nd/prefix'
PATHS = frozenset({SCRIPTS, SCHEDULER, CLIENTS})

WATCH = 'cf-pd-watch'
POLL = 'cf-pd-poll'
ROLLBACK = 'cf-pd-rollback'
LAST = 'cf-pd-last'
HOLD = 3600
POLICY = 'read,write,test'
VERSION = 1
DEFAULTS = {'enabled': False}
SCRIPT_COMMENT = 'cf-pd: IPv6 prefix change guard (ConfigFlow)'
POLL_COMMENT = 'cf-pd'
POLL_EVENT = '/system script run cf-pd-watch'
HOOK = ':if ($"pd-valid" = 1) do={:for i from=1 to=10 do={:delay 1s; /system script run cf-pd-watch}}'
# Earlier hand-made hook: one run, before the LAN address had the new prefix.
LEGACY_HOOKS = ('/system script run cf-pd-watch',)
DEPRECATED = re.compile(r'^cf-pd: deprecated until \d+$')
# A RouterOS string literal is written as-is into the template.
NAME = re.compile(r'^[^"\\$\s;{}\[\]]{1,64}$')
SCRIPT_FIELDS = ('name', 'source', 'policy', 'comment', 'dont-require-permissions')
SCHEDULER_FIELDS = ('name', 'on-event', 'interval', 'start-time', 'start-date', 'policy', 'comment', 'disabled')
# Lease state changes on its own; only configuration identifies the client.
CLIENT_RUNTIME = {'status', 'prefix', 'dhcp-server-v6', 'duid', 'invalid', 'dynamic', 'expires-after', 'address'}
ROLE_NAMES = {'script': f'脚本 {WATCH}', 'scheduler': f'定时任务 {POLL}', 'hook': 'dhcp-client 钩子', 'state': '运行状态'}

_TEMPLATE = r''':local lan "{lan}"
:local pool "{pool}"
:local hold {hold}
:local tag "cf-pd: deprecated until "
:local now [:tonum [:timestamp]]
:local ids [/ipv6 address find where interface=$lan from-pool=$pool disabled=no invalid=no]
:if ([:len $ids] = 1) do={{
  :local a [:tostr [/ipv6 address get ($ids->0) address]]
  :local cur ([:tostr ([:toip6 [:pick $a 0 [:find $a "/"]]] & ffff:ffff:ffff:ffff::)] . "/64")
  :if ([:pick $cur 0 2] != "::") do={{
    # A prefix that is current again must not stay deprecated; the static
    # entry also blocks the dynamic one, so re-trigger advertising.
    :local revived false
    :foreach i in=[/ipv6 nd prefix find where comment~"^cf-pd: deprecated"] do={{
      :if ([:tostr [/ipv6 nd prefix get $i prefix]] = $cur) do={{ /ipv6 nd prefix remove $i; :set revived true }}
    }}
    :if ($revived) do={{
      /ipv6 address set ($ids->0) advertise=no
      /ipv6 address set ($ids->0) advertise=yes
      :log warning ("cf-pd: " . $cur . " is current again, re-advertised it")
    }}
    :local id [/ipv6 firewall address-list find where list="cf-pd-last"]
    :if ([:len $id] = 0) do={{
      /ipv6 firewall address-list add list="cf-pd-last" address=$cur comment="cf-pd: current LAN prefix"
      :log info ("cf-pd: recorded " . $cur)
    }} else={{
      :local old [:tostr [/ipv6 firewall address-list get ($id->0) address]]
      :if ($old != $cur) do={{
        :foreach i in=[/ipv6 nd prefix find where comment~"^cf-pd: deprecated"] do={{
          :if ([:tostr [/ipv6 nd prefix get $i prefix]] = $old) do={{ /ipv6 nd prefix remove $i }}
        }}
        /ipv6 nd prefix add interface=$lan prefix=$old preferred-lifetime=0s valid-lifetime=10m comment=($tag . ($now + $hold))
        /ipv6 firewall address-list set ($id->0) address=$cur
        :log warning ("cf-pd: LAN prefix changed " . $old . " -> " . $cur)
      }}
    }}
  }}
}}
:foreach i in=[/ipv6 nd prefix find where comment~"^cf-pd: deprecated until "] do={{
  :local c [/ipv6 nd prefix get $i comment]
  :if ([:tonum [:pick $c [:len $tag] [:len $c]]] <= $now) do={{
    :log info ("cf-pd: stopped advertising deprecated " . [:tostr [/ipv6 nd prefix get $i prefix]])
    /ipv6 nd prefix remove $i
  }}
}}
'''


class SettingsError(ValueError):
    pass


def normalize_settings(value: Any, router_type: str) -> Dict[str, Any]:
    if value is None:
        return copy.deepcopy(DEFAULTS)
    if not isinstance(value, dict) or set(value) - set(DEFAULTS) or type(value.get('enabled', False)) is not bool:
        raise SettingsError('IPv6 前缀变化保护的开关必须为布尔值')
    settings = {**DEFAULTS, **value}
    if settings['enabled'] and router_type != 'routeros':
        raise SettingsError('IPv6 前缀变化保护仅支持 RouterOS')
    return settings


def settings_of(router: Dict[str, Any]) -> Dict[str, Any]:
    return copy.deepcopy({**DEFAULTS, **(router.get('ipv6_pd_guard') or {})})


def relevant(router: Dict[str, Any]) -> bool:
    """Read the router only while enabled or while ConfigFlow still owns objects."""
    return router.get('type') == 'routeros' and (
        settings_of(router)['enabled'] is True or bool(router.get('ipv6_pd_guard_state')))


def watch_source(lan: str, pool: str, hold: int = HOLD) -> str:
    return _TEMPLATE.format(lan=lan, pool=pool, hold=hold)


def normalize(text: Any) -> str:
    """Script text without comments, blank lines and whitespace differences."""
    lines = []
    for line in str(text or '').replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        line = ' '.join(line.split())
        if line and not line.startswith('#'):
            lines.append(line)
    return '\n'.join(lines)


def parameters(source: Any) -> Dict[str, str]:
    """The ``:local lan/pool/hold`` values of a cf-pd script."""
    found = {}
    for line in normalize(source).split('\n'):
        match = re.fullmatch(r':local (lan|pool) "([^"\\]*)"', line) or re.fullmatch(r':local (hold) (\d+)', line)
        if match and match.group(1) not in found:
            found[match.group(1)] = match.group(2)
    return found


def _is_watch(source: Any) -> bool:
    text = normalize(source)
    return LAST in text and ':local lan' in text and ':local pool' in text


def _policies(value: Any) -> set:
    return {item.strip() for item in str(value or '').split(',') if item.strip()}


def payload(path: str, row: Dict[str, Any]) -> Dict[str, Any]:
    """Stable fields for the driver's ownership snapshot and restore."""
    if path == SCRIPTS:
        return {key: row[key] for key in SCRIPT_FIELDS if key in row}
    if path == SCHEDULER:
        return {key: row[key] for key in SCHEDULER_FIELDS if key in row}
    values = {key: value for key, value in row.items() if not key.startswith('.') and key not in CLIENT_RUNTIME}
    values.setdefault('script', '')  # RouterOS omits an empty script field
    return values


def _rows(driver, path: str) -> List[Dict[str, Any]]:
    rows = driver._list(path)
    if any(not isinstance(row.get('.id'), str) or not row['.id'] for row in rows):
        raise RouterError(f'RouterOS {path} 的对象缺少 ID，不能确认前缀保护的安装状态')
    return rows


def snapshot(driver, withdraw: bool = False) -> Dict[str, Any]:
    live = {'clients': _rows(driver, CLIENTS), 'addresses': _rows(driver, ADDRESSES),
            'scripts': _rows(driver, SCRIPTS), 'schedulers': _rows(driver, SCHEDULER)}
    live['ipv4_addresses'] = None  # read only when several LANs share the pool
    live['last'] = _rows(driver, LISTS + '?list=' + LAST)
    live['prefixes'] = _rows(driver, PREFIXES) if withdraw else None
    return live


def derive(live: Dict[str, Any], gateway: str, driver=None) -> Dict[str, Any]:
    """The pool, LAN and dhcp-client the script watches, or a RouterError saying why not."""
    clients = [row for row in live['clients'] if _enabled(row)
               and 'prefix' in {item.strip() for item in str(row.get('request', '')).split(',')}]
    if not clients:
        raise RouterError('没有启用并请求前缀（request 含 prefix）的 IPv6 DHCP 客户端，前缀保护不适用')
    if len(clients) > 1:
        names = '、'.join(str(row.get('interface') or row['.id']) for row in clients)
        raise RouterError(f'有多个启用的 IPv6 DHCP 客户端在请求前缀（{names}），无法确定监控哪一个')
    client = clients[0]
    pool = str(client.get('pool-name') or '')
    if not pool:
        raise RouterError(f'IPv6 DHCP 客户端（{client.get("interface")}）没有设置 pool-name，无法确定前缀来源')
    if not NAME.fullmatch(pool):
        raise RouterError(f'地址池名称「{pool}」含有脚本无法安全使用的字符')
    rows = [row for row in live['addresses'] if row.get('from-pool') == pool and _enabled(row)]
    interfaces = sorted({str(row.get('interface') or '') for row in rows})
    if not rows:
        raise RouterError(f'地址池 {pool} 没有分配给任何 LAN（/ipv6 address 中没有启用的 from-pool={pool}）')
    if '' in interfaces:
        raise RouterError(f'地址池 {pool} 的 LAN 地址缺少接口信息')
    warnings = []
    if len(interfaces) == 1:
        lan = interfaces[0]
    else:
        if live.get('ipv4_addresses') is None and driver is not None:
            live['ipv4_addresses'] = _rows(driver, IPV4_ADDRESSES)
        mihomo = _lan({'ipv4_addresses': live.get('ipv4_addresses') or []}, gateway) if gateway else None
        if mihomo not in interfaces:
            raise RouterError(f'地址池 {pool} 分给了多个 LAN（{"、".join(interfaces)}），'
                              '且 Mihomo 网关不在其中任何一个，无法确定监控哪一个')
        lan = mihomo
        others = '、'.join(item for item in interfaces if item != lan)
        warnings.append(f'地址池 {pool} 还分给了 {others}，这些 LAN 不受前缀变化保护；脚本只监控 Mihomo 所在的 {lan}。')
    if not NAME.fullmatch(lan):
        raise RouterError(f'接口名称「{lan}」含有脚本无法安全使用的字符')
    selected = [row for row in rows if row.get('interface') == lan]
    if len(selected) != 1:
        raise RouterError(f'{lan} 上有多条来自地址池 {pool} 的地址，脚本无法确定当前前缀')
    address = selected[0]
    try:
        length = ipaddress.IPv6Interface(str(address.get('address') or '')).network.prefixlen
    except ValueError:
        raise RouterError(f'{lan} 的 IPv6 地址无法识别') from None
    if length != 64:
        raise RouterError(f'{lan} 的 IPv6 地址前缀长度为 /{length}，前缀保护只支持 /64')
    if not yes(address.get('advertise', 'false')):
        raise RouterError(f'{lan} 的 IPv6 地址没有开启 advertise，客户端不通过 RA 获得前缀，前缀保护没有作用')
    return {'spec': {'lan': lan, 'pool': pool, 'client': client['.id'],
                     'client_interface': str(client.get('interface') or ''), 'hold': HOLD},
            'client': client, 'warnings': warnings}


def _named(rows, name):
    return next((row for row in rows if row.get('name') == name), None)


def _script_state(row, spec):
    if row is None:
        return 'missing', '发布时新建'
    source = row.get('source', '')
    if not _is_watch(source):
        return 'conflict', f'已有同名脚本 {WATCH}，但内容不是前缀保护脚本，不能覆盖；请改名或删除后重新检查'
    same = normalize(source) == normalize(watch_source(spec['lan'], spec['pool']))
    if same and _policies(row.get('policy')) == _policies(POLICY):
        return 'ok', f'监控 {spec["lan"]}（地址池 {spec["pool"]}），旧前缀弃用通告 {HOLD} 秒'
    found, diffs = parameters(source), []
    for key, label in (('lan', 'LAN'), ('pool', '地址池')):
        if found.get(key) != spec[key]:
            diffs.append(f'{label} 为 {found.get(key) or "未知"}，应为 {spec[key]}')
    if found.get('hold') != str(HOLD):
        diffs.append(f'弃用通告时长为 {found.get("hold") or "未知"} 秒，模板为 {HOLD} 秒')
    if not diffs and not same:
        diffs.append('脚本内容与当前模板版本不同')
    if _policies(row.get('policy')) != _policies(POLICY):
        diffs.append(f'权限为 {row.get("policy") or "空"}，模板为 {POLICY}')
    return 'update', '；'.join(diffs)


def _scheduler_state(row):
    if row is None:
        return 'missing', '发布时新建，每 30 秒兜底检查一次'
    if WATCH not in str(row.get('on-event', '')):
        return 'conflict', f'已有同名定时任务 {POLL}，但不是运行 {WATCH}，不能覆盖；请改名或删除后重新检查'
    diffs = []
    if normalize(row.get('on-event')) != POLL_EVENT:
        diffs.append('执行内容与模板不同')
    if str(row.get('interval')) not in ('30s', '00:00:30'):
        diffs.append(f'间隔为 {row.get("interval") or "空"}，模板为 30s')
    if str(row.get('start-time')) != 'startup':
        diffs.append('不是开机后立即开始')
    if _policies(row.get('policy')) != _policies(POLICY):
        diffs.append(f'权限为 {row.get("policy") or "空"}，模板为 {POLICY}')
    if yes(row.get('disabled')):
        diffs.append('已停用')
    return ('update', '；'.join(diffs)) if diffs else ('ok', '每 30 秒兜底检查一次')


def _hook_state(client):
    text = normalize(client.get('script', ''))
    if not text:
        return 'missing', '发布时写入钩子：前缀更新后每秒检查一次，共 10 次'
    if text == normalize(HOOK):
        return 'ok', '前缀更新后每秒检查一次，共 10 次'
    if text in {normalize(item) for item in LEGACY_HOOKS}:
        return 'update', '旧版钩子只运行一次，赶不上 LAN 地址更新；发布时改为每秒检查一次、共 10 次'
    if normalize(HOOK) in text.split('\n'):
        return 'shared', '钩子与其他内容共用同一个 script，ConfigFlow 不改动也不撤回这段内容'
    return 'conflict', (f'dhcp-client（{client.get("interface")}）的 script 已有其他内容，不能覆盖。'
                        f'可把下面这行加到原内容末尾后重新检查：{HOOK}')


def _owned(previous, live):
    """Manifest entries that still point at the object they were written as."""
    if not previous:
        return {}
    if (not isinstance(previous, dict) or previous.get('version') != VERSION
            or not isinstance(previous.get('resources'), list)):
        raise RouterError('IPv6 前缀变化保护的发布记录无效，保留现场，请核对')
    owned = {}
    for entry in previous['resources']:
        if not isinstance(entry, dict) or entry.get('role') not in ('script', 'scheduler', 'hook') \
                or not isinstance(entry.get('id'), str):
            raise RouterError('IPv6 前缀变化保护的发布记录缺少对象信息，保留现场，请核对')
        rows = {'script': live['scripts'], 'scheduler': live['schedulers'], 'hook': live['clients']}[entry['role']]
        row = next((row for row in rows if row['.id'] == entry['id']), None)
        expected = {'script': WATCH, 'scheduler': POLL}.get(entry['role'])
        if row is not None and (expected is None or row.get('name') == expected):
            owned[entry['role']] = row
    return owned


def _change(driver, method, path, row=None, values=None, role=''):
    change = {'role': role, 'method': method, 'path': path}
    if row is not None:
        change.update(id=row['.id'], expected=driver._ownership_snapshot(path, row))
    if values is not None:
        change['values'] = values
    return change


def _install_changes(driver, live, spec, states, script, scheduler, client, owned):
    changes = []
    if states['script'][0] == 'missing':
        changes.append(_change(driver, 'PUT', SCRIPTS, values={
            'name': WATCH, 'source': watch_source(spec['lan'], spec['pool']), 'policy': POLICY,
            'comment': SCRIPT_COMMENT}, role='script'))
    elif states['script'][0] == 'update':
        changes.append(_change(driver, 'PATCH', SCRIPTS, script, {
            'source': watch_source(spec['lan'], spec['pool']), 'policy': POLICY}, 'script'))
        found = parameters(script.get('source'))
        if (found.get('lan'), found.get('pool')) != (spec['lan'], spec['pool']):
            # The recorded prefix belongs to the old LAN or pool: let the script
            # record afresh instead of deprecating it on this LAN.
            changes.extend(_change(driver, 'DELETE', LISTS, row, role='state') for row in live['last'])
    if states['scheduler'][0] == 'missing':
        changes.append(_change(driver, 'PUT', SCHEDULER, values={
            'name': POLL, 'interval': '30s', 'start-time': 'startup', 'policy': POLICY,
            'on-event': POLL_EVENT, 'comment': POLL_COMMENT, 'disabled': 'false'}, role='scheduler'))
    elif states['scheduler'][0] == 'update':
        changes.append(_change(driver, 'PATCH', SCHEDULER, scheduler, {
            'on-event': POLL_EVENT, 'interval': '30s', 'start-time': 'startup', 'policy': POLICY,
            'disabled': 'false'}, 'scheduler'))
    old_client = owned.get('hook')
    if old_client is not None and old_client['.id'] != client['.id'] and _hook_state(old_client)[0] in ('ok', 'update'):
        # The prefix moved to another dhcp-client; take our hook off the old one.
        changes.append(_change(driver, 'PATCH', CLIENTS, old_client, {'script': ''}, 'hook'))
    if states['hook'][0] in ('missing', 'update'):
        changes.append(_change(driver, 'PATCH', CLIENTS, client, {'script': HOOK}, 'hook'))
    return changes


def _withdraw_changes(driver, live, owned):
    """Remove what ConfigFlow owns, then the runtime state its script left behind."""
    changes, blockers = [], []
    hook = owned.get('hook')
    if hook is not None and _hook_state(hook)[0] in ('ok', 'update'):
        changes.append(_change(driver, 'PATCH', CLIENTS, hook, {'script': ''}, 'hook'))
    scheduler = owned.get('scheduler')
    if scheduler is not None:
        if WATCH not in str(scheduler.get('on-event', '')):
            blockers.append(f'定时任务 {POLL} 已被改成其他内容，不能确认可以删除，请先核对')
        changes.append(_change(driver, 'DELETE', SCHEDULER, scheduler, role='scheduler'))
    script = owned.get('script')
    if script is not None:
        if not _is_watch(script.get('source')):
            blockers.append(f'脚本 {WATCH} 已被改成其他内容，不能确认可以删除，请先核对')
        changes.append(_change(driver, 'DELETE', SCRIPTS, script, role='script'))
    if script is not None:
        # The script produces this state; without it nothing would expire it.
        changes.extend(_change(driver, 'DELETE', LISTS, row, role='state') for row in live['last'])
        changes.extend(_change(driver, 'DELETE', PREFIXES, row, role='state') for row in live['prefixes']
                       if DEPRECATED.fullmatch(str(row.get('comment', ''))) and not yes(row.get('dynamic')))
    return changes, blockers


def _evidence(live, spec, owned):
    def ids(rows, names):
        return sorted(row['.id'] for row in rows if row.get('name') in names)
    return {'spec': spec, 'owned': {role: row['.id'] for role, row in sorted(owned.items())},
            'scripts': ids(live['scripts'], (WATCH, ROLLBACK)), 'schedulers': ids(live['schedulers'], (POLL,))}


def inspect(router: Dict[str, Any], gateway: str, driver) -> Dict[str, Any]:
    """Report items, ops and readback check from one read of the router."""
    previous = copy.deepcopy(router.get('ipv6_pd_guard_state'))
    driver.ipv6_pd_guard_manifest = copy.deepcopy(previous)
    switch = settings_of(router)['enabled']
    plan = {'enabled': switch, 'status': 'disabled', 'message': '', 'lan': '', 'pool': '', 'client': '',
            'hold': HOLD, 'managed': bool(previous), 'rollback_script': False, 'items': [], 'ops': [],
            'blockers': [], 'warnings': [], 'check': {}, 'evidence': {}}
    try:
        live = snapshot(driver, withdraw=not switch)
        owned = _owned(previous, live)
        plan['rollback_script'] = _named(live['scripts'], ROLLBACK) is not None
        if switch:
            _inspect_enabled(plan, live, owned, previous, gateway, driver)
        else:
            _inspect_withdraw(plan, live, owned, previous, driver)
    except RouterError as error:
        plan.update(status='blocked', message=str(error), blockers=[str(error)], ops=[], check={})
        for item in plan['items']:
            if item['status'] not in ('ok', 'borrowed', 'synced'):
                item['status'] = 'blocked'
    return plan


def _inspect_enabled(plan, live, owned, previous, gateway, driver):
    derived = derive(live, gateway, driver)
    spec, client = derived['spec'], derived['client']
    plan.update(lan=spec['lan'], pool=spec['pool'], client=spec['client_interface'])
    plan['warnings'].extend(derived['warnings'])
    script, scheduler = _named(live['scripts'], WATCH), _named(live['schedulers'], POLL)
    states = {'script': _script_state(script, spec), 'scheduler': _scheduler_state(scheduler),
              'hook': _hook_state(client)}
    current = {'script': script, 'scheduler': scheduler, 'hook': client}
    written = {role for role in ('script', 'scheduler', 'hook') if states[role][0] in ('missing', 'update')}
    still_owned = any(current[role] is not None and row['.id'] == current[role]['.id'] for role, row in owned.items())
    # Ownership covers the whole installation or nothing: half-withdrawn objects
    # would keep running a missing script. A matching hand install that nothing
    # needs to rewrite is used as it is and never claimed; once a confirmed
    # release writes any part, ConfigFlow manages the set. A hook sharing the
    # script field with other content is never ours to clear.
    after = sorted(role for role in ('script', 'scheduler', 'hook') if role != 'hook' or states['hook'][0] != 'shared') \
        if written or still_owned else []
    for role in ('script', 'scheduler', 'hook'):
        state, detail = states[role]
        status = {'missing': 'missing', 'update': 'update', 'conflict': 'conflict'}.get(state)
        if status is None and role in after:
            mine = role in owned and current[role] is not None and owned[role]['.id'] == current[role]['.id']
            status = 'synced' if mine else 'adopt'
            detail += '' if mine else '；内容一致，发布后一并由 ConfigFlow 管理'
        elif status is None:
            status = 'borrowed'
        plan['items'].append({'role': role, 'object': ROLE_NAMES[role], 'status': status, 'detail': detail})
    conflicts = [states[role][1] for role in ('script', 'scheduler', 'hook') if states[role][0] == 'conflict']
    if conflicts:
        raise RouterError('；'.join(conflicts))
    changes = _install_changes(driver, live, spec, states, script, scheduler, client, owned)
    before = sorted(entry['role'] for entry in (previous or {}).get('resources', []))
    record = after != before or (bool(after) and (previous or {}).get('spec') != spec)
    borrowed = [ROLE_NAMES[role] for role in ('script', 'scheduler', 'hook')
                if role not in after and states[role][0] in ('ok', 'shared')]
    updating = any(state == 'update' for state, _ in states.values())
    if changes:
        plan['status'] = 'pending'
        plan['message'] = (('路由器上已有前缀保护，但与模板不一致；确认发布后按模板更新。'
                            '更新后整套脚本、定时任务和钩子由 ConfigFlow 管理，关闭开关并发布时一并撤回。') if updating
                           else '发布时安装前缀变化保护：脚本、定时任务和 dhcp-client 钩子。')
    elif record:
        plan['status'] = 'pending'
        plan['message'] = '已安装；发布时更新 ConfigFlow 的管理记录，路由器上的配置不变。'
    elif after:
        plan['status'] = 'synced'
        plan['message'] = '已安装，由 ConfigFlow 管理；关闭开关并发布时撤回。'
    else:
        plan['status'] = 'borrowed'
        plan['message'] = ('已安装（沿用）：路由器上的手动安装与 ConfigFlow 模板一致，本次不改写。'
                           '它不算 ConfigFlow 受管对象，关闭开关时不会删除。')
    if after and borrowed:
        plan['message'] += f'{"、".join(borrowed)}与其他内容共用，撤回时保留。'
    if states['hook'][0] == 'shared':
        plan['warnings'].append('钩子与其他内容共用 dhcp-client 的 script；撤回时这段内容保留，需要时请手动删除那一行。')
    plan['managed'] = bool(after)
    plan['check'] = {'enabled': True, 'spec': spec, 'gateway': gateway, 'owned': after, 'withdraw': False}
    plan['evidence'] = _evidence(live, spec, owned)
    if plan['status'] == 'pending':
        plan['ops'] = [{'kind': 'ipv6_pd_guard_sync', 'enabled': True, 'gateway': gateway, 'spec': spec,
                        'previous': previous, 'changes': changes, 'owned': after,
                        'summary': (f'{"更新" if updating else "安装"} IPv6 前缀变化保护'
                                    f'（{spec["lan"]}，地址池 {spec["pool"]}）' if changes
                                    else '更新 IPv6 前缀变化保护的管理记录')}]


def _inspect_withdraw(plan, live, owned, previous, driver):
    if not previous:
        return
    changes, blockers = _withdraw_changes(driver, live, owned)
    spec = previous.get('spec') or {}
    plan.update(lan=spec.get('lan', ''), pool=spec.get('pool', ''), client=spec.get('client_interface', ''))
    for role in ('hook', 'scheduler', 'script'):
        if any(change['role'] == role for change in changes):
            detail = '清空 script 字段' if role == 'hook' else '删除'
            plan['items'].append({'role': role, 'object': ROLE_NAMES[role], 'status': 'remove', 'detail': detail})
    state = [change for change in changes if change['role'] == 'state']
    if state:
        lists = sum(change['path'] == LISTS for change in state)
        prefixes = len(state) - lists
        plan['items'].append({'role': 'state', 'object': ROLE_NAMES['state'], 'status': 'remove',
                              'detail': f'删除脚本留下的 {LAST} 记录 {lists} 条、弃用前缀通告 {prefixes} 条'})
    if blockers:
        raise RouterError('；'.join(blockers))
    plan['status'] = 'pending'
    plan['message'] = ('已关闭：发布时撤回 ConfigFlow 安装的脚本、定时任务和钩子，并清除脚本留下的状态。' if changes
                       else '已关闭：ConfigFlow 安装的对象已不存在，发布时清除管理记录。')
    plan['check'] = {'enabled': False, 'withdraw': True,
                     'removed': sorted([change['path'], change['id']] for change in changes if change['method'] == 'DELETE')}
    plan['evidence'] = _evidence(live, None, owned)
    plan['ops'] = [{'kind': 'ipv6_pd_guard_sync', 'enabled': False, 'gateway': '', 'spec': None,
                    'previous': previous, 'changes': changes, 'owned': [],
                    'summary': '撤回 IPv6 前缀变化保护' if changes else '清除 IPv6 前缀变化保护的管理记录'}]


def report(plan: Dict[str, Any]) -> Dict[str, Any]:
    return {key: copy.deepcopy(plan[key]) for key in (
        'enabled', 'status', 'message', 'lan', 'pool', 'client', 'hold', 'managed', 'rollback_script',
        'items', 'blockers', 'warnings')}


def _replan(driver, op):
    router = {'type': 'routeros', 'ipv6_pd_guard': {'enabled': bool(op.get('enabled'))},
              'ipv6_pd_guard_state': op.get('previous')}
    plan = inspect(router, op.get('gateway') or '', driver)
    return plan


def _rollback(driver, changes):
    failures, remap = [], {}
    for change in changes:
        try:
            check = driver._perform(change, [], restoring=True)
            driver._verify_change(check)
            if change['method'] == 'RESTORE':
                remap[str(change['id'])] = check['id']
        except RouterError as error:
            failures.append(str(error))
    if failures:
        raise RouterRollbackError('IPv6 前缀变化保护回滚未完成：' + '；'.join(failures))
    return remap


def _remapped(previous, remap):
    result = copy.deepcopy(previous)
    for entry in (result or {}).get('resources', []):
        entry['id'] = remap.get(entry['id'], entry['id'])
    return result


def apply(driver, op: Dict[str, Any]) -> Dict[str, Any]:
    """Re-read, prove the preview still holds, write in order; undo on any failure."""
    fresh = _replan(driver, {**op, 'gateway': op.get('gateway', '')})
    fresh_op = fresh['ops'][0] if fresh['ops'] else None
    if fresh['blockers'] or fresh_op is None or fresh_op['changes'] != op.get('changes') \
            or fresh_op.get('spec') != op.get('spec'):
        raise RouterError('IPv6 前缀变化保护的路由器配置在预览后发生变化，请重新预览')
    previous = op.get('previous')
    undo: List[Dict[str, Any]] = []
    created = {}
    try:
        for change in op['changes']:
            step = {key: copy.deepcopy(change[key]) for key in ('method', 'path', 'id', 'values', 'expected')
                    if key in change}
            check = driver._perform(step, undo)
            driver._verify_change(check)
            if change['method'] == 'PUT':
                created[change['role']] = check['id']
        if not op.get('enabled') and any(change['path'] == SCRIPTS for change in op['changes']):
            # A run started just before the script was deleted (the hook loop
            # lasts 10 s) can still write state; sweep once more.
            for path, rows in ((LISTS, _rows(driver, LISTS + '?list=' + LAST)), (PREFIXES, _rows(driver, PREFIXES))):
                for row in rows:
                    if path == LISTS or (DEPRECATED.fullmatch(str(row.get('comment', ''))) and not yes(row.get('dynamic'))):
                        check = driver._perform(_change(driver, 'DELETE', path, row), undo)
                        driver._verify_change(check)
        manifest = None
        roles = list(op.get('owned') or [])
        if op.get('enabled') and roles:
            live = snapshot(driver)
            ids = {'script': created.get('script') or (_named(live['scripts'], WATCH) or {}).get('.id'),
                   'scheduler': created.get('scheduler') or (_named(live['schedulers'], POLL) or {}).get('.id'),
                   'hook': op['spec']['client']}
            if not all(ids[role] for role in roles):
                raise RouterError('写入后没有找到前缀保护的脚本或定时任务')
            manifest = {'version': VERSION, 'spec': op['spec'],
                        'resources': [{'role': role, 'path': {'script': SCRIPTS, 'scheduler': SCHEDULER,
                                                              'hook': CLIENTS}[role], 'id': ids[role]}
                                      for role in ('script', 'scheduler', 'hook') if role in roles]}
        driver.ipv6_pd_guard_manifest = manifest
    except RouterError as error:
        try:
            remap = _rollback(driver, list(reversed(undo)))
        except RouterRollbackError as rollback_error:
            raise RouterRollbackError(f'{error}；{rollback_error}') from error
        driver.ipv6_pd_guard_manifest = _remapped(previous, remap)
        raise
    return {'kind': 'ipv6_pd_guard_restore', 'changes': list(reversed(undo)), 'previous': previous}


def restore(driver, op: Dict[str, Any]) -> Dict[str, Any]:
    remap = _rollback(driver, op['changes'])
    driver.ipv6_pd_guard_manifest = _remapped(op.get('previous'), remap)
    return {}


def verify(driver, check: Dict[str, Any]) -> None:
    if not check:
        return
    manifest = getattr(driver, 'ipv6_pd_guard_manifest', None)
    live = snapshot(driver, withdraw=check.get('withdraw', False))
    if check.get('withdraw'):
        paths = {'scripts': SCRIPTS, 'schedulers': SCHEDULER, 'last': LISTS, 'prefixes': PREFIXES}
        present = {(paths[key], row['.id']) for key in paths for row in live[key] or []}
        if manifest or present & {tuple(item) for item in check.get('removed') or []}:
            raise RouterError('回读校验失败：IPv6 前缀变化保护未按计划撤回')
        return
    if derive(live, check.get('gateway') or '', driver)['spec'] != check['spec']:
        raise RouterError('回读校验失败：IPv6 前缀变化保护监控的 LAN 或 dhcp-client 已变化')
    client = next(row for row in live['clients'] if row['.id'] == check['spec']['client'])
    script, scheduler = _named(live['scripts'], WATCH), _named(live['schedulers'], POLL)
    if (_script_state(script, check['spec'])[0] != 'ok' or _scheduler_state(scheduler)[0] != 'ok'
            or _hook_state(client)[0] not in ('ok', 'shared')):
        raise RouterError('回读校验失败：IPv6 前缀变化保护的脚本、定时任务或钩子与模板不一致')
    current = {'script': script, 'scheduler': scheduler, 'hook': client}
    owned = _owned(manifest, live)
    if sorted(owned) != sorted(check.get('owned') or []) or any(
            row['.id'] != current[role]['.id'] for role, row in owned.items()):
        raise RouterError('回读校验失败：IPv6 前缀变化保护的管理记录与路由器对象不一致')
