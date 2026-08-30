"""认证相关工具模块"""
import os
import secrets
import jwt
from datetime import datetime, timedelta
from functools import wraps
from flask import request, jsonify

from backend.common.internal_call import is_internal_call

# JWT 配置
# 未显式配置时随机生成（进程级），避免使用可预测的硬编码默认密钥
JWT_SECRET_KEY = os.environ.get('JWT_SECRET_KEY') or secrets.token_urlsafe(48)
JWT_ALGORITHM = 'HS256'
JWT_EXPIRATION_HOURS = 24

# 登录配置（从环境变量读取）
ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', '')  # 默认为空表示不需要登录
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '')


def is_auth_enabled():
    """检查是否启用了认证"""
    return bool(ADMIN_USERNAME and ADMIN_PASSWORD)


def generate_token(username):
    """生成 JWT token"""
    payload = {
        'username': username,
        # Use UTC timestamps to keep PyJWT numeric dates consistent across timezones
        'exp': datetime.utcnow() + timedelta(hours=JWT_EXPIRATION_HOURS),
        'iat': datetime.utcnow()
    }
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


def verify_token(token):
    """验证 JWT token"""
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        return {'error': 'expired'}
    except jwt.InvalidTokenError as e:
        return {'error': 'invalid', 'detail': str(e)}


def validate_token_or_jwt(request_obj):
    """验证 JWT token（前端）或 URL query token（外部客户端）

    Args:
        request_obj: Flask request 对象

    Returns:
        dict: {'valid': bool, 'message': str}
    """
    # MCP 层发起的进程内调用，认证已在 /mcp 入口完成
    if is_internal_call():
        return {'valid': True}

    # 2. 检查 URL query token（用于外部客户端）
    from backend.common.config import config_data
    config_token = config_data.get('system_config', {}).get('config_token', '')

    # 如果没有启用认证，直接通过
    if not is_auth_enabled() and not config_token:
        return {'valid': True}

    # 1. 先检查 Authorization header (JWT token)
    auth_header = request_obj.headers.get('Authorization')
    if auth_header and auth_header.startswith('Bearer '):
        token = auth_header.split(' ')[1]
        payload = verify_token(token)
        # 如果 payload 不为 None 且不包含 error 键，说明验证成功
        if payload and not (isinstance(payload, dict) and 'error' in payload):
            return {'valid': True}

    # 如果没有配置 config_token，允许无 token 访问（外部客户端）
    if not config_token:
        return {'valid': True}

    # 如果配置了 config_token，检查 URL query 参数中的 token
    url_token = request_obj.args.get('token', '')
    if url_token and url_token == config_token:
        return {'valid': True}

    return {'valid': False, 'message': 'Invalid or missing authentication'}


def require_auth(f):
    """认证装饰器 - 只有在启用认证时才检查 token"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # MCP 层发起的进程内调用，认证已在 /mcp 入口完成
        if is_internal_call():
            return f(*args, **kwargs)

        # 如果没有设置用户名和密码，则不需要认证（直接放行，忽略任何 token）
        if not is_auth_enabled():
            return f(*args, **kwargs)

        # 认证已启用，检查 token
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return jsonify({'success': False, 'message': 'Unauthorized: Missing or invalid Authorization header'}), 401

        token = auth_header.split(' ')[1]
        payload = verify_token(token)

        # 检查验证结果
        if not payload:
            return jsonify({'success': False, 'message': 'Invalid or expired token'}), 401

        # 如果返回的是错误信息（字典包含 'error' 键）
        if isinstance(payload, dict) and 'error' in payload:
            if payload['error'] == 'expired':
                return jsonify({'success': False, 'message': 'Token expired', 'error': 'token_expired'}), 401
            else:
                return jsonify({'success': False, 'message': f"Invalid token: {payload.get('detail', 'unknown error')}", 'error': 'token_invalid'}), 401

        return f(*args, **kwargs)
    return decorated_function


def validate_required_env_vars():
    """启动时提示认证状态（认证为可选功能，不做强制校验）"""
    if is_auth_enabled():
        return

    print('\n' + '=' * 80)
    print('INFO: Running WITHOUT authentication')
    print('=' * 80)
    print('⚠️  Authentication is DISABLED. Anyone can access the application.')
    print('')
    print('To enable authentication, set:')
    print('  ADMIN_USERNAME=admin')
    print('  ADMIN_PASSWORD=your-password')
    print('  JWT_SECRET_KEY=your-secret-key')
    print('=' * 80 + '\n', flush=True)
