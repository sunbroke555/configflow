"""统计数据路由"""
from flask import jsonify
from backend.routes import Blueprint
from backend.common.config import get_config, get_shared_config

stats_bp = Blueprint('stats', __name__, url_prefix='/api/stats')


def get_data_count(data_type):
    """获取各类数据的数量"""
    config = get_shared_config() if data_type in ('subscriptions', 'nodes') else get_config()

    if data_type == 'subscriptions':
        return len(config.get('subscriptions', []))
    elif data_type == 'nodes':
        return len(config.get('nodes', []))
    elif data_type == 'rules':
        # 规则存储在 rule_configs 字段中
        return len(config.get('rule_configs', []))
    elif data_type == 'proxy_groups':
        return len(config.get('proxy_groups', []))

    return 0


@stats_bp.route('/overview', methods=['GET'])
def get_overview():
    """获取总览统计数据"""
    try:
        overview_data = {
            'subscriptions': {
                'total': get_data_count('subscriptions'),
                'icon': '📝',
                'color': '#6B73FF'
            },
            'nodes': {
                'total': get_data_count('nodes'),
                'icon': '🌐',
                'color': '#4ECDC4'
            },
            'rules': {
                'total': get_data_count('rules'),
                'icon': '⚡',
                'color': '#F7B731'
            },
            'proxyGroups': {
                'total': get_data_count('proxy_groups'),
                'icon': '🔗',
                'color': '#FF6B9D'
            }
        }

        return jsonify({
            'success': True,
            'data': overview_data
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500
