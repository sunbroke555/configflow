"""内部 REST 调用封装

MCP 工具不复制业务逻辑，而是在进程内复用现有 REST 路由：
所有既有的参数校验、持久化和副作用都保持单一实现。
"""
import json
from typing import Any, Dict, Optional

from flask import current_app

from backend.common.internal_call import INTERNAL_CALL_HEADER, INTERNAL_CALL_TOKEN


class ApiError(Exception):
    """内部 REST 调用返回非 2xx 时抛出"""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def call_api(
    method: str,
    path: str,
    body: Optional[Dict[str, Any]] = None,
    query: Optional[Dict[str, Any]] = None,
) -> Any:
    """在进程内调用自身的 REST 接口

    Args:
        method: HTTP 方法
        path: 完整路径，如 '/api/subscriptions'
        body: JSON 请求体
        query: URL 查询参数（None 值会被剔除）

    Returns:
        解析后的响应体（dict / list / str）

    Raises:
        ApiError: 状态码非 2xx
    """
    client = current_app.test_client()
    clean_query = {k: v for k, v in (query or {}).items() if v is not None}

    response = client.open(
        path,
        method=method,
        json=body if body is not None else None,
        query_string=clean_query,
        headers={INTERNAL_CALL_HEADER: INTERNAL_CALL_TOKEN},
    )

    # 部分接口（如 MosDNS 配置生成）返回 zip 等二进制，不能按文本解码
    data = response.get_data()
    try:
        raw = data.decode('utf-8')
    except UnicodeDecodeError:
        raw = None

    if raw is None:
        payload = {
            'binary': True,
            'content_type': response.mimetype,
            'size': len(data),
        }
    else:
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = raw

    if response.status_code >= 400:
        raise ApiError(response.status_code, _extract_message(payload, response.status_code))

    return payload


def _extract_message(payload: Any, status_code: int) -> str:
    if isinstance(payload, dict):
        for key in ('message', 'error', 'msg'):
            if payload.get(key):
                return str(payload[key])
    if isinstance(payload, str) and payload.strip():
        return payload.strip()
    return f'HTTP {status_code}'
