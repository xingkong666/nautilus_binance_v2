"""实盘进程主管 (Supervisor).

负责协调和管理实盘运行的全生命周期：
  - 启动/停止所有子服务（AccountSync、Watchdog、HealthProbe）
  - 监听系统事件（熔断、重连、优雅关闭）
  - 捕获异常并决策是重启、降级还是停止

典型用法::

    supervisor = LiveSupervisor(container)
    supervisor.start()
    # 阻塞运行，直到收到 stop() 或致命错误
    supervisor.join()
"""

from __future__ import annotations

import asyncio
import threading
from enum import Enum, unique
from typing import TYPE_CHECKING

import structlog

from src.core.events import Event, EventType

if TYPE_CHECKING:
    from src.app.container import Container
    from src.live.account_sync import AccountSync
    from src.live.health import LiveHealthProbe
    from src.live.watchdog import Watchdog

logger = structlog.get_logger(__name__)


@unique
class SupervisorState(Enum):
    """Supervisor 状态机."""

    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    DEGRADED = "degraded"  # 部分子服务异常，已降级运行
    STOPPING = "stopping"
    STOPPED = "stopped"


class LiveSupervisor:
    """实盘主管，协调所有实盘子服务的生命周期.

    Attributes:
        container: 应用依赖容器。
        state: 当前运行状态（SupervisorState）。

    Example::

        sup = LiveSupervisor(container)
        sup.start()
        sup.join()          # 阻塞到停止
        sup.stop()          # 也可由信号触发

    """

    def __init__(
        self,
        container: Container,
        *,
        max_recovery_attempts: int = 3,
        recovery_backoff_base_sec: float = 5.0,
    ) -> None:
        """初始化 Supervisor.

        Args:
            container: 已 build 的应用依赖容器，提供 event_bus 等服务。
            max_recovery_attempts: DEGRADED 状态下最大自动重连次数，默认 3。
            recovery_backoff_base_sec: 指数退避基础等待秒数，默认 5.0。
                实际等待 = base * 2^(attempt-1)，即 5s → 10s → 20s。

        """
        self._container = container
        self._state = SupervisorState.IDLE
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        # 子服务引用（start 后填充）
        self._account_sync: AccountSync | None = None
        self._watchdog: Watchdog | None = None
        self._health_probe: LiveHealthProbe | None = None

        # 错误计数，用于决策是否重启
        self._error_count = 0
        self._max_errors = 5

        # 自动重连配置
        self._max_recovery_attempts = max_recovery_attempts
        self._recovery_backoff_base_sec = recovery_backoff_base_sec

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    @property
    def state(self) -> SupervisorState:
        """返回当前 Supervisor 状态.

        Returns:
            SupervisorState 枚举值。

        """
        return self._state

    def start(self) -> None:
        """在后台线程中启动 asyncio 事件循环及所有子服务.

        Raises:
            RuntimeError: Supervisor 已在运行时调用。

        """
        if self._state not in (SupervisorState.IDLE, SupervisorState.STOPPED):
            raise RuntimeError(f"Cannot start in state {self._state}")

        self._state = SupervisorState.STARTING
        self._stop_event.clear()

        self._thread = threading.Thread(
            target=self._run_in_thread,
            name="LiveSupervisor",
            daemon=True,
        )
        self._thread.start()
        logger.info("supervisor_started")

    def stop(self, timeout: float = 30.0) -> None:
        """发送停止信号，等待所有子服务优雅关闭.

        Args:
            timeout: 最长等待秒数，超时后强制停止。

        """
        logger.info("supervisor_stop_requested")
        self._stop_event.set()

        if self._loop and self._loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self._async_stop(), self._loop)
            try:
                future.result(timeout=timeout)
            except (TimeoutError, RuntimeError):
                logger.exception("supervisor_async_stop_failed")

        if self._thread:
            self._thread.join(timeout=timeout)

        self._force_stop_services()
        self._state = SupervisorState.STOPPED
        logger.info("supervisor_stopped")

    def join(self) -> None:
        """阻塞调用方线程，直到 Supervisor 停止.

        通常在主线程中调用，配合信号处理器一起使用。
        """
        if self._thread:
            self._thread.join()

    # ------------------------------------------------------------------
    # 内部启动逻辑
    # ------------------------------------------------------------------

    def _run_in_thread(self) -> None:
        """在独立线程中运行 asyncio 事件循环.

        捕获顶层异常，决策是重启还是放弃。
        """
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._async_main())
        except (RuntimeError, OSError, asyncio.CancelledError):
            logger.exception("supervisor_fatal_error")
            self._state = SupervisorState.STOPPED
        finally:
            self._force_stop_services()
            self._loop.close()

    async def _async_main(self) -> None:
        """异步主流程：初始化子服务 → 事件监听 → 优雅关闭."""
        await self._start_services()
        self._state = SupervisorState.RUNNING
        logger.info("supervisor_running")

        # 订阅熔断事件，收到后进入 DEGRADED
        self._container.event_bus.subscribe(EventType.CIRCUIT_BREAKER, self._on_circuit_breaker)

        # 主循环：等待停止信号
        while not self._stop_event.is_set():
            await asyncio.sleep(1.0)

        await self._async_stop()

    async def _start_services(self) -> None:
        """按顺序启动所有子服务.

        Raises:
            Exception: 任意子服务启动失败时向上传播。

        """
        from src.live.account_sync import AccountSync
        from src.live.health import LiveHealthProbe
        from src.live.watchdog import Watchdog

        logger.info("starting_live_services")

        self._health_probe = LiveHealthProbe(container=self._container)
        self._account_sync = AccountSync(
            container=self._container,
            redis_client=self._container.redis_client,
        )
        self._watchdog = Watchdog(container=self._container)

        self._health_probe.start()
        self._account_sync.start()
        self._watchdog.start()

        logger.info("live_services_started")

    async def _async_stop(self) -> None:
        """异步优雅关闭所有子服务."""
        if self._state == SupervisorState.STOPPED:
            return

        self._state = SupervisorState.STOPPING
        logger.info("supervisor_stopping_services")

        for svc_name, svc in [
            ("watchdog", self._watchdog),
            ("account_sync", self._account_sync),
            ("health_probe", self._health_probe),
        ]:
            if svc is not None:
                try:
                    svc.stop()
                    logger.info("service_stopped", service=svc_name)
                except Exception as exc:
                    logger.error("service_stop_failed", service=svc_name, error=str(exc), exc_info=True)

    def _force_stop_services(self) -> None:
        """兜底同步停止，避免后台线程泄漏."""
        for svc_name, svc in [
            ("watchdog", self._watchdog),
            ("account_sync", self._account_sync),
            ("health_probe", self._health_probe),
        ]:
            if svc is None:
                continue
            try:
                if getattr(svc, "is_running", False):
                    svc.stop()
                    logger.info("service_force_stopped", service=svc_name)
            except Exception as exc:
                logger.error("service_force_stop_failed", service=svc_name, error=str(exc), exc_info=True)

    # ------------------------------------------------------------------
    # 事件处理
    # ------------------------------------------------------------------

    def _on_circuit_breaker(self, event: Event) -> None:
        """处理熔断事件，进入 DEGRADED 状态，并尝试自动重连.

        Args:
            event: CircuitBreaker 触发的 Event 对象。

        """
        logger.warning(
            "supervisor_circuit_breaker_triggered",
            payload=event.payload,
        )
        self._state = SupervisorState.DEGRADED
        self._error_count += 1

        if self._error_count >= self._max_errors:
            logger.critical(
                "supervisor_max_errors_reached",
                error_count=self._error_count,
            )
            self._stop_event.set()
            return

        # 启动后台重连任务（指数退避）
        if self._loop is not None and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._attempt_recovery(), self._loop)
            logger.info(
                "supervisor_recovery_scheduled",
                error_count=self._error_count,
                max_recovery_attempts=self._max_recovery_attempts,
            )

    async def _attempt_recovery(self) -> None:
        """指数退避重连：等待 backoff_base * 2^(attempt-1) 秒后重启 adapter.

        最多尝试 max_recovery_attempts 次；全部失败则设置 stop_event。
        """
        for attempt in range(1, self._max_recovery_attempts + 1):
            wait_sec = self._recovery_backoff_base_sec * (2 ** (attempt - 1))
            logger.info(
                "supervisor_recovery_attempt",
                attempt=attempt,
                max_attempts=self._max_recovery_attempts,
                wait_sec=wait_sec,
            )
            await asyncio.sleep(wait_sec)
            try:
                await self._restart_adapter()
                self._state = SupervisorState.RUNNING
                self._error_count = 0
                logger.info("supervisor_recovery_succeeded", attempt=attempt)
                return
            except Exception as exc:
                logger.warning(
                    "supervisor_recovery_failed",
                    attempt=attempt,
                    error=str(exc),
                )

        # 所有重连尝试均失败 → 停止进程
        logger.critical(
            "supervisor_recovery_exhausted",
            max_recovery_attempts=self._max_recovery_attempts,
        )
        self._stop_event.set()

    async def _restart_adapter(self) -> None:
        """重启 BinanceAdapter（stop → start）.

        Raises:
            Exception: adapter.stop() 或 adapter.start() 抛出时向上传播。
        """
        adapter = getattr(self._container, "binance_adapter", None)
        if adapter is None:
            logger.debug("supervisor_restart_adapter_skipped_no_adapter")
            return
        if hasattr(adapter, "stop"):
            await adapter.stop()
        if hasattr(adapter, "start"):
            await adapter.start()
        logger.info("supervisor_adapter_restarted")
