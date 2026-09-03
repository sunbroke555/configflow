"""规则工具模块

提供规则相关的辅助功能，包括：
- 规则目录管理
- 规则名称清理
- 规则文件保存
"""
import os
import re
from typing import Dict, Any
from backend.common.config import get_repository
from backend.common.profile_context import resolve_profile_id
from backend.utils.url_utils import safe_url_for_log


def get_rules_dir(profile_id: str = None) -> str:
    """获取规则缓存目录路径

    Returns:
        规则目录的绝对路径
    """
    return str(get_repository().rules_dir(resolve_profile_id(profile_id)))


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


def save_rule_to_local(rule: Dict[str, Any], profile_id: str = None) -> str:
    """将规则内容保存到当前 profile 的 rules/{rule_name}.list

    Args:
        rule: 规则字典

    Returns:
        保存的文件名
    """
    import requests
    from backend.utils.logger import get_logger

    logger = get_logger(__name__)

    rule_name = rule.get('name', 'unnamed')
    filename = f"{sanitize_rule_name(rule_name)}.list"
    resolved_profile_id = resolve_profile_id(profile_id)
    rules_dir = get_rules_dir(resolved_profile_id)
    filepath = os.path.join(rules_dir, filename)

    # 确保目录存在
    os.makedirs(rules_dir, exist_ok=True)

    source_type = rule.get('source_type', 'url')

    if source_type == 'content':
        # 直接保存内容
        content = rule.get('content', '')
        get_repository().write_profile_text(
            resolved_profile_id,
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
            response = requests.get(url, timeout=10)
            response.raise_for_status()

            get_repository().write_profile_text(
                resolved_profile_id,
                os.path.join('rules', filename),
                response.text,
            )

            logger.info(f"Downloaded and saved rule from {safe_url_for_log(url)} to {filepath}")

        except requests.RequestException as e:
            logger.error(f"Failed to download rule from {safe_url_for_log(url)}")
            # 下载失败时抛出异常，不创建占位符文件
            raise

    return filename
