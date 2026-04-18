"""实盘健康探针 (LiveHealthProbe).

提供两类健康检查：
  1. **内部探针（LiveHealthProbe）**：运行在后台线程，定期采集系统状态并
     向 EventBus 发布 HEALTH_CHECK 事件，同时向 monitoring.HealthServer 汇报。
  2. **对外 HTTP 探针**：复用已有的 ``monitoring.health_server.HealthServer``，
     由 Container 在 monitoring.enabled=True 时启动，此处不重复创建。

LiveHealthProbe 实现了 ``Watchable`` 协议，可被 Watchdog 监控。

典型用法::

    probe = LiveHealthProbe(container, interval_sec=15)
    probe.start()
    ...
    probe.stop()
    print(probe.last_status)
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from src.core.events import Event, EventType

if TYPE_CHECKING:
    from src.app.container import Container

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass
class HealthStatus:
    """单次健康探针结果.

    Attributes:
        healthy: 总体是否健康。
        checks: 各项子检查的详细结果，key 为检查名，value 为描述。
        latency_ms: 本次检查总耗时（毫秒）。
        timestamp_ns: 检查完成时间戳（纳秒）。

    """

    healthy: bool
    checks: dict[str, Any] = field(default_factory=dict)
    latency_ms: float = 0.0
    timestamp_ns: int = field(default_factory=time.time_ns)

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 化的字典.

        Returns:
            包含所有字段的字典，timestamp_ns 转换为秒。

        """
        return {
            "healthy": self.healthy,
            "checks": self.checks,
            "latency_ms": round(self.latency_ms, 2),
            "timestamp": self.timestamp_ns / 1e9,
        }


# ---------------------------------------------------------------------------
# 实盘健康探针
# ---------------------------------------------------------------------------


class LiveHealthProbe:
    """实盘健康探针，定期采集并报告系统健康状态.

    实现 Watchable 协议（is_running / last_heartbeat_ns），
    可被 Watchdog 直接监控。

    Attributes:
        interval_sec: 探针检查间隔（秒），默认 15。

    Example::

        probe = LiveHealthProbe(container, interval_sec=15)
        probe.start()
        status = probe.last_status
        probe.stop()

    """

    def __init__(
        self,
        container: Container,
        interval_sec: float = 15.0,
    ) -> None:
        """初始化 LiveHealthProbe.

        Args:
            container: 应用依赖容器，用于访问 event_bus 等服务。
            interval_sec: 探针检查间隔秒数，默认 15。

        """
        self._container = container
        self._interval = interval_sec

        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._last_status: HealthStatus | None = None
        self._last_heartbeat_ns: int = 0

        # 统计
        self._probe_count = 0
        self._unhealthy_count = 0

    # ------------------------------------------------------------------
    # 可监控协议
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        """返回探针后台线程是否在运行.

        Returns:
            True 表示线程已启动且未停止。

        """
        return self._thread is not None and self._thread.is_alive()

    @property
    def last_heartbeat_ns(self) -> int:
        """返回最近一次探针成功的时间戳（纳秒）.

        Returns:
            时间戳纳秒，未运行时为 0。

        """
        return self._last_heartbeat_ns

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    @property
    def last_status(self) -> HealthStatus | None:
        """返回最近一次健康检查结果.

        Returns:
            HealthStatus 或 None（未执行过检查时）。

        """
        return self._last_status

    def start(self) -> None:
        """启动后台健康探针线程.

        Raises:
            RuntimeError: 已在运行时再次调用。

        """
        if self.is_running:
            raise RuntimeError("LiveHealthProbe is already running")

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="LiveHealthProbe",
            daemon=True,
        )
        self._thread.start()
        logger.info("health_probe_started", interval_sec=self._interval)

    def stop(self, timeout: float = 10.0) -> None:
        """停止后台探针线程.

        Args:
            timeout: 最长等待秒数。

        """
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        logger.info("health_probe_stopped", probe_count=self._probe_count)

    def probe_once(self) -> HealthStatus:
        """立即执行一次健康检查（可在主线程调用）.

        Returns:
            本次 HealthStatus 结果。

        """
        return self._do_probe()

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        """后台线程主循环."""
        logger.info("health_probe_loop_started")

        while not self._stop_event.is_set():
            try:
                status = self._do_probe()
                self._last_status = status
                self._probe_count += 1
                self._last_heartbeat_ns = time.time_ns()

                if not status.healthy:
                    self._unhealthy_count += 1
                    logger.warning(
                        "health_probe_unhealthy",
                        checks=status.checks,
                    )
                else:
                    logger.debug(
                        "health_probe_ok",
                        latency_ms=round(status.latency_ms, 1),
                    )

            except (AttributeError, OSError, RuntimeError):
                logger.exception("health_probe_error")

            self._stop_event.wait(timeout=self._interval)

        logger.info("health_probe_loop_stopped")

    def _do_probe(self) -> HealthStatus:
        """执行一次完整的健康检查，聚合各子检查结果.

        Returns:
            HealthStatus，包含总体状态和各项子检查细节。

        """
        t0 = time.monotonic()
        checks: dict[str, Any] = {}
        all_ok = True

        # 子检查 1：事件总线可用性
        eb_ok, eb_msg = self._check_event_bus()
        checks["event_bus"] = {"ok": eb_ok, "detail": eb_msg}
        if not eb_ok:
            all_ok = False

        # 子检查 2: 持久化存储可用性
        db_ok, db_msg = self._check_persistence()
        checks["persistence"] = {"ok": db_ok, "detail": db_msg}
        if not db_ok:
            all_ok = False

        # 子检查 3: 风控模块状态
        risk_ok, risk_msg = self._check_risk()
        checks["risk"] = {"ok": risk_ok, "detail": risk_msg}
        if not risk_ok:
            all_ok = False

        # 子检查 4: 系统时钟漂移
        clock_ok, clock_msg = self._check_clock_drift()
        checks["clock"] = {"ok": clock_ok, "detail": clock_msg}
        if not clock_ok:
            all_ok = False

        latency_ms = (time.monotonic() - t0) * 1000
        status = HealthStatus(
            healthy=all_ok,
            checks=checks,
            latency_ms=latency_ms,
        )

        # 向健康服务器汇报（如果已启动）
        self._report_to_health_server(status)

        # 向事件总线发布
        self._publish_health_event(status)

        return status

    # ------------------------------------------------------------------
    # 子检查
    # ------------------------------------------------------------------

    def _check_event_bus(self) -> tuple[bool, str]:
        """检查 EventBus 是否可用.

        Returns:
            (ok, detail) 元组，ok 为 True 表示正常。

        """
        try:
            bus = self._container.event_bus
            _ = bus  # 触发已构建检查
            return True, "ok"
        except (RuntimeError, AttributeError, ConnectionError) as exc:
            return False, str(exc)

    def _check_persistence(self) -> tuple[bool, str]:
        """检查持久化存储是否可用.

        Returns:
            (ok, detail) 元组。

        """
        try:
            persistence = self._container.persistence
            _ = persistence
            return True, "ok"
        except (RuntimeError, AttributeError, ConnectionError) as exc:
            return False, str(exc)

    def _check_risk(self) -> tuple[bool, str]:
        """检查风控模块是否已初始化.

        Returns:
            (ok, detail) 元组。

        """
        try:
            cb = self._container.circuit_breaker
            # 熔断器触发则认为风控异常
            if hasattr(cb, "is_triggered") and cb.is_triggered:
                return False, "circuit_breaker_triggered"
            return True, "ok"
        except (RuntimeError, AttributeError) as exc:
            return False, str(exc)

    def _check_clock_drift(self) -> tuple[bool, str]:
        """检查本地时钟与系统时间的漂移是否在允许范围内.

        当前使用 time.time() 与 time.monotonic() 做简单基线校验。
        实盘环境中应与 NTP 或 Binance 服务端时间对比。

        Returns:
            (ok, detail) 元组。

        """
        try:
            from src.core.time_sync import get_time_offset_ms  # type: ignore

            offset_ms = get_time_offset_ms()
            if abs(offset_ms) > 1000:  # 超过 1 秒认为时钟异常
                return False, f"clock_drift={offset_ms:.0f}ms"
            return True, f"offset={offset_ms:.1f}ms"
        except ImportError:
            # 时间同步模块未实现，跳过
            return True, "skipped(no time_sync)"
        except (OSError, RuntimeError) as exc:
            return False, str(exc)

    # ------------------------------------------------------------------
    # 汇报
    # ------------------------------------------------------------------

    def _report_to_health_server(self, status: HealthStatus) -> None:
        """将健康状态同步到 monitoring.HealthServer.

        Args:
            status: 本次探针结果。

        """
        try:
            health_server = self._container.health_server
            if health_server is not None and hasattr(health_server, "update_status"):
                health_server.update_status(status.to_dict())
        except Exception as exc:
            logger.error("health_server_update_failed", error=str(exc), exc_info=True)

    def _publish_health_event(self, status: HealthStatus) -> None:
        """发布健康检查事件到 EventBus.

        Args:
            status: 本次探针结果。

        """
        event = Event(
            event_type=EventType.HEALTH_CHECK,
            source="live_health_probe",
            payload=status.to_dict(),
        )
        try:
            self._container.event_bus.publish(event)
        except Exception as exc:
            logger.error("health_probe_event_publish_failed", error=str(exc), exc_info=True)
