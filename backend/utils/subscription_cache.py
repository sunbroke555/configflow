"""订阅节点缓存工具"""
import json
import os
from datetime import datetime
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


def _get_cache_path(sub_id: str) -> str:
    """获取缓存文件路径"""
    cache_dir = _ensure_cache_dir()
    safe_id = sub_id.replace('/', '_').replace('\\', '_')
    return os.path.join(cache_dir, f'{safe_id}.json')


def save_subscription_nodes(
    sub_id: str,
    nodes: Any,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """保存订阅节点到本地缓存"""
    cache_path = _get_cache_path(sub_id)
    payload: Dict[str, Any] = {
        'subscription_id': sub_id,
        'updated_at': datetime.now().isoformat() + 'Z',
        'count': len(nodes) if isinstance(nodes, list) else 0,
        'nodes': nodes
    }
    if metadata:
        payload['metadata'] = metadata
    try:
        get_repository().write_shared_json(
            os.path.join('subscribes', os.path.basename(cache_path)), payload,
        )
    except OSError as exc:
        logger.error("写入订阅缓存失败 (%s): %s", sub_id, exc)
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
