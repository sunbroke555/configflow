"""设置相关路由"""
from flask import request, jsonify, current_app

from backend.routes import settings_bp
from backend.common.auth import require_auth
from backend.common.utils import generate_random_token
from backend.common.config import get_repository, get_system_config, save_system_config
from backend.common.config_export import contains_internal_rule_proxy_token
from backend.version import get_version_info
from backend.utils.rule_fetch import normalize_rule_fetch_proxy
from backend.utils.webdav_backup import describe_webdav_error


@settings_bp.route('/settings/rule-fetch-proxy', methods=['GET', 'POST'])
@require_auth
def handle_rule_fetch_proxy():
    """Global HTTP proxy for rule downloads and their connectivity checks."""
    config_data = get_system_config()
    if request.method == 'GET':
        return jsonify({
            'rule_fetch_proxy': config_data.get('system_config', {}).get('rule_fetch_proxy', '')
        })

    data = request.get_json(silent=True)
    if not isinstance(data, dict) or 'rule_fetch_proxy' not in data:
        return jsonify({'success': False, 'message': '请提供规则下载代理，留空可清除设置'}), 400

    try:
        proxy = normalize_rule_fetch_proxy(data['rule_fetch_proxy'])
    except ValueError:
        return jsonify({
            'success': False,
            'message': '规则下载代理须为有效的 HTTP/HTTPS 代理地址，或留空使用现有网络',
        }), 400

    try:
        config_data.setdefault('system_config', {})['rule_fetch_proxy'] = proxy
        save_system_config(config_data)
    except Exception as error:
        current_app.logger.error(
            'Failed to update rule download proxy: exception_type=%s', type(error).__name__
        )
        return jsonify({'success': False, 'message': '保存规则下载代理失败'}), 500

    current_app.logger.info('Rule download proxy %s', 'enabled' if proxy else 'cleared')
    return jsonify({'success': True, 'rule_fetch_proxy': proxy})


@settings_bp.route('/server-domain', methods=['GET', 'POST'])
@require_auth
def handle_server_domain():
    """服务域名管理"""
    config_data = get_system_config()

    if request.method == 'GET':
        # 获取当前服务域名
        server_domain = config_data.get('system_config', {}).get('server_domain', '')
        return jsonify({'server_domain': server_domain})

    elif request.method == 'POST':
        # 更新服务域名
        try:
            data = request.json
            new_domain = data.get('new_domain', '').strip()

            if not new_domain:
                return jsonify({'success': False, 'message': 'New domain is required'}), 400

            # 确保 system_config 存在
            if 'system_config' not in config_data:
                config_data['system_config'] = {}

            # 更新配置中的服务域名
            config_data['system_config']['server_domain'] = new_domain

            current_app.logger.info(f"Updated server domain to {new_domain}")

            save_system_config(config_data)
            return jsonify({
                'success': True,
                'server_domain': new_domain
            })
        except Exception as e:
            current_app.logger.error(f"Failed to update server domain: {e}")
            return jsonify({'success': False, 'message': str(e)}), 500


@settings_bp.route('/config-token', methods=['GET', 'POST', 'DELETE'])
@require_auth
def handle_config_token():
    """配置令牌管理"""
    config_data = get_system_config()

    if request.method == 'GET':
        # 获取当前配置令牌
        config_token = config_data.get('system_config', {}).get('config_token', '')
        return jsonify({'config_token': config_token})

    elif request.method == 'POST':
        # 更新或生成配置令牌
        try:
            data = request.json
            new_token = data.get('token', '').strip()

            # 如果提供了 generate=true，生成随机令牌
            if data.get('generate', False):
                new_token = generate_random_token()

            # 如果没有提供令牌且不生成，返回错误
            if not new_token:
                return jsonify({'success': False, 'message': 'Token is required or set generate=true'}), 400

            if contains_internal_rule_proxy_token(new_token):
                return jsonify({
                    'success': False,
                    'message': 'Config token must not contain an internal rule proxy token',
                }), 400

            # 确保 system_config 存在
            if 'system_config' not in config_data:
                config_data['system_config'] = {}

            # 更新配置中的令牌
            config_data['system_config']['config_token'] = new_token

            current_app.logger.info(f"Updated config token")

            save_system_config(config_data)
            return jsonify({
                'success': True,
                'config_token': new_token
            })
        except Exception as e:
            current_app.logger.error(f"Failed to update config token: {e}")
            return jsonify({'success': False, 'message': str(e)}), 500

    elif request.method == 'DELETE':
        # 删除配置令牌（禁用令牌验证）
        try:
            if 'system_config' not in config_data:
                config_data['system_config'] = {}
            config_data['system_config']['config_token'] = ''
            save_system_config(config_data)
            current_app.logger.info("Deleted config token")
            return jsonify({'success': True})
        except Exception as e:
            current_app.logger.error(f"Failed to delete config token: {e}")
            return jsonify({'success': False, 'message': str(e)}), 500


@settings_bp.route('/version', methods=['GET'])
def get_version():
    """获取系统版本信息"""
    return jsonify(get_version_info())


_BACKUP_TEXT_FIELDS = ('webdav_url', 'webdav_username', 'webdav_password', 'webdav_path')


def _backup_request_data():
    data = request.get_json(silent=True)
    message = None
    if not isinstance(data, dict) or not data:
        message = '请求体必须是非空 JSON 对象，请使用 Content-Type: application/json'
    elif not any(key in data for key in (*_BACKUP_TEXT_FIELDS, 'auto_backup')):
        message = '请提供 WebDAV 备份配置字段'
    elif any(key in data and not isinstance(data[key], str) for key in _BACKUP_TEXT_FIELDS):
        message = 'WebDAV 地址、用户名、密码和路径必须是字符串'
    elif 'auto_backup' in data and not isinstance(data['auto_backup'], bool):
        message = 'auto_backup 必须是布尔值'
    if message:
        return None, (jsonify({'success': False, 'message': message}), 400)
    return data, None


@settings_bp.route('/backup/config', methods=['GET', 'POST'])
@require_auth
def backup_config():
    """获取或保存备份配置"""
    config_data = get_system_config()

    if request.method == 'GET':
        # 返回备份配置（密码脱敏）
        backup_data = config_data.get('backup', {})
        return jsonify({
            'webdav_url': backup_data.get('webdav_url', ''),
            'webdav_username': backup_data.get('webdav_username', ''),
            'webdav_password': '******' if backup_data.get('webdav_password') else '',
            'webdav_path': backup_data.get('webdav_path', '/config-flow-backup/'),
            'auto_backup': backup_data.get('auto_backup', False)
        })

    elif request.method == 'POST':
        # 保存备份配置
        data, error = _backup_request_data()
        if error is not None:
            return error
        backup = config_data.setdefault('backup', {})
        # 部分更新保留未提交字段；掩码表示沿用已保存的密码。
        for key in (*_BACKUP_TEXT_FIELDS, 'auto_backup'):
            if key in data and not (key == 'webdav_password' and data[key] == '******'):
                backup[key] = data[key]

        save_system_config(config_data)
        return jsonify({'success': True, 'message': '备份配置已保存'})


@settings_bp.route('/backup/test', methods=['POST'])
@require_auth
def test_backup():
    """测试 WebDAV 连接"""
    data, error = _backup_request_data()
    if error is not None:
        return error
    try:
        from webdav3.client import Client

        config_data = get_system_config()
        webdav_url = data.get('webdav_url', '').rstrip('/')
        webdav_username = data.get('webdav_username', '')
        webdav_password = data.get('webdav_password', '')
        # 如果密码是掩码，从配置中读取真实密码
        if webdav_password == '******':
            webdav_password = config_data.get('backup', {}).get('webdav_password', '')
        webdav_path = data.get('webdav_path', '/config-flow-backup/')

        if not webdav_url or not webdav_username or not webdav_password:
            return jsonify({'success': False, 'message': '请填写完整的 WebDAV 配置'}), 400

        # 创建 WebDAV 客户端
        options = {
            'webdav_hostname': webdav_url,
            'webdav_login': webdav_username,
            'webdav_password': webdav_password,
            'webdav_timeout': 30
        }

        client = Client(options)

        # 测试连接 - 检查根目录
        if not client.check('/'):
            return jsonify({'success': False, 'message': '无法连接到 WebDAV 服务器'}), 400

        # 检查或创建备份目录
        if not webdav_path.startswith('/'):
            webdav_path = '/' + webdav_path
        if not webdav_path.endswith('/'):
            webdav_path = webdav_path + '/'

        # 递归创建目录（包括所有父目录）
        if not client.check(webdav_path):
            # 分解路径，逐级创建目录
            path_parts = [p for p in webdav_path.strip('/').split('/') if p]
            current_path = '/'
            for part in path_parts:
                current_path = current_path + part + '/'
                if not client.check(current_path):
                    try:
                        client.mkdir(current_path)
                    except Exception as mkdir_error:
                        detail = describe_webdav_error(mkdir_error)
                        current_app.logger.warning('WebDAV 目录创建失败：%s', detail)
                        return jsonify({'success': False, 'message': f'无法创建目录 {current_path}：{detail}'}), 400

        return jsonify({'success': True, 'message': 'WebDAV 连接测试成功'})

    except Exception as e:
        detail = describe_webdav_error(e)
        current_app.logger.error('WebDAV 连接测试失败：%s', detail)
        return jsonify({'success': False, 'message': f'连接测试失败：{detail}'}), 500


@settings_bp.route('/backup/now', methods=['POST'])
@require_auth
def backup_now():
    """立即备份配置到 WebDAV"""
    data, error = _backup_request_data()
    if error is not None:
        return error
    try:
        from backend.utils.webdav_backup import upload_backup, WebDAVConfigurationError

        if data.get('webdav_password') == '******':
            data['webdav_password'] = get_system_config().get('backup', {}).get('webdav_password', '')
        try:
            filename = upload_backup(get_repository().export_all(), data)
        except WebDAVConfigurationError as error:
            return jsonify({'success': False, 'message': str(error)}), 400
        return jsonify({'success': True, 'message': f'备份成功：{filename}'})

    except Exception as e:
        # 保留异常类型与 HTTP 状态，避免远端异常正文携带凭证。
        detail = describe_webdav_error(e)
        current_app.logger.error('WebDAV 手动备份失败：%s', detail)
        return jsonify({'success': False, 'message': f'备份失败：{detail}'}), 500


@settings_bp.route('/settings/subscription-aggregation', methods=['GET', 'POST'])
@require_auth
def handle_subscription_aggregation():
    """订阅聚合开关管理"""
    config_data = get_system_config()

    if request.method == 'GET':
        # 获取当前订阅聚合开关状态
        enabled = config_data.get('system_config', {}).get('subscription_aggregation_enabled', False)
        return jsonify({'enabled': enabled})

    elif request.method == 'POST':
        # 更新订阅聚合开关状态
        try:
            data = request.json
            enabled = data.get('enabled', False)

            # 确保 system_config 存在
            if 'system_config' not in config_data:
                config_data['system_config'] = {}

            # 更新配置中的订阅聚合开关
            config_data['system_config']['subscription_aggregation_enabled'] = enabled

            current_app.logger.info(f"Updated subscription aggregation enabled to {enabled}")

            save_system_config(config_data)
            return jsonify({
                'success': True,
                'enabled': enabled
            })
        except Exception as e:
            current_app.logger.error(f"Failed to update subscription aggregation: {e}")
            return jsonify({'success': False, 'message': str(e)}), 500


@settings_bp.route('/settings/dependencies', methods=['GET'])
@require_auth
def list_third_party_dependencies():
    """第三方依赖状态：当前版本、最新版本、是否可在线更新"""
    from backend.utils.dependencies import list_dependencies
    force = request.args.get('refresh') in ('1', 'true')
    return jsonify({'dependencies': list_dependencies(force_check=force)})


@settings_bp.route('/settings/dependencies/sub-store/update', methods=['POST'])
@require_auth
def update_sub_store_dependency():
    """在线更新内置 Sub-Store（后台执行，进度通过依赖列表接口轮询）"""
    from backend.utils.dependencies import start_sub_store_update
    ok, message = start_sub_store_update()
    return jsonify({'success': ok, 'message': message}), (202 if ok else 409)


@settings_bp.route('/settings/dependencies/sub-store/mode', methods=['POST'])
@require_auth
def set_sub_store_mode():
    from backend.utils.sub_store_runtime import RuntimeUnavailable, runtime
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or data.get('mode') not in ('on_demand', 'always'):
        return jsonify({'success': False, 'message': '请选择按需启动或常驻运行'}), 400
    try:
        runtime.set_mode(data['mode'])
        return jsonify({'success': True, 'mode': data['mode']})
    except RuntimeUnavailable as exc:
        return jsonify({'success': False, 'message': str(exc)}), 409
