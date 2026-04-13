"""周期自动快照调度器.

后台线程，每 interval_sec 秒触发一次状态快照保存。
实现 Watchable 协议，可注册到 Watchdog 进行心跳监控。

典型用法::

    scheduler = SnapshotScheduler(
        snapshot_mgr=container.snapshot_manager,
        state_provider=lambda: build_system_snapshot(container),
        interval_sec=60,
    )
    scheduler.start()
    # 注册到 Watchdog
    watchdog.register("snapshot_scheduler", scheduler, max_heartbeat_gap_sec=120)
    ...
    scheduler.stop()
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from src.state.snapshot import SnapshotManager, SystemSnapshot

logger = structlog.get_logger(__name__)


class SnapshotScheduler:
    """后台线程，周期性保存系统状态快照.

    实现 Watchable 协议（``is_running`` / ``last_heartbeat_ns``），
    可直接注册到 Watchdog 进行存活监控。

    Args:
        snapshot_mgr: SnapshotManager 实例，负责实际的文件写入。
        state_provider: 可调用对象，调用时返回当前 SystemSnapshot。
        interval_sec: 两次快照之间的间隔秒数，默认 60。
        cleanup_keep: 保留的快照文件数量，默认 20。

    Example::

        scheduler = SnapshotScheduler(mgr, provider, interval_sec=60)
        scheduler.start()
        scheduler.stop()

    """

    def __init__(
        self,
        snapshot_mgr: SnapshotManager,
        state_provider: Callable[[], SystemSnapshot],
        interval_sec: float = 60.0,
        cleanup_keep: int = 20,
    ) -> None:
        """初始化 SnapshotScheduler."""
        self._mgr = snapshot_mgr
        self._state_provider = state_provider
        self._interval = interval_sec
        self._cleanup_keep = cleanup_keep

        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._last_heartbeat_ns: int = time.time_ns()

        # 统计
        self._snapshot_count = 0
        self._error_count = 0

    # ------------------------------------------------------------------
    # Watchable 协议
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        """返回后台线程是否正在运行."""
        return self._thread is not None and self._thread.is_alive()

    @property
    def last_heartbeat_ns(self) -> int:
        """返回最近一次成功快照的时间戳（纳秒）."""
        return self._last_heartbeat_ns

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def start(self) -> None:
        """启动后台快照线程.

        Raises:
            RuntimeError: 已在运行时再次调用 start()。

        """
        if self.is_running:
            raise RuntimeError("SnapshotScheduler is already running")

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="SnapshotScheduler",
            daemon=True,
        )
        self._thread.start()
        logger.info("snapshot_scheduler_started", interval_sec=self._interval)

    def stop(self, timeout: float = 10.0) -> None:
        """停止后台快照线程.

        Args:
            timeout: 最长等待秒数。

        """
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        logger.info("snapshot_scheduler_stopped", snapshot_count=self._snapshot_count)

    def snapshot_now(self) -> None:
        """立即触发一次快照（可在主线程调用）."""
        self._do_snapshot()

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        """后台线程主循环."""
        logger.info("snapshot_scheduler_loop_started")

        while not self._stop_event.is_set():
            self._do_snapshot()
            self._stop_event.wait(timeout=self._interval)

        logger.info("snapshot_scheduler_loop_stopped")

    def _do_snapshot(self) -> None:
        """执行一次快照保存，捕获所有异常（快照失败不中断主流程）."""
        try:
            snapshot = self._state_provider()
            self._mgr.save(snapshot)
            self._mgr.cleanup(keep_count=self._cleanup_keep)
            self._snapshot_count += 1
            self._last_heartbeat_ns = time.time_ns()
            logger.debug(
                "snapshot_scheduled_ok",
                count=self._snapshot_count,
                positions=len(snapshot.positions),
            )
        except Exception as exc:
            self._error_count += 1
            logger.warning(
                "snapshot_scheduled_failed",
                error=str(exc),
                error_count=self._error_count,
            )
