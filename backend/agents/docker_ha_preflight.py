"""Bounded, read-only Docker HA environment evidence and text-only evaluation."""
import base64
import binascii
import ipaddress
import json
import re

MAX_OUTPUT_BYTES = 96 * 1024
MAX_FIELD_BYTES = 16 * 1024
BEGIN = 'CONFIGFLOW_DOCKER_HA_PREFLIGHT_V1_BEGIN'
END = 'CONFIGFLOW_DOCKER_HA_PREFLIGHT_V1_END'
FIELDS = ('kernel', 'docker_endpoint', 'docker_version', 'daemon_os', 'daemon_platform',
          'daemon_security', 'compose', 'ipv4', 'udp53', 'tcp53', 'nesting', 'machine_fingerprint')


def collector_script():
    # Do not use set -e: an unavailable daemon or command must not hide later evidence.
    return r"""#!/bin/bash
(
set +e
set +u
export LC_ALL=C
if ! command -v base64 >/dev/null 2>&1 || ! command -v tr >/dev/null 2>&1; then
  printf '%s\n' '基础检查无法生成结构化输出：缺少 base64 或 tr。请使用具备这些工具的 Linux/NAS 终端后重新采集。'
  exit 0
fi
cf_emit() {
  local cf_key=$1 cf_value cf_rc cf_encoded
  shift
  cf_value=$("$@" 2>/dev/null)
  cf_rc=$?
  if [ "${#cf_value}" -gt 16384 ]; then cf_value=OUTPUT_TRUNCATED; cf_rc=125; fi
  cf_encoded=$(set -o pipefail; printf '%s' "$cf_value" | base64 | tr -d '\r\n')
  if [ $? -ne 0 ]; then printf '%s\n' '采集编码失败，请重新运行检查命令。'; return 1; fi
  [ -n "$cf_encoded" ] || cf_encoded='-'
  printf 'CFHA1\t%s\t%s\t%s\n' "$cf_key" "$cf_rc" "$cf_encoded"
}
cf_docker() {
  if command -v timeout >/dev/null 2>&1; then timeout 8 docker "$@"; else docker "$@"; fi
}
cf_endpoint() {
  local cf_context cf_endpoint
  if [ -n "${DOCKER_CONTEXT:-}" ]; then
    cf_endpoint=$(cf_docker context inspect "$DOCKER_CONTEXT" --format '{{.Endpoints.docker.Host}}' 2>/dev/null) || return 1
  elif [ -n "${DOCKER_HOST:-}" ]; then
    cf_endpoint=$DOCKER_HOST
  else
    cf_context=$(cf_docker context show 2>/dev/null) || return 1
    cf_endpoint=$(cf_docker context inspect "$cf_context" --format '{{.Endpoints.docker.Host}}' 2>/dev/null) || return 1
  fi
  case "$cf_endpoint" in unix:///*) printf '%s' local-unix ;; '') return 1 ;; *) printf '%s' remote ;; esac
}
cf_probe_endpoint() {
  printf '%s' "$cf_endpoint_value"
  return "$cf_endpoint_rc"
}
cf_local_docker() {
  if [ "$cf_endpoint_rc" != 0 ] || [ "$cf_endpoint_value" != local-unix ]; then
    printf '%s' LOCAL_DOCKER_NOT_CHECKED
    return 126
  fi
  cf_docker "$@"
}
cf_nesting() {
  local cf_virt cf_rc
  if [ -f /.dockerenv ] || [ -f /run/.containerenv ]; then printf '%s' container; return 0; fi
  if command -v systemd-detect-virt >/dev/null 2>&1; then
    cf_virt=$(systemd-detect-virt --container 2>/dev/null); cf_rc=$?
    if [ "$cf_rc" = 0 ]; then printf '%s' container; return 0; fi
    if [ "$cf_rc" = 1 ]; then printf '%s' none; return 0; fi
    return 1
  fi
  if [ -r /proc/1/cgroup ] && command -v grep >/dev/null 2>&1; then
    if grep -Eq '(docker|lxc|kubepods|containerd)' /proc/1/cgroup; then printf '%s' container; else printf '%s' none; fi
  else return 1; fi
}
cf_ports() {
  local cf_protocol=$1 cf_ss cf_rc cf_family cf_file cf_rows='' cf_table
  if command -v ss >/dev/null 2>&1; then
    if [ "$cf_protocol" = tcp ]; then cf_ss=$(ss -H -ltn 'sport = :53' 2>/dev/null)
    else cf_ss=$(ss -H -lun 'sport = :53' 2>/dev/null); fi
    cf_rc=$?
    if [ "$cf_rc" = 0 ]; then printf 'source=ss\n%s' "$cf_ss"; return 0; fi
  fi
  command -v awk >/dev/null 2>&1 || return 127
  for cf_family in 4 6; do
    cf_file="/proc/net/$cf_protocol"
    [ "$cf_family" = 4 ] || cf_file="${cf_file}6"
    [ -r "$cf_file" ] || return 1
    cf_table=$(awk -v family="$cf_family" -v protocol="$cf_protocol" '
      function hex(value,width) { return length(value)==width && value !~ /[^0-9A-Fa-f]/ }
      function pair(value,left,right,parts) { return split(value,parts,":")==2 && hex(parts[1],left) && hex(parts[2],right) }
      NR==1 {
        if (NF<12 || $1!="sl" || $2!="local_address" || ($3!="rem_address" && $3!="remote_address") || $4!="st" ||
            $5!="tx_queue" || $6!="rx_queue" || $7!="tr" || $8!="tm->when" || $9!="retrnsmt" ||
            $10!="uid" || $11!="timeout" || $12!="inode" ||
            (NF!=12 && !(NF==15 && $13=="ref" && $14=="pointer" && $15=="drops"))) { bad=1; exit 2 }
        header=1; next
      }
      {
        width=(family==4 ? 8 : 32)
        state=toupper($4)
        if (NF<10 || NF>24 || $1 !~ /^[0-9]+:$/ || !pair($2,width,4) || !pair($3,width,4) || !hex($4,2) ||
            !pair($5,8,8) || !pair($6,2,8) || !hex($7,8) || $8 !~ /^[0-9]+$/ || $9 !~ /^[0-9]+$/ || $10 !~ /^[0-9]+$/ ||
            (protocol=="tcp" && state !~ /^(01|02|03|04|05|06|07|08|09|0A|0B|0C)$/) ||
            (protocol=="udp" && state !~ /^(01|07)$/)) { bad=1; exit 2 }
        split($2,local,":")
        if (toupper(local[2])=="0035" && (protocol=="udp" || state=="0A")) print protocol " " family " " toupper($2) " " state
      }
      END { if (!header || bad) exit 2 }
    ' "$cf_file") || return 2
    if [ -n "$cf_table" ]; then cf_rows="${cf_rows}${cf_table}
"; fi
  done
  printf 'source=proc-net-v1\ntables=%s,%s6\n%s' "$cf_protocol" "$cf_protocol" "$cf_rows"
}
cf_fingerprint() {
  local cf_file cf_identity cf_hash
  command -v sha256sum >/dev/null 2>&1 || return 127
  for cf_file in /etc/machine-id; do
    [ ! -L "$cf_file" ] && [ -f "$cf_file" ] && [ -r "$cf_file" ] || continue
    cf_identity=$(cat "$cf_file" 2>/dev/null) || continue
    [[ "$cf_identity" =~ ^[0-9a-fA-F]{32}$ ]] || continue
    cf_hash=$(printf '%s' "$cf_identity" | sha256sum) || return 1
    printf '%s' "${cf_hash%% *}"
    return 0
  done
  return 1
}
cf_endpoint_value=$(cf_endpoint)
cf_endpoint_rc=$?
printf '%s\n' 'CONFIGFLOW_DOCKER_HA_PREFLIGHT_V1_BEGIN'
cf_emit kernel uname -s
cf_emit docker_endpoint cf_probe_endpoint
cf_emit docker_version cf_local_docker version --format '{"client":{{json .Client.Version}},"server":{{json .Server.Version}}}'
cf_emit daemon_os cf_local_docker info --format '{{.OSType}}'
cf_emit daemon_platform cf_local_docker info --format '{"os":{{json .OperatingSystem}},"kernel":{{json .KernelVersion}}}'
cf_emit daemon_security cf_local_docker info --format '{{json .SecurityOptions}}'
cf_emit compose cf_docker compose version --short
cf_emit ipv4 ip -o -4 address show
cf_emit udp53 cf_ports udp
cf_emit tcp53 cf_ports tcp
cf_emit nesting cf_nesting
cf_emit machine_fingerprint cf_fingerprint
printf '%s\n' 'CONFIGFLOW_DOCKER_HA_PREFLIGHT_V1_END'
)
"""


def _check(ident, label, status, detail, remedy=None):
    result = {'id': ident, 'label': label, 'status': status, 'detail': detail}
    if remedy:
        result['remedy'] = remedy
    return result


def _unknown(reason):
    return {'status': 'unknown', 'summary': '输出不完整或无法识别，尚不能判断基础环境是否符合。',
            'checks': [_check('output', '采集输出', 'unknown', reason,
                              '重新运行当前页面的检查命令，粘贴包含 V1_BEGIN 到 V1_END 的完整输出。')]}


def _protocol_row(line):
    line = line.lstrip(' \t')
    match = re.fullmatch(r'CFHA1[ \t]+([a-z0-9_]+)[ \t]+(0|[1-9][0-9]{0,2})([ \t].*)?', line)
    if not match or int(match[2]) > 255:
        return None
    key, code = match[1], int(match[2])
    payload = (match[3] or '').strip(' \t')
    if not payload and code == 0 and line != f'CFHA1\t{key}\t0\t':
        # A collapsed three-column success cannot prove that its payload was empty.
        return None
    if payload != '-' and not re.fullmatch(r'[A-Za-z0-9+/]*={0,2}', payload):
        return None
    return key, code, payload


def _markdown_protocol_line(line):
    stripped = line.rstrip(' \t')
    if stripped.endswith('\\'):
        candidate = stripped[:-1]
        marker = candidate.strip(' \t')
        if marker in (BEGIN, END):
            return marker
        if _protocol_row(candidate) is not None:
            return candidate
    return line


def _decode(output):
    if not isinstance(output, str):
        raise ValueError('output 须为检查命令的文本输出')
    try:
        size = len(output.encode('utf-8'))
    except UnicodeError:
        raise ValueError('检查输出包含无效文本编码') from None
    if size > MAX_OUTPUT_BYTES:
        raise ValueError('检查输出超过 96 KiB，请只粘贴本次检查结果')
    lines = [_markdown_protocol_line(line) for line in output.replace('\r\n', '\n').splitlines()]
    starts = [i for i, line in enumerate(lines) if line.strip() == BEGIN]
    ends = [i for i, line in enumerate(lines) if line.strip() == END]
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        return None, '未找到唯一、完整的新版采集块；旧版输出、截断或拼接多次输出不能判定通过。'
    start, end = starts[0], ends[0]
    for i, line in enumerate(lines):
        if i not in (start, end) and line.startswith('CONFIGFLOW_DOCKER_HA_PREFLIGHT_'):
            return None, '输出混有其他版本或重复的采集标记。'
        if not start < i < end and re.match(r'^[ \t]*CFHA1(?:[ \t]|$)', line):
            return None, '完整块之外还有采集字段，请只保留本次完整输出。'
    values = {}
    for line in lines[start + 1:end]:
        row = _protocol_row(line)
        if row is None or row[0] not in FIELDS or row[0] in values:
            return None, '采集字段重复、缺损或格式不受支持。'
        key, code, payload = row
        try:
            decoded = b'' if payload == '-' else base64.b64decode(payload, validate=True)
            if len(decoded) > MAX_FIELD_BYTES:
                return None, '单项输出过大，无法确认采集结果完整。'
            text = decoded.decode('utf-8')
            if '\x00' in text:
                return None, '采集字段包含无效二进制内容。'
        except (ValueError, binascii.Error, UnicodeError):
            return None, '采集字段编码无效，请重新采集。'
        values[key] = (code, text)
    if set(values) != set(FIELDS):
        return None, '缺少必需检查项，不能把缺失结果视为通过。'
    return values, None


def _json_value(item, expected_type):
    code, text = item
    if code:
        return None
    try:
        value = json.loads(text)
    except (ValueError, TypeError, RecursionError):
        return None
    return value if isinstance(value, expected_type) else None


def _port_check(values, proto, mode):
    ident = proto.lower() + '53'
    code, text = values[ident]
    label = '宿主机 ' + proto + ' 53 端口'
    if mode == 'macvlan':
        return _check(ident, label, 'pass', 'Macvlan 不要求宿主机 53 端口空闲，此项不是安装前置条件；未读取或已有监听均不阻断。容器独立地址和 VIP 的可用性尚未验证。',
                      '安装后检查容器的 DNS 监听，并从实际局域网设备验证容器地址与 VIP。')
    if code:
        return _check(ident, label, 'unknown', '未能读取当前网络命名空间的监听状态，不能视为端口空闲。',
                      '重新运行新版只读命令；它会先使用 ss，再尝试 /proc/net。若均不可读，请在实际 Docker 宿主机检查，勿直接停止服务。')
    lines = text.splitlines()
    source = 'ss'
    if lines and lines[0].startswith('source='):
        source = lines.pop(0).removeprefix('source=')
    if source == 'proc-net-v1':
        expected = proto.lower()
        if not lines or lines.pop(0) != f'tables={expected},{expected}6':
            return _check(ident, label, 'unknown', '/proc/net 未确认 IPv4/IPv6 两张表均完整读取，不能视为端口空闲。')
        for line in lines:
            columns = line.split(' ')
            if (len(columns) != 4 or columns[0] != expected or columns[1] not in ('4', '6')
                    or not re.fullmatch(r'[0-9A-F]{' + ('8' if columns[1] == '4' else '32') + r'}:0035', columns[2])
                    or columns[3] not in (('0A',) if proto == 'TCP' else ('01', '07'))):
                return _check(ident, label, 'unknown', '/proc/net 端口记录格式无效，不能视为端口空闲。')
    elif source == 'ss':
        lines = [line for line in lines if line.strip()]
        for line in lines:
            columns = line.split()
            if columns and columns[0] in ('udp', 'tcp'):
                columns = columns[1:]
            if (len(columns) != 5 or columns[0] not in (('UNCONN',) if proto == 'UDP' else ('LISTEN',))
                    or not columns[1].isdigit() or not columns[2].isdigit() or not columns[3].endswith(':53')):
                return _check(ident, label, 'unknown', '无法识别 ss 的监听输出，尚未确认端口状态。', '重新运行页面提供的命令，不要手工改写检查项。')
    else:
        return _check(ident, label, 'unknown', '端口检查来源未知，不能视为端口空闲。')
    source_label = '/proc/net' if source == 'proc-net-v1' else 'ss'
    if lines and mode == 'host':
        return _check(ident, label, 'fail', '通过 ' + source_label + ' 检测到 ' + str(len(lines)) + ' 项监听；默认新装会同时绑定 TCP/UDP 53，包含回环地址的占用也需处理。',
                      '核对现有 DNS 服务；若为待迁移的原 OxiDNS，请按迁移步骤操作。不要直接停止正在服务的主机；自定义仅单协议配置需另行确认。')
    return _check(ident, label, 'pass', '本次通过 ' + source_label + ' 未发现当前网络命名空间的 53 端口监听；不代表其他命名空间或后续启动时仍空闲。')


def parse_report(output, network_mode='host'):
    if network_mode not in ('host', 'macvlan'):
        raise ValueError('network_mode 须为 host 或 macvlan')
    values, error = _decode(output)
    if error:
        return _unknown(error)
    checks = []
    code, kernel = values['kernel']
    checks.append(_check('linux', 'Linux 宿主环境', 'unknown' if code else 'pass' if kernel.strip() == 'Linux' else 'fail',
                         '当前内核为 Linux。' if not code and kernel.strip() == 'Linux' else '未确认 Linux 宿主；macOS/Windows 上的 Docker Desktop 不支持此 HA 部署。',
                         None if not code and kernel.strip() == 'Linux' else '请在实际 Linux/NAS Docker 宿主机的终端重新采集。'))
    code, endpoint = values['docker_endpoint']
    endpoint = endpoint.strip()
    checks.append(_check('docker_endpoint', 'Docker 检查目标', 'unknown' if code or endpoint not in ('local-unix', 'remote') else 'pass' if endpoint == 'local-unix' else 'fail',
                         'Docker 使用本机 Unix socket；以下网卡与端口结果来自当前终端主机。' if not code and endpoint == 'local-unix' else
                         '当前 Docker 目标为远端或无法确认；本机网卡、身份和端口结果不能代表 daemon 宿主。',
                         None if not code and endpoint == 'local-unix' else '切换到实际宿主机的本地 Docker context；检查 DOCKER_CONTEXT/DOCKER_HOST 后重新采集。'))
    version = _json_value(values['docker_version'], dict)
    valid_version = version and all(isinstance(version.get(k), str) and re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+[A-Za-z0-9_.+-]*', version[k]) for k in ('client', 'server'))
    checks.append(_check('docker_daemon', 'Docker CLI 与 daemon', 'pass' if valid_version else 'unknown' if values['docker_version'][0] == 126 else 'fail' if values['docker_version'][0] else 'unknown',
                         '已同时读取 Docker 客户端和服务端版本。' if valid_version else '检查目标不是已确认的本机 Unix socket，已跳过 daemon 读取；这不代表远端服务未启动。' if values['docker_version'][0] == 126 else 'Docker 命令或 daemon 未就绪，尚不能安装。',
                         None if valid_version else '先确认 Docker 服务已启动、当前用户有访问权限，并能运行 docker version 读取 Server 信息。'))
    code, daemon_os = values['daemon_os']
    checks.append(_check('docker_os', 'Docker 容器系统', 'unknown' if code else 'pass' if daemon_os.strip() == 'linux' else 'fail',
                         'Docker daemon 使用 Linux 容器。' if not code and daemon_os.strip() == 'linux' else '未确认 daemon 为 Linux 容器模式。',
                         None if not code and daemon_os.strip() == 'linux' else '使用实际 Linux/NAS 上的 rootful Docker Engine。'))
    platform = _json_value(values['daemon_platform'], dict)
    platform_valid = platform and all(isinstance(platform.get(k), str) and platform[k].strip() for k in ('os', 'kernel'))
    desktop = platform_valid and any(term in (platform['os'] + ' ' + platform['kernel']).lower() for term in ('docker desktop', 'docker-desktop', 'linuxkit'))
    checks.append(_check('docker_platform', '原生 Docker Engine', 'unknown' if not platform_valid else 'fail' if desktop else 'pass',
                         '检测到 Docker Desktop/其虚拟机内核，不支持此网络部署。' if desktop else '未发现 Docker Desktop 标记。' if platform_valid else '无法确认 Docker 运行平台。',
                         '请在 Linux/NAS 原生 Docker Engine 上部署。' if desktop or not platform_valid else None))
    security = _json_value(values['daemon_security'], list)
    valid_security = security is not None and len(security) <= 32 and all(isinstance(x, str) for x in security)
    rootless = valid_security and any('rootless' in x.lower() for x in security)
    checks.append(_check('docker_rootful', 'Rootful Docker', 'unknown' if not valid_security else 'fail' if rootless else 'pass',
                         '检测到 rootless Docker，无法提供所需网络能力。' if rootless else '服务端未启用 rootless 模式。' if valid_security else '无法读取 daemon 的安全模式。',
                         '使用宿主机的 rootful Docker 服务；不要用 privileged 代替环境检查。' if rootless or not valid_security else None))
    code, compose = values['compose']
    match = re.fullmatch(r'v?([0-9]+)\.([0-9]+)\.([0-9]+)[A-Za-z0-9_.+-]*', compose.strip()) if not code else None
    modern_compose = match and tuple(map(int, match.groups())) >= (2, 30, 0)
    checks.append(_check('compose', 'Compose 维护工具', 'pass' if modern_compose else 'warning',
                         'Compose 支持迁移模板的 raw 环境文件格式。' if modern_compose else '未检测到 Compose 2.30+；自动安装使用 Docker Run，可继续准备，Compose 维护模板暂不能直接使用。',
                         None if modern_compose else '若需使用 Compose 维护模板，再安装或升级到 2.30+；安装脚本不要求 Compose。'))
    code, addresses = values['ipv4']
    candidates = []
    malformed = False
    if not code:
        for line in addresses.splitlines():
            if not line.strip():
                continue
            match = re.match(r'^\d+:\s+(\S+)\s+inet\s+(\S+)\s+', line)
            try:
                iface = ipaddress.IPv4Interface(match[2]) if match else None
            except ValueError:
                iface = None
            if iface is None:
                malformed = True
            elif not any((iface.ip.is_loopback, iface.ip.is_link_local, iface.ip.is_unspecified, iface.ip.is_multicast, iface.ip.is_reserved)):
                raw_name = match[1]
                safe_name = re.sub(r'[^A-Za-z0-9_.:@-]', '', raw_name)[:48] or '未识别接口'
                if safe_name != raw_name:
                    safe_name += '（名称已截短或清理）'
                candidates.append((safe_name, iface.with_prefixlen))
    candidate_detail = ('读取到 ' + str(len(candidates)) + ' 个候选 IPv4：'
                        + '；'.join(name + ' · ' + address for name, address in candidates[:8])
                        + ('；仅显示前 8 条。' if len(candidates) > 8 else '。')
                        + '其中可能有 Docker bridge，尚未验证局域网可达性或固定地址；不会自动选择网卡。')
    checks.append(_check('ipv4', '网卡 IPv4 信息', 'unknown' if code or malformed else 'pass' if candidates else 'warning',
                         candidate_detail if not code and not malformed and candidates else
                         '无法可靠读取非回环 IPv4 候选地址。',
                         '请核对实际局域网网卡、静态地址及网关；Macvlan 还需确认 parent 接口和网段。'))
    checks += [_port_check(values, 'UDP', network_mode), _port_check(values, 'TCP', network_mode)]
    code, nested = values['nesting']
    checks.append(_check('nesting', '虚拟化与嵌套容器', 'pass' if not code and nested == 'none' else 'warning',
                         '未发现常见容器嵌套标记；仍需后续网络验证。' if not code and nested == 'none' else
                         '当前可能位于 LXC/Docker 等容器内，或无法确认；这不直接判定不支持。',
                         None if not code and nested == 'none' else '确认上层允许所需网络能力，安装后以 Agent 环境检测和真实 LAN/VIP 测试为准。'))
    code, fingerprint = values['machine_fingerprint']
    fingerprint_ok = not code and re.fullmatch(r'[0-9a-f]{64}', fingerprint)
    checks.append(_check('host_identity', '宿主身份信息', 'pass' if fingerprint_ok else 'warning',
                         '已获取默认 /etc/machine-id 的 SHA-256 指纹，不包含原始身份值。' if fingerprint_ok else '默认 /etc/machine-id 缺失、不可读、为软链接或未获取有效指纹；不能直接假定默认身份挂载可用，也尚不能对比两份输出。',
                         None if fingerprint_ok else '精简 Compose 会自动准备安装身份，无需此文件，独立主机以安装后的实时检查为准；使用脚本或旧 Compose 时仍需提供稳定的普通身份文件。'))
    statuses = {item['status'] for item in checks}
    status = next((s for s in ('fail', 'unknown', 'warning') if s in statuses), 'pass')
    summary = {'pass': '基础检查通过；仍需安装后的环境检测及真实局域网/VIP 验证。',
               'warning': '基础信息已读取，有注意事项需确认；尚不能据此保证安装和故障接管成功。',
               'fail': '发现需要处理的基础环境问题；处理后请重新采集。',
               'unknown': '部分必要检查未完成，尚不能判断基础环境是否符合。'}[status]
    result = {'status': status, 'summary': summary, 'checks': checks}
    if fingerprint_ok:
        result['host_fingerprint'] = fingerprint
    return result
