"""日志工具模块"""
import os
import logging


def get_logger(name: str = __name__) -> logging.Logger:
    """
    获取日志记录器

    Args:
        name: 日志记录器名称，默认使用模块名

    Returns:
        配置好的日志记录器
    """
    logger = logging.getLogger(name)

    # 不需要添加处理器，因为 backend.py 中的 logging.basicConfig() 已经配置了根 logger
    # 子 logger 会自动继承根 logger 的配置
    # 只需要设置日志级别即可
    log_level = os.environ.get('LOG_LEVEL', 'INFO').upper()
    level_map = {
        'DEBUG': logging.DEBUG,
        'INFO': logging.INFO,
        'WARNING': logging.WARNING,
        'ERROR': logging.ERROR,
        'CRITICAL': logging.CRITICAL
    }
    logger.setLevel(level_map.get(log_level, logging.INFO))

    return logger


def log_text(value, limit: int = 120) -> str:
    """User-supplied text on one bounded log line; control characters cannot forge entries."""
    text = ''.join(char if char.isprintable() else '?' for char in str(value))
    return text if len(text) <= limit else text[:limit] + '…'


def client_ip() -> str:
    """Request source for operation logs. nginx sets X-Real-IP; direct runs use the socket address."""
    from flask import has_request_context, request
    if not has_request_context():
        return '-'
    return log_text(request.headers.get('X-Real-IP') or request.remote_addr or '-', 64)


# 向后兼容的函数
def is_debug_enabled() -> bool:
    """检查是否启用了 DEBUG 日志级别"""
    log_level = os.environ.get('LOG_LEVEL', 'INFO').upper()
    return log_level == 'DEBUG'


def debug_log(message: str):
    """输出调试日志（仅在 DEBUG 模式下）- 已废弃，建议使用 get_logger()"""
    logger = get_logger('debug_log')
    logger.debug(message)
