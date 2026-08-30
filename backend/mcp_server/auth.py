"""MCP 端点的认证

复用 ConfigFlow 既有的凭证，不引入新的证书体系：

- 系统设置里的配置令牌。支持 Authorization: Bearer <token> 和
  ?token=<token> 两种带法，因为多数 MCP 客户端只能配其中一种。
- 启用账号密码登录时，也接受前端签发的 JWT。
- 两者都没有配置时放行，与现有「认证完全可选」的行为一致。

安全须知（有意为之的取舍，勿在不了解影响时"顺手修正"）：
配置令牌同时也是订阅链接令牌（前端会把它拼进订阅 URL 分发到各代理客户端
设备），而 MCP 工具可以导出整份配置、重置系统、卸载 Agent。也就是说
**持有订阅链接即等同持有管理员权限**。这是项目所有者在知悉该影响后作出的
选择，理由是订阅链接不对外分享。若日后订阅链接需要外发，应改用独立的
MCP 令牌，而不是继续共用此令牌。
"""
from flask import request

from backend.common.auth import is_auth_enabled, verify_token


def _config_token() -> str:
    from backend.common.config import get_config

    return (get_config().get('system_config', {}) or {}).get('config_token', '') or ''


def _bearer() -> str:
    header = request.headers.get('Authorization', '')
    if header.startswith('Bearer '):
        return header[len('Bearer '):].strip()
    return ''


def _is_valid_jwt(token: str) -> bool:
    if not token:
        return False
    payload = verify_token(token)
    return bool(payload) and not (isinstance(payload, dict) and 'error' in payload)


def authenticate() -> bool:
    """校验当前 MCP 请求，返回是否放行"""
    config_token = _config_token()
    bearer = _bearer()

    # 配置令牌（两种带法）
    if config_token and (bearer == config_token or request.args.get('token', '') == config_token):
        return True

    # 启用账号密码登录时，前端签发的 JWT 同样可用
    if is_auth_enabled():
        return _is_valid_jwt(bearer)

    # 未启用登录：设了配置令牌就必须带对，没设则放行
    return not config_token
