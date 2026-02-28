"""应用组装层.

提供统一的启动入口和依赖管理。

Quick start:
    from src.app import bootstrap_context, AppFactory

    with bootstrap_context(env="dev") as ctx:
        runner = ctx.factory.create_backtest_runner(start, end)
"""

from src.app.bootstrap import AppContext, bootstrap, bootstrap_app, bootstrap_context, register_shutdown_handler
from src.app.container import Container
from src.app.factory import AppFactory

__all__ = [
    "AppContext",
    "AppFactory",
    "Container",
    "bootstrap",
    "bootstrap_app",
    "bootstrap_context",
    "register_shutdown_handler",
]
