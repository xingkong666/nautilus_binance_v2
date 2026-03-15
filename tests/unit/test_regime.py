from __future__ import annotations

import pandas as pd

from src.backtest.regime import evaluate_symbol_regime_from_data, regime_allows_strategy


def _ohlc_from_close(values: list[float]) -> pd.DataFrame:
    index = pd.date_range("2024-01-01", periods=len(values), freq="1h", tz="UTC")
    close = pd.Series(values, index=index)
    return pd.DataFrame(
        {
            "open": close.shift().fillna(close.iloc[0]),
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
        }
    )


def test_regime_vetoes_weak_trend() -> None:
    ohlc = _ohlc_from_close([100.0 + (i % 3) * 0.1 for i in range(120)])
    funding = pd.DataFrame({"timestamp": [], "funding_rate": []})

    snapshot = evaluate_symbol_regime_from_data(
        symbol="ETHUSDT",
        ohlc=ohlc,
        funding_window=funding,
        config={
            "min_abs_slope_ratio": 0.005,
            "min_abs_gap_ratio": 0.002,
            "min_adx": 25.0,
        },
    )

    assert snapshot.regime_pass is False
    assert snapshot.reason == "weak_trend"


def test_regime_allows_strong_slope_even_with_high_funding() -> None:
    ohlc = _ohlc_from_close([100.0 + i * 1.5 for i in range(120)])
    funding = pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-05-01", periods=5, freq="8h", tz="UTC"),
            "funding_rate": [0.00011] * 5,
        }
    )

    snapshot = evaluate_symbol_regime_from_data(
        symbol="BTCUSDT",
        ohlc=ohlc,
        funding_window=funding,
        config={
            "min_abs_slope_ratio": 0.005,
            "min_abs_gap_ratio": 0.002,
            "min_adx": 25.0,
            "max_abs_funding_rate": 0.00008,
            "funding_slope_override_ratio": 0.007,
        },
    )

    assert snapshot.regime_pass is True
    assert snapshot.reason == "pass"


def test_regime_returns_insufficient_data_when_bars_too_short() -> None:
    ohlc = _ohlc_from_close([100.0 + i for i in range(20)])
    funding = pd.DataFrame({"timestamp": [], "funding_rate": []})

    snapshot = evaluate_symbol_regime_from_data(
        symbol="BTCUSDT",
        ohlc=ohlc,
        funding_window=funding,
        config={},
    )

    assert snapshot.regime_pass is True
    assert snapshot.reason == "insufficient_data"


def test_regime_allows_strategy_only_blocks_configured_names() -> None:
    snapshot = evaluate_symbol_regime_from_data(
        symbol="ETHUSDT",
        ohlc=_ohlc_from_close([100.0 + (i % 3) * 0.1 for i in range(120)]),
        funding_window=pd.DataFrame({"timestamp": [], "funding_rate": []}),
        config={
            "min_abs_slope_ratio": 0.005,
            "min_abs_gap_ratio": 0.002,
            "min_adx": 25.0,
        },
    )

    assert snapshot.regime_pass is False
    assert regime_allows_strategy(
        strategy_name="vegas_tunnel",
        snapshot=snapshot,
        veto_strategy_names=["vegas_tunnel"],
    ) is False
    assert regime_allows_strategy(
        strategy_name="ema_pullback_atr",
        snapshot=snapshot,
        veto_strategy_names=["vegas_tunnel"],
    ) is True
