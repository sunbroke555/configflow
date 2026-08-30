"""进程内调用标记

MCP 层在完成自身认证后，会以 Flask test client 在进程内复用既有 REST 路由。
这些请求带上下面的 header，认证中间件据此跳过重复校验。

token 在进程启动时随机生成，不落盘、不外发，外部无法伪造。
"""
import secrets

from flask import request

INTERNAL_CALL_HEADER = 'X-ConfigFlow-Internal'
INTERNAL_CALL_TOKEN = secrets.token_urlsafe(32)


def is_internal_call() -> bool:
    """当前请求是否为已认证过的进程内调用"""
    try:
        return request.headers.get(INTERNAL_CALL_HEADER) == INTERNAL_CALL_TOKEN
    except RuntimeError:
        # 不在请求上下文中
        return False
