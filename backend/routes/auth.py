"""认证相关路由"""
from flask import request, jsonify
from backend.routes import auth_bp
from backend.common.internal_call import is_internal_call
from backend.common.auth import (
    is_auth_enabled,
    generate_token,
    verify_token,
    require_auth,
    ADMIN_USERNAME,
    ADMIN_PASSWORD,
    JWT_EXPIRATION_HOURS
)


@auth_bp.route('/status', methods=['GET'])
def auth_status():
    """检查认证状态"""
    return jsonify({
        'authEnabled': is_auth_enabled()
    })


@auth_bp.route('/login', methods=['POST'])
def login():
    """用户登录"""
    try:
        data = request.json
        username = data.get('username')
        password = data.get('password')

        # 如果没有启用认证，返回错误
        if not is_auth_enabled():
            return jsonify({'success': False, 'message': 'Authentication is not enabled'}), 400

        # 验证用户名和密码
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            token = generate_token(username)
            return jsonify({
                'success': True,
                'token': token,
                'username': username,
                'expiresIn': JWT_EXPIRATION_HOURS * 3600  # 以秒为单位
            })
        else:
            return jsonify({'success': False, 'message': 'Invalid username or password'}), 401

    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@auth_bp.route('/verify', methods=['GET'])
@require_auth
def verify():
    """验证 token 是否有效"""
    return jsonify({'success': True, 'message': 'Token is valid'})


def setup_before_request(app):
    """设置全局认证中间件"""
    @app.before_request
    def before_request_auth():
        """全局认证检查 - 只在启用认证时生效"""
        # 由 MCP 层发起的进程内调用，认证已在 /mcp 入口完成，不再重复校验
        if is_internal_call():
            return None

        # 如果没有启用认证，跳过
        if not is_auth_enabled():
            return None

        # 不需要认证的路径列表
        public_paths = [
            '/api/auth/status',
            '/api/auth/login',
            '/api/config/mihomo',  # 配置订阅 URL
            '/api/config/surge',
            '/api/config/mosdns',
            '/api/agents/register',  # Agent 注册
            '/api/agents/install-script',  # 安装脚本
            '/api/agents/docker-compose',
            '/api/agents/docker-run',
            '/api/agents/docker-mihomo-compose',  # Docker Mihomo 脚本
            '/api/agents/docker-mihomo-run',
            '/api/agents/docker-mosdns-compose',  # Docker mosdns 脚本
            '/api/agents/docker-mosdns-run',
            '/api/rule-library/content/',  # 规则库内容
            '/api/rules/local/',  # 本地规则文件（供 Mihomo 访问）
            '/api/mosdns/rule-proxy',  # MosDNS 规则代理
            '/api/version',  # 版本信息
            '/mcp',  # MCP 端点（在 mcp_server.auth 中自行校验 JWT / 配置令牌）
            '/api/static/agents/',  # Agent 二进制文件下载
            '/',  # 前端首页
        ]

        # Agent 心跳路径（动态匹配）
        if request.path.startswith('/api/agents/') and request.path.endswith('/heartbeat'):
            return None

        # 检查是否是公共路径
        is_public = any(request.path.startswith(path) for path in public_paths)
        if is_public:
            return None

        # 静态文件不需要认证
        if request.path.startswith('/assets/') or request.path.startswith('/static/'):
            return None

        # 检查 token
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return jsonify({'success': False, 'message': 'Unauthorized'}), 401

        token = auth_header.split(' ')[1]
        payload = verify_token(token)

        if not payload:
            return jsonify({'success': False, 'message': 'Invalid or expired token'}), 401

        return None
