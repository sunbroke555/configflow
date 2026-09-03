"""配置管理路由"""
import os
import json
import io
from flask import request, jsonify, send_file

from backend.routes import config_bp
from backend.common.auth import require_auth, validate_token_or_jwt
from backend.common.config import get_config, save_config
from backend.common.config_export import prepare_config_export
from backend.common.profile_context import resolve_profile_id
from backend.utils.logger import get_logger

logger = get_logger(__name__)
from backend.converters.mihomo import generate_mihomo_config
from backend.converters.surge import generate_surge_config
from backend.converters.mosdns import generate_mosdns_config


def _reject_invalid_config_auth(config_data):
    auth_result = validate_token_or_jwt(request, config_data)
    if auth_result.get('valid'):
        return None
    return jsonify({
        'success': False,
        'message': auth_result.get('message', 'Unauthorized'),
    }), 401


@config_bp.route('/mihomo', methods=['GET'])
def get_mihomo_config():
    """获取 Mihomo 配置内容（通过 URL 访问）

    支持两种授权方式：
    1. 前端请求：使用 Authorization header (Bearer token)
    2. 外部请求：使用 URL 参数 ?token=xxx
    """
    try:
        config_data = get_config(resolve_profile_id(fallback='default'))

        auth_error = _reject_invalid_config_auth(config_data)
        if auth_error:
            return auth_error

        # 获取前端传递的 base_url（协议 + 主机 + 端口）
        # 优先从query参数获取，如果没有则尝试从JSON body获取（兼容POST请求）
        base_url = request.args.get('base_url', '')
        if not base_url:
            data = request.get_json(silent=True) or {}
            base_url = data.get('base_url', '')

        # 生成配置
        yaml_content = generate_mihomo_config(config_data, base_url=base_url)

        # 直接返回内容，不下载
        return yaml_content, 200, {
            'Content-Type': 'text/plain; charset=utf-8',
            'Content-Disposition': 'inline'
        }
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@config_bp.route('/surge', methods=['GET'])
def get_surge_config():
    """获取 Surge 配置内容（通过 URL 访问）

    支持两种授权方式：
    1. 前端请求：使用 Authorization header (Bearer token)
    2. 外部请求：使用 URL 参数 ?token=xxx
    """
    try:
        config_data = get_config(resolve_profile_id(fallback='default'))

        auth_error = _reject_invalid_config_auth(config_data)
        if auth_error:
            return auth_error

        # 获取 base_url（从请求头构建）
        scheme = request.headers.get('X-Forwarded-Proto', request.scheme)
        host = request.headers.get('X-Forwarded-Host', request.host)
        base_url = f"{scheme}://{host}"

        # 生成配置
        config_content = generate_surge_config(config_data, base_url=base_url)

        # 直接返回内容，不下载
        return config_content, 200, {
            'Content-Type': 'text/plain; charset=utf-8',
            'Content-Disposition': 'inline'
        }
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@config_bp.route('/mosdns', methods=['GET'])
def get_mosdns_config():
    """获取 MosDNS 配置内容（通过 URL 访问）

    支持两种授权方式：
    1. 前端请求：使用 Authorization header (Bearer token)
    2. 外部请求：使用 URL 参数 ?token=xxx
    """
    try:
        config_data = get_config(resolve_profile_id(fallback='default'))

        auth_error = _reject_invalid_config_auth(config_data)
        if auth_error:
            return auth_error

        # 获取 base_url（从请求头构建）
        scheme = request.headers.get('X-Forwarded-Proto', request.scheme)
        host = request.headers.get('X-Forwarded-Host', request.host)
        base_url = f"{scheme}://{host}"

        # 生成配置
        yaml_content = generate_mosdns_config(config_data, base_url=base_url)

        # 直接返回内容，不下载
        return yaml_content, 200, {
            'Content-Type': 'text/plain; charset=utf-8',
            'Content-Disposition': 'inline'
        }
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@config_bp.route('/<profile_id>/mihomo', methods=['GET'])
def get_profile_mihomo_config(profile_id):
    return get_mihomo_config()


@config_bp.route('/<profile_id>/surge', methods=['GET'])
def get_profile_surge_config(profile_id):
    return get_surge_config()


@config_bp.route('/<profile_id>/mosdns', methods=['GET'])
def get_profile_mosdns_config(profile_id):
    return get_mosdns_config()


@config_bp.route('/export', methods=['GET'])
@require_auth
def export_config():
    """导出配置为 JSON"""
    config_data = get_config()

    desensitize = request.args.get('desensitize', 'false').lower() == 'true'
    export_data = prepare_config_export(config_data, desensitize=desensitize)
    payload = json.dumps(export_data, ensure_ascii=False, indent=2).encode('utf-8')
    return send_file(
        io.BytesIO(payload),
        as_attachment=True,
        download_name='config_desensitized.json' if desensitize else 'config.json',
        mimetype='application/json',
    )


@config_bp.route('/import', methods=['POST'])
@require_auth
def import_config():
    try:
        if not isinstance(request.json, dict):
            return jsonify({'success': False, 'message': '请求数据必须是 JSON 对象'}), 400
        from backend.common.config import safe_import_config
        safe_import_config(request.json)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@config_bp.route('/reset', methods=['POST'])
@require_auth
def reset_config():
    """重置配置为默认模板"""
    try:
        from backend.common.config import config_data as global_config

        # 读取模板配置
        template_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config_template.json')
        with open(template_path, 'r', encoding='utf-8') as f:
            template_data = json.load(f)

        # 清空当前配置并使用模板数据
        global_config.clear()
        global_config.update(template_data)
        save_config()

        return jsonify({'success': True, 'message': '配置已重置为默认值'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


# 自定义配置路由（使用独立蓝图）

from backend.routes import custom_config_bp


@custom_config_bp.route('/mihomo', methods=['GET', 'POST'])
@require_auth
def handle_custom_mihomo_config():
    """获取或保存 Mihomo 自定义配置"""
    config_data = get_config()

    # 确保 mihomo 字段存在
    if 'mihomo' not in config_data:
        config_data['mihomo'] = {'custom_config': ''}

    if request.method == 'GET':
        # 获取自定义配置（从嵌套结构中读取）
        mihomo_config = config_data['mihomo'].get('custom_config', '')
        return jsonify({'config': mihomo_config})

    elif request.method == 'POST':
        # 保存自定义配置（保存到嵌套结构中）
        try:
            custom_config = request.json.get('config', '')
            config_data['mihomo']['custom_config'] = custom_config
            save_config()
            return jsonify({'success': True})
        except Exception as e:
            return jsonify({'success': False, 'message': str(e)}), 500


@custom_config_bp.route('/surge', methods=['GET', 'POST'])
@require_auth
def handle_custom_surge_config():
    """获取或保存 Surge 自定义配置"""
    config_data = get_config()

    # 确保 surge 字段存在
    if 'surge' not in config_data:
        config_data['surge'] = {'custom_config': ''}

    if request.method == 'GET':
        # 获取自定义配置（从嵌套结构中读取）
        surge_config = config_data['surge']
        return jsonify({
            'config': surge_config.get('custom_config', ''),
            'smart_groups': surge_config.get('smart_groups', [])
        })

    elif request.method == 'POST':
        # 按字段合并更新（而非整体覆盖）
        try:
            data = request.json or {}
            if 'config' in data:
                config_data['surge']['custom_config'] = data['config']
            if 'smart_groups' in data:
                config_data['surge']['smart_groups'] = data['smart_groups']
            save_config()
            return jsonify({'success': True})
        except Exception as e:
            return jsonify({'success': False, 'message': str(e)}), 500


@custom_config_bp.route('/mosdns', methods=['GET', 'POST'])
@require_auth
def handle_custom_mosdns_config():
    """获取或保存 MosDNS 自定义配置"""
    config_data = get_config()

    # 确保 mosdns 字段存在
    if 'mosdns' not in config_data:
        config_data['mosdns'] = {
            'direct_rulesets': [],
            'proxy_rulesets': [],
            'direct_rules': [],
            'proxy_rules': [],
            'local_dns': '',
            'remote_dns': '',
            'fallback_dns': '',
            'default_forward': 'forward_remote',
            'custom_hosts': '',
            'custom_config': ''
        }

    if request.method == 'GET':
        # 获取自定义配置（从嵌套结构中读取）
        mosdns_config = config_data['mosdns'].get('custom_config', '')
        return jsonify({'config': mosdns_config})

    elif request.method == 'POST':
        # 保存自定义配置（保存到嵌套结构中）
        try:
            custom_config = request.json.get('config', '')
            config_data['mosdns']['custom_config'] = custom_config
            save_config()
            return jsonify({'success': True})
        except Exception as e:
            return jsonify({'success': False, 'message': str(e)}), 500
