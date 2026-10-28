#!/usr/bin/env python3
"""Prepare one owned installation, then enter the ordinary OxiDNS service path."""
import fcntl
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import tempfile
from urllib.parse import urlsplit

_spec = importlib.util.spec_from_file_location('compose_install', Path(__file__).with_name('docker-compose-install.py'))
legacy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(legacy)
fail = legacy.fail
present = legacy.present
write_json = legacy.write_json
MARKER = '.configflow-compose-compact.json'
IDENTITY = '.configflow-ha-identity'
LOCK = '.configflow-compose-compact.lock'
AGENT = Path('/opt/configflow-agent')
CORE = Path('/etc/oxidns')
HA = Path('/var/lib/configflow-ha')


def read_regular(path, limit=65536):
    # O_NONBLOCK lets a named pipe fail the ordinary-file check rather than hang.
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                fail('安装文件不是普通文件，拒绝继续。')
            value = stream.read(limit + 1)
            if len(value) > limit:
                fail('安装文件过大，拒绝继续。')
            return value
    except OSError:
        fail('安装文件缺失、不可读或为软链接；请保留数据并检查挂载。')


legacy.read_regular = read_regular
read_json = legacy.read_json


def settings_from_environment(environment=None):
    env = os.environ if environment is None else environment
    role = env.get('SERVICE_TYPE', '')
    if role not in ('oxidns-ha-host', 'oxidns-ha-macvlan'):
        fail('精简 Compose 的服务类型无效。')
    server = env.get('SERVER_URL', '').rstrip('/')
    try:
        parsed = urlsplit(server)
        address = ipaddress.IPv4Address(env.get('AGENT_IP', ''))
        port = int(env.get('AGENT_PORT', '8082'))
    except (ValueError, TypeError):
        fail('服务器地址、主机 IPv4 或 Agent 端口无效。')
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or re.search(r'\s', server)
            or address.is_unspecified or address.is_loopback or address.is_multicast or address.is_reserved
            or not 1 <= port <= 65535):
        fail('服务器地址、主机 IPv4 或 Agent 端口无效。')
    name = env.get('AGENT_NAME', '')
    if not re.fullmatch(r'[A-Za-z0-9_-]+', name):
        fail('Agent 名称无效。')
    key = env.get('INSTALL_KEY', '')
    if key and not re.fullmatch(r'[0-9a-f]{32}\.[A-Za-z0-9_-]{43}', key):
        fail('安装凭据格式无效，请重新生成 Compose。')
    session, token = key.split('.', 1) if key else ('', '')
    return {'server_url': server, 'session': session, 'token': token, 'name': name,
            'ip': str(address), 'port': port, 'network': role.removeprefix('oxidns-ha-')}


def plain_directory(path):
    try:
        if not stat.S_ISDIR(path.lstat().st_mode):
            fail('持久目录不是普通目录，拒绝接管。')
    except OSError:
        fail('持久目录不存在；请检查 Compose 目录映射。')


def owned_temporary(path, owner):
    if not re.fullmatch(r'\.compose-write-' + owner + r'-[a-z0-9_]{8}', path.name):
        return False
    read_regular(path)
    return True


def write_identity(path, value, owner):
    descriptor, temporary = tempfile.mkstemp(prefix='.compose-write-' + owner + '-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(value.encode() + b'\n')
            stream.flush(); os.fsync(stream.fileno())
        os.link(temporary, path, follow_symlinks=False)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.unlink(temporary)


def binding_for(settings, saved=None):
    session = settings['session']
    if not session:
        if not saved or saved.get('ready') is not True:
            fail('首次安装需要 INSTALL_KEY，请重新生成 Compose。')
        session = saved.get('binding', {}).get('session', '')
    if not isinstance(session, str) or not re.fullmatch(r'[0-9a-f]{32}', session):
        fail('安装记录缺少有效登记范围，拒绝接管。')
    binding = {key: settings[key] for key in ('server_url', 'name', 'ip', 'port', 'network')}
    binding['session'] = session
    return binding


def check_record(record, owner, installation_id=None):
    if (record.get('protocol') != 2 or record.get('owner') != owner
            or not isinstance(record.get('installation_id'), str)
            or not re.fullmatch(r'[0-9a-f]{32}', record['installation_id'])
            or (installation_id is not None and record['installation_id'] != installation_id)):
        fail('数据目录属于其他安装或记录损坏，拒绝覆盖；旧安装请使用迁移入口。')


def prepare(settings, agent=AGENT, core=CORE, ha=HA):
    directories = (agent, core, ha)
    for directory in directories:
        plain_directory(directory)
    marker = agent / MARKER
    saved = read_json(marker) if present(marker) else None
    binding = binding_for(settings, saved)
    owner = hashlib.sha256(json.dumps(binding, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    # Reject foreign data before even creating a lock. Failed compact attempts
    # may leave only our empty lock or owner-tagged atomic-write residue.
    if saved is None:
        for directory in directories:
            for entry in directory.iterdir():
                if (directory == agent and entry.name == LOCK) or owned_temporary(entry, owner):
                    read_regular(entry)
                    continue
                # A concurrent first startup may have claimed the directory
                # since our initial read. Validate its marker under the lock.
                if present(marker):
                    break
                fail('数据目录已有内容，拒绝覆盖；旧安装请使用迁移入口。')
    lock_descriptor = os.open(agent / LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(lock_descriptor, 'r+') as lock:
        if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
            fail('安装锁不是普通文件，拒绝继续。')
        fcntl.flock(lock, fcntl.LOCK_EX)
        saved = read_json(marker) if present(marker) else None
        if saved is None:
            # Repeat the empty-directory check while holding the installation lock.
            for directory in directories:
                if any(not ((directory == agent and entry.name == LOCK) or owned_temporary(entry, owner))
                       for entry in directory.iterdir()):
                    fail('持久目录出现未知内容，拒绝覆盖。')
            saved = {'protocol': 2, 'owner': owner, 'binding': binding,
                     'installation_id': secrets.token_hex(16), 'ready': False}
            write_json(marker, saved)
        check_record(saved, owner)
        if saved.get('binding') != binding:
            fail('地址、名称或网络与已有安装不符，请使用迁移入口。')
        installation_id = saved['installation_id']
        complete = saved.get('ready') is True
        for directory in directories:
            record_path = directory / MARKER
            record = read_json(record_path) if present(record_path) else None
            if record is not None:
                check_record(record, owner, installation_id)
            elif complete:
                fail('持久目录的安装记录丢失，拒绝重新接管。')
            if not complete:
                for entry in directory.iterdir():
                    if entry.name == MARKER or (directory == agent and entry.name == LOCK) or owned_temporary(entry, owner):
                        continue
                    if directory == agent and entry.name == IDENTITY:
                        if read_regular(entry).strip().decode('ascii', errors='replace') == installation_id:
                            continue
                    fail('初始化尚未完成但已有其他数据，拒绝覆盖。')
                if record is None:
                    write_json(record_path, {key: saved[key] for key in ('protocol', 'owner', 'installation_id')})
        identity = agent / IDENTITY
        if present(identity):
            if read_regular(identity).strip() != installation_id.encode():
                fail('安装身份与持久记录不符，拒绝覆盖。')
        elif complete:
            fail('持久安装身份已丢失，拒绝重新生成。')
        else:
            write_identity(identity, installation_id, owner)
        if not complete:
            # The companion may start only after all three directories and the
            # immutable installation identity are claimed consistently.
            write_json(marker, {**saved, 'ready': True}, replace=True)
    return {'owner': owner, 'url': binding['server_url'] + '/api/agents/docker-oxidns-ha-enroll/' + binding['session'],
            'token': settings['token']}


def run(settings, agent=AGENT, core=CORE, ha=HA):
    enrollment = prepare(settings, agent, core, ha)
    legacy.bootstrap(enrollment, agent)
    print('持久目录、安装身份和登记已核对，启动 OxiDNS。', flush=True)


def main():
    settings = settings_from_environment()
    run(settings)
    os.environ.pop('INSTALL_KEY', None)
    os.environ.update(SERVICE_TYPE='oxidns', CONFIGFLOW_DNS_HA='1', CONFIGFLOW_DNS_HA_NETWORK=settings['network'],
                      CONFIGFLOW_DNS_HA_IDENTITY='installation', ENABLE_MIHOMO='false', ENABLE_MOSDNS='false', ENABLE_OXIDNS='true')
    command = sys.argv[1:] or ['/usr/bin/supervisord', '-c', '/etc/supervisor/supervisord.conf']
    os.execv('/entrypoint.sh', ['/entrypoint.sh', *command])


if __name__ == '__main__':
    try:
        main()
    except OSError:
        fail('无法安全读写指定持久目录；请检查挂载权限，未启动核心。')
