"""订单生命周期管理模块.

监控 PENDING_CANCEL 状态的订单，检测超时情况并发布告警。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

import structlog

from src.core.events import EventBus, RiskAlertEvent

logger = structlog.get_logger(__name__)


@dataclass
class _PendingEntry:
    """PENDING_CANCEL 订单条目."""

    client_order_id: str
    venue_order_id: str
    registered_at: float = field(default_factory=time.monotonic)


class OrderLifecycleManager:
    """订单生命周期管理器.

    跟踪 PENDING_CANCEL 状态的订单，监控超时情况。
    """

    def __init__(self, event_bus: EventBus | None = None) -> None:
        """初始化订单生命周期管理器.

        Args:
            event_bus: 事件总线，用于发布告警事件。
        """
        self._event_bus = event_bus
        self._pending_orders: dict[str, _PendingEntry] = {}
        self._lock = threading.Lock()
        self._monitor_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._timeout_sec = 60.0
        self._check_interval_sec = 10.0

    def register_pending_cancel(self, client_order_id: str, venue_order_id: str) -> None:
        """注册一笔 PENDING_CANCEL 订单.

        Args:
            client_order_id: 客户端订单 ID。
            venue_order_id: 交易所订单 ID。
        """
        with self._lock:
            self._pending_orders[client_order_id] = _PendingEntry(
                client_order_id=client_order_id,
                venue_order_id=venue_order_id,
            )

        logger.debug(
            "registered_pending_cancel",
            client_order_id=client_order_id,
            venue_order_id=venue_order_id,
        )

    def on_cancel_confirmed(self, client_order_id: str) -> None:
        """取消确认回调，移除已完成取消的订单.

        Args:
            client_order_id: 客户端订单 ID。
        """
        with self._lock:
            entry = self._pending_orders.pop(client_order_id, None)

        if entry:
            logger.debug(
                "cancel_confirmed",
                client_order_id=client_order_id,
                venue_order_id=entry.venue_order_id,
            )

    def get_timed_out(self, timeout_sec: float = 60.0) -> list[dict[str, Any]]:
        """获取超时的 PENDING_CANCEL 订单.

        Args:
            timeout_sec: 超时时间（秒）。

        Returns:
            超时的订单信息列表。
        """
        current_time = time.monotonic()
        timed_out = []

        with self._lock:
            for entry in self._pending_orders.values():
                if current_time - entry.registered_at >= timeout_sec:
                    timed_out.append(
                        {
                            "client_order_id": entry.client_order_id,
                            "venue_order_id": entry.venue_order_id,
                            "registered_at": entry.registered_at,
                            "elapsed_sec": current_time - entry.registered_at,
                        }
                    )

        return timed_out

    def start_monitor(self, timeout_sec: float = 60.0, check_interval_sec: float = 10.0) -> None:
        """启动后台监控线程.

        Args:
            timeout_sec: 超时时间（秒）。
            check_interval_sec: 检查间隔（秒）。
        """
        if self._monitor_thread is not None:
            logger.warning("monitor_already_started")
            return

        self._timeout_sec = timeout_sec
        self._check_interval_sec = check_interval_sec
        self._stop_event.clear()

        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            name="OrderLifecycleMonitor",
            daemon=True,
        )
        self._monitor_thread.start()

        logger.info(
            "monitor_started",
            timeout_sec=timeout_sec,
            check_interval_sec=check_interval_sec,
        )

    def stop_monitor(self) -> None:
        """停止后台监控线程."""
        if self._monitor_thread is None:
            return

        self._stop_event.set()
        self._monitor_thread.join(timeout=5.0)
        self._monitor_thread = None

        logger.info("monitor_stopped")

    def _monitor_loop(self) -> None:
        """后台监控循环."""
        while not self._stop_event.wait(self._check_interval_sec):
            try:
                timed_out_orders = self.get_timed_out(self._timeout_sec)

                if timed_out_orders:
                    logger.warning(
                        "pending_cancel_timeout_detected",
                        count=len(timed_out_orders),
                        orders=timed_out_orders,
                    )

                    if self._event_bus:
                        self._event_bus.publish(
                            RiskAlertEvent(
                                level="WARNING",
                                rule_name="pending_cancel_timeout",
                                message=f"检测到 {len(timed_out_orders)} 笔 PENDING_CANCEL 超时订单",
                                details={"orders": timed_out_orders},
                            )
                        )

                    # 从跟踪列表中移除超时订单，避免重复告警
                    with self._lock:
                        for order in timed_out_orders:
                            self._pending_orders.pop(order["client_order_id"], None)

            except Exception as e:
                logger.error("monitor_loop_error", error=str(e), exc_info=True)
