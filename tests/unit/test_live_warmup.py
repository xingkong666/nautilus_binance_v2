"""Live 历史预热测试."""

from __future__ import annotations

from datetime import UTC, datetime

from nautilus_trader.adapters.binance.common.enums import BinanceEnvironment
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Price, Quantity

from src.core.events import EventBus
from src.live.warmup import bar_type_to_binance_interval, fetch_binance_futures_bars, preload_strategy_warmup
from src.strategy.ema_cross import EMACrossConfig, EMACrossStrategy

INSTRUMENT_ID = InstrumentId.from_str("BTCUSDT-PERP.BINANCE")
BAR_TYPE = BarType.from_str("BTCUSDT-PERP.BINANCE-15-MINUTE-LAST-INTERNAL@1-MINUTE-EXTERNAL")


def _make_historical_bars(prices: list[int]) -> list[Bar]:
    base_ts = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp() * 1_000_000_000)
    bars: list[Bar] = []
    for index, close in enumerate(prices):
        ts = base_ts + index * 900 * 1_000_000_000
        bars.append(
            Bar(
                bar_type=BAR_TYPE,
                open=Price.from_str(str(close)),
                high=Price.from_str(str(close + 1)),
                low=Price.from_str(str(close - 1)),
                close=Price.from_str(str(close)),
                volume=Quantity.from_str("1"),
                ts_event=ts,
                ts_init=ts,
            )
        )
    return bars


def test_bar_type_to_binance_interval_supports_internal_and_external() -> None:
    """Verify that bar type to Binance interval supports internal and external bars."""
    external = BarType.from_str("BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL")

    assert bar_type_to_binance_interval(BAR_TYPE) == "15m"
    assert bar_type_to_binance_interval(external) == "1h"


def test_fetch_binance_futures_bars_parses_klines_payload(monkeypatch) -> None:
    """Verify that fetch Binance futures bars parses Kline payload."""
    captured: dict[str, object] = {}

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> list[list[object]]:
            return [
                [1704067200000, "100", "101", "99", "100.5", "12.3", 1704068099999],
                [1704068100000, "100.5", "102", "100", "101.5", "8.1", 1704068999999],
            ]

    def _fake_get(url: str, *, params: dict[str, object], timeout: float) -> _Response:
        captured["url"] = url
        captured["params"] = params
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr("src.live.warmup.httpx.get", _fake_get)

    bars = fetch_binance_futures_bars(
        symbol="BTCUSDT",
        bar_type=BAR_TYPE,
        interval="15m",
        limit=2,
        environment=BinanceEnvironment.LIVE,
    )

    assert captured["url"] == "https://fapi.binance.com/fapi/v1/klines"
    assert captured["params"] == {"symbol": "BTCUSDT", "interval": "15m", "limit": 2}
    assert len(bars) == 2
    assert bars[0].bar_type == BAR_TYPE
    assert str(bars[0].close) == "100.5"
    assert bars[1].ts_event > bars[0].ts_event


def test_preload_strategy_warmup_fetches_and_applies_bars(monkeypatch) -> None:
    """Verify that preload strategy warmup fetches and applies bars."""
    strategy = EMACrossStrategy(
        config=EMACrossConfig(
            instrument_id=INSTRUMENT_ID,
            bar_type=BAR_TYPE,
            fast_ema_period=3,
            slow_ema_period=5,
            entry_min_atr_ratio=0.0,
        ),
        event_bus=EventBus(),
    )

    monkeypatch.setattr(
        "src.live.warmup.fetch_binance_futures_bars",
        lambda **kwargs: _make_historical_bars([100, 101, 102, 103, 104, 105]),
    )

    loaded = preload_strategy_warmup(
        strategy,
        environment=BinanceEnvironment.LIVE,
    )

    assert loaded == 6
    assert strategy.indicators_initialized() is True
    assert strategy._bar_index == 6
    assert strategy._warmup_history_preloaded is True
