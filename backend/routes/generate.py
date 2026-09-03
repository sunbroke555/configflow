"""配置生成路由"""
import io
import zipfile

import requests
from flask import request, jsonify, send_file

from backend.routes import generate_bp
from backend.common.auth import require_auth
from backend.common.config import get_config, get_repository
from backend.common.profile_context import resolve_profile_id
from backend.converters.mihomo import generate_mihomo_config
from backend.converters.surge import generate_surge_config
from backend.converters.mosdns import (
    generate_mosdns_config,
    get_mosdns_ruleset_downloads,
    get_mosdns_custom_files,
)


@generate_bp.route('/mihomo', methods=['POST'])
@require_auth
def generate_mihomo():
    """生成 Mihomo 配置"""
    try:
        config_data = get_config()
        # 获取前端传递的 base_url（协议 + 主机 + 端口）
        data = request.get_json() or {}
        base_url = data.get('base_url', '')

        # 传递 base_url 给生成器
        yaml_content = generate_mihomo_config(config_data, base_url=base_url)

        # 保存到数据目录
        output_file = get_repository().write_generated(
            resolve_profile_id(), 'config.yaml', yaml_content
        )

        return send_file(output_file, as_attachment=True, download_name='mihomo.yaml')
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@generate_bp.route('/surge', methods=['POST'])
@require_auth
def generate_surge():
    """生成 Surge 配置"""
    try:
        config_data = get_config()
        # 获取前端传递的 base_url（协议 + 主机 + 端口）
        data = request.get_json() or {}
        base_url = data.get('base_url', '')

        # 传递 base_url 给生成器
        config_content = generate_surge_config(config_data, base_url=base_url)

        # 保存到数据目录
        output_file = get_repository().write_generated(
            resolve_profile_id(), 'config.conf', config_content
        )

        return send_file(output_file, as_attachment=True, download_name='surge.conf')
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@generate_bp.route('/mosdns', methods=['POST'])
@require_auth
def generate_mosdns():
    """生成 MosDNS 配置"""
    try:
        config_data = get_config()
        # 获取前端传递的 base_url（协议 + 主机 + 端口）
        data = request.get_json() or {}
        base_url = (data.get('base_url', '') or '').strip()
        if not base_url:
            scheme = request.headers.get('X-Forwarded-Proto', request.scheme)
            host = request.headers.get('X-Forwarded-Host', request.host)
            base_url = f"{scheme}://{host}"

        # 传递 base_url 给生成器
        yaml_content = generate_mosdns_config(config_data, base_url=base_url)

        # 准备规则及自定义文件
        ruleset_downloads = get_mosdns_ruleset_downloads(config_data, base_url=base_url)
        custom_files = get_mosdns_custom_files(config_data)

        # 构建 ZIP 包含 config.yaml 与 rules 下的所有规则
        zip_buffer = io.BytesIO()
        session = requests.Session()

        def _write_to_zip(zip_file, arcname, content):
            normalized_name = arcname.lstrip('./')
            if not normalized_name:
                return
            if isinstance(content, bytes):
                data_bytes = content
            else:
                data_bytes = content.encode('utf-8')
            zip_file.writestr(normalized_name, data_bytes)

        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            # 写入主配置文件
            zip_file.writestr('config.yaml', yaml_content)

            # 写入自定义规则文件
            for custom_file in custom_files:
                path = custom_file.get('path')
                content = custom_file.get('content', '')
                if not path or content is None:
                    continue
                _write_to_zip(zip_file, path, content)

            # 下载并写入规则集文件
            for download in ruleset_downloads:
                download_url = download.get('url')
                local_path = download.get('local_path')
                if not download_url or not local_path:
                    continue

                if download_url.startswith('/'):
                    download_url = f"{base_url.rstrip('/')}{download_url}"

                try:
                    response = session.get(download_url, timeout=20)
                    response.raise_for_status()
                except requests.exceptions.RequestException as fetch_error:
                    return jsonify({'success': False, 'message': f'规则下载失败: {fetch_error}'}), 500

                _write_to_zip(zip_file, local_path, response.text)

        zip_buffer.seek(0)
        download_name = 'mosdns-config.zip'
        return send_file(zip_buffer, as_attachment=True, download_name=download_name, mimetype='application/zip')
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@generate_bp.route('/mihomo/preview', methods=['POST'])
@require_auth
def preview_mihomo():
    """预览 Mihomo 配置"""
    try:
        config_data = get_config()
        # 获取前端传递的 base_url（协议 + 主机 + 端口）
        data = request.get_json() or {}
        base_url = data.get('base_url', '')

        # 传递 base_url 给生成器
        yaml_content = generate_mihomo_config(config_data, base_url=base_url)
        return jsonify({'content': yaml_content})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@generate_bp.route('/surge/preview', methods=['POST'])
@require_auth
def preview_surge():
    """预览 Surge 配置"""
    try:
        config_data = get_config()
        # 获取前端传递的 base_url（协议 + 主机 + 端口）
        data = request.get_json() or {}
        base_url = data.get('base_url', '')

        # 传递 base_url 给生成器
        config_content = generate_surge_config(config_data, base_url=base_url)
        return jsonify({'content': config_content})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@generate_bp.route('/mosdns/preview', methods=['POST'])
@require_auth
def preview_mosdns():
    """预览 MosDNS 配置"""
    try:
        config_data = get_config()
        # 获取前端传递的 base_url（协议 + 主机 + 端口）
        data = request.get_json() or {}
        base_url = data.get('base_url', '')

        # 传递 base_url 给生成器
        yaml_content = generate_mosdns_config(config_data, base_url=base_url)
        return jsonify({'content': yaml_content})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500
