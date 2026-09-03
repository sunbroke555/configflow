"""集合排序工具

排序接口原本只接受「整份集合的完整对象数组」，调用方必须把列表接口读到的数据原样回写。
列表接口返回的却是加工过的展示数据（拼过域名的 URL、缓存计数、脱敏后的字段），
回写就会把这些加工结果写进配置。按 id 排序可以让服务端在存量数据上重排，避免这个问题。
"""
from typing import Any, Dict, List, Tuple


def reorder_by_ids(
    items: List[Dict[str, Any]],
    ids: List[str],
    position: str = 'top',
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """把 ids 指定的条目按给定顺序移到集合一端，其余条目保持原有相对顺序

    Args:
        items: 存量集合
        ids: 要移动的条目 id，按目标先后顺序排列
        position: 'top' 移到最前，'bottom' 移到最后

    Returns:
        (重排后的集合, 在集合中找不到的 id 列表)
    """
    by_id = {item.get('id'): item for item in items if isinstance(item, dict)}
    missing = [item_id for item_id in ids if item_id not in by_id]
    if missing:
        return items, missing

    selected = set(ids)
    moved = [by_id[item_id] for item_id in ids]
    rest = [item for item in items if not (isinstance(item, dict) and item.get('id') in selected)]
    ordered = moved + rest if position == 'top' else rest + moved
    return ordered, []


def resolve_new_order(
    current: List[Dict[str, Any]],
    body: Dict[str, Any],
    object_key: str,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """解析排序请求体：优先按 id 在存量数据上重排，否则沿用整份对象数组的旧格式

    Returns:
        (重排后的集合, 无法处理的 id / 错误说明列表)
    """
    ids = body.get('ids')
    if ids is None:
        return body.get(object_key, []), []
    if not isinstance(ids, list):
        return current, ['ids 必须是数组']
    return reorder_by_ids(current, ids, body.get('position', 'top'))
