"""结构化日志 — 深度集成 NautilusTrader 日志系统.

使用 structlog 提供 JSON 格式日志。NautilusTrader 内部 Rust 日志的
``use_pyo3=True`` 桥接由 ``TradingNodeConfig.logging`` 负责（在
``BinanceAdapterConfig._build_node_config()`` 中设置），无需在此处
重复初始化 NT 日志系统。

架构:
    NT Rust Logger ──(use_pyo3=True, 由 TradingNodeConfig 激活)──►
    structlog loggers ─────────────────────────────────────────────►
                                    Python logging.Handler
                                            │
                                            ▼
                                    structlog processor chain
                                            │
                                            ▼
                                    stdout (JSON or Console)

注意: ``use_pyo3=True`` 的激活在 ``BinanceAdapter._build_node_config()``
中通过 ``LoggingConfig(use_pyo3=True)`` 完成。``setup_logging()`` 只负责
配置 Python structlog / stdlib logging 层，不调用 NT ``init_logging()``，
避免 API 不兼容问题。
"""

from __future__ import annotations

import logging
import sys
import threading
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from src.core.config import LoggingConfig

# 防止重复配置结构化日志（配置调用是幂等的，但标准库处理器重置需保护）
_INITIALIZED: bool = False
_INIT_LOCK: threading.Lock = threading.Lock()


def setup_logging(
    level: str = "INFO",
    json_format: bool = True,
    console: bool = True,
    nautilus_cfg: LoggingConfig | None = None,
) -> None:
    """初始化结构化日志（structlog + Python stdlib logging）.

    配置 structlog 处理链和 Python 标准库 logging handler，使所有日志
    以统一格式输出到 stdout。NautilusTrader 内部 Rust 日志的 pyo3 桥接
    由 ``BinanceAdapter._build_node_config()`` 中的
    ``LoggingConfig(use_pyo3=True)`` 负责，无需在此调用 NT ``init_logging()``。

    Args:
        level: 日志级别 (DEBUG/INFO/WARNING/ERROR/CRITICAL)。
            若提供了 nautilus_cfg，则由 cfg.level 覆盖此参数。
        json_format: 是否输出 JSON 格式（False 则彩色控制台）。
            若提供了 nautilus_cfg，则由 cfg.format 决定。
        console: 是否输出到控制台。
            若提供了 nautilus_cfg，则由 cfg.console 决定。
        nautilus_cfg: 来自 AppConfig 的 LoggingConfig，若提供则
            优先使用其中的 level / format / console 字段。

    """
    global _INITIALIZED  # noqa: PLW0603

    # 从 Nautilus配置中提取参数（优先级高于位置参数）
    if nautilus_cfg is not None:
        level = nautilus_cfg.level
        json_format = nautilus_cfg.format.lower() != "console"
        console = nautilus_cfg.console

    log_level = getattr(logging, level.upper(), logging.INFO)

    with _INIT_LOCK:
        if _INITIALIZED:
            # 结构化日志已配置，仅更新标准库日志级别（允许动态调整）
            logging.getLogger().setLevel(log_level)
            return

        # -------------------------------------------------------------------
        # 1. 配置结构化日志处理链
        # -------------------------------------------------------------------
        processors: list[Any] = [
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

        # -------------------------------------------------------------------
        # 2. 标准库日志配置
        # -------------------------------------------------------------------
        handler: logging.Handler = logging.StreamHandler(sys.stdout) if console else logging.NullHandler()
        handler.setLevel(log_level)

        root_logger = logging.getLogger()
        root_logger.setLevel(log_level)
        root_logger.handlers = [handler]

        _INITIALIZED = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """获取 logger 实例.

    Args:
        name: Logger 名称，通常传入 ``__name__`` 以标识模块来源。
    """
    from typing import cast

    return cast("structlog.stdlib.BoundLogger", structlog.get_logger(name))
