"""Self-contained, new-install-only Compose packages using scoped enrollment."""
import base64
import hashlib
import io
import json
import shlex
import zipfile

import yaml

from .install_script import generate_docker_agent_compose

LABEL = 'io.configflow.dns-ha.installation'

# Executed in the final Agent container, so Macvlan enrollment uses its actual IP.
# Kept as a standalone file so tests exercise exactly what the package ships.
BOOTSTRAP = r'''#!/usr/bin/env python3
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import urllib.error
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        # Never forward the scoped bearer capability to another URL.
        return None


def fail(message):
    raise SystemExit(message)


def read_regular(path, limit=65536):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                fail('安装文件不是普通文件，拒绝继续。')
            raw = stream.read(limit + 1)
            if len(raw) > limit:
                fail('安装文件过大，拒绝继续。')
            return raw
    except OSError:
        fail('安装文件不存在、不可读或为软链接；请重新核对安装包和持久目录。')


def read_json(path):
    try:
        value = json.loads(read_regular(path))
    except (ValueError, UnicodeError, RecursionError):
        fail('安装或身份文件损坏，拒绝覆盖。')
    if not isinstance(value, dict):
        fail('安装或身份文件格式无效，拒绝覆盖。')
    return value


def present(path):
    return path.exists() or path.is_symlink()


def identity_digest(value):
    if any(not isinstance(value.get(key), str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,256}', value[key])
           for key in ('agent_id', 'token')):
        fail('持久 Agent 身份不完整，拒绝启动或覆盖。')
    return hashlib.sha256(json.dumps({key: value[key] for key in ('agent_id', 'token')},
                                     sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def write_json(path, value, replace=False):
    # Exclusive publication never overwrites an unexpected identity or symlink.
    fd, temporary = tempfile.mkstemp(prefix='.compose-enroll-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path, follow_symlinks=False)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def bootstrap(settings_path, agent_dir, setup_path):
    settings = read_json(Path(settings_path))
    owner = settings.get('owner')
    if not isinstance(owner, str) or not re.fullmatch(r'[0-9a-f]{64}', owner):
        fail('安装归属标记无效，请重新生成安装包。')
    if read_regular(Path(setup_path)).decode().strip() != owner:
        fail('未完成本次安装的准备步骤，或持久目录属于另一份安装。')
    root = Path(agent_dir)
    if root.is_symlink() or not root.is_dir():
        fail('Agent 持久目录不存在或为软链接。')
    lock = os.open(root / '.compose-enrollment.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock, 'r+') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            fail('Agent 登记锁不是普通文件。')
        fcntl.flock(stream, fcntl.LOCK_EX)
        path = root / 'config-oxidns.json'
        marker = root / '.docker-ha-installation.json'
        receipt = None
        if present(marker):
            receipt = read_json(marker)
            if receipt.get('owner') != owner or receipt.get('url') != settings.get('url'):
                fail('持久身份属于另一份安装，拒绝接管。')
        if present(path):
            existing = read_json(path)
            if not receipt or receipt.get('identity_digest') != identity_digest(existing):
                fail('持久身份与本次安装标记不匹配，拒绝接管或覆盖。')
            print('已核对持久 Agent 身份；无需安装凭据或登记网络，继续启动。', flush=True)
            return
        if not receipt:
            # Reserve ownership before the HTTP request; retries retain its scope.
            write_json(marker, {'owner': owner, 'url': settings.get('url')})
        url, token = settings.get('url'), settings.get('token')
        if not isinstance(url, str) or not re.fullmatch(r'https?://[^\s]+/api/agents/docker-oxidns-ha-enroll/[0-9a-f]{32}', url):
            fail('首次登记地址无效，请重新生成安装包。')
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
            fail('首次登记凭据无效，请重新生成安装包。')
        request = urllib.request.Request(url, data=b'{}', method='POST',
                                        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
            with opener.open(request, timeout=30) as response:
                raw = response.read(16385)
            if len(raw) > 16384:
                fail('安装登记响应过大，未启动服务。')
            identity = json.loads(raw)
        except urllib.error.HTTPError as error:
            fail('首次登记被拒绝（HTTP ' + str(error.code) + '）；不跟随地址重定向。请确认最终服务地址；凭据过期时以相同设置重新生成安装包并准备后重试。')
        except (OSError, ValueError, UnicodeError, RecursionError):
            fail('首次登记未完成；请检查 ConfigFlow 地址、证书和网络，服务尚未启动。')
        if not isinstance(identity, dict) or identity.get('success') is not True:
            fail('安装登记响应无效，服务尚未启动。')
        digest = identity_digest(identity)
        if receipt and receipt.get('identity_digest') not in (None, digest):
            fail('服务端返回了不同的身份，拒绝覆盖。')
        write_json(marker, {'owner': owner, 'url': url, 'identity_digest': digest}, replace=True)
        write_json(path, {key: identity[key] for key in ('agent_id', 'token')})
        print('本机 Agent 身份已持久保存；继续启动并等待真实心跳。', flush=True)


if __name__ == '__main__':
    try:
        bootstrap('/run/configflow-compose/enrollment.json', '/opt/configflow-agent', '/run/configflow-compose/setup')
    except OSError:
        fail('无法安全保存安装身份；请保留现场并检查目录权限，服务尚未启动。')
    os.execv('/entrypoint.sh', ['/entrypoint.sh', '/usr/bin/supervisord', '-c', '/etc/supervisor/supervisord.conf'])
'''


def _prepare(params, request, owner, project, compose):
    from .docker_ha_guide import _daemon_check, _fail, _header, _identity_check, identity_command
    q = shlex.quote
    data, state = params['data_dir'], params['data_dir'] + '/.ha-compose'
    script = _header() + r'''
cf_bundle=$(cd -- "$(dirname -- "$0")" && pwd -P)
for cf_file in compose.yaml prepare.sh bootstrap.py enrollment.json README.txt; do
  [ ! -L "$cf_bundle/$cf_file" ] && [ -f "$cf_bundle/$cf_file" ] || { printf '%s\n' '安装包缺少普通文件或存在软链接，请重新解压。' >&2; exit 1; }
done
# Inspect context configuration only before contacting the local daemon.
if [ -n "${DOCKER_CONTEXT:-}" ]; then
  cf_endpoint=$(docker context inspect "$DOCKER_CONTEXT" --format '{{.Endpoints.docker.Host}}')
elif [ -n "${DOCKER_HOST:-}" ]; then cf_endpoint=$DOCKER_HOST
else cf_context=$(docker context show); cf_endpoint=$(docker context inspect "$cf_context" --format '{{.Endpoints.docker.Host}}'); fi
case "$cf_endpoint" in unix:///*) ;; *) printf '%s\n' '请在实际 Docker 宿主机使用本地 Unix socket，不能准备远端 daemon。' >&2; exit 1 ;; esac
'''
    script += _daemon_check()
    script += r'''
case "$(docker info --format '{{.OperatingSystem}} {{.KernelVersion}}')" in
  *Docker\ Desktop*|*docker-desktop*|*linuxkit*) printf '%s\n' '此部署不支持 Docker Desktop。' >&2; exit 1 ;;
esac
cf_compose=$(docker compose version --short)
if [[ "$cf_compose" =~ ^v?([0-9]+)\.([0-9]+)\.([0-9]+) ]]; then
  [ "${BASH_REMATCH[1]}" -gt 2 ] || { [ "${BASH_REMATCH[1]}" -eq 2 ] && [ "${BASH_REMATCH[2]}" -ge 20 ]; } || { printf '%s\n' '此安装包需要 Docker Compose 2.20+。' >&2; exit 1; }
else printf '%s\n' '无法识别 Docker Compose，请安装 2.20+。' >&2; exit 1; fi
cf_no_links() {
  local cf_path=$1
  while [ "$cf_path" != / ] && [ "$cf_path" != . ]; do
    [ ! -L "$cf_path" ] || { printf '%s\n' '持久路径或父目录是软链接，拒绝继续。' >&2; return 1; }
    cf_path=$(dirname -- "$cf_path")
  done
}
'''
    script += 'cf_data=' + q(data) + '\ncf_state=' + q(state) + '\ncf_owner=' + q(owner) + '\ncf_project=' + q(project) + '\ncf_identity_source=' + q(params['ha_host_id_path']) + '\n'
    script += 'cf_no_links "$cf_data"\ncf_no_links ' + q(params['ha_host_id_path']) + '\n'
    script += r'''
cf_known=false
if [ -e "$cf_state/setup" ] || [ -L "$cf_state/setup" ]; then
  cf_no_links "$cf_state/setup"
  [ -f "$cf_state/setup" ] && [ "$(cat "$cf_state/setup")" = "$cf_owner" ] || { printf '%s\n' '持久目录属于另一份安装，拒绝接管。' >&2; exit 1; }
  cf_known=true
else
  [ ! -e "$cf_state" ] && [ ! -L "$cf_state" ] || { printf '%s\n' '准备记录不完整，请保留现场检查，不能覆盖。' >&2; exit 1; }
  if [ -e "$cf_data" ]; then
    [ -d "$cf_data" ] || { printf '%s\n' '持久路径不是目录。' >&2; exit 1; }
    shopt -s nullglob dotglob
    cf_contents=("$cf_data"/*)
    for cf_entry in "${cf_contents[@]}"; do
      # A selected existing root-level identity file is safe to reuse, never replace.
      if [ "$cf_entry" = "$cf_identity_source" ] && [ ! -L "$cf_entry" ] && [ -f "$cf_entry" ]; then continue; fi
      [ "$cf_bundle" = "$cf_data" ] || { printf '%s\n' '首次 Compose 安装需要空数据目录；已有数据请使用迁移，不能覆盖。' >&2; exit 1; }
      case "${cf_entry##*/}" in compose.yaml|prepare.sh|bootstrap.py|enrollment.json|README.txt) ;;
        *) printf '%s\n' '数据目录中含有安装包之外的文件；已有数据请使用迁移，不能覆盖。' >&2; exit 1 ;; esac
      [ ! -L "$cf_entry" ] && [ -f "$cf_entry" ] || { printf '%s\n' '安装包文件异常，拒绝继续。' >&2; exit 1; }
    done
    shopt -u nullglob dotglob
  fi
fi
'''
    for key, service in compose['services'].items():
        name = q(service['container_name'])
        script += 'if docker container inspect ' + name + ' >/dev/null 2>&1; then\n'
        script += '  [ "$cf_known" = true ] || ' + _fail('同名容器已存在，拒绝接管：' + service['container_name']) + '\n'
        checks = [('{{index .Config.Labels "' + LABEL + '"}}', owner),
                  ('{{index .Config.Labels "com.docker.compose.project"}}', project),
                  ('{{index .Config.Labels "com.docker.compose.service"}}', key)]
        for mount in service['volumes']:
            fmt = '{{range .Mounts}}{{if eq .Destination ' + json.dumps(mount['target']) + '}}{{.Type}}|{{.Source}}|{{.RW}}{{end}}{{end}}'
            checks.append((fmt, 'bind|' + mount['source'] + '|' + ('false' if mount.get('read_only') else 'true')))
        for fmt, value in checks:
            script += '  [ "$(docker container inspect --format ' + q(fmt) + ' ' + name + ')" = ' + q(value) + ' ] || ' + _fail('现有容器的项目、归属或挂载与本次安装不符；未修改容器') + '\n'
        script += 'fi\n'
    for network in compose.get('networks', {}).values():
        name = q(network['name'])
        script += 'if docker network inspect ' + name + ' >/dev/null 2>&1; then\n'
        script += '  [ "$cf_known" = true ] || ' + _fail('同名网络已存在，拒绝接管') + '\n'
        ipam = network['ipam']['config'][0]
        checks = [('{{.Driver}}', 'macvlan'), ('{{index .Options "parent"}}', network['driver_opts']['parent']),
                  ('{{index .Labels "' + LABEL + '"}}', owner),
                  ('{{index .Labels "com.docker.compose.project"}}', project),
                  ('{{len .IPAM.Config}}', '1'), ('{{(index .IPAM.Config 0).Subnet}}', ipam['subnet'])]
        if ipam.get('gateway'):
            checks.append(('{{(index .IPAM.Config 0).Gateway}}', ipam['gateway']))
        for fmt, value in checks:
            script += '  [ "$(docker network inspect --format ' + q(fmt) + ' ' + name + ')" = ' + q(value) + ' ] || ' + _fail('现有网络参数或项目归属不符，拒绝复用') + '\n'
        script += 'fi\n'
    identity_path = params['ha_host_id_path']
    if request.get('create_host_identity'):
        script += 'if [ ! -e ' + q(identity_path) + ' ] && [ ! -L ' + q(identity_path) + ' ]; then\n'
        script += identity_command(identity_path).removeprefix(_header()) + 'fi\n'
    script += _identity_check(identity_path)
    script += r'''
if [ "$cf_known" != true ]; then
  mkdir -p -- "$cf_data"
  mkdir -- "$cf_state"
  (set -o noclobber; printf '%s\n' "$cf_owner" > "$cf_state/setup")
fi
for cf_dir in agent oxidns ha; do
  cf_no_links "$cf_data/$cf_dir"
  mkdir -p -- "$cf_data/$cf_dir"
done
'''
    script += r'''
# Keep startup files in persistent storage, not in a movable download directory.
for cf_file in bootstrap.py enrollment.json; do
  [ ! -L "$cf_state/$cf_file" ] && { [ ! -e "$cf_state/$cf_file" ] || [ -f "$cf_state/$cf_file" ]; } || { printf '%s\n' '持久安装文件异常，拒绝覆盖。' >&2; exit 1; }
  cf_tmp="$cf_state/.$cf_file.$$"
  (set -o noclobber; cat "$cf_bundle/$cf_file" > "$cf_tmp")
  chmod 600 "$cf_tmp"
  mv -f -- "$cf_tmp" "$cf_state/$cf_file"
done
chmod 700 "$cf_state"
chmod 600 "$cf_state/setup" "$cf_bundle/enrollment.json"
printf '%s\n' '准备完成；在本安装包目录执行 docker compose up -d。启动会完成首次安全登记，页面真实心跳与 HA 检测通过后再配对。'
'''
    return script


def generate_bundle(params, request, enrollment=None):
    from .docker_ha_guide import _volume
    image = request.get('image', 'thsrite/config-flow-agent:latest')
    plan = {**params, 'image': image, 'enrollment_url': (enrollment or {}).get('url', ''), 'install_method': 'compose'}
    owner = hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    project = 'cf-oxidns-' + owner[:16]
    data, state = params['data_dir'], params['data_dir'] + '/.ha-compose'
    # Identity must not alias mutable package files or become a directory mount.
    identity = params['ha_host_id_path']
    if identity == data or any(identity == data + '/' + part or identity.startswith(data + '/' + part + '/')
                               for part in ('agent', 'oxidns', 'ha', '.ha-compose', '.ha-install',
                                            'compose.yaml', 'prepare.sh', 'bootstrap.py', 'enrollment.json', 'README.txt')):
        raise ValueError('宿主身份文件不能放在本实例的受管数据子目录内，请选择独立持久文件')
    compose = yaml.safe_load(generate_docker_agent_compose(**params))
    compose.pop('version', None)
    compose['name'] = project
    for key, service in compose['services'].items():
        service['image'] = image
        service['labels'] = {LABEL: owner}
        readonly = key == 'ha-companion'
        service['volumes'] = [_volume({'type': 'bind', 'source': data + '/agent'}, '/opt/configflow-agent', readonly),
                              _volume({'type': 'bind', 'source': data + '/oxidns'}, '/etc/oxidns', readonly),
                              _volume({'type': 'bind', 'source': data + '/ha'}, '/var/lib/configflow-ha'),
                              _volume({'type': 'bind', 'source': identity}, '/run/configflow-host-machine-id', True)]
        if key == 'agent':
            if params['network_mode'] == 'host':
                service['volumes'].append(_volume({'type': 'bind', 'source': '/proc/1/ns/net'}, '/run/configflow-host-netns', True))
            # Directory bind sees atomic file renewals; a file bind pins the old inode.
            service['volumes'].append(_volume({'type': 'bind', 'source': state}, '/run/configflow-compose', True))
            service['entrypoint'] = ['python3', '/run/configflow-compose/bootstrap.py']
            service['command'] = []  # Bootstrap explicitly execs the original entrypoint with supervisord.
    for network in compose.get('networks', {}).values():
        network['name'] = project + '-dns-ha'
        network['labels'] = {LABEL: owner}
    settings = {'owner': owner, 'url': (enrollment or {}).get('url', ''), 'token': (enrollment or {}).get('token', '')}
    content = yaml.safe_dump(compose, sort_keys=False, allow_unicode=True).replace('$', '$$')
    files = {'compose.yaml': (content, 0o600),
             'prepare.sh': (_prepare(params, request, owner, project, compose), 0o700),
             'bootstrap.py': (BOOTSTRAP, 0o600),
             'enrollment.json': (json.dumps(settings, ensure_ascii=True, indent=2) + '\n', 0o600),
             'README.txt': ('仅用于本机新安装，不能接管已有容器或身份。\n'
                            '1. 将整个安装包解压到本机目录（也可直接解压到表单填写的数据目录），保留原文件名。\n'
                            '2. 在该目录执行：sudo bash prepare.sh\n'
                            '3. 执行：docker compose up -d（Docker 权限不足时加 sudo）。\n'
                            '需要 Linux rootful Docker 与 Compose 2.20+。\n'
                            '首次登记凭据有效期 24 小时，未登记时过期需以相同设置重新生成并再次准备；已保存身份后重启不需要安装凭据或登记 API。\n'
                            '相同安装包可重复准备，已有本项目的容器和网络必须通过归属及挂载核对。不要用 -p 或 COMPOSE_PROJECT_NAME 改项目名。\n'
                            + 'NAS 项目导入时请使用项目名称：' + project + '。\n'
                            '请勿手改设置后接管其他数据；不要删除持久数据。安装包含限定设备的登记凭据，请保护文件，不要公开分享。\n'
                            'Host 模式保留独立 HA companion；Macvlan 固定 IP 属于主容器本身，必须能直接访问 ConfigFlow。\n'
                            '启动不代表 HA 已启用；请回页面验证真实心跳、HA 环境，再配对并检查 LAN/VIP。\n', 0o600)}
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as output:
        for name, (value, permissions) in files.items():
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = (0o100000 | permissions) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            output.writestr(info, value.encode())
    return content, {'filename': project + '.zip', 'project_name': project, 'content_base64': base64.b64encode(archive.getvalue()).decode(),
                     'prepare_command': 'sudo bash prepare.sh', 'start_command': 'docker compose up -d'}
