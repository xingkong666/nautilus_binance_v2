"""运行期交易对忽略注册表.

当检测到账户中存在外部活动的交易对时，将其加入忽略集合，
后续执行链对这些交易对的信号一律跳过，避免与人工/其他系统冲突。
"""

from __future__ import annotations

import threading
from typing import Any

import structlog

from src.core.events import EventBus, RiskAlertEvent

logger = structlog.get_logger(__name__)


class IgnoredInstrumentRegistry:
    """线程安全的交易对忽略注册表."""

    def __init__(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        self._ignored: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def ignore(
        self,
        instrument_id: str,
        reason: str,
        source: str,
        details: dict[str, Any] | None = None,
    ) -> bool:
        normalized = str(instrument_id).strip()
        if not normalized:
            return False

        with self._lock:
            if normalized in self._ignored:
                return False
            self._ignored[normalized] = {
                "reason": reason,
                "source": source,
                "details": details or {},
            }

        logger.warning(
            "instrument_ignored",
            instrument_id=normalized,
            reason=reason,
            source=source,
            details=details or {},
        )
        self._event_bus.publish(
            RiskAlertEvent(
                level="WARNING",
                rule_name="instrument_ignored_external_activity",
                message=f"Ignoring instrument due to external activity: {normalized}",
                details={
                    "instrument_id": normalized,
                    "reason": reason,
                    **(details or {}),
                },
                source=source,
            )
        )
        return True

    def is_ignored(self, instrument_id: str) -> bool:
        with self._lock:
            return str(instrument_id).strip() in self._ignored

    def get(self, instrument_id: str) -> dict[str, Any] | None:
        with self._lock:
            value = self._ignored.get(str(instrument_id).strip())
            return dict(value) if value is not None else None

    def items(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {key: dict(value) for key, value in self._ignored.items()}
