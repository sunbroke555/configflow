"""Real proxy latency through an independent, on-demand worker.

The worker uses complete node credentials, never the running Mihomo controller.
Results from earlier probe semantics are excluded from proxy-latency history.
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
METHOD = 'proxy_http_v2'
HISTORY_SIZE = 14
MAX_NODES = 20
MAX_WORKERS = MAX_NODES  # Keep in sync with the bounded node-probe batch.
MAX_CONVERSION_WORKERS = 5  # Avoid flooding Sub-Store for URI-only nodes.
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
    """Preserve complete nodes and distinguish equal names from different sources."""
    config = get_shared_config()
    candidates = {}

    def add(node, identity):
        if isinstance(node, dict) and node.get('enabled') is not False and node.get('name'):
            key = json.dumps(identity, ensure_ascii=False, separators=(',', ':'))
            candidates.setdefault(key, node)

    for node in config.get('nodes', []):
        if isinstance(node, dict) and node.get('id'):
            add(node, ['node', node['id']])
    for sub in config.get('subscriptions', []):
        if sub.get('enabled') is False or not sub.get('id'):
            continue
        cache = load_subscription_cache(sub['id']) or {}
        for node in filter_subscription_items(sub, cache.get('nodes', []) or []):
            if isinstance(node, dict):
                add(node, ['subscription', sub['id'], node.get('name')])
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


def _needs_sub_store(node):
    from backend.converters.mihomo import _parse_structured_proxy_string

    raw = node.get('proxy_string')
    return isinstance(raw, str) and bool(raw) and _parse_structured_proxy_string(raw) is None


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
        if response['version'] != 2:
            raise ProbeUnavailable('测速组件版本不匹配，请更新 ConfigFlow 镜像；源码部署请重新构建 backend/node-probe')
        if not isinstance(response['results'], list):
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


def run_tests(names: Optional[Iterable[str]] = None, *, keys: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    if not _test_lock.acquire(blocking=False):
        raise ProbeBusy('已有节点测速正在进行，请稍后重试')
    try:
        return _run_tests(names, keys)
    finally:
        _test_lock.release()


def _run_tests(names, keys):
    candidates = collect_candidates()
    if names is not None and keys is not None:
        raise ValueError('names 与 keys 不能同时指定')
    requested_ids = names if names is not None else keys
    requested = list(dict.fromkeys(candidates if requested_ids is None else requested_ids))
    if len(requested) > MAX_NODES:
        raise ValueError(f'单次最多测试 {MAX_NODES} 个节点，请分批测试')
    # Explicit legacy names requests remain supported only when unambiguous.
    # Responses use the requested names, but all persisted records use source keys.
    identities = {key: key for key in requested if key in candidates}
    if names is not None:
        by_name = {}
        for key, node in candidates.items():
            by_name.setdefault(node['name'], []).append(key)
        if any(len(by_name.get(name, [])) > 1 for name in requested):
            raise ValueError('存在同名节点，请使用 keys 指定节点来源后重试')
        identities = {name: by_name[name][0] for name in requested if name in by_name}
    wanted = list(identities.values())
    # Keep URI conversion within one bounded wave even when local nodes fill a
    # 20-node probe batch. Four sequential conversion waves can outlast HTTP.
    if sum(_needs_sub_store(candidates[key]) for key in wanted) > MAX_CONVERSION_WORKERS:
        raise ValueError(f'单次最多测试 {MAX_CONVERSION_WORKERS} 个需要 Sub-Store 转换的节点，请分批测试')
    options = settings()
    measured = {}
    if wanted:
        _probe_binary()  # Fail before conversion if the component is missing.
        with ThreadPoolExecutor(max_workers=min(MAX_CONVERSION_WORKERS, len(wanted))) as pool:
            converted = list(pool.map(lambda key: _proxy_of(candidates[key]), wanted))
        proxies = []
        for key, proxy in zip(wanted, converted):
            if proxy:
                # The worker uses its name only to correlate a result with input.
                # Source keys prevent same-name nodes from overwriting each other.
                proxy['name'] = key
                proxies.append(proxy)
            else:
                measured[key] = {'latency': None, 'error_code': 'config',
                                 'error': '无法解析完整节点配置，请检查格式或 Sub-Store 服务'}
        if proxies:
            measured.update(_run_probe(proxies, options))

    stamp = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    stored = load_results()
    for key, measurement in measured.items():
        entry = stored.get(key, {})
        same_test = entry.get('url') == options['url'] and entry.get('unified_delay') == options['unified_delay']
        history = list(entry.get('history') or [])[-(HISTORY_SIZE - 1):] if same_test else []
        history.append(measurement['latency'])
        stored[key] = {field: value for field, value in measurement.items() if field != 'name'}
        stored[key].update(method=METHOD, tested_at=stamp, history=history, **options)
    stored = {key: entry for key, entry in stored.items() if key in candidates}
    _save_results(stored)
    return {'tested_at': stamp, 'results': {requested_id: stored[key] for requested_id, key in identities.items()},
            'missing': [requested_id for requested_id in requested if requested_id not in identities], 'settings': options}
