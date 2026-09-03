"""认证相关路由"""
import re
from flask import request, jsonify
from backend.routes import auth_bp
from backend.common.internal_call import is_internal_call
from backend.common.auth import is_auth_enabled, generate_token, parse_bearer_token, verify_token, require_auth, ADMIN_USERNAME, ADMIN_PASSWORD, JWT_EXPIRATION_HOURS

@auth_bp.route('/status', methods=['GET'])
def auth_status():
    return jsonify({'authEnabled': is_auth_enabled()})

@auth_bp.route('/login', methods=['POST'])
def login():
    try:
        data = request.json
        username, password = data.get('username'), data.get('password')
        if not is_auth_enabled():
            return jsonify({'success': False, 'message': 'Authentication is not enabled'}), 400
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            return jsonify({'success': True, 'token': generate_token(username), 'username': username, 'expiresIn': JWT_EXPIRATION_HOURS * 3600})
        return jsonify({'success': False, 'message': 'Invalid username or password'}), 401
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@auth_bp.route('/verify', methods=['GET'])
@require_auth
def verify():
    return jsonify({'success': True, 'message': 'Token is valid'})

def setup_before_request(app):
    @app.before_request
    def before_request_auth():
        if is_internal_call() or not is_auth_enabled():
            return None
        public_exact_paths = {
            '/api/auth/status', '/api/auth/login', '/api/config/mihomo', '/api/config/surge', '/api/config/mosdns',
            '/api/mosdns/rule-proxy',
            '/api/agents/register', '/api/agents/install-script', '/api/agents/docker-compose', '/api/agents/docker-run',
            '/api/agents/docker-mihomo-compose', '/api/agents/docker-mihomo-run', '/api/agents/docker-mosdns-compose',
            '/api/agents/docker-mosdns-run', '/api/version', '/', '/mcp',
        }
        public_prefixes = ('/api/rule-library/content/', '/api/rules/local/', '/mcp/', '/api/static/agents/', '/assets/', '/static/')
        if request.path.startswith('/api/agents/') and request.path.endswith('/heartbeat'):
            return None
        profile_public_patterns = (
            r'^/api/config/[A-Za-z0-9][A-Za-z0-9_-]{0,63}/(?:mihomo|surge|mosdns)$',
            r'^/api/profiles/[A-Za-z0-9][A-Za-z0-9_-]{0,63}/mosdns/rule-proxy$',
            r'^/api/profiles/[A-Za-z0-9][A-Za-z0-9_-]{0,63}/(?:subscriptions/[^/]+/proxies|aggregations/[^/]+/provider|rules/local/[^/]+|rule-library/content/[^/]+)$',
        )
        is_public = request.path in public_exact_paths or any(request.path.startswith(path) for path in public_prefixes)
        is_public = is_public or any(re.match(pattern, request.path) for pattern in profile_public_patterns)
        if is_public:
            return None
        token = parse_bearer_token(request.headers.get('Authorization'))
        if token is None:
            return jsonify({'success': False, 'message': 'Unauthorized'}), 401
        payload = verify_token(token)
        if not payload or (isinstance(payload, dict) and 'error' in payload):
            return jsonify({'success': False, 'message': 'Invalid or expired token'}), 401
        return None
