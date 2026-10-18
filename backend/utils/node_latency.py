"""Real proxy latency through an independent, on-demand worker.

The worker uses complete node credentials, never the running Mihomo controller.
Legacy TCP results are excluded from proxy-latency history.
"""
import json
import os
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional
from urllib.parse import urlsplit

from backend.common.config import get_repository, get_shared_config
from backend.common.config_repository import ProfileRepositoryError
from backend.utils.logger import get_logger
from backend.utils.subscription_cache import load_subscription_cache
from backend.utils.subscription_filter import filter_subscription_items

logger = get_logger(__name__)
LATENCY_FILE = 'node_latency.json'
METHOD = 'proxy_http_v1'
HISTORY_SIZE = 14
MAX_WORKERS = 5
MAX_NODES = 20  # The UI sends batches of five, below ordinary HTTP timeouts.
_test_lock = threading.Lock()


class ProbeUnavailable(RuntimeError):
    pass


class ProbeBusy(RuntimeError):
    pass


def settings() -> Dict[str, Any]:
    url = os.environ.get('CONFIGFLOW_NODE_TEST_URL', 'https://www.gstatic.com/generate_204').strip()
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError
        _ = parsed.port
        timeout = int(os.environ.get('CONFIGFLOW_NODE_TEST_TIMEOUT_MS', '5000'))
        if not 500 <= timeout <= 10000:
            raise ValueError
    except ValueError as exc:
        raise ProbeUnavailable('节点测速设置无效，请检查测试地址及超时（500–10000 毫秒）') from exc
    return {'url': url, 'timeout_ms': timeout,
            'unified_delay': os.environ.get('CONFIGFLOW_NODE_TEST_UNIFIED_DELAY', '1').lower() not in ('0', 'false')}


def collect_candidates() -> Dict[str, Dict[str, Any]]:
    """Preserve credentials and transport options; manual nodes win name collisions."""
    config = get_shared_config()
    candidates = {}

    def add(node):
        if isinstance(node, dict) and node.get('enabled') is not False and node.get('name'):
            candidates.setdefault(node['name'], node)

    for node in config.get('nodes', []):
        add(node)
    for sub in config.get('subscriptions', []):
        if sub.get('enabled') is False:
            continue
        cache = load_subscription_cache(sub.get('id', '')) or {}
        for node in filter_subscription_items(sub, cache.get('nodes', []) or []):
            add(node)
    return candidates


def _proxy_of(node):
    from backend.converters.mihomo import convert_node_to_mihomo
    from backend.utils.proxy_utils import fix_proxy_fields

    try:
        node = deepcopy(node)
        if node.get('proxy_string') or 'params' in node:
            proxy = convert_node_to_mihomo(node)
        else:
            proxy = node
            fix_proxy_fields(proxy)
        if not isinstance(proxy, dict) or not proxy.get('type'):
            return None
        proxy['name'] = node['name']
        return proxy
    except Exception:  # A malformed node must not prevent testing the others.
        return None


def _probe_binary() -> str:
    configured = os.environ.get('CONFIGFLOW_NODE_PROBE_BIN')
    local = Path(__file__).resolve().parents[1] / 'node-probe' / 'configflow-node-probe'
    paths = [configured] if configured else [
        '/opt/configflow/node-probe/configflow-node-probe', str(local),
        shutil.which('configflow-node-probe'),
    ]
    for path in paths:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return str(path)
    raise ProbeUnavailable('独立测速组件未安装，请更新 ConfigFlow 镜像；源码部署请先构建 backend/node-probe')


def _run_probe(proxies, options):
    binary = _probe_binary()
    payload = json.dumps({**options, 'nodes': proxies}, ensure_ascii=False)
    if len(payload.encode()) > 8 * 1024 * 1024:
        raise ValueError('本批节点配置过大，请减少每批节点数')
    env = dict(os.environ)
    env.setdefault('GOMAXPROCS', '2')
    env.setdefault('GOMEMLIMIT', '128MiB')
    budget = ((len(proxies) + MAX_WORKERS - 1) // MAX_WORKERS) * options['timeout_ms'] / 1000 + 5
    try:
        process = subprocess.run([binary], input=payload, capture_output=True, text=True,
                                 timeout=budget, env=env)
    except subprocess.TimeoutExpired as exc:
        raise ProbeUnavailable('测速组件执行超时，已结束本批测试，请重试') from exc
    except OSError as exc:
        raise ProbeUnavailable('测速组件无法启动，请检查可执行权限和 CPU 架构') from exc
    # Do not expose worker stderr or the input (both may contain node credentials).
    if process.returncode:
        raise ProbeUnavailable('测速组件异常退出，请检查组件版本后重试')
    try:
        response = json.loads(process.stdout)
        if response['version'] != 1 or not isinstance(response['results'], list):
            raise ValueError
        results = {entry['name']: entry for entry in response['results']}
        if set(results) != {proxy['name'] for proxy in proxies}:
            raise ValueError
        for entry in results.values():
            latency = entry['latency']
            if latency is not None and (type(latency) is not int or latency < 1):
                raise ValueError
            if latency is None and not entry.get('error'):
                raise ValueError
        return results
    except (ValueError, TypeError, KeyError) as exc:
        raise ProbeUnavailable('测速组件返回了无效结果，请检查组件版本') from exc


def load_results() -> Dict[str, Any]:
    try:
        data = get_repository().read_shared_json(LATENCY_FILE)
        if not isinstance(data, dict):
            return {}
        return {name: entry for name, entry in data.items()
                if isinstance(entry, dict) and entry.get('method') == METHOD}
    except (OSError, ValueError, ProfileRepositoryError):
        return {}


def _save_results(results):
    try:
        get_repository().write_shared_json(LATENCY_FILE, results)
    except (OSError, ProfileRepositoryError):
        logger.error('写入节点延迟结果失败')


def run_tests(names: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    if not _test_lock.acquire(blocking=False):
        raise ProbeBusy('已有节点测速正在进行，请稍后重试')
    try:
        return _run_tests(names)
    finally:
        _test_lock.release()


def _run_tests(names):
    candidates = collect_candidates()
    requested = list(dict.fromkeys(candidates if names is None else names))
    if len(requested) > MAX_NODES:
        raise ValueError(f'单次最多测试 {MAX_NODES} 个节点，请分批测试')
    wanted = [name for name in requested if name in candidates]
    options = settings()
    measured = {}
    if wanted:
        _probe_binary()  # Fail before conversion if the component is missing.
        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(wanted))) as pool:
            converted = list(pool.map(lambda name: _proxy_of(candidates[name]), wanted))
        proxies = []
        for name, proxy in zip(wanted, converted):
            if proxy:
                proxies.append(proxy)
            else:
                measured[name] = {'latency': None, 'error_code': 'config',
                                  'error': '无法解析完整节点配置，请检查格式或 Sub-Store 服务'}
        if proxies:
            measured.update(_run_probe(proxies, options))

    stamp = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    stored = load_results()
    for name, measurement in measured.items():
        entry = stored.get(name, {})
        same_test = entry.get('url') == options['url'] and entry.get('unified_delay') == options['unified_delay']
        history = list(entry.get('history') or [])[-(HISTORY_SIZE - 1):] if same_test else []
        history.append(measurement['latency'])
        stored[name] = {key: value for key, value in measurement.items() if key != 'name'}
        stored[name].update(method=METHOD, tested_at=stamp, history=history, **options)
    stored = {name: entry for name, entry in stored.items() if name in candidates}
    _save_results(stored)
    return {'tested_at': stamp, 'results': {name: stored[name] for name in measured},
            'missing': [name for name in requested if name not in candidates], 'settings': options}
