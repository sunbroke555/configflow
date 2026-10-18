"""规则工具模块

提供规则相关的辅助功能，包括：
- 规则目录管理
- 规则名称清理
- 规则文件保存
"""
import os
import re
import hashlib
from typing import Dict, Any
from backend.common.config import get_repository
from backend.utils.url_utils import safe_url_for_log
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
    return f'{namespace}/{hashlib.sha256(identity.encode()).hexdigest()}.list'


def rule_cache_read_path(rule, library=None, *, rules_dir=None):
    """Read new caches first, retaining unambiguous legacy caches offline."""
    root = rules_dir if rules_dir is not None else get_rules_dir()
    path = os.path.join(root, rule_cache_filename(rule))
    if os.path.exists(path):
        return path
    if library is None:
        library = get_repository().get_shared().get('rule_library', [])
    name = rule.get('name', '')
    legacy_name = sanitize_rule_name(name)
    matches = [item for item in library if sanitize_rule_name(item.get('name', '')) == legacy_name]
    # A legacy file has no identity receipt. Never attribute a shared filename
    # to either of two resources, or to an unknown spelling of a known name.
    if len(matches) > 1 or (matches and matches[0].get('name') != name):
        return path
    legacy = os.path.join(root, legacy_name + '.list')
    return legacy if os.path.exists(legacy) else path


def save_rule_to_local(rule: Dict[str, Any]) -> str:
    """将共享规则原始内容按规则 ID 保存到共享缓存。

    Args:
        rule: 规则字典

    Returns:
        保存的文件名
    """
    import requests
    from backend.utils.logger import get_logger

    logger = get_logger(__name__)

    rule_name = rule.get('name', 'unnamed')
    filename = rule_cache_filename(rule)
    rules_dir = get_rules_dir()
    filepath = os.path.join(rules_dir, filename)

    # 确保目录存在
    os.makedirs(rules_dir, exist_ok=True)

    source_type = rule.get('source_type', 'url')

    if source_type == 'content':
        # 直接保存内容
        content = rule.get('content', '')
        get_repository().write_shared_text(
            os.path.join('rules', filename),
            content,
        )
        logger.info(f"Saved rule content to {filepath}")

    elif source_type == 'url':
        # 从 URL 下载内容
        url = rule.get('url', '')
        if not url:
            logger.warning(f"Rule '{rule_name}' has no URL, skipping save")
            return filename

        try:
            logger.info(f"Fetching rule content from {safe_url_for_log(url)}")
            response = request_rule(url, timeout=10)
            response.raise_for_status()

            get_repository().write_shared_text(
                os.path.join('rules', filename),
                response.text,
            )

            logger.info(f"Downloaded and saved rule from {safe_url_for_log(url)} to {filepath}")

        except (requests.RequestException, ValueError) as e:
            logger.error(f"Failed to download rule from {safe_url_for_log(url)}")
            # 调用方可能将异常写入 HTTP 响应或日志，不能携带代理凭据。
            raise requests.RequestException(f'Rule download failed ({type(e).__name__})') from None

    return filename
