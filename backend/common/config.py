"""配置管理模块"""
import os
import json
import copy
from contextvars import ContextVar
from typing import Callable, Dict, Any, Optional

from backend.common.utils import get_local_ip
from backend.common.resource import get_backend_resource
from backend.common.config_repository import ProfileRepository
from backend.common.oxidns_settings import get_default_oxidns_settings
from backend.utils.logger import get_logger

# 获取当前模块的日志记录器
logger = get_logger(__name__)

# 配置存储文件
# 优先使用环境变量指定的路径，否则使用默认路径
DATA_DIR = os.environ.get('DATA_DIR', '/data')
if not os.path.exists(DATA_DIR) and 'DATA_DIR' not in os.environ:
    DATA_DIR = '.'  # 开发模式，使用当前目录
CONFIG_DATABASE_FILE = os.path.join(DATA_DIR, 'configflow.sqlite3')
# pre-migration copy, read only for rollback detection
LEGACY_CONFIG_FILE = os.path.join(DATA_DIR, 'config.json')
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
            'rule_fetch_proxy': '',
        },
        'subscription_aggregations': [],
        'mihomo': {  # Mihomo 配置
            'custom_config': ''
        },
        'oxidns': get_default_oxidns_settings(),
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
_CONFIG_BASELINES: ContextVar[Optional[Dict[str, Any]]] = ContextVar(
    'configflow_profile_baselines', default=None
)
# 本请求已校验过的配置空间：(请求对象, 仓库, 提交数, {请求键: ID})。请求边界清除；
# 期间任何写事务（包括删除配置空间）都会让它失效，之后再解析会重新校验。
_RESOLVED_PROFILES: ContextVar[Optional[tuple]] = ContextVar('configflow_resolved_profiles', default=None)


def get_repository() -> ProfileRepository:
    global _repository
    if _repository is None:
        _repository = ProfileRepository(
            DATA_DIR,
            default_config_factory=get_default_config,
            initial_config_factory=_get_initial_config,
        )
    return _repository


def get_default_profile_id() -> str:
    """当前默认配置空间的 ID（可在删除默认配置时切换，未切换过时为 default）。"""
    return get_repository().default_profile_id()


def set_repository(repository: ProfileRepository) -> None:
    """Replace the repository for tests and embedded deployments."""
    global _repository
    _repository = repository
    _CONFIG_CACHE.set({})
    _CONFIG_BASELINES.set({})
    _RESOLVED_PROFILES.set(None)


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
    _RESOLVED_PROFILES.set(None)


def resolved_profiles(request_object) -> Dict[Optional[str], str]:
    """本请求已校验过的配置空间 ID，供 resolve_profile_id 复用。"""
    repository = get_repository()
    commits = repository.commit_count
    memo = _RESOLVED_PROFILES.get()
    if memo is None or memo[0] is not request_object or memo[1] is not repository or memo[2] != commits:
        memo = (request_object, repository, commits, {})
        _RESOLVED_PROFILES.set(memo)
    return memo[3]


class _LazyBaseline:
    """三方合并基线的来源：读取时的已提交文档，保存时才解析。

    已提交的文档从不被原地修改（写事务先深拷贝再改），所以保留引用就能在
    保存时得到与读取时完全相同的基线，纯读请求不必为它再深拷贝一份配置。
    """
    __slots__ = ('build',)

    def __init__(self, build):
        self.build = build


def _set_baseline(key: str, value) -> None:
    baselines = dict(_CONFIG_BASELINES.get() or {})
    baselines[key] = value
    _CONFIG_BASELINES.set(baselines)


def _baseline(key: str):
    value = (_CONFIG_BASELINES.get() or {}).get(key)
    return value.build() if isinstance(value, _LazyBaseline) else value


def get_config(profile_id: Optional[str] = None) -> Dict[str, Any]:
    """Get a request-scoped compatibility view of a profile."""
    from backend.common.profile_context import resolve_profile_id

    # 请求内已校验过的 ID 直接复用（见 resolve_profile_id），快照命中时不再读仓库
    resolved_id = resolve_profile_id(profile_id)
    cache = _cache()
    if resolved_id not in cache:
        repository = get_repository()
        config, source = repository.get_compat_config_with_source(resolved_id)
        cache[resolved_id] = config
        baselines = _CONFIG_BASELINES.get()
        if baselines is None:
            baselines = {}
            _CONFIG_BASELINES.set(baselines)
        baselines[resolved_id] = (_LazyBaseline(lambda: repository.compat_config_from(source, resolved_id))
                                  if source is not None else copy.deepcopy(config))
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
    # Inside a container this guess is the bridge address and the internal
    # backend port, never reachable from the LAN; requests supply the origin.
    in_container = os.path.exists('/.dockerenv') or os.path.exists('/run/.containerenv')
    if not in_container and not repository.get_system()['system_config'].get('server_domain', '').strip():
        from backend.http_server import backend_address
        port = backend_address()['port']
        repository.update_system_transaction(
            lambda system: system['system_config'].update(server_domain=f'http://{get_local_ip()}:{port}'))
    from backend.common.agent_manager import init_agent_manager
    init_agent_manager()
    return get_config()


def save_config(config: Optional[Dict[str, Any]] = None, profile_id: Optional[str] = None) -> bool:
    """Save only independent profile fields from an explicit/request snapshot."""
    from backend.common.profile_context import resolve_profile_id
    resolved_id = resolve_profile_id(profile_id)
    if config is None:
        config = get_config(resolved_id)
    baseline = _baseline(resolved_id)
    get_repository().save_profile(resolved_id, config, baseline=baseline)
    reset_config_context()
    return True


def save_config_with_publish_guard(config: Dict[str, Any], service_type: str,
                                   profile_id: Optional[str] = None) -> Dict[str, Any]:
    """Save a draft and return the exact committed version for an optional push."""
    from backend.common.profile_context import resolve_profile_id

    resolved_id = resolve_profile_id(profile_id)
    baseline = _baseline(resolved_id)
    _, revision = get_repository().save_profile(
        resolved_id, config, baseline=baseline, include_config_revision=True)
    reset_config_context()
    return {'expected_profile_id': resolved_id, 'expected_service_type': service_type,
            'expected_config_revision': revision}


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
    result, source = get_repository().get_shared_with_source()
    _set_baseline('@shared', _LazyBaseline(lambda: copy.deepcopy(source)) if source is not None
                  else copy.deepcopy(result))
    return result


def save_shared_config(config: Dict[str, Any], baseline=None) -> bool:
    if baseline is None:
        baseline = _baseline('@shared')
    get_repository().save_shared(config, baseline=baseline)
    reset_config_context()
    return True


def update_shared_config_transaction(updater) -> Dict[str, Any]:
    result = get_repository().update_shared_transaction(updater)
    reset_config_context()
    return result


def update_shared_and_config_transaction(shared_updater, updater, profile_ids=None):
    """Update shared resources and one or more profiles in a single commit.

    updater(profile_id, before, after) runs for each profile; before is resolved
    against the shared data as it was, after against shared_updater's result.
    profile_ids defaults to the request profile and may be a callable, see
    ProfileRepository.update_shared_and_profiles_transaction.
    """
    from backend.common.profile_context import resolve_profile_id

    if profile_ids is None:
        profile_ids = [resolve_profile_id()]
    result = get_repository().update_shared_and_profiles_transaction(shared_updater, updater, profile_ids)
    reset_config_context()
    return result


def get_system_config() -> Dict[str, Any]:
    """Return the global system section, including settings, backup and agents."""
    result, source = get_repository().get_system_with_source()
    _set_baseline('@system', _LazyBaseline(lambda: copy.deepcopy(source)) if source is not None
                  else copy.deepcopy(result))
    return result


def save_system_config(config: Dict[str, Any], baseline=None) -> bool:
    if baseline is None:
        baseline = _baseline('@system')
    get_repository().save_system(config, baseline=baseline)
    reset_config_context()
    return True
