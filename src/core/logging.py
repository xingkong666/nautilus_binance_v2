"""结构化日志.

使用 structlog 提供 JSON 格式日志, 方便后续接入 ELK / Loki.
"""

from __future__ import annotations

import logging
import sys

import structlog


def setup_logging(level: str = "INFO", json_format: bool = True, console: bool = True) -> None:
    """初始化结构化日志.

    Args:
        level: 日志级别 (DEBUG/INFO/WARNING/ERROR/CRITICAL)
        json_format: 是否输出 JSON 格式
        console: 是否输出到控制台
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    # structlog 处理链
    processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    if json_format:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # 标准库 logging 配置
    handler = logging.StreamHandler(sys.stdout) if console else logging.NullHandler()
    handler.setLevel(log_level)

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.handlers = [handler]


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """获取 logger 实例."""
    return structlog.get_logger(name)
