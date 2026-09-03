"""配置管理模块"""
import os
import json
import copy
from contextvars import ContextVar
from collections.abc import MutableMapping, Iterator
from typing import Callable, Dict, Any, Optional

from backend.common.utils import get_local_ip
from backend.common.resource import get_backend_resource
from backend.common.config_repository import ProfileRepository
from backend.utils.logger import get_logger

# 获取当前模块的日志记录器
logger = get_logger(__name__)

# 配置存储文件
# 优先使用环境变量指定的路径，否则使用默认路径
DATA_DIR = os.environ.get('DATA_DIR', '/data')
if not os.path.exists(DATA_DIR) and 'DATA_DIR' not in os.environ:
    DATA_DIR = '.'  # 开发模式，使用当前目录
CONFIG_FILE = os.path.join(DATA_DIR, 'config.json')
AGGREGATION_PROVIDERS_DIR = os.path.join(DATA_DIR, 'providers')


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
            'github_proxy_domain': '',
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


def _get_initial_config() -> Dict[str, Any]:
    """Keep the existing first-start template separate from migration defaults."""
    config = get_default_config()
    template_file = get_backend_resource('config_template.json')
    try:
        with open(template_file, 'r', encoding='utf-8') as handle:
            template = json.load(handle)
        if isinstance(template, dict):
            config = _deep_merge(config, template)
    except (OSError, json.JSONDecodeError):
        pass
    return config


_repository: Optional[ProfileRepository] = None
_CONFIG_CACHE: ContextVar[Optional[Dict[str, Dict[str, Any]]]] = ContextVar(
    'configflow_profile_cache', default=None
)
_CONFIG_BASELINES: ContextVar[Optional[Dict[str, Dict[str, Any]]]] = ContextVar(
    'configflow_profile_baselines', default=None
)


def get_repository() -> ProfileRepository:
    global _repository
    if _repository is None:
        _repository = ProfileRepository(
            DATA_DIR,
            default_config_factory=get_default_config,
            initial_config_factory=_get_initial_config,
        )
    return _repository


def set_repository(repository: ProfileRepository) -> None:
    """Replace the repository for tests and embedded deployments."""
    global _repository
    _repository = repository
    _CONFIG_CACHE.set({})
    _CONFIG_BASELINES.set({})


def _cache() -> Dict[str, Dict[str, Any]]:
    cache = _CONFIG_CACHE.get()
    if cache is None:
        cache = {}
        _CONFIG_CACHE.set(cache)
    return cache


def reset_config_context() -> None:
    """Discard compatibility snapshots at request/task boundaries."""
    _CONFIG_CACHE.set(None)
    _CONFIG_BASELINES.set(None)


class ProfileConfigProxy(MutableMapping[str, Any]):
    """Compatibility mapping resolved to the current request profile."""

    def _data(self) -> Dict[str, Any]:
        return get_config()

    def __getitem__(self, key: str) -> Any:
        return self._data()[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._data()[key] = value

    def __delitem__(self, key: str) -> None:
        del self._data()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data())

    def __len__(self) -> int:
        return len(self._data())

    def __deepcopy__(self, memo: Dict[int, Any]) -> Dict[str, Any]:
        return copy.deepcopy(self._data(), memo)


config_data = ProfileConfigProxy()


def get_config(profile_id: Optional[str] = None) -> Dict[str, Any]:
    """Get a request-scoped compatibility view of a profile."""
    from backend.common.profile_context import resolve_profile_id

    resolved_id = resolve_profile_id(profile_id)
    cache = _cache()
    if resolved_id not in cache:
        cache[resolved_id] = get_repository().get_compat_config(resolved_id)
        baselines = _CONFIG_BASELINES.get()
        if baselines is None:
            baselines = {}
            _CONFIG_BASELINES.set(baselines)
        baselines[resolved_id] = copy.deepcopy(cache[resolved_id])
    return cache[resolved_id]


def load_config() -> Dict[str, Any]:
    """Initialize storage and normalize the active profile."""
    import uuid
    from backend.common.profile_context import resolve_profile_id

    repository = get_repository()
    _CONFIG_CACHE.set({})
    active_id = resolve_profile_id()
    data = get_config(active_id)
    changed = False
    for node in data.get('nodes', []):
        if 'id' not in node:
            node['id'] = f"node_{uuid.uuid4().hex[:8]}"
            changed = True
    if clean_invalid_aggregation_references():
        changed = True
    if clean_invalid_proxy_group_aggregations():
        changed = True
    if not data.get('system_config', {}).get('server_domain', '').strip():
        data.setdefault('system_config', {})['server_domain'] = f"http://{get_local_ip()}:5001"
        changed = True
    if changed:
        save_config(data, active_id)

    from backend.common.agent_manager import init_agent_manager
    init_agent_manager()
    return data


def save_config(config: Optional[Dict[str, Any]] = None, profile_id: Optional[str] = None) -> bool:
    """Persist a profile while retaining the legacy call signature."""
    from backend.common.profile_context import resolve_profile_id

    resolved_id = resolve_profile_id(profile_id)
    if config is None:
        config = get_config(resolved_id)
    repository = get_repository()
    baseline = (_CONFIG_BASELINES.get() or {}).get(resolved_id, {})
    profile_changes = {
        key: value for key, value in config.items()
        if key in repository.PROFILE_FIELDS and baseline.get(key) != value
    }
    if profile_changes:
        repository.update_profile_fields(
            resolved_id,
            profile_changes,
            baseline={key: baseline.get(key) for key in profile_changes},
        )
    system_changes = {
        key: config[key] for key in ("system_config", "backup")
        if key in config and baseline.get(key) != config[key]
    }
    if system_changes:
        def update_system(system):
            for key, value in system_changes.items():
                if isinstance(system.get(key), dict) and isinstance(value, dict):
                    system[key] = _deep_merge(system[key], value)
                else:
                    system[key] = copy.deepcopy(value)
        repository.update_system_transaction(update_system)
    _cache()[resolved_id] = copy.deepcopy(config)
    baselines = _CONFIG_BASELINES.get()
    if baselines is not None:
        baselines[resolved_id] = copy.deepcopy(config)
    return True


def update_config_transaction(
    updater: Callable[[Dict[str, Any]], Optional[Dict[str, Any]]],
    profile_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Apply an incremental resource update to freshly locked profile data."""
    from backend.common.profile_context import resolve_profile_id

    resolved_id = resolve_profile_id(profile_id)
    result = get_repository().update_profile_transaction(resolved_id, updater)
    _cache().pop(resolved_id, None)
    baselines = _CONFIG_BASELINES.get()
    if baselines is not None:
        baselines.pop(resolved_id, None)
    return result


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


def safe_import_config(new_data: Dict[str, Any], profile_id: Optional[str] = None) -> None:
    """Safely import a legacy/full configuration into one profile."""
    repository = get_repository()
    profile_data = {
        key: value for key, value in new_data.items()
        if key not in repository.SYSTEM_FIELDS
    }
    merged = _deep_merge(get_config(profile_id), profile_data)
    save_config(merged, profile_id)


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
