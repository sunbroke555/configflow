"""配置管理模块"""
import os
import json
import copy
from contextvars import ContextVar
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


def get_resource_config(kind, resource_id=None, profile_id=None):
    """Resolve an explicit shared-resource request without persisting selection."""
    from backend.common.profile_context import resolve_profile_id
    return get_repository().get_compat_config(resolve_profile_id(profile_id),
        resource_roots={kind: [resource_id] if resource_id is not None else None})


def load_config() -> Dict[str, Any]:
    """Initialize global settings without silently rewriting resource references."""
    reset_config_context()
    repository = get_repository()
    if not repository.get_system()['system_config'].get('server_domain', '').strip():
        repository.update_system_transaction(
            lambda system: system['system_config'].update(server_domain=f'http://{get_local_ip()}:5001'))
    from backend.common.agent_manager import init_agent_manager
    init_agent_manager()
    return get_config()


def save_config(config: Optional[Dict[str, Any]] = None, profile_id: Optional[str] = None) -> bool:
    """Save only independent profile fields from an explicit/request snapshot."""
    from backend.common.profile_context import resolve_profile_id
    resolved_id = resolve_profile_id(profile_id)
    if config is None:
        config = get_config(resolved_id)
    baseline = (_CONFIG_BASELINES.get() or {}).get(resolved_id)
    get_repository().save_profile(resolved_id, config, baseline=baseline)
    reset_config_context()
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
    """Import independent profile parameters, never shared source copies."""
    from backend.common.profile_context import resolve_profile_id
    get_repository().import_profile(resolve_profile_id(profile_id), new_data)
    reset_config_context()






def get_shared_config() -> Dict[str, Any]:
    result = get_repository().get_shared()
    baselines = dict(_CONFIG_BASELINES.get() or {})
    baselines['@shared'] = copy.deepcopy(result)
    _CONFIG_BASELINES.set(baselines)
    return result


def save_shared_config(config: Dict[str, Any], baseline=None) -> bool:
    if baseline is None:
        baseline = (_CONFIG_BASELINES.get() or {}).get('@shared')
    get_repository().save_shared(config, baseline=baseline)
    reset_config_context()
    return True


def update_shared_config_transaction(updater) -> Dict[str, Any]:
    result = get_repository().update_shared_transaction(updater)
    reset_config_context()
    return result


def get_system_config() -> Dict[str, Any]:
    """Return the global system section, including settings, backup and agents."""
    result = get_repository().get_system()
    baselines = dict(_CONFIG_BASELINES.get() or {})
    baselines['@system'] = copy.deepcopy(result)
    _CONFIG_BASELINES.set(baselines)
    return result


def save_system_config(config: Dict[str, Any], baseline=None) -> bool:
    if baseline is None:
        baseline = (_CONFIG_BASELINES.get() or {}).get('@system')
    get_repository().save_system(config, baseline=baseline)
    reset_config_context()
    return True
