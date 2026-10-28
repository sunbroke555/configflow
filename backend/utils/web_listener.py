"""Manage this deployment's nginx listener; also usable before Flask starts."""

import errno
import fcntl
import http.client
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager


TEMPLATE_PATH = Path('/etc/nginx/configflow-nginx.conf.template')
CONFIG_PATH = Path('/etc/nginx/nginx.conf')
STATE_PATH = Path(os.environ.get('DATA_DIR', '/data')) / 'web-listener.json'
LOCK_PATH = STATE_PATH.with_suffix('.lock')
NGINX_BINARY = '/usr/sbin/nginx'
HEALTH_PATH = '/_configflow/listener-health'
HEALTH_TIMEOUT = 5.0
MARKER = '# configflow-web-listener '


class WebListenerError(Exception):
    def __init__(self, message, status_code=503):
        super().__init__(message)
        self.status_code = status_code


def _port(value):
    if type(value) is not int or not 1 <= value <= 65535:
        raise WebListenerError('Web 端口须为 1–65535 的整数', 400)
    return value


def _supported():
    return Path(TEMPLATE_PATH).is_file() and os.access(NGINX_BINARY, os.X_OK)


@contextmanager
def _lock():
    Path(LOCK_PATH).parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK_PATH, 'a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise WebListenerError('Web 端口正在更新，请稍后重试', 409) from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _selection():
    if 'CONFIGFLOW_WEB_PORT' in os.environ:
        raw = os.environ['CONFIGFLOW_WEB_PORT'].strip()
        if len(raw) > 5 or not raw.isascii() or not raw.isdecimal():
            raise WebListenerError('CONFIGFLOW_WEB_PORT 须为 1–65535 的整数', 400)
        return _port(int(raw)), 'environment'
    try:
        saved = json.loads(Path(STATE_PATH).read_text())
        return _port(saved['port']), 'saved'
    except FileNotFoundError:
        return 80, 'default'
    except (OSError, ValueError, KeyError, TypeError, WebListenerError):
        raise WebListenerError('无法读取已保存的 Web 端口；可设置 CONFIGFLOW_WEB_PORT 恢复访问') from None


def _ipv6_available():
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            probe.bind(('::1', 0))
        return True
    except OSError as error:
        if error.errno in (errno.EAFNOSUPPORT, errno.EPROTONOSUPPORT, errno.EADDRNOTAVAIL, errno.ENODEV):
            return False
        raise WebListenerError('无法检查 IPv6 监听能力') from None


def _read_active():
    try:
        first = Path(CONFIG_PATH).read_text().splitlines()[0]
        if not first.startswith(MARKER):
            raise ValueError()
        active = json.loads(first[len(MARKER):])
        _port(active['port'])
        if type(active['ipv6']) is not bool or not isinstance(active['token'], str):
            raise ValueError()
        return active
    except (OSError, IndexError, ValueError, KeyError, TypeError, WebListenerError):
        raise WebListenerError('Web 监听配置尚未初始化，请重启容器') from None


def _status():
    port, source = _selection()
    active = _read_active()
    if active['port'] != port:
        raise WebListenerError('Web 监听配置与保存的端口不一致，请重启容器')
    status = {'supported': True, 'port': port, 'editable': source != 'environment',
              'ipv6': active['ipv6'], 'source': source}
    if source == 'environment':
        status['message'] = '端口由 CONFIGFLOW_WEB_PORT 指定；移除该环境变量并重建容器后可在此修改'
    return status


def listener_status():
    if not _supported():
        return {'supported': False, 'port': None, 'editable': False, 'ipv6': False,
                'source': 'default', 'message': '当前部署不支持在 Web 中修改监听端口，请通过部署配置调整'}
    with _lock():
        return _status()


def _atomic_write(path, content, mode=0o644):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _restore(path, content, mode=0o644):
    if content is None:
        Path(path).unlink(missing_ok=True)
    else:
        _atomic_write(path, content, mode)


def _snapshot(path):
    try:
        return Path(path).read_bytes()
    except FileNotFoundError:
        return None


def _render(port, ipv6):
    template = Path(TEMPLATE_PATH).read_text()
    if template.count('__CONFIGFLOW_LISTEN__') != 1 or '__CONFIGFLOW_LISTENER_TOKEN__' not in template:
        raise WebListenerError('Web 监听模板不完整，请更新容器镜像')
    token = secrets.token_hex(16)
    listeners = f'listen 0.0.0.0:{port};'
    if ipv6:
        listeners += f'\n        listen [::]:{port} ipv6only=on;'
    metadata = {'port': port, 'ipv6': ipv6, 'token': token}
    rendered = template.replace('__CONFIGFLOW_LISTEN__', listeners).replace('__CONFIGFLOW_LISTENER_TOKEN__', token)
    return (MARKER + json.dumps(metadata) + '\n' + rendered).encode(), metadata


def _nginx(*arguments):
    try:
        result = subprocess.run([str(NGINX_BINARY), '-c', str(CONFIG_PATH), *arguments],
                                capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise WebListenerError('无法执行 Nginx 配置检查或重载') from None
    if result.returncode:
        # nginx diagnostics include filesystem/config contents; keep them out of the API.
        print('[web-listener] ' + result.stderr[-4096:].decode(errors='replace'), file=sys.stderr)
        raise WebListenerError('Nginx 配置检查或重载失败，请查看容器日志')


def _check_reserved(port):
    try:
        sub_store_port = int(os.environ.get('SUB_STORE_BACKEND_API_PORT', '3001'))
    except ValueError:
        sub_store_port = 3001
    if port in (5001, sub_store_port):
        service = 'ConfigFlow 后端' if port == 5001 else 'Sub-Store'
        raise WebListenerError(f'端口 {port} 已保留给 {service}，请选择其他端口', 409)


def _check_available(port, ipv6):
    families = [(socket.AF_INET, '0.0.0.0')]
    if ipv6:
        families.append((socket.AF_INET6, '::'))
    for family, host in families:
        try:
            with socket.socket(family, socket.SOCK_STREAM) as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if family == socket.AF_INET6:
                    probe.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                probe.bind((host, port))
        except OSError as error:
            if error.errno == errno.EADDRINUSE:
                raise WebListenerError(f'端口 {port} 已被占用，请选择其他端口', 409) from None
            raise WebListenerError(f'无法监听端口 {port}，请检查权限和网络配置', 409) from None


def _healthy(active):
    for host in (['127.0.0.1', '::1'] if active['ipv6'] else ['127.0.0.1']):
        connection = http.client.HTTPConnection(host, active['port'], timeout=0.3)
        try:
            connection.request('GET', HEALTH_PATH, headers={'Connection': 'close'})
            response = connection.getresponse()
            if response.status != 200 or response.read(256).decode('ascii', errors='replace').strip() != active['token']:
                return False
        except (OSError, http.client.HTTPException):
            return False
        finally:
            connection.close()
    return True


def _wait_healthy(active):
    deadline = time.monotonic() + HEALTH_TIMEOUT
    while True:
        if _healthy(active):
            return
        if time.monotonic() >= deadline:
            raise WebListenerError('Nginx 未能在新端口提供服务，已尝试恢复原端口')
        time.sleep(0.1)


def prepare_listener():
    """Render/validate startup configuration without altering saved preferences."""
    if not _supported():
        raise WebListenerError('缺少 Nginx 或 Web 监听模板')
    with _lock():
        port, _ = _selection()
        _check_reserved(port)
        content, _ = _render(port, _ipv6_available())
        old_config = _snapshot(CONFIG_PATH)
        try:
            _atomic_write(CONFIG_PATH, content)
            _nginx('-t')
        except Exception:
            _restore(CONFIG_PATH, old_config)
            raise
        return _status()


def update_listener(port):
    port = _port(port)
    _check_reserved(port)
    if not _supported():
        raise WebListenerError('当前部署不支持在 Web 中修改监听端口')
    with _lock():
        status = _status()
        if not status['editable']:
            raise WebListenerError(status['message'], 409)
        if port == status['port']:
            return status
        old_active = _read_active()
        ipv6 = _ipv6_available()
        _check_available(port, ipv6)
        content, active = _render(port, ipv6)
        old_config = _snapshot(CONFIG_PATH)
        reload_attempted = False
        try:
            _atomic_write(CONFIG_PATH, content)
            _nginx('-t')
            reload_attempted = True
            _nginx('-s', 'reload')
            _wait_healthy(active)
            _atomic_write(STATE_PATH, (json.dumps({'port': port}) + '\n').encode(), 0o600)
        except Exception as error:
            try:
                _restore(CONFIG_PATH, old_config)
                if reload_attempted:
                    _nginx('-s', 'reload')
                    _wait_healthy(old_active)
            except Exception:
                raise WebListenerError('更新失败且无法确认原端口已恢复；请设置 CONFIGFLOW_WEB_PORT 并重建容器恢复访问') from None
            if isinstance(error, WebListenerError):
                raise error
            raise WebListenerError('保存 Web 端口失败，已恢复原配置') from None
        return _status()


if __name__ == '__main__':
    try:
        if sys.argv[1:] != ['prepare']:
            raise WebListenerError('usage: configflow-web-listener.py prepare', 400)
        print(json.dumps(prepare_listener(), ensure_ascii=False))
    except (WebListenerError, OSError) as error:
        print(f'[web-listener] {error}', file=sys.stderr)
        sys.exit(1)
