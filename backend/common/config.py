"""配置管理模块"""
import os
import json
from typing import Dict, Any

from backend.common.utils import get_local_ip
from backend.common.resource import get_backend_resource
from backend.utils.logger import get_logger

# 获取当前模块的日志记录器
logger = get_logger(__name__)

# 配置存储文件
# 优先使用环境变量指定的路径，否则使用默认路径
DATA_DIR = os.environ.get('DATA_DIR', '/data')
if not os.path.exists(DATA_DIR):
    DATA_DIR = '.'  # 开发模式，使用当前目录
CONFIG_FILE = os.path.join(DATA_DIR, 'config.json')
AGGREGATION_PROVIDERS_DIR = os.path.join(DATA_DIR, 'providers')

# 确保聚合 providers 目录存在
if not os.path.exists(AGGREGATION_PROVIDERS_DIR):
    os.makedirs(AGGREGATION_PROVIDERS_DIR)

# 全局配置初始化函数
def get_default_config() -> Dict[str, Any]:
    """获取默认配置，根据专业版权限决定包含哪些字段"""

    # 基础配置（所有版本都有）
    config = {
        'subscriptions': [],
        'nodes': [],
        'rule_configs': [],  # 规则配置：统一存储规则和规则集，通过 itemType 字段区分
        'proxy_groups': [],
        'rule_library': [],  # 规则仓库
        'system_config': {  # 系统配置
            'server_domain': '',
            'github_proxy_domain': {},
        },
        'subscription_aggregations': [],
        'mihomo': {  # Mihomo 配置
            'custom_config': ''
        },
        'mosdns': {  # MosDNS 配置
            'direct_rulesets': [],
            'proxy_rulesets': [],
            'direct_rules': [],
            'proxy_rules': [],
            'local_dns': '',
            'remote_dns': '',
            'fallback_dns': '',
            'default_forward': 'forward_remote',
            'custom_hosts': '',
            'custom_config': '',
            'custom_matches': [],
            'custom_match_position': 'tail',
            'cache_enabled': True,
            'cache_size': 10240,
            'cache_lazy_ttl': 21600,
            'cache_dump_enabled': True,
            'cache_dump_file': './cache.dump',
            'cache_dump_interval': 300
        }
    }

    return config

# 全局配置
config_data: Dict[str, Any] = get_default_config()

def get_config():
    """获取全局配置"""
    return config_data


def load_config():
    """加载配置"""
    global config_data, agent_manager
    import uuid

    # 如果 config.json 是目录（Docker 挂载时可能发生），删除它
    if os.path.isdir(CONFIG_FILE):
        import shutil
        shutil.rmtree(CONFIG_FILE)

    if os.path.exists(CONFIG_FILE) and os.path.isfile(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                content = f.read().strip()
                # 如果文件为空，使用默认配置
                if not content:
                    save_config()
                    return
                loaded_data = json.loads(content)

            # 与默认配置递归合并，补齐旧版本 config.json 中缺失的配置节。
            # 代码中存在直接按键访问（config_data['subscriptions'] 等），缺键会抛
            # KeyError 导致接口 500、页面报「加载订阅列表失败」。用户已有的值优先。
            missing_keys = [k for k in get_default_config() if k not in loaded_data]
            merged_data = _deep_merge(get_default_config(), loaded_data)

            # 这样所有持有 config_data 引用的对象（如 agent_manager）都能看到更新
            config_data.clear()
            config_data.update(merged_data)

            if missing_keys:
                logger.info(f"配置缺少顶层项，已补齐默认值: {', '.join(missing_keys)}")

            # 为没有 ID 的节点生成 ID
            nodes_updated = False
            for node in config_data.get('nodes', []):
                if 'id' not in node:
                    node['id'] = f"node_{uuid.uuid4().hex[:8]}"
                    nodes_updated = True

            # 清理聚合中的无效引用
            aggregations_cleaned = clean_invalid_aggregation_references()

            # 清理策略组中对已删除/已禁用聚合的引用
            proxy_groups_cleaned = clean_invalid_proxy_group_aggregations()

            # 如果补齐了缺失项、节点被更新或聚合/策略组被清理，保存配置
            if missing_keys or nodes_updated or aggregations_cleaned or proxy_groups_cleaned:
                save_config()

        except (json.JSONDecodeError, ValueError) as e:
            # JSON 解析失败，使用默认配置
            print(f"配置文件解析失败，使用默认配置: {e}")
            save_config()
    else:
        # 配置文件不存在，尝试从模板初始化
        # 使用 resource helper 获取模板文件路径（兼容开发环境和 PyInstaller 打包后的环境）
        template_file = get_backend_resource('config_template.json')
        if os.path.exists(template_file) and os.path.isfile(template_file):
            try:
                # 从模板加载配置
                import shutil
                shutil.copy(template_file, CONFIG_FILE)
                print(f'已从模板初始化配置文件: {template_file} -> {CONFIG_FILE}')

                # 读取模板配置并加载到 config_data
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    loaded_template = json.loads(f.read())

                # 更新 config_data（不要重新赋值，而是清空并更新）
                config_data.clear()
                config_data.update(loaded_template)
            except Exception as e:
                print(f'从模板初始化配置失败，使用空配置: {e}')
                save_config()
        else:
            # 模板文件不存在，创建默认空配置
            save_config()


    # 如果 server_domain 未配置，自动设置为 http://本机IP:5001
    server_domain_updated = False
    if not config_data.get('system_config', {}).get('server_domain', '').strip():
        local_ip = get_local_ip()
        if 'system_config' not in config_data:
            config_data['system_config'] = {}
        config_data['system_config']['server_domain'] = f"http://{local_ip}:5001"
        server_domain_updated = True
        logger.info(f"Server domain not configured, auto-detected and set to: {config_data['system_config']['server_domain']}")

    # 如果 server_domain 被自动设置，保存配置
    if server_domain_updated:
        save_config()

    # 初始化 Agent 管理器单例
    # 注意：由于我们使用 config_data.clear() + update() 而不是重新赋值
    # agent_manager 持有的 config_data 引用始终有效
    # 这里重新初始化是为了确保 agent_manager 正确初始化
    from backend.common.agent_manager import init_agent_manager
    init_agent_manager()


def save_config():
    """保存配置到文件"""
    try:
        # 创建要保存的配置副本
        config_to_save = {**config_data}
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config_to_save, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"Error saving config: {e}")
        return False


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """递归深度合并两个字典，override 中的值优先"""
    result = {}
    for key in base:
        if key in override:
            if isinstance(base[key], dict) and isinstance(override[key], dict):
                result[key] = _deep_merge(base[key], override[key])
            else:
                result[key] = override[key]
        else:
            result[key] = base[key]
    for key in override:
        if key not in result:
            result[key] = override[key]
    return result


def safe_import_config(new_data: Dict[str, Any]) -> None:
    """安全导入配置：与默认配置递归合并，避免丢失字段。"""
    default = get_default_config()
    merged = _deep_merge(default, new_data)
    config_data.clear()
    config_data.update(merged)
    save_config()


def clean_invalid_aggregation_references():
    """清理聚合中所有无效的订阅和节点引用（不存在或已禁用的），如果聚合变空则禁用"""
    aggregations = config_data.get('subscription_aggregations', [])
    if not aggregations:
        return False

    # 获取所有启用的订阅和节点的 ID
    enabled_subscription_ids = {
        sub['id'] for sub in config_data.get('subscriptions', [])
        if sub.get('enabled', True)
    }
    enabled_node_ids = {
        node['id'] for node in config_data.get('nodes', [])
        if node.get('enabled', True)
    }
    enabled_node_ids.update(['DIRECT', 'REJECT'])  # 添加特殊值

    config_changed = False

    for agg in aggregations:
        original_subs = set(agg.get('subscriptions', []))
        original_nodes = set(agg.get('nodes', []))

        # 过滤掉无效的订阅引用
        valid_subs = [
            sub_id for sub_id in agg.get('subscriptions', [])
            if sub_id in enabled_subscription_ids
        ]

        # 过滤掉无效的节点引用
        valid_nodes = [
            node_id for node_id in agg.get('nodes', [])
            if node_id in enabled_node_ids
        ]

        # 如果有变化，更新聚合
        if set(valid_subs) != original_subs or set(valid_nodes) != original_nodes:
            agg['subscriptions'] = valid_subs
            agg['nodes'] = valid_nodes
            config_changed = True

        # 如果聚合变空（既没有订阅也没有节点），禁用该聚合
        if not valid_subs and not valid_nodes:
            if agg.get('enabled', True):
                agg['enabled'] = False
                config_changed = True

    return config_changed


def clean_invalid_proxy_group_aggregations():
    """清理策略组中所有无效的聚合引用（不存在或已禁用的）"""
    proxy_groups = config_data.get('proxy_groups', [])
    if not proxy_groups:
        return False

    # 获取所有启用的聚合 ID
    enabled_aggregation_ids = {
        agg['id'] for agg in config_data.get('subscription_aggregations', [])
        if agg.get('enabled', True)
    }

    config_changed = False

    for group in proxy_groups:
        aggregation_ids = group.get('aggregations', [])
        if not aggregation_ids:
            continue

        original_count = len(aggregation_ids)

        # 过滤掉无效的聚合引用
        valid_aggregation_ids = [
            agg_id for agg_id in aggregation_ids
            if agg_id in enabled_aggregation_ids
        ]

        # 如果有变化，更新策略组
        if len(valid_aggregation_ids) != original_count:
            group['aggregations'] = valid_aggregation_ids
            config_changed = True

    return config_changed
