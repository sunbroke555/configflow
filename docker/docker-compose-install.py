#!/usr/bin/env python3
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
    owner = value.get('owner', '')
    prefix = '.compose-write-' + owner + '-' if isinstance(owner, str) and re.fullmatch(r'[0-9a-f]{64}', owner) else '.compose-enroll-'
    fd, temporary = tempfile.mkstemp(prefix=prefix, dir=path.parent)
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


def bootstrap(settings, agent_dir):
    owner = settings.get('owner')
    if not isinstance(owner, str) or not re.fullmatch(r'[0-9a-f]{64}', owner):
        fail('安装归属标记无效，请重新生成 Compose。')
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
            fail('首次登记地址无效，请重新生成 Compose。')
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
            fail('首次登记凭据无效，请重新生成 Compose。')
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
            fail('首次登记被拒绝（HTTP ' + str(error.code) + '）；不跟随地址重定向。请确认最终服务地址；凭据过期时以相同设置重新生成 Compose 后重试。')
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




ROOT_MARKER = '.configflow-compose-direct.json'
AGENT_SETUP = '.configflow-compose-direct.json'
IDENTITY_DIRECTORY = Path('/run/configflow-host-identity')


def settings_from_environment():
    raw = os.environ.get('CONFIGFLOW_COMPOSE_INSTALL', '')
    if len(raw) > 16384:
        fail('安装设置过大，拒绝执行。')
    try:
        settings = json.loads(raw)
    except (ValueError, RecursionError):
        fail('安装设置无效，请重新生成 Compose。')
    if (not isinstance(settings, dict) or settings.get('protocol') != 1
            or not isinstance(settings.get('owner'), str) or not re.fullmatch(r'[0-9a-f]{64}', settings['owner'])
            or settings.get('identity_mode') not in ('file', 'docker-volume')
            or not isinstance(settings.get('url'), str)):
        fail('安装设置或协议无效，请重新生成 Compose。')
    return settings


def plain_directory(path):
    try:
        if not stat.S_ISDIR(path.lstat().st_mode):
            fail('持久路径不是普通目录或为软链接，拒绝继续。')
    except OSError:
        fail('持久目录不存在或不可读，拒绝继续。')


def same_setup(record, settings):
    return all(record.get(key) == settings[key] for key in ('protocol', 'owner', 'url', 'identity_mode'))


def owned_temporary(path, owner):
    if not re.fullmatch(r'\.compose-write-' + re.escape(owner) + r'-[a-z0-9_]{8}', path.name):
        return False
    # A killed atomic write may be incomplete; preserve it rather than deleting.
    read_regular(path)
    return True


def check_layout(root, settings):
    """Empty directories pre-created by Docker are not treated as an old install."""
    plain_directory(root)
    marker = root / ROOT_MARKER
    record = read_json(marker) if present(marker) else None
    if record and not same_setup(record, settings):
        fail('数据目录属于另一份安装，拒绝接管。')
    allowed = {'agent', 'oxidns', 'ha', 'compose.yaml', ROOT_MARKER}
    for entry in root.iterdir():
        if owned_temporary(entry, settings['owner']):
            continue
        if entry.name not in allowed:
            fail('数据目录含旧数据或未知文件；请选专用空目录，已有安装请使用迁移。')
        if entry.name in ('agent', 'oxidns', 'ha'):
            plain_directory(entry)
            if not record and any(entry.iterdir()):
                fail('数据子目录已有内容且无本次归属记录，拒绝覆盖。')
        else:
            read_regular(entry, 1024 * 1024)
    return record


def ensure_host_identity(settings, directory=IDENTITY_DIRECTORY, file_path=Path('/run/configflow-host-machine-id'), create=True):
    if settings['identity_mode'] == 'file':
        value = read_regular(file_path).strip()
    else:
        plain_directory(directory)
        path = directory / 'host-id'
        if not present(path) and create:
            value = os.urandom(16).hex().encode() + b'\n'
            fd, temporary = tempfile.mkstemp(prefix='.host-id-', dir=directory)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(value); stream.flush(); os.fsync(stream.fileno())
                try:
                    os.link(temporary, path, follow_symlinks=False)
                except FileExistsError:
                    pass  # Another project initialized this shared Docker volume.
                descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            finally:
                os.unlink(temporary)
        value = read_regular(path).strip()
    if not 16 <= len(value) <= 128 or b'\r' in value or b'\n' in value:
        fail('宿主身份文件无效，拒绝覆盖或启动。')
    return hashlib.sha256(value).hexdigest()


def prepare(settings, root=Path('/configflow-data'), identity_directory=IDENTITY_DIRECTORY,
            identity_file=Path('/run/configflow-host-machine-id')):
    record = check_layout(root, settings)
    # Identity failures must not reserve an otherwise unused data directory.
    host_id = ensure_host_identity(settings, identity_directory, identity_file)
    owner = {key: settings[key] for key in ('protocol', 'owner', 'url', 'identity_mode')}
    marker = root / ROOT_MARKER
    if not record:
        try:
            write_json(marker, {**owner, 'host_id': host_id, 'ready': False})
        except FileExistsError:
            pass
    descriptor = os.open(marker, os.O_RDWR | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'r+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        record = check_layout(root, settings)
        if not record or record.get('host_id') != host_id:
            fail('宿主身份与初始化记录不符，拒绝接管。')
        for name in ('agent', 'oxidns', 'ha'):
            directory = root / name
            if not present(directory):
                directory.mkdir(mode=0o700)
            plain_directory(directory)
        setup = root / 'agent' / AGENT_SETUP
        if present(setup):
            saved = read_json(setup)
            if not same_setup(saved, settings) or saved.get('host_id') != host_id or saved.get('ready') is not True:
                fail('Agent 初始化记录异常，拒绝覆盖。')
        elif record.get('ready'):
            fail('Agent 初始化记录丢失；请保留现场，拒绝重新接管。')
        else:
            # Before completion only empty core/HA dirs and our own setup are allowed.
            if (any(any((root / name).iterdir()) for name in ('oxidns', 'ha'))
                    or any(not owned_temporary(entry, settings['owner']) for entry in (root / 'agent').iterdir())):
                fail('未完成的初始化目录出现未知内容，拒绝覆盖。')
            write_json(setup, {**owner, 'host_id': host_id, 'ready': True})
        if record.get('ready') is not True:
            write_json(marker, {**owner, 'host_id': host_id, 'ready': True}, replace=True)
    print('初始化完成；仅使用指定应用目录和选定宿主身份来源。', flush=True)


def run(settings, root=Path('/opt/configflow-agent'), identity_directory=IDENTITY_DIRECTORY,
        identity_file=Path('/run/configflow-host-machine-id')):
    setup = read_json(root / AGENT_SETUP)
    if not same_setup(setup, settings) or setup.get('ready') is not True:
        fail('本次安装尚未初始化完成，或持久目录属于其他安装。')
    host_id = ensure_host_identity(settings, identity_directory, identity_file, create=False)
    if setup.get('host_id') != host_id:
        fail('宿主身份与持久初始化记录不符，拒绝启动或覆盖。')
    bootstrap(settings, root)


if __name__ == '__main__':
    try:
        settings = settings_from_environment()
        if sys.argv[1:] == ['prepare']:
            prepare(settings)
        elif sys.argv[1:] == ['run']:
            run(settings)
            # This does not remove the short-lived value from Docker inspect.
            os.environ.pop('CONFIGFLOW_COMPOSE_INSTALL', None)
            os.execv('/entrypoint.sh', ['/entrypoint.sh', '/usr/bin/supervisord', '-c', '/etc/supervisor/supervisord.conf'])
        else:
            fail('仅支持 prepare 或 run。')
    except OSError:
        fail('无法安全读写指定持久目录；请检查该目录权限，未启动核心。')
