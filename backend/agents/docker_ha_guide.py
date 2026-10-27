"""Copyable Docker HA setup steps; no Docker host is contacted by this module."""
import ipaddress
import hashlib
import json
import os
import re
import shlex
from urllib.parse import urlsplit

import yaml

from .install_script import generate_docker_agent_compose, validate_agent_name

IMAGE = 'thsrite/config-flow-agent:latest'
Q = shlex.quote


def _path(value, label):
    if (not isinstance(value, str) or not value.startswith('/') or value == '/'
            or os.path.normpath(value) != value or any(c in value for c in ',:\x00\r\n')):
        raise ValueError(label + '须为规范的绝对路径，不能包含逗号、冒号或换行')
    return value


def _name(value, label):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', value):
        raise ValueError(label + '只能包含字母、数字、点、下划线或短横线')
    return value


def _mount(value, label):
    if not isinstance(value, dict) or value.get('type') not in ('bind', 'volume'):
        raise ValueError(label + '请选择 bind 目录或 Docker 命名卷')
    kind, source = value['type'], value.get('source')
    return {'type': kind, 'source': _path(source, label) if kind == 'bind' else _name(source, label)}


def _volume(mount, target, readonly=False):
    result = {**mount, 'target': target, 'read_only': readonly}
    if mount['type'] == 'bind':
        result['bind'] = {'create_host_path': False}
    return result


def _mount_arg(mount):
    return 'type=' + mount['type'] + ',src=' + mount['source'] + ',dst=' + mount['target'] + (',readonly' if mount.get('read_only') else '')


def _header():
    return '#!/bin/bash\nset -euo pipefail\numask 077\n'


def _fail(message):
    return '{ printf "%s\\n" ' + Q(message) + ' >&2; exit 1; }'


def _identity_check(path):
    q = Q(path)
    return ('[ ! -L ' + q + ' ] && [ -f ' + q + ' ] && [ -r ' + q + ' ] || '
            + _fail('宿主身份文件不可读、不是普通文件或为软链接；请先完成身份文件步骤') + '\n'
            + 'cf_identity=$(cat -- ' + q + ')\n'
            + '[ "${#cf_identity}" -ge 16 ] && [ "${#cf_identity}" -le 128 ] && [[ "$cf_identity" != *$\'\\n\'* ]] || '
            + _fail('宿主身份文件须包含一行 16–128 字符的稳定标识') + '\n')


def identity_command(path):
    """Bash noclobber creates with O_EXCL, including for dangling symlinks."""
    path = _path(path, '宿主身份文件')
    return (_header() + 'cf_identity_path=' + Q(path) + '\n'
            + '[ ! -e "$cf_identity_path" ] && [ ! -L "$cf_identity_path" ] || '
            + _fail('目标已存在（包括软链接）；未改写，请沿用有效身份文件或另选路径') + '\n'
            + 'mkdir -p -- ' + Q(os.path.dirname(path)) + '\n'
            + 'cf_identity=$(tr -cd "0123456789abcdefABCDEF" < /proc/sys/kernel/random/uuid)\n'
            + '[[ "$cf_identity" =~ ^[0-9a-fA-F]{32}$ ]] || ' + _fail('无法读取内核随机 UUID') + '\n'
            + '(set -o noclobber; printf "%s\\n" "$cf_identity" > "$cf_identity_path") || '
            + _fail('身份文件被并发创建或无法写入；未覆盖任何现有文件') + '\n'
            + 'printf "%s\\n" "宿主身份文件已创建；同一宿主所有实例沿用此文件，不要复制到第二台宿主。"\n')


def inspection_command(container_name=''):
    from .docker_ha_preflight import collector_script
    command = collector_script()
    if container_name:
        container_name = _name(container_name, '原容器名')
        command += ('docker inspect --format \'Name={{.Name}} Image={{.Config.Image}} Network={{.HostConfig.NetworkMode}}\' ' + Q(container_name) + '\n'
                    + 'docker inspect --format \'{{range .Mounts}}{{printf "%s | %s | %s | %s | rw=%t\\n" .Type .Name .Source .Destination .RW}}{{end}}\' ' + Q(container_name) + '\n'
                    + 'docker exec ' + Q(container_name) + ' python3 -c ' + Q(
                        'import json; c=json.load(open("/opt/configflow-agent/config-oxidns.json")); '
                        'print(json.dumps({k:c.get(k) for k in ("agent_name","agent_port","service_type","config_path")},ensure_ascii=False))') + '\n')
    return command


def _daemon_check():
    return ('[ "$(uname -s)" = Linux ] || ' + _fail('仅支持 Linux Docker 宿主机') + '\n'
            + '[ "$(docker info --format \'{{.OSType}}\')" = linux ] || ' + _fail('Docker daemon 不是 Linux') + '\n'
            + 'case "$(docker info --format \'{{json .SecurityOptions}}\')" in *rootless*) '
            + _fail('DNS 高可用需要 rootful Docker，当前是 rootless') + ' ;; esac\n')


def _run(service, cid_file=None, env_file=None, cid_variable=None):
    args = ['docker', 'run', '-d', '--name', service['container_name'], '--restart', 'unless-stopped', '--stop-timeout', '30']
    if cid_file:
        args += ['--cidfile', cid_file]
    if cid_variable:
        args += ['--cidfile', ('raw', '"$' + cid_variable + '"')]
    if env_file:
        args += ['--env-file', env_file]
    if service.get('_owner'):
        args += ['--label', 'io.configflow.dns-ha.installation=' + service['_owner']]
    if service.get('network_mode'):
        args += ['--network', service['network_mode']]
    else:
        args += ['--network', service['_network_name'], '--ip', service['networks']['dns-ha']['ipv4_address']]
    for cap in service.get('cap_add', []):
        args += ['--cap-add', cap]
    for mount in service['volumes']:
        args += ['--mount', _mount_arg(mount)]
    for env in service['environment']:
        args += ['-e', env]
    args += [service['image']]
    return ' \\\n  '.join(arg[1] if isinstance(arg, tuple) else Q(arg) for arg in args) + '\n'



def _new_install(params, request, enrollment):
    """Replay only this setup's immutable plan and captured container IDs."""
    compose = yaml.safe_load(generate_docker_agent_compose(**params))
    image = request.get('image', IMAGE)
    plan = {**params, 'image': image, 'enrollment_url': (enrollment or {}).get('url', '')}
    owner = hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    data = params['data_dir']
    state = data + '/.ha-install'
    for key, service in compose['services'].items():
        service['image'] = image
        service['labels'] = {'io.configflow.dns-ha.installation': owner}
        service['_owner'] = owner
        readonly = key == 'ha-companion'
        service['volumes'] = [_volume({'type': 'bind', 'source': data + '/agent'}, '/opt/configflow-agent', readonly),
                              _volume({'type': 'bind', 'source': data + '/oxidns'}, '/etc/oxidns', readonly),
                              _volume({'type': 'bind', 'source': data + '/ha'}, '/var/lib/configflow-ha')]
        service['volumes'].append({'type': 'bind', 'source': params['ha_host_id_path'],
                                   'target': '/run/configflow-host-machine-id', 'read_only': True,
                                   'bind': {'create_host_path': False}})
        if key == 'agent' and params['network_mode'] == 'host':
            service['volumes'].append({'type': 'bind', 'source': '/proc/1/ns/net', 'target': '/run/configflow-host-netns',
                                       'read_only': True, 'bind': {'create_host_path': False}})
    script = _header() + _daemon_check() + _identity_check(params['ha_host_id_path'])
    script += 'cf_data=' + Q(data) + '\ncf_state=' + Q(state) + '\ncf_owner=' + Q(owner) + '\n'
    script += '[ ! -L "$cf_data" ] && [ ! -L "$cf_state" ] && [ ! -L "$cf_state/setup" ] || ' + _fail('安装目录或标记为软链接；拒绝继续') + '\n'
    script += 'if [ -e "$cf_state/setup" ]; then\n  [ -f "$cf_state/setup" ] && [ "$(cat "$cf_state/setup")" = "$cf_owner" ] || ' + _fail('数据目录属于另一份安装设置，请保留原命令续装或使用迁移') + '\nelse\n'
    script += '  [ ! -e "$cf_state" ] && [ ! -e "$cf_data/agent/config-oxidns.json" ] && [ ! -L "$cf_data/agent/config-oxidns.json" ] || ' + _fail('目录已有身份或未识别的安装状态；请使用原命令或迁移，不能接管') + '\n'
    for name in (params['agent_name'] + '-oxidns', params['agent_name'] + '-oxidns-ha'):
        script += '  ! docker container inspect ' + Q(name) + ' >/dev/null 2>&1 || ' + _fail('同名容器已存在且不属于本次安装：' + name) + '\n'
    script += '  mkdir -p -- "$cf_data"\n  mkdir -- "$cf_state"\n  (set -o noclobber; printf "%s\\n" "$cf_owner" > "$cf_state/setup")\nfi\n'
    script += r'''
# Only a recorded exact ID plus the immutable setup label permits reuse.
cf_owned_container() {
  local cf_role=$1 cf_name=$2 cf_id cf_record cf_saved cf_found=false
  cf_id=$(docker container inspect --format '{{.Id}}' "$cf_name" 2>/dev/null || true)
  for cf_record in "$cf_state/$cf_role-id."*; do
    [ -e "$cf_record" ] || [ -L "$cf_record" ] || continue
    [ ! -L "$cf_record" ] && [ -f "$cf_record" ] || { printf '%s\n' '安装 CID 记录异常，拒绝继续' >&2; return 1; }
    [ -s "$cf_record" ] || continue
    cf_saved=$(cat "$cf_record")
    [[ "$cf_saved" =~ ^[0-9a-f]{64}$ ]] || { printf '%s\n' '安装 CID 记录无效，拒绝继续' >&2; return 1; }
    if [ "$cf_saved" = "$cf_id" ]; then
      cf_found=true
    elif docker container inspect "$cf_saved" >/dev/null 2>&1; then
      printf '%s\n' '本次安装的容器已被改名，请先核对，拒绝创建重复服务' >&2; return 1
    fi
  done
  if [ -n "$cf_id" ]; then
    [ "$cf_found" = true ] && [ "$(docker inspect --format '{{index .Config.Labels "io.configflow.dns-ha.installation"}}' "$cf_id")" = "$cf_owner" ] || {
      printf '%s\n' '同名容器不是本次安装记录的容器，拒绝接管' >&2; return 1;
    }
    printf '%s' "$cf_id"
  fi
}
'''
    main = compose['services']['agent']
    script += 'cf_main_id=$(cf_owned_container main ' + Q(main['container_name']) + ')\n'
    if 'ha-companion' in compose['services']:
        script += 'cf_companion_id=$(cf_owned_container companion ' + Q(compose['services']['ha-companion']['container_name']) + ')\n'
    if compose.get('networks'):
        network = compose['networks']['dns-ha']
        network['labels'] = {'io.configflow.dns-ha.installation': owner}
        main['_network_name'] = network['name']
        ipam = network['ipam']['config'][0]
        script += 'if docker network inspect ' + Q(network['name']) + ' >/dev/null 2>&1; then\n'
        checks = [('{{.Driver}}', 'macvlan'), ('{{index .Options "parent"}}', network['driver_opts']['parent']),
                  ('{{index .Labels "io.configflow.dns-ha.installation"}}', owner),
                  ('{{len .IPAM.Config}}', '1'), ('{{(index .IPAM.Config 0).Subnet}}', ipam['subnet'])]
        if ipam.get('gateway'):
            checks.append(('{{(index .IPAM.Config 0).Gateway}}', ipam['gateway']))
        for fmt, value in checks:
            script += '  [ "$(docker network inspect --format ' + Q(fmt) + ' ' + Q(network['name']) + ')" = ' + Q(value) + ' ] || ' + _fail('同名 Macvlan 网络不属于本次安装或参数已改变；拒绝复用') + '\n'
        script += 'else\n'
        args = ['docker', 'network', 'create', '--driver', 'macvlan', '--subnet', ipam['subnet'], '--opt', 'parent=' + network['driver_opts']['parent'],
                '--label', 'io.configflow.dns-ha.installation=' + owner]
        if ipam.get('gateway'):
            args += ['--gateway', ipam['gateway']]
        script += '  ' + ' '.join(map(Q, args + [network['name']])) + '\nfi\n'
    script += 'docker image inspect ' + Q(image) + ' >/dev/null 2>&1 || docker pull ' + Q(image) + '\n'
    for dirname in ('agent', 'oxidns', 'ha'):
        script += '[ ! -L "$cf_data/' + dirname + '" ] || ' + _fail('数据子目录是软链接，拒绝继续') + '\n'
    script += 'mkdir -p "$cf_data/agent" "$cf_data/oxidns" "$cf_data/ha"\n'
    if enrollment:
        from .docker_ha_enrollment import bootstrap_command
        # A created Macvlan container retains its fixed IP even while stopped.
        # Re-enrolling on that IP would collide; verify the durable seed locally.
        proof = '''import json, sys
from pathlib import Path
root=Path('/opt/configflow-agent')
path=root/'config-oxidns.json'; marker=root/'.docker-ha-installation.json'
if path.is_symlink() or marker.is_symlink() or not path.is_file() or not marker.is_file():
    sys.exit('本次安装的身份文件或标记缺失；请保留数据并检查，拒绝覆盖')
try:
    config=json.loads(path.read_text()); setup=json.loads(marker.read_text())
except Exception:
    sys.exit('本次安装的身份文件不可读，拒绝覆盖')
if setup.get('url') != sys.argv[1] or any(not isinstance(config.get(k),str) or not config[k] or config[k]=='[REDACTED]' for k in ('agent_id','token')):
    sys.exit('本次安装的身份与标记不匹配，拒绝继续')
print('已核对本次安装的本地身份；保留原凭据并继续启动。')
'''
        script += 'if [ -z "$cf_main_id" ]; then\n' + bootstrap_command(enrollment, request) + '\nelse\n'
        args = ['docker', 'run', '--rm', '--network', 'none', '--entrypoint', 'python3', '--mount',
                _mount_arg(_volume({'type': 'bind', 'source': data + '/agent'}, '/opt/configflow-agent', True)),
                image, '-c', proof, enrollment['url']]
        script += '  ' + ' '.join(map(Q, args)) + '\nfi\n'
    else:
        script += '[ ! -e "$cf_data/agent/config-oxidns.json" ] || ' + _fail('缺少本次注册凭据；请在页面重新生成安装命令') + '\n'
    for role, service in [('companion', compose['services'].get('ha-companion')), ('main', main)]:
        if service is None:
            continue
        script += 'if [ -n "$cf_' + role + '_id" ]; then\n'
        script += '  if [ "$(docker inspect --format \'{{.State.Running}}\' "$cf_' + role + '_id")" != true ]; then docker start "$cf_' + role + '_id"; fi\nelse\n'
        script += '  cf_attempt=$(tr -cd "0123456789abcdefABCDEF" < /proc/sys/kernel/random/uuid)\n'
        script += '  [[ "$cf_attempt" =~ ^[0-9a-fA-F]{32}$ ]] || exit 1\n'
        script += '  cf_cid_path="$cf_state/' + role + '-id.$cf_attempt"\n'
        script += _run(service, cid_variable='cf_cid_path') + 'fi\n'
    script += 'printf "%s\\n" "本次安装的容器已启动或恢复；请回到页面检查真实心跳和高可用环境，再配对。相同命令可安全续装，不会接管同名其他容器。"\n'
    for service in compose['services'].values():
        service.pop('_owner', None)
        service.pop('_network_name', None)
    return yaml.safe_dump(compose, sort_keys=False, allow_unicode=True).replace('$', '$$'), script


def _migration(params, request):
    old = _name(request.get('container_name'), '原容器名')
    old_agent = request.get('existing_agent_name')
    if not isinstance(old_agent, str):
        raise ValueError('请填写原 Agent 完整名称')
    validate_agent_name(old_agent)
    agent = _mount(request.get('agent_mount'), '原 Agent 挂载')
    core = _mount(request.get('core_mount'), '原 OxiDNS 挂载')
    ha_dir = _path(request.get('ha_data_dir'), '新增 HA 目录')
    backup = _path(request.get('backup_dir'), '备份目录')
    directories = [ha_dir, backup] + [item['source'] for item in (agent, core) if item['type'] == 'bind']
    for i, left in enumerate(directories):
        for right in directories[i + 1:]:
            if os.path.commonpath((left, right)) in (left, right):
                raise ValueError('Agent、OxiDNS、HA 和备份目录须彼此独立，不能相同或互相包含')
    if agent == core:
        raise ValueError('Agent 和 OxiDNS 必须使用不同的数据挂载')
    params = {**params, 'data_dir': '/unused-migration-path'}
    compose = yaml.safe_load(generate_docker_agent_compose(**params))
    main = compose['services']['agent']
    main['container_name'] = old
    main['environment'] = [f'AGENT_OXIDNS_NAME={old_agent}' if v.startswith('AGENT_OXIDNS_NAME=') else v for v in main['environment'] if not v.startswith('TZ=')]
    main['env_file'] = [{'path': backup + '/runtime.env', 'format': 'raw'}]
    private = {'type': 'bind', 'source': ha_dir}
    for key, service in compose['services'].items():
        readonly = key == 'ha-companion'
        service['image'] = request.get('image', IMAGE)
        service['volumes'] = [_volume(agent, '/opt/configflow-agent', readonly), _volume(core, '/etc/oxidns', readonly),
                              _volume(private, '/var/lib/configflow-ha')] + [v for v in service['volumes'] if isinstance(v, dict)]
        if readonly:
            service['container_name'] = old + '-ha'
    if compose.get('networks'):
        main['_network_name'] = compose['networks']['dns-ha']['name']
    external = {m['source']: {'external': True, 'name': m['source']} for m in (agent, core) if m['type'] == 'volume'}
    if external:
        compose['volumes'] = external
    previous = old + '-pre-ha'
    failed = old + '-failed-ha'
    companion = old + '-ha'
    script = _header() + _daemon_check() + _identity_check(params['ha_host_id_path'])
    script += 'cf_old=' + Q(old) + '\ncf_backup=' + Q(backup) + '\n'
    script += 'cf_old_id=$(docker inspect --format \'{{.Id}}\' "$cf_old")\n'
    script += '[ "$(docker inspect --format \'{{.State.Running}}\' "$cf_old_id")" = true ] || ' + _fail('原容器须正在运行，以核对配置；未执行迁移') + '\n'
    for name in (previous, failed, companion):
        script += '! docker container inspect ' + Q(name) + ' >/dev/null 2>&1 || ' + _fail('保留名称已被占用：' + name + '；未执行迁移') + '\n'
    script += '[ "$(docker inspect --format \'{{len .Mounts}}\' "$cf_old_id")" = 2 ] || ' + _fail('仅支持只有 Agent 与 OxiDNS 两个挂载的单核心容器；请先核对额外挂载或已有 HA 配置') + '\n'
    for mount, target in ((agent, '/opt/configflow-agent'), (core, '/etc/oxidns')):
        fmt = '{{range .Mounts}}{{if eq .Destination ' + json.dumps(target) + '}}{{.Type}}|{{if eq .Type "volume"}}{{.Name}}{{else}}{{.Source}}{{end}}|{{.RW}}{{end}}{{end}}'
        script += '[ "$(docker inspect --format ' + Q(fmt) + ' "$cf_old_id")" = ' + Q(mount['type'] + '|' + mount['source'] + '|true') + ' ] || ' + _fail('实际挂载与所填信息不一致：' + target + '；未执行迁移') + '\n'
        if mount['type'] == 'bind':
            script += '[ -d ' + Q(mount['source']) + ' ] && [ ! -L ' + Q(mount['source']) + ' ] || ' + _fail('原数据目录不存在或是软链接；未执行迁移') + '\n'
    script += 'cf_old_image=$(docker inspect --format \'{{.Image}}\' "$cf_old_id")\n'
    for field in ('Entrypoint', 'Cmd'):
        fmt = '{{json .Config.' + field + '}}'
        script += '[ "$(docker inspect --format ' + Q(fmt) + ' "$cf_old_id")" = "$(docker image inspect --format ' + Q(fmt) + ' "$cf_old_image")" ] || ' + _fail('原容器有自定义启动命令，请保留原 Compose 手动调整，不执行自动迁移') + '\n'
    check = r"""import json, os, pathlib, re, sys
environment=json.load(sys.stdin)
if not isinstance(environment,list): sys.exit('无法读取原容器环境配置')
original={}
for item in environment:
    if not isinstance(item,str) or '=' not in item: sys.exit('原环境变量格式不支持自动迁移')
    key,value=item.split('=',1)
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',key) or any(c in value for c in ('\n','\r','\x00')):
        sys.exit('原环境变量含多行值或无效名称，请保留原 Compose 手动迁移')
    original[key]=value
c=json.load(open('/opt/configflow-agent/config-oxidns.json'))
expected_name, expected_server, expected_port=sys.argv[1:]
if (c.get('agent_name') != expected_name or c.get('server_url') != expected_server or str(c.get('agent_port')) != expected_port
    or c.get('service_type') != 'oxidns' or c.get('config_path') != '/etc/oxidns/config.yaml'):
    sys.exit('原 Agent 名称、服务器地址、端口或配置路径不匹配；未执行迁移')
if (os.environ.get('CONFIGFLOW_DNS_HA') == '1' or any(pathlib.Path('/opt/configflow-agent', name).exists() for name in ('oxidns-ha.json','oxidns-ha-handoff.json'))):
    sys.exit('原容器已有 HA 配置；请先在现有高可用设置中迁移虚拟 IP、停用并确认清理后再迁移，勿删除记录')
if any(os.environ.get(key,'false').lower() == 'true' for key in ('ENABLE_MIHOMO','ENABLE_MOSDNS')):
    sys.exit('仅支持单独运行 OxiDNS 的容器')
if os.environ.get('SERVICE_TYPE','') not in ('', 'oxidns'):
    sys.exit('原容器不是单 OxiDNS 服务，请沿用原 Compose 手动调整')
if c.get('health_url'):
    sys.exit('原 Agent 使用自定义 HTTP 健康检查，请沿用原 Compose 手动迁移')
settings={'HEARTBEAT_INTERVAL':c.get('heartbeat_interval',30), 'DEPLOYMENT_HEALTH_TIMEOUT':c.get('deployment_health_timeout',30),
          'AGENT_HOST':c.get('agent_host','0.0.0.0'), 'OXIDNS_HEALTH_DNS_ADDRESS':c.get('health_dns_address',''),
          'OXIDNS_HEALTH_DNS_NAME':c.get('health_dns_name',''), 'TZ':os.environ.get('TZ','Asia/Shanghai')}
original.update(settings)
for key,value in original.items():
    value=str(value)
    if '\n' in value or '\r' in value or '\x00' in value: sys.exit('原运行设置含多行值，请沿用原 Compose 手动迁移')
    print(key+'='+value)
"""
    check_cmd = 'docker inspect --format \'{{json .Config.Env}}\' "$cf_old_id" | docker exec -i "$cf_old_id" python3 -c ' + Q(check) + ' ' + ' '.join(map(Q, (old_agent, params['server_url'], str(params['oxidns_port']))))
    script += 'cf_runtime_env=$(' + check_cmd + ')\n'
    script += 'docker image inspect ' + Q(request.get('image', IMAGE)) + ' >/dev/null 2>&1 || docker pull ' + Q(request.get('image', IMAGE)) + '\n'
    script += '[ ! -e ' + Q(ha_dir) + ' ] && [ ! -L ' + Q(ha_dir) + ' ] || ' + _fail('请选择全新 HA 目录，不能复用已有 HA 状态') + '\n'
    script += 'mkdir -p -- ' + Q(os.path.dirname(backup)) + '\nmkdir -- "$cf_backup"\n'
    script += 'printf "%s\\n" "$cf_old_id" > "$cf_backup/old-id"\nprintf "%s\\n" "$cf_runtime_env" > "$cf_backup/runtime.env"\n'
    script += 'docker inspect --format \'{{.HostConfig.RestartPolicy.Name}}:{{.HostConfig.RestartPolicy.MaximumRetryCount}}\' "$cf_old_id" > "$cf_backup/restart-policy"\n'
    if compose.get('networks'):
        network = compose['networks']['dns-ha']
        ipam = network['ipam']['config'][0]
        args = ['docker', 'network', 'create', '--driver', 'macvlan', '--subnet', ipam['subnet'], '--opt', 'parent=' + network['driver_opts']['parent']]
        if ipam.get('gateway'):
            args += ['--gateway', ipam['gateway']]
        script += ' '.join(map(Q, args + [network['name']])) + '\n'
    script += 'mkdir -p -- ' + Q(os.path.dirname(ha_dir)) + '\nmkdir -- ' + Q(ha_dir) + '\n'
    script += 'docker update --restart=no "$cf_old_id" >/dev/null\ndocker stop "$cf_old_id" >/dev/null\n'
    script += 'mkdir "$cf_backup/agent" "$cf_backup/oxidns"\n'
    script += 'docker cp "$cf_old_id:/opt/configflow-agent/." "$cf_backup/agent/"\ndocker cp "$cf_old_id:/etc/oxidns/." "$cf_backup/oxidns/"\n'
    script += 'docker rename "$cf_old_id" ' + Q(previous) + '\n'
    if 'ha-companion' in compose['services']:
        script += _run(compose['services']['ha-companion'], backup + '/companion-id')
    script += _run(main, backup + '/new-id', backup + '/runtime.env')
    script += 'printf "%s\\n" "新容器已启动；先检查 Agent 身份、DNS 与高可用环境，再处理第二台并配对。旧容器和数据备份均已保留。"\n'
    rollback = _header() + _daemon_check() + 'cf_backup=' + Q(backup) + '\n'
    rollback += 'cf_old_id=$(cat "$cf_backup/old-id")\n[[ "$cf_old_id" =~ ^[0-9a-f]{64}$ ]] || exit 1\n'
    rollback += 'docker container inspect "$cf_old_id" >/dev/null\n'
    rollback += 'cf_name_id=$(docker container inspect --format \'{{.Id}}\' ' + Q(old) + ' 2>/dev/null || true)\n'
    rollback += 'cf_new_id=$(cat "$cf_backup/new-id" 2>/dev/null || true)\n'
    rollback += '[ -z "$cf_name_id" ] || [ "$cf_name_id" = "$cf_old_id" ] || [ "$cf_name_id" = "$cf_new_id" ] || ' + _fail('原名称现由其他容器占用；拒绝停止或改名') + '\n'
    rollback += 'if [ -n "$cf_new_id" ]; then\n  [[ "$cf_new_id" =~ ^[0-9a-f]{64}$ ]] || exit 1\n'
    rollback += '  docker run --rm --network none --mount ' + Q(_mount_arg(_volume(agent, '/opt/configflow-agent', True))) + ' --entrypoint /bin/sh ' + Q(request.get('image', IMAGE)) + ' -c ' + Q('test ! -e /opt/configflow-agent/oxidns-ha.json && test ! -e /opt/configflow-agent/oxidns-ha-handoff.json') + ' || ' + _fail('无法确认新容器未启用 HA；请先在页面停用 HA 并确认记录清理后回滚') + '\n'
    rollback += '  ! docker container inspect ' + Q(failed) + ' >/dev/null 2>&1 || ' + _fail('回滚保留名已占用；未执行回滚') + '\n'
    rollback += '  docker update --restart=no "$cf_new_id" >/dev/null\n  docker stop "$cf_new_id" >/dev/null\n  docker rename "$cf_new_id" ' + Q(failed) + '\nfi\n'
    rollback += 'if [ -s "$cf_backup/companion-id" ]; then\n  cf_companion_id=$(cat "$cf_backup/companion-id")\n  [[ "$cf_companion_id" =~ ^[0-9a-f]{64}$ ]] || exit 1\n  docker update --restart=no "$cf_companion_id" >/dev/null\n  docker stop "$cf_companion_id" >/dev/null\nfi\n'
    rollback += '[ "$(docker inspect --format \'{{.Name}}\' "$cf_old_id")" = ' + Q('/' + old) + ' ] || docker rename "$cf_old_id" ' + Q(old) + '\n'
    rollback += 'cf_policy=$(cat "$cf_backup/restart-policy")\ncase "$cf_policy" in no:0|always:0|unless-stopped:0) cf_policy=${cf_policy%:0} ;; on-failure:[0-9]*) ;; *) exit 1 ;; esac\n'
    rollback += 'docker update --restart="$cf_policy" "$cf_old_id" >/dev/null\ndocker start "$cf_old_id"\n'
    rollback += 'printf "%s\\n" "已恢复原容器与重启策略；数据卷保持原位，原配置备份与失败容器均保留。"\n'
    main.pop('_network_name', None)
    return yaml.safe_dump(compose, sort_keys=False, allow_unicode=True).replace('$', '$$'), script, rollback


def generate_guide(request, enrollment=None):
    if not isinstance(request, dict):
        raise ValueError('请提交 JSON 对象')
    action = request.get('action', 'generate')
    if action == 'check':
        from .docker_ha_preflight import parse_report
        return {'success': True, 'preflight_report': parse_report(request.get('output'), request.get('network_mode', 'host'))}
    if action == 'inspect':
        return {'success': True, 'inspection_command': inspection_command(request.get('container_name', ''))}
    if action == 'verify':
        try:
            vip = ipaddress.IPv4Address(request.get('vip'))
            if vip.is_unspecified or vip.is_loopback or vip.is_multicast or vip.is_reserved or vip.is_link_local:
                raise ValueError()
        except (ValueError, TypeError):
            raise ValueError('请填写有效的虚拟 IPv4 地址') from None
        return {'success': True, 'verification_command': 'nslookup example.com ' + str(vip) + '\n'}
    if action != 'generate' or request.get('mode', 'new') not in ('new', 'migrate'):
        raise ValueError('安装步骤或模式无效')
    method = request.get('install_method', 'script')
    if method not in ('script', 'compose', 'compose_direct'):
        raise ValueError('install_method 须为 script、compose 或 compose_direct')
    if method in ('compose', 'compose_direct') and request.get('mode', 'new') != 'new':
        raise ValueError('Compose 安装包仅支持新安装；已有容器请使用迁移脚本')
    image = request.get('image', IMAGE)
    if not isinstance(image, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/@-]{0,254}', image):
        raise ValueError('Docker 镜像名称无效')
    server = request.get('server_url', '')
    if not isinstance(server, str) or not re.match(r'^https?://[^\s]+$', server):
        raise ValueError('请填写完整的 ConfigFlow HTTP(S) 地址')
    parsed = urlsplit(server)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname:
        raise ValueError('ConfigFlow 地址不能包含登录凭据、查询参数或片段')
    server = server.rstrip('/')
    port = request.get('oxidns_port', 8082)
    if isinstance(port, bool) or not isinstance(port, int):
        raise ValueError('Agent 端口须为整数')
    params = dict(server_url=server, agent_name=request.get('agent_name', 'agent'), agent_ip=request.get('agent_ip', ''),
                  data_dir=request.get('data_dir', ''), network_mode=request.get('network_mode', 'host'),
                  enable_mihomo=False, enable_oxidns=True, enable_dns_ha=True, oxidns_port=port,
                  ha_parent=request.get('ha_parent', ''), ha_subnet=request.get('ha_subnet', ''),
                  ha_gateway=request.get('ha_gateway', ''), ha_host_id_path=request.get('ha_host_id_path', '/etc/machine-id'))
    create_identity = request.get('create_host_identity', False)
    if not isinstance(create_identity, bool):
        raise ValueError('create_host_identity 须为布尔值')
    if not (method == 'compose_direct' and create_identity):
        _path(params['ha_host_id_path'], '宿主身份文件')
    identity = identity_command(params['ha_host_id_path']) if create_identity and method != 'compose_direct' else ''
    mode = request.get('mode', 'new')
    if mode == 'migrate':
        compose, install, rollback = _migration(params, request)
    else:
        params['data_dir'] = _path(params['data_dir'], '持久数据根目录')
        if method == 'compose_direct':
            from .docker_ha_compose_direct import generate_direct
            compose, direct = generate_direct(params, request, enrollment)
            install = direct['command']
        else:
            compose, install = _new_install(params, request, enrollment)
        rollback = ''
    inspection = inspection_command(request.get('container_name', '') if mode == 'migrate' else '')
    logs_name = request.get('container_name') if mode == 'migrate' else params['agent_name'] + '-oxidns'
    logs = ('docker compose logs --tail 80\n' if method == 'compose_direct'
            else 'docker logs --tail 80 ' + Q(logs_name) + '\n')
    blocks = [{'id': 'inspect', 'title': '只读检查宿主机与原容器', 'command': inspection}]
    if identity:
        blocks.append({'id': 'identity', 'title': '仅在身份文件缺失时创建一次', 'command': identity})
    blocks += [{'id': 'install', 'title': '备份并迁移当前主机' if mode == 'migrate' else '安装当前主机', 'command': install},
               {'id': 'logs', 'title': '查看启动日志', 'command': logs}]
    if rollback:
        blocks.append({'id': 'rollback', 'title': '需要时恢复原容器', 'command': rollback})
    result = {'success': True, 'inspection_command': inspection, 'identity_command': identity, 'compose': compose,
            'install_command': install, 'rollback_command': rollback, 'command_blocks': blocks,
            'warnings': (['迁移仅适用于 ConfigFlow 单 OxiDNS 默认启动容器；先执行迁移脚本，不要直接 Compose up 跳过原挂载核对和备份。',
                          '已有高可用组请先在页面处理虚拟 IP 与停用；脚本拒绝迁移仍有 HA 记录的容器。',
                          '备份包含 Agent 身份与配置，请保护备份目录；旧容器和数据不会被删除。',
                          '迁移 Compose 的 raw 环境文件格式需要 Docker Compose 2.30+；安装命令使用 Docker Run，不依赖 Compose 版本。'] if mode == 'migrate' else ['首次安装请先执行安装命令完成安全注册；Compose 仅供查看或后续维护，直接 Compose up 不会执行注册步骤。'])}

    if method == 'compose':
        from .docker_ha_compose import generate_bundle
        result['compose'], result['compose_bundle'] = generate_bundle(params, request, enrollment)
        result['warnings'] = ['安装包仅支持新安装：解压后先执行 sudo bash prepare.sh，再运行 docker compose up -d；需要 Compose 2.20+。',
                              '安装包包含限定本机的首次登记凭据，请勿公开分享；已登记并持久保存身份后，重启不依赖该凭据或登记 API。',
                              '请在 Compose 安装包和备用安装脚本中选择一种方式，不要对同一数据目录混用。']
    if method == 'compose_direct':
        result['compose_deployment'] = direct
        result['warnings'] = ['只需保存 compose.yaml 并运行 docker compose up -d；需要包含内置安装器的新镜像和 Compose 2.20+。',
                              'YAML 包含仅用于本设备的24小时登记凭据，也可被 Docker inspect 读取；请勿公开分享。已保存 Agent 身份后重启不依赖该凭据。',
                              '初始化仅写应用数据目录；选用自动身份时还使用固定 Docker 身份卷。不要复制或删除该共享身份卷，不要运行 down -v。']
    return result
