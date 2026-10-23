"""订阅节点缓存工具"""
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from backend.common.config import get_repository
from backend.common.config_repository import ProfileRepositoryError
from backend.utils.logger import get_logger

logger = get_logger(__name__)

def _ensure_cache_dir() -> str:
    """确保缓存目录存在"""
    try:
        os.makedirs(get_repository().shared_cache_dir(), exist_ok=True)
    except OSError as exc:
        logger.warning("创建订阅缓存目录失败: %s", exc)
    return str(get_repository().shared_cache_dir())


def _cache_name(sub_id: str) -> str:
    safe_id = sub_id.replace('/', '_').replace('\\', '_')
    return f'{safe_id}.json'


def _get_cache_path(sub_id: str) -> str:
    """获取缓存文件路径"""
    return os.path.join(_ensure_cache_dir(), _cache_name(sub_id))


def _remove_cache_file(path: str) -> bool:
    removed = False
    for target in (path, path + '.lock'):
        try:
            os.remove(target)
            removed = removed or target == path
        except FileNotFoundError:
            pass
    return removed


def delete_subscription_cache(sub_id: str) -> bool:
    """删除已移除订阅的节点缓存（含节点地址与凭据），返回是否删除了文件。"""
    try:
        return _remove_cache_file(_get_cache_path(sub_id))
    except OSError as exc:
        logger.warning("删除订阅缓存失败 (%s): %s", sub_id, exc)
        return False


def prune_orphan_caches() -> list:
    """删除不属于任何现存订阅的节点缓存，返回被删除的文件名。

    只有成功读到订阅列表时才清理，读取失败不删除任何文件。
    """
    try:
        keep = {_cache_name(item['id']) for item in get_repository().get_shared()['subscriptions']}
        cache_dir = _ensure_cache_dir()
        names = [name for name in os.listdir(cache_dir) if name.endswith('.json') and name not in keep]
    except (OSError, ProfileRepositoryError, KeyError, TypeError) as exc:
        logger.warning("检查订阅缓存失败，跳过清理: %s", exc)
        return []
    removed = []
    for name in names:
        try:
            if _remove_cache_file(os.path.join(cache_dir, name)):
                removed.append(name)
        except OSError as exc:
            logger.warning("删除孤立订阅缓存失败 (%s): %s", name, exc)
    if removed:
        logger.info("已清理 %d 个已删除订阅的节点缓存", len(removed))
    return removed


def save_subscription_nodes(
    sub_id: str,
    nodes: Any,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """保存订阅节点到本地缓存"""
    cache_path = _get_cache_path(sub_id)
    payload: Dict[str, Any] = {
        'subscription_id': sub_id,
        'updated_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'count': len(nodes) if isinstance(nodes, list) else 0,
        'nodes': nodes
    }
    if metadata:
        payload['metadata'] = metadata
        if metadata.get('fetched_at'):
            payload['updated_at'] = metadata['fetched_at']
    previous = load_subscription_cache(sub_id)
    try:
        get_repository().write_shared_json(
            os.path.join('subscribes', os.path.basename(cache_path)), payload,
        )
    except OSError as exc:
        logger.error("写入订阅缓存失败 (%s): %s", sub_id, exc)
        return payload
    if previous is not None and previous.get('count') != payload['count']:
        from backend.utils.logger import log_text
        logger.info("订阅 %s 节点数变化：%s → %s", log_text((metadata or {}).get('subscription_name') or sub_id),
                    previous.get('count'), payload['count'])
    return payload


def load_subscription_cache(sub_id: str) -> Optional[Dict[str, Any]]:
    """加载共享原始缓存，如果不存在则返回 None。"""
    cache_path = _get_cache_path(sub_id)
    if not os.path.exists(cache_path):
        return None

    try:
        data = get_repository().read_shared_json(
            os.path.join('subscribes', os.path.basename(cache_path)),
        )
        # 兼容旧数据，确保 count 存在
        if 'count' not in data and isinstance(data.get('nodes'), list):
            data['count'] = len(data['nodes'])
        return data
    except (OSError, ProfileRepositoryError, json.JSONDecodeError) as exc:
        logger.warning("读取订阅缓存失败 (%s): %s", sub_id, exc)
        return None
