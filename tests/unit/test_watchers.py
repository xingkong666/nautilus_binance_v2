"""Watchers 单元测试."""

from __future__ import annotations

from src.core.events import EventBus, RiskAlertEvent
from src.monitoring.watchers import RiskAlertWatcher


class _AlertManagerSpy:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def send_direct(
        self,
        level,
        rule_name,
        message,
        details=None,
        source="",
    ) -> int:
        self.calls.append(
            {
                "level": level,
                "rule_name": rule_name,
                "message": message,
                "details": details,
                "source": source,
            }
        )
        return 1


def test_risk_alert_watcher_dedupes_same_rule_and_instrument() -> None:
    bus = EventBus()
    spy = _AlertManagerSpy()
    RiskAlertWatcher(event_bus=bus, alert_manager=spy, cooldown_seconds=60.0)  # type: ignore[arg-type]

    event = RiskAlertEvent(
        source="order_router",
        level="WARNING",
        rule_name="order_router_quantity_normalized",
        message="normalized",
        details={"instrument_id": "BTCUSDT-PERP.BINANCE"},
    )

    bus.publish(event)
    bus.publish(event)

    assert len(spy.calls) == 1


def test_risk_alert_watcher_allows_different_instruments() -> None:
    bus = EventBus()
    spy = _AlertManagerSpy()
    RiskAlertWatcher(event_bus=bus, alert_manager=spy, cooldown_seconds=60.0)  # type: ignore[arg-type]

    bus.publish(
        RiskAlertEvent(
            source="order_router",
            level="WARNING",
            rule_name="order_router_quantity_normalized",
            message="normalized",
            details={"instrument_id": "BTCUSDT-PERP.BINANCE"},
        )
    )
    bus.publish(
        RiskAlertEvent(
            source="order_router",
            level="WARNING",
            rule_name="order_router_quantity_normalized",
            message="normalized",
            details={"instrument_id": "ETHUSDT-PERP.BINANCE"},
        )
    )

    assert len(spy.calls) == 2


def test_risk_alert_watcher_sends_again_after_cooldown(monkeypatch) -> None:
    bus = EventBus()
    spy = _AlertManagerSpy()
    watcher = RiskAlertWatcher(event_bus=bus, alert_manager=spy, cooldown_seconds=60.0)  # type: ignore[arg-type]

    now = {"t": 1000.0}

    def _fake_time() -> float:
        return now["t"]

    monkeypatch.setattr("src.monitoring.watchers.time.time", _fake_time)

    event = RiskAlertEvent(
        source="order_router",
        level="WARNING",
        rule_name="order_router_quantity_normalized",
        message="normalized",
        details={"instrument_id": "BTCUSDT-PERP.BINANCE"},
    )

    bus.publish(event)
    now["t"] = 1020.0
    bus.publish(event)
    now["t"] = 1061.0
    bus.publish(event)

    assert watcher is not None
    assert len(spy.calls) == 2
