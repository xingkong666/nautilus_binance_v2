"""实盘看门狗 (Watchdog).

持续监控关键子服务和系统指标的存活状态，
发现异常时触发告警并可选地执行自动恢复动作。

监控维度：
  - 各子服务的心跳（AccountSync、HealthProbe 等）
  - 系统资源（内存占用、文件句柄数）
  - EventBus 事件流是否停滞（心跳超时）

典型用法::

    watchdog = Watchdog(container, check_interval_sec=10)
    watchdog.register("account_sync", my_account_sync)
    watchdog.start()
    ...
    watchdog.stop()
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

import structlog

from src.core.events import Event, EventType

if TYPE_CHECKING:
    from src.app.container import Container

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# 协议：被看门狗监控的服务必须实现
# ---------------------------------------------------------------------------


class Watchable(Protocol):
    """被看门狗监控的服务协议.

    任何实现了 ``is_running`` 和 ``last_heartbeat_ns`` 属性的对象
    都可以注册到 Watchdog。
    """

    @property
    def is_running(self) -> bool:
        """返回服务是否正在运行."""
        ...

    @property
    def last_heartbeat_ns(self) -> int:
        """返回服务最近一次心跳时间戳（纳秒）."""
        ...


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass
class WatchEntry:
    """单个被监控服务的记录.

    Attributes:
        name: 服务名称（唯一标识）。
        target: 被监控的服务对象（实现 Watchable 协议）。
        max_heartbeat_gap_sec: 允许的最大心跳间隔（秒），超过则认为异常。
        on_failure: 自定义失败回调，None 时仅告警。
        fail_count: 连续失败次数，用于抑制重复告警。

    """

    name: str
    target: Watchable
    max_heartbeat_gap_sec: float = 60.0
    on_failure: Callable[[], None] | None = None
    fail_count: int = field(default=0, init=False)


@dataclass
class WatchCheckResult:
    """单次全量检查结果.

    Attributes:
        timestamp_ns: 检查时间戳。
        healthy_count: 健康服务数量。
        unhealthy_names: 异常服务名称列表。
        system_ok: 系统资源是否正常。

    """

    timestamp_ns: int = field(default_factory=time.time_ns)
    healthy_count: int = 0
    unhealthy_names: list[str] = field(default_factory=list)
    system_ok: bool = True

    @property
    def all_ok(self) -> bool:
        """返回所有服务和系统资源是否均正常.

        Returns:
            True 表示无任何异常。

        """
        return len(self.unhealthy_names) == 0 and self.system_ok


# ---------------------------------------------------------------------------
# Watchdog
# ---------------------------------------------------------------------------


class Watchdog:
    """实盘看门狗，周期性检查所有注册服务的存活状态.

    Attributes:
        check_interval_sec: 两次全量检查之间的间隔（秒），默认 10。

    Example::

        wd = Watchdog(container, check_interval_sec=10)
        wd.register("my_svc", my_service)
        wd.start()
        wd.stop()

    """

    def __init__(
        self,
        container: Container,
        check_interval_sec: float = 10.0,
        max_memory_mb: float = 2048.0,
    ) -> None:
        """初始化 Watchdog.

        Args:
            container: 应用依赖容器，用于访问 event_bus / alert_manager。
            check_interval_sec: 检查间隔秒数，默认 10。
            max_memory_mb: 内存告警阈值（MB），默认 2048。

        """
        self._container = container
        self._interval = check_interval_sec
        self._max_memory_mb = max_memory_mb

        self._entries: dict[str, WatchEntry] = {}
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._last_result: WatchCheckResult | None = None

        # 统计
        self._check_count = 0
        self._alert_count = 0

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        """返回 Watchdog 是否正在运行.

        Returns:
            True 表示后台线程已启动且未停止。

        """
        return self._thread is not None and self._thread.is_alive()

    @property
    def last_result(self) -> WatchCheckResult | None:
        """返回最近一次检查结果.

        Returns:
            WatchCheckResult 或 None（未执行过检查时）。

        """
        return self._last_result

    def register(
        self,
        name: str,
        target: Watchable,
        max_heartbeat_gap_sec: float = 60.0,
        on_failure: Callable[[], None] | None = None,
    ) -> None:
        """注册一个需要被监控的服务.

        Args:
            name: 服务唯一名称。
            target: 实现 Watchable 协议的服务对象。
            max_heartbeat_gap_sec: 心跳超时阈值（秒）。
            on_failure: 发现异常时执行的自定义回调，可为 None。

        """
        self._entries[name] = WatchEntry(
            name=name,
            target=target,
            max_heartbeat_gap_sec=max_heartbeat_gap_sec,
            on_failure=on_failure,
        )
        logger.debug("watchdog_service_registered", name=name)

    def unregister(self, name: str) -> None:
        """取消注册服务.

        Args:
            name: 服务名称。

        """
        self._entries.pop(name, None)

    def start(self) -> None:
        """启动后台看门狗线程.

        Raises:
            RuntimeError: 已在运行时再次调用 start()。

        """
        if self.is_running:
            raise RuntimeError("Watchdog is already running")

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="Watchdog",
            daemon=True,
        )
        self._thread.start()
        logger.info("watchdog_started", interval_sec=self._interval)

    def stop(self, timeout: float = 10.0) -> None:
        """停止看门狗后台线程.

        Args:
            timeout: 最长等待秒数。

        """
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        logger.info("watchdog_stopped", check_count=self._check_count)

    def check_once(self) -> WatchCheckResult:
        """立即执行一次全量检查（可在主线程调用）.

        Returns:
            WatchCheckResult 包含本次检查结果。

        """
        return self._do_check()

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        """后台线程主循环."""
        logger.info("watchdog_loop_started")

        while not self._stop_event.is_set():
            try:
                result = self._do_check()
                self._last_result = result
                self._check_count += 1

                if not result.all_ok:
                    logger.warning(
                        "watchdog_unhealthy",
                        unhealthy=result.unhealthy_names,
                        system_ok=result.system_ok,
                    )
            except Exception as exc:
                logger.error("watchdog_check_error", error=str(exc), exc_info=True)

            self._stop_event.wait(timeout=self._interval)

        logger.info("watchdog_loop_stopped")

    def _do_check(self) -> WatchCheckResult:
        """执行一次全量存活检查.

        Returns:
            WatchCheckResult 汇总本次检查结果。

        """
        result = WatchCheckResult()
        now_ns = time.time_ns()

        # 检查各注册服务
        for name, entry in self._entries.items():
            ok = self._check_entry(entry, now_ns)
            if ok:
                result.healthy_count += 1
                entry.fail_count = 0
            else:
                result.unhealthy_names.append(name)
                entry.fail_count += 1
                self._handle_failure(entry)

        # 检查系统资源
        result.system_ok = self._check_system()

        # 发布健康检查事件
        self._publish_health_event(result)

        return result

    def _check_entry(self, entry: WatchEntry, now_ns: int) -> bool:
        """检查单个服务是否健康.

        Args:
            entry: 服务监控记录。
            now_ns: 当前时间戳（纳秒）。

        Returns:
            True 表示服务健康。

        """
        try:
            if not entry.target.is_running:
                logger.warning("service_not_running", name=entry.name)
                return False

            last_hb = entry.target.last_heartbeat_ns
            gap_sec = (now_ns - last_hb) / 1e9

            if gap_sec > entry.max_heartbeat_gap_sec:
                logger.warning(
                    "service_heartbeat_timeout",
                    name=entry.name,
                    gap_sec=round(gap_sec, 1),
                    threshold_sec=entry.max_heartbeat_gap_sec,
                )
                return False

            return True

        except Exception as exc:
            logger.error("watchdog_entry_check_error", name=entry.name, error=str(exc), exc_info=True)
            return False

    def _check_system(self) -> bool:
        """检查系统资源是否在阈值内.

        Returns:
            True 表示系统资源正常。

        """
        try:
            import os

            import psutil  # type: ignore

            process = psutil.Process(os.getpid())
            mem_mb = process.memory_info().rss / 1024 / 1024

            if mem_mb > self._max_memory_mb:
                logger.warning(
                    "system_memory_high",
                    mem_mb=round(mem_mb, 1),
                    threshold_mb=self._max_memory_mb,
                )
                return False

            return True

        except ImportError:
            # psutil 未安装，跳过系统检查
            return True
        except Exception as exc:
            logger.error("system_check_error", error=str(exc), exc_info=True)
            return True  # 检查失败不认为系统异常

    def _handle_failure(self, entry: WatchEntry) -> None:
        """处理服务失败：触发告警 + 执行自定义回调.

        Args:
            entry: 发生失败的服务监控记录。

        """
        # 抑制重复告警（同一服务连续失败，每 3 次才再次告警）
        if entry.fail_count % 3 != 1:
            return

        self._alert_count += 1

        # 尝试通过 alert_manager 发送告警
        try:
            msg = f"[Watchdog] 服务 {entry.name} 异常（连续失败 {entry.fail_count} 次）"
            self._container.alert_manager.send_direct(
                level="ERROR",
                rule_name="watchdog_service_failure",
                message=msg,
            )
        except Exception as exc:
            logger.error("watchdog_alert_failed", name=entry.name, error=str(exc), exc_info=True)

        # 执行自定义失败回调
        if entry.on_failure:
            try:
                entry.on_failure()
            except Exception as exc:
                logger.error("watchdog_on_failure_callback_error", name=entry.name, error=str(exc), exc_info=True)

    def _publish_health_event(self, result: WatchCheckResult) -> None:
        """发布健康检查事件到 EventBus.

        Args:
            result: 本次检查结果。

        """
        event = Event(
            event_type=EventType.HEALTH_CHECK,
            source="watchdog",
            payload={
                "all_ok": result.all_ok,
                "healthy_count": result.healthy_count,
                "unhealthy": result.unhealthy_names,
                "system_ok": result.system_ok,
                "check_count": self._check_count,
            },
        )
        try:
            self._container.event_bus.publish(event)
        except Exception as exc:
            logger.error("watchdog_health_event_publish_failed", error=str(exc), exc_info=True)
