"""第三方依赖管理（目前只有内置 Sub-Store）

镜像内置 Sub-Store 后端：构建时把指定版本的 sub-store.bundle.js 放进
/opt/sub-store，由 supervisord 以 127.0.0.1:3001 拉起。在线更新时把新版本
下载到数据目录（DATA_DIR/sub-store/runtime），启动脚本 sub-store-start.sh
会在镜像内置版本和数据目录版本中取较新的一个，因此：
  - 在线更新跨容器重建保留；
  - 升级镜像后若镜像自带版本更新，自动用回镜像版本。
"""
import hashlib
import os
import shutil
import subprocess
import threading
import time

import requests

from backend.utils.logger import get_logger
from backend.utils.sub_store_runtime import runtime
from backend.utils.url_utils import safe_exception_details

logger = get_logger(__name__)

SUB_STORE_REPO = 'sub-store-org/Sub-Store'
SUB_STORE_ASSET = 'sub-store.bundle.js'
SUB_STORE_BUILTIN_URL = 'http://127.0.0.1:3001'
SUB_STORE_BUILTIN_DIR = os.environ.get('SUB_STORE_BUILTIN_DIR', '/opt/sub-store')
SUB_STORE_PROGRAM = 'sub-store'

_LATEST_TTL = 30 * 60
_MIN_BUNDLE_SIZE = 500 * 1024

class DependencyUpdateError(Exception):
    """可直接展示给用户的更新错误（不含敏感信息）"""


_latest_cache = {'checked_at': 0.0, 'data': None, 'error': ''}
_update_lock = threading.Lock()
_update_state = {'state': 'idle', 'message': '', 'target_version': '', 'finished_at': 0.0}


def _data_dir():
    from backend.common.config import DATA_DIR
    return DATA_DIR


def sub_store_runtime_dir():
    """在线更新下载的 bundle 所在目录（持久化在数据卷中）"""
    return os.path.join(_data_dir(), 'sub-store', 'runtime')


def parse_version(value):
    """'2.42.3' / 'v2.42.3' -> (2, 42, 3)；无法解析返回 None"""
    if not isinstance(value, str):
        return None
    parts = value.strip().lstrip('vV').split('.')
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return None


def is_newer(candidate, current):
    a, b = parse_version(candidate), parse_version(current)
    return a is not None and (b is None or a > b)


def _read_version_file(directory):
    try:
        with open(os.path.join(directory, 'VERSION'), encoding='utf-8') as f:
            return f.read().strip()
    except OSError:
        return ''


def is_builtin_available():
    """当前运行环境是否带内置 Sub-Store（即官方镜像）"""
    return os.path.isfile(os.path.join(SUB_STORE_BUILTIN_DIR, SUB_STORE_ASSET))


def active_bundle():
    """返回 (目录, 版本)，与 sub-store-start.sh 的选择逻辑保持一致"""
    builtin_version = _read_version_file(SUB_STORE_BUILTIN_DIR)
    runtime_dir = sub_store_runtime_dir()
    runtime_version = _read_version_file(runtime_dir)
    if (os.path.isfile(os.path.join(runtime_dir, SUB_STORE_ASSET))
            and is_newer(runtime_version, builtin_version)):
        return runtime_dir, runtime_version
    return SUB_STORE_BUILTIN_DIR, builtin_version


def fetch_running_version(base_url, timeout=5):
    """通过 Sub-Store 的 /api/utils/env 获取正在运行的后端版本，失败返回空串"""
    try:
        resp = requests.get(f'{base_url}/api/utils/env', timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        payload = data.get('data', data) if isinstance(data, dict) else {}
        version = payload.get('version', '') if isinstance(payload, dict) else ''
        return version if isinstance(version, str) else ''
    except Exception:
        return ''


def _node_version():
    try:
        out = subprocess.run(['node', '--version'], capture_output=True, text=True, timeout=5)
        return out.stdout.strip() if out.returncode == 0 else ''
    except Exception:
        return ''


def _request_options():
    """GitHub 请求复用「规则下载代理」"""
    try:
        from backend.utils.rule_fetch import get_rule_fetch_proxy
        proxy = get_rule_fetch_proxy()
    except Exception:
        proxy = ''
    return {'proxies': {'http': proxy, 'https': proxy}} if proxy else {}


def _download_url(url):
    """下载地址套用「GitHub 代理域名」镜像前缀（API 请求不套，镜像通常不支持）"""
    try:
        from backend.common.config import get_system_config
        from backend.converters.mihomo import apply_github_proxy_domain
        return apply_github_proxy_domain(url, get_system_config())
    except Exception:
        return url


def fetch_latest_release(force=False):
    """获取 Sub-Store 最新 release（带缓存）。返回 (data, error)"""
    now = time.time()
    if not force and _latest_cache['data'] and now - _latest_cache['checked_at'] < _LATEST_TTL:
        return _latest_cache['data'], ''
    try:
        resp = requests.get(
            f'https://api.github.com/repos/{SUB_STORE_REPO}/releases/latest',
            headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'ConfigFlow'},
            timeout=15,
            **_request_options(),
        )
        resp.raise_for_status()
        release = resp.json()
        asset = next((a for a in release.get('assets', []) if a.get('name') == SUB_STORE_ASSET), None)
        if not asset:
            raise ValueError(f'最新 release 中没有 {SUB_STORE_ASSET}')
        digest = asset.get('digest') or ''
        data = {
            'version': str(release.get('tag_name', '')).lstrip('vV'),
            'published_at': release.get('published_at', ''),
            'release_url': release.get('html_url', ''),
            'notes': (release.get('body') or '')[:2000],
            'download_url': asset.get('browser_download_url', ''),
            'size': asset.get('size') or 0,
            'sha256': digest.split(':', 1)[1] if digest.startswith('sha256:') else '',
        }
        _latest_cache.update(checked_at=now, data=data, error='')
        return data, ''
    except Exception as e:
        error = f'检查更新失败：{safe_exception_details(e)}'
        logger.warning('Sub-Store %s', error)
        _latest_cache.update(checked_at=now, error=error)
        # 网络失败时仍返回上次成功的结果，避免页面空白
        return _latest_cache['data'], error


def get_update_state():
    return dict(_update_state)


def get_sub_store_status(force_check=False):
    from backend.utils.sub_store_client import _get_base_url

    builtin = is_builtin_available()
    running_version = fetch_running_version(_get_base_url(), timeout=1 if builtin else 5)
    bundle_dir, bundle_version = active_bundle() if builtin else ('', '')
    current = running_version or bundle_version
    latest, latest_error = fetch_latest_release(force=force_check)
    latest_version = latest['version'] if latest else ''

    return {
        'key': 'sub-store',
        'name': 'Sub-Store',
        'description': '订阅解析与节点格式转换',
        'homepage': f'https://github.com/{SUB_STORE_REPO}',
        'running': bool(running_version),
        'lifecycle': runtime.status(bool(running_version)),
        'current_version': current,
        'builtin_version': _read_version_file(SUB_STORE_BUILTIN_DIR) if builtin else '',
        'online_updated': bool(bundle_dir) and bundle_dir != SUB_STORE_BUILTIN_DIR,
        'runtime': _node_version() if builtin else '',
        'latest_version': latest_version,
        'latest_published_at': latest['published_at'] if latest else '',
        'release_url': latest['release_url'] if latest else '',
        'release_notes': latest['notes'] if latest else '',
        'check_error': latest_error,
        'has_update': bool(current) and is_newer(latest_version, current),
        # 本地开发环境没有内置 bundle，只做检测
        'updatable': builtin,
        'update': get_update_state(),
    }


def _restart_sub_store():
    result = subprocess.run(
        ['supervisorctl', '-c', '/etc/supervisor/conf.d/supervisord.conf', 'restart', SUB_STORE_PROGRAM],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stdout + result.stderr).strip() or 'supervisorctl restart 失败')


def _wait_for_version(expected, timeout=40):
    deadline = time.time() + timeout
    version = ''
    while time.time() < deadline:
        version = fetch_running_version(SUB_STORE_BUILTIN_URL, timeout=3)
        if version and (not expected or parse_version(version) == parse_version(expected)):
            return version
        time.sleep(1)
    return version


def _download_bundle(release, dest):
    url = _download_url(release['download_url'])
    sha = hashlib.sha256()
    size = 0
    with requests.get(url, stream=True, timeout=(15, 120), **_request_options()) as resp:
        resp.raise_for_status()
        with open(dest, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                if chunk:
                    f.write(chunk)
                    sha.update(chunk)
                    size += len(chunk)
    if size < _MIN_BUNDLE_SIZE:
        raise DependencyUpdateError(f'下载的文件过小（{size} 字节），可能不是有效的 Sub-Store 后端')
    if release.get('sha256') and sha.hexdigest() != release['sha256']:
        raise DependencyUpdateError('文件校验失败（SHA256 不匹配），请检查 GitHub 代理设置')
    check = subprocess.run(['node', '--check', dest], capture_output=True, text=True, timeout=60)
    if check.returncode != 0:
        raise DependencyUpdateError(f'下载的文件不是有效的 JavaScript：{check.stderr.strip()[:200]}')


def _set_state(state, message='', **extra):
    _update_state.update(state=state, message=message, **extra)
    if state in ('success', 'failed'):
        _update_state['finished_at'] = time.time()


def _run_update(release):
    try:
        with runtime.maintenance():
            _install_release(release)
    except Exception as exc:
        logger.warning('Sub-Store 更新等待失败: %s', safe_exception_details(exc))
        _set_state('failed', 'Sub-Store 正忙或服务不可用，请稍后重试')
    finally:
        _update_lock.release()


def _install_release(release):
    runtime_dir = sub_store_runtime_dir()
    bundle = os.path.join(runtime_dir, SUB_STORE_ASSET)
    version_file = os.path.join(runtime_dir, 'VERSION')
    tmp_bundle = os.path.join(runtime_dir, 'sub-store.download.js')  # node --check 需要 .js 后缀
    backup_dir = runtime_dir + '.bak'
    target = release['version']
    replaced = False
    had_previous = False
    previous_version = active_bundle()[1]
    try:
        os.makedirs(runtime_dir, exist_ok=True)
        _set_state('running', f'正在下载 Sub-Store {target}…')
        _download_bundle(release, tmp_bundle)

        # 保留旧的在线更新版本以便失败回滚
        shutil.rmtree(backup_dir, ignore_errors=True)
        had_previous = os.path.isfile(bundle)
        if had_previous:
            shutil.copytree(runtime_dir, backup_dir, ignore=shutil.ignore_patterns('*.download.js'))

        os.replace(tmp_bundle, bundle)
        replaced = True
        with open(version_file, 'w', encoding='utf-8') as f:
            f.write(target + '\n')

        _set_state('running', f'正在重启 Sub-Store {target}…')
        _restart_sub_store()
        running = _wait_for_version(target)
        if running and parse_version(running) == parse_version(target):
            shutil.rmtree(backup_dir, ignore_errors=True)
            logger.info('Sub-Store 已在线更新：%s → %s', previous_version or '内置版本', target)
            _set_state('success', f'Sub-Store 已更新到 {target}')
            return

        raise DependencyUpdateError(f'Sub-Store {target} 启动后未就绪')
    except Exception as e:
        logger.warning('Sub-Store 在线更新失败: %s', safe_exception_details(e))
        try:
            os.remove(tmp_bundle)
        except OSError:
            pass
        detail = str(e) if isinstance(e, DependencyUpdateError) else safe_exception_details(e)
        if replaced:
            try:
                shutil.rmtree(runtime_dir)
                if had_previous:
                    os.replace(backup_dir, runtime_dir)
                _restart_sub_store()
                restored = _wait_for_version(previous_version, timeout=30)
                if not restored or (previous_version and parse_version(restored) != parse_version(previous_version)):
                    raise DependencyUpdateError('原版本仍未就绪')
                detail += f'；已回滚到 {restored}'
                logger.info('Sub-Store 已回滚到 %s', restored)
            except Exception as rollback_error:
                logger.warning('Sub-Store 回滚失败: %s', safe_exception_details(rollback_error))
                detail += '；回滚未完成，请检查容器日志'
        _set_state('failed', f'更新失败：{detail}')


def start_sub_store_update():
    """启动后台更新任务。返回 (ok, message)"""
    if not is_builtin_available():
        return False, '当前环境没有内置 Sub-Store，无法在线更新'
    if not _update_lock.acquire(blocking=False):
        return False, '已有更新任务在进行中'
    try:
        release, error = fetch_latest_release(force=True)
        if not release:
            raise RuntimeError(error or '无法获取最新版本')
        current = fetch_running_version(SUB_STORE_BUILTIN_URL) or active_bundle()[1]
        if current and not is_newer(release['version'], current):
            raise RuntimeError(f'当前已是最新版本 {current}')
        _set_state('running', f'准备更新到 {release["version"]}…',
                   target_version=release['version'], finished_at=0.0)
        threading.Thread(target=_run_update, args=(release,), daemon=True,
                         name='sub-store-update').start()
        return True, f'开始更新到 {release["version"]}'
    except Exception as e:
        _update_lock.release()
        return False, str(e)


def list_dependencies(force_check=False):
    return [get_sub_store_status(force_check=force_check)]
