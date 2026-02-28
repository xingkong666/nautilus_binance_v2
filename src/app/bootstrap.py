"""应用启动引导.

提供统一的应用初始化入口，按环境加载配置、构建容器、启动服务。
所有模式（回测、模拟盘、实盘）都通过 bootstrap 获取就绪的 Container。

典型用法:
    # 回测
    container = bootstrap(env="dev")
    factory = AppFactory(container)
    runner = factory.create_backtest_runner(start, end)

    # 关闭
    container.teardown()

    # 或使用上下文管理器（推荐）:
    with bootstrap_context(env="prod") as ctx:
        ctx.factory.create_backtest_runner(...)
"""

from __future__ import annotations

import signal
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Generator

import structlog

from src.app.container import Container
from src.app.factory import AppFactory
from src.core.config import AppConfig, load_app_config
from src.core.logging import setup_logging

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# 启动上下文
# ---------------------------------------------------------------------------


@dataclass
class AppContext:
    """应用运行上下文，持有 container 和 factory 的引用.

    Attributes:
        config: 已加载的应用配置。
        container: 已 build 的依赖容器。
        factory: 对象工厂，基于 container 创建业务对象。
    """

    config: AppConfig
    container: Container
    factory: AppFactory


# ---------------------------------------------------------------------------
# 核心启动函数
# ---------------------------------------------------------------------------


def bootstrap(env: str | None = None, log_level: str = "INFO") -> Container:
    """加载配置、初始化日志、构建并返回 Container.

    这是最轻量的启动方式，返回裸 Container，适合脚本场景。

    Args:
        env: 运行环境标识（dev/stage/prod），None 时从 .env 文件读取。
        log_level: 日志级别，默认 INFO。

    Returns:
        已 build 的 Container 实例。
    """
    config = load_app_config(env=env)
    setup_logging(level=log_level)

    logger.info("app_bootstrap", env=config.env)

    container = Container(config)
    container.build()

    return container


def bootstrap_app(env: str | None = None, log_level: str = "INFO") -> AppContext:
    """完整启动应用，返回包含 container + factory 的 AppContext.

    适合需要同时使用容器和工厂的场景（大多数情况推荐此函数）。

    Args:
        env: 运行环境标识。
        log_level: 日志级别。

    Returns:
        AppContext，含 config / container / factory。
    """
    container = bootstrap(env=env, log_level=log_level)
    factory = AppFactory(container)

    return AppContext(
        config=container.config,
        container=container,
        factory=factory,
    )


@contextmanager
def bootstrap_context(
    env: str | None = None,
    log_level: str = "INFO",
) -> Generator[AppContext, None, None]:
    """上下文管理器形式的启动，退出时自动 teardown.

    推荐在脚本和测试中使用，确保资源（DB 连接等）正确释放。

    Args:
        env: 运行环境标识。
        log_level: 日志级别。

    Yields:
        AppContext，含 config / container / factory。

    Example:
        with bootstrap_context(env="dev") as ctx:
            runner = ctx.factory.create_backtest_runner(start, end)
            result = runner.run(strategy_cls, strategy_cfg)
    """
    ctx = bootstrap_app(env=env, log_level=log_level)
    try:
        yield ctx
    finally:
        ctx.container.teardown()


# ---------------------------------------------------------------------------
# 信号处理（实盘用）
# ---------------------------------------------------------------------------


def register_shutdown_handler(container: Container) -> None:
    """注册 SIGINT / SIGTERM 信号处理器，优雅关闭应用.

    收到信号后执行 container.teardown()，然后退出进程。
    适合实盘长驻进程场景。

    Args:
        container: 需要在退出时 teardown 的 Container 实例。
    """

    def _handler(signum, frame):
        sig_name = signal.Signals(signum).name
        logger.warning("shutdown_signal_received", signal=sig_name)
        container.teardown()
        sys.exit(0)

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)
    logger.info("shutdown_handler_registered")
