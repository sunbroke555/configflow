"""Short-lived, single-device enrollment for the Docker installation guide.

Generating a command creates no Agent. The executed installer can obtain only
the credentials of its exact new OxiDNS identity, even with registration locked.
"""
from datetime import datetime
import hashlib
import hmac
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import time
import uuid


TTL = 24 * 60 * 60


class EnrollmentError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def issue(repository, body, profile_id):
    """Call only after guide input validation and administrator authentication."""
    repository.validate_profile_id(profile_id)
    root = Path(repository.data_dir) / 'docker-ha-installations'
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Retain the installation identity when a dialog closes or an interrupted
    # install is retried. Different settings never gain access to that identity.
    spec = {**body, 'mode': 'new', 'server_url': body['server_url'].rstrip('/'), 'profile_id': profile_id}
    spec.pop('action', None)
    if spec.get('install_method') == 'compose_compact':
        # Updating the image is routine maintenance, not a new Agent identity.
        # The compact runtime does not use legacy host-file/volume choices.
        for ignored in ('image', 'ha_host_id_path', 'create_host_identity'):
            spec.pop(ignored, None)
    key = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
    index = root / (key + '.index')
    fd = os.open(root / '.issue.lock', os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, 'r+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        session = None
        if index.exists():
            session_id = index.read_text()
            if not re.fullmatch(r'[a-f0-9]{32}', session_id):
                raise EnrollmentError('安装记录无法读取，请保留现场并检查服务端数据。', 409)
            try:
                session = json.loads((root / (session_id + '.json')).read_text())
            except (FileNotFoundError, ValueError):
                raise EnrollmentError('安装记录无法读取，请保留现场并检查服务端数据。', 409) from None
        if session is None:
            session = {'id': uuid.uuid4().hex, 'profile_id': profile_id,
                       'name': body.get('agent_name', 'agent') + '-oxidns',
                       'host': body['agent_ip'], 'port': body.get('oxidns_port', 8082)}
        if session.get('expires', 0) <= time.time():
            token = secrets.token_urlsafe(32)
            session.update(token=token, token_hash=hashlib.sha256(token.encode()).hexdigest(), expires=int(time.time()) + TTL)
            path = root / (session['id'] + '.json')
            temp = path.with_suffix('.tmp')
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as output:
                json.dump(session, output)
            os.replace(temp, path)
        if not index.exists():
            fd = os.open(index, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as output:
                output.write(session['id'])
        return {'url': spec['server_url'] + '/api/agents/docker-oxidns-ha-enroll/' + session['id'],
                'token': session['token'], 'expires_at': session['expires']}


def enroll(repository, manager, session_id, token):
    if (not re.fullmatch(r'[a-f0-9]{32}', session_id or '')
            or not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', token)):
        raise EnrollmentError('安装凭据无效，请重新生成安装命令。', 401)
    path = Path(repository.data_dir) / 'docker-ha-installations' / (session_id + '.json')
    try:
        session = json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        raise EnrollmentError('安装凭据无效，请重新生成安装命令。', 401) from None
    if (time.time() >= session['expires']
            or not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), session['token_hash'])):
        raise EnrollmentError('安装凭据无效或已过期，请重新生成安装命令。', 401)
    repository.validate_profile_id(session['profile_id'])
    expected = {key: session[key] for key in ('name', 'host', 'port', 'profile_id')}
    expected.update(service_type='oxidns', deployment_method='docker')

    def register(agents):
        owned = next((a for a in agents if a.get('docker_ha_installation_id') == session_id), None)
        if owned:
            if any(owned.get(key) != value for key, value in expected.items()):
                raise EnrollmentError('本次安装的设备信息已改变，请重新生成命令。', 409)
            return {'agent_id': owned['id'], 'token': owned['token']}
        if any(a.get('name') == expected['name'] and a.get('host') == expected['host'] for a in agents):
            raise EnrollmentError('该名称与地址已有 Agent；请使用旧容器迁移，不能创建或覆盖其身份。', 409)
        agent = {**expected, 'id': 'agent_' + uuid.uuid4().hex, 'token': secrets.token_urlsafe(24),
                 'docker_ha_installation_id': session_id, 'enabled': True,
                 'status': 'offline', 'last_heartbeat': '', 'version': 'unknown', 'config_version': '0',
                 'created_at': datetime.now().isoformat()}
        agents.append(agent)
        return {'agent_id': agent['id'], 'token': agent['token']}

    # Scope, conflict detection and replay live in the same repository transaction.
    return manager._update_agents(register, session['profile_id'])


def bootstrap_command(enrollment, request):
    """Use the shipped image's Python; no Python/curl dependency on a NAS host."""
    data_dir = request['data_dir'].rstrip('/') + '/agent'
    image = request.get('image', 'thsrite/config-flow-agent:latest')
    network = ['--network', 'host']
    if request.get('network_mode') == 'macvlan':
        network = ['--network', request.get('agent_name', 'agent') + '-oxidns-ha-net', '--ip', request['agent_ip']]
    values = json.dumps({'url': enrollment['url'], 'token': enrollment['token']}, ensure_ascii=True)
    source = '''import json, os, sys, urllib.error, urllib.request
from pathlib import Path
settings = json.loads(SETTINGS_LITERAL)
path = Path('/opt/configflow-agent/config-oxidns.json')
marker = path.with_name('.docker-ha-installation.json')
if path.is_symlink() or marker.is_symlink():
    sys.exit('Agent 身份路径是软链接，已停止安装。')
if marker.exists():
    try: same_installation = json.loads(marker.read_text()).get('url') == settings['url']
    except Exception: same_installation = False
    if not same_installation: sys.exit('此目录属于另一份安装命令；请保留原命令或使用迁移。')
elif path.exists():
    sys.exit('Agent 数据目录已有身份文件；请选择旧容器迁移，不能覆盖。')
request = urllib.request.Request(settings['url'], data=b'{}', method='POST',
    headers={'Authorization': 'Bearer ' + settings['token'], 'Content-Type': 'application/json'})
try:
    with urllib.request.urlopen(request, timeout=30) as response:
        identity = json.load(response)
except urllib.error.HTTPError as error:
    try: message = json.load(error).get('message', '安装登记失败')
    except Exception: message = '安装登记失败'
    sys.exit(message + '；请回到引导重新生成或重试。')
except Exception:
    sys.exit('无法连接 ConfigFlow 登记设备，请检查服务地址、证书和网络后重试。')
if not all(isinstance(identity.get(key), str) and identity[key] for key in ('agent_id', 'token')):
    sys.exit('安装登记响应无效，未写入身份文件。')
if path.exists():
    try: existing = json.loads(path.read_text())
    except Exception: sys.exit('现有 Agent 身份文件不可读，已停止安装。')
    if any(existing.get(key) != identity[key] for key in ('agent_id', 'token')):
        sys.exit('现有 Agent 身份与本次安装不一致，已停止安装。')
    print('已核对本次安装的 Agent 身份，保留原文件。')
    sys.exit(0)
if not marker.exists():
    fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as output: json.dump({'url': settings['url']}, output)
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as output:
    json.dump({key: identity[key] for key in ('agent_id', 'token')}, output)
print('本机 Agent 身份已登记；启动后等待真实心跳与高可用环境检查。')
'''.replace('SETTINGS_LITERAL', repr(values))
    return ('# 安装凭据仅用于本机 OxiDNS，有效期 24 小时；不包含管理账号或全局注册密钥。\n'
            + 'docker run --rm -i ' + ' '.join(map(shlex.quote, network)) + ' --entrypoint python3 --mount '
            + shlex.quote('type=bind,src=' + data_dir + ',dst=/opt/configflow-agent') + ' '
            + shlex.quote(image) + " - <<'CF_DOCKER_HA_ENROLL'\n" + source + 'CF_DOCKER_HA_ENROLL\n')
