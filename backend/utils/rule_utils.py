"""规则工具模块

提供规则相关的辅助功能，包括：
- 规则目录管理
- 规则名称清理
- 规则文件保存
"""
import os
import re
import hashlib
import json
from typing import Dict, Any
from backend.common.config import get_repository
from backend.utils.rule_fetch import request_rule


def get_rules_dir() -> str:
    """获取规则缓存目录路径

    Returns:
        规则目录的绝对路径
    """
    return str(get_repository().shared_rules_dir())


def sanitize_rule_name(name: str) -> str:
    """将规则名称转换为文件系统安全的文件名

    Args:
        name: 原始规则名称

    Returns:
        安全的文件名（不含扩展名）
    """
    if not name:
        return 'unnamed'

    # 移除/替换文件系统不安全的字符: / \ : * ? " < > |
    safe_name = re.sub(r'[/\\:*?"<>|]', '_', name)
    # 移除首尾的空格和点
    safe_name = safe_name.strip('. ')
    # 限制长度为 200 字符
    return safe_name[:200] if safe_name else 'unnamed'


def rule_cache_filename(rule: Dict[str, Any]) -> str:
    """Stable resource identity; names are display labels and can collide."""
    identity = rule.get('library_rule_id') or rule.get('id')
    namespace = 'by-id' if identity is not None else 'by-name'
    identity = str(identity if identity is not None else rule.get('name', ''))
    if rule.get('source_type', 'url') == 'url' and rule.get('url'):
        # Bind remote bytes to the declaration as well as the resource ID. A
        # delayed refresh of an old URL/type cannot replace the current cache.
        namespace = 'by-source'
        identity = json.dumps([identity, rule.get('url'), rule.get('behavior', 'domain'),
                               rule.get('format') or ''], ensure_ascii=False)
    return f'{namespace}/{hashlib.sha256(identity.encode()).hexdigest()}.list'


def rule_cache_read_path(rule, library=None, *, rules_dir=None):
    """Read new caches first, retaining unambiguous legacy caches offline."""
    root = rules_dir if rules_dir is not None else get_rules_dir()
    path = os.path.join(root, rule_cache_filename(rule))
    if os.path.exists(path):
        return path
    if library is None:
        library = get_repository().get_shared().get('rule_library', [])
    # Migrate pre-validation identity caches only for the stored declaration;
    # proposed edits and stale publication snapshots cannot claim these bytes.
    stored_library = get_repository().get_shared().get('rule_library', [])
    current = next((item for item in stored_library if item.get('id') == rule.get('id')), None)
    fields = ('url', 'behavior', 'format', 'source_type')
    if current is not None and all(current.get(key) == rule.get(key) for key in fields):
        identity = str(rule.get('library_rule_id') or rule.get('id'))
        old = os.path.join(root, 'by-id', hashlib.sha256(identity.encode()).hexdigest() + '.list')
        if os.path.exists(old):
            return old
    if rule.get('source_type', 'url') == 'url' and rule.get('url') and (
            current is None or any(current.get(key) != rule.get(key) for key in fields)):
        return path
    name = rule.get('name', '')
    legacy_name = sanitize_rule_name(name)
    matches = [item for item in library if sanitize_rule_name(item.get('name', '')) == legacy_name]
    # A legacy file has no identity receipt. Never attribute a shared filename
    # to either of two resources, or to an unknown spelling of a known name.
    if len(matches) > 1 or (matches and matches[0].get('name') != name):
        return path
    legacy = os.path.join(root, legacy_name + '.list')
    return legacy if os.path.exists(legacy) else path


def read_validated_rule_cache(rule, config_data=None):
    """Return only cache bytes that satisfy the current remote declaration."""
    from backend.utils.ruleset_content import validate_remote_rule_content, RemoteRuleValidationError
    library = config_data.get('rule_library', []) if config_data is not None else None
    path = rule_cache_read_path(rule, library)
    try:
        with open(path, 'rb') as handle:
            content = handle.read()
        if rule.get('source_type', 'url') == 'url':
            validate_remote_rule_content(content, rule, config_data)
        return content
    except (OSError, RemoteRuleValidationError):
        return None


def write_validated_rule_cache(rule, content, config_data=None):
    """Validate the complete response before atomically replacing raw bytes."""
    from backend.utils.ruleset_content import validate_remote_rule_content
    if rule.get('source_type', 'url') == 'url':
        validate_remote_rule_content(content, rule, config_data)
    filename = rule_cache_filename(rule)
    get_repository().write_shared_text(os.path.join('rules', filename), content)
    return filename


def refresh_remote_rule(rule, config_data=None, *, timeout=10, fetch=None, allow_stale=False):
    """Return raw bytes and an optional refresh error when using valid cache.

    Explicit refresh callers leave allow_stale false to report the failure.
    Consumers may serve validated previous bytes while retaining the diagnosis.
    """
    from backend.utils.ruleset_content import RemoteRuleValidationError
    from backend.utils.logger import get_logger
    try:
        if fetch is None:
            response = request_rule(rule.get('url', ''), timeout=timeout, config_data=config_data)
            response.raise_for_status()
            content = response.content
        else:
            content = fetch(rule.get('url', ''))
        write_validated_rule_cache(rule, content, config_data)
        return content.encode('utf-8') if isinstance(content, str) else content, None
    except Exception as error:
        if allow_stale:
            cached = read_validated_rule_cache(rule, config_data)
            if cached is not None:
                from backend.utils.url_utils import safe_exception_details
                get_logger(__name__).warning('规则 %s 更新失败，继续使用上一次有效缓存：%s',
                                            rule.get('name', ''), str(error) if isinstance(error, RemoteRuleValidationError) else safe_exception_details(error))
                return cached, error
        raise


def read_rule_source_content(rule: Dict[str, Any]) -> str | bytes | None:
    """Validate a source without publishing its catalog entry or cache."""
    import requests
    from backend.utils.ruleset_content import validate_remote_rule_content, RemoteRuleValidationError
    from backend.converters.common import ruleset_format
    source_type = rule.get('source_type', 'url')
    if source_type == 'content':
        return rule.get('content', '')
    if source_type != 'url' or not rule.get('url'):
        return None
    try:
        response = request_rule(rule['url'], timeout=10)
        response.raise_for_status()
        content = response.content
        validate_remote_rule_content(content, rule)
        return content if ruleset_format(rule, {}) == 'mrs' else content.decode('utf-8')
    except RemoteRuleValidationError:
        raise
    except (requests.RequestException, ValueError) as error:
        # Never expose proxy credentials through transport error messages.
        raise requests.RequestException(f'Rule download failed ({type(error).__name__})') from None


def save_rule_to_local(rule: Dict[str, Any]) -> str:
    """Refresh a source; failed validation/download never changes its cache."""
    content = read_rule_source_content(rule)
    if content is None:
        raise ValueError('请提供有效的规则来源和订阅地址')
    return write_validated_rule_cache(rule, content)
