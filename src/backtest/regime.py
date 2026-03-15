"""Walk-forward regime 过滤工具."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
from nautilus_trader.persistence.catalog import ParquetDataCatalog

from src.core.enums import Interval


@dataclass(frozen=True)
class SymbolRegimeSnapshot:
    symbol: str
    slope_ratio: float
    ema_gap_ratio: float
    adx: float
    funding_mean: float
    funding_abs_mean: float
    weak_trend: bool
    overheated: bool
    regime_pass: bool
    reason: str


def regime_allows_strategy(
    *,
    strategy_name: str,
    snapshot: SymbolRegimeSnapshot | None,
    veto_strategy_names: list[str] | None,
) -> bool:
    if snapshot is None or snapshot.regime_pass:
        return True
    if not veto_strategy_names:
        return False
    return strategy_name not in set(veto_strategy_names)


def pandas_freq_for_interval(interval: Interval) -> str:
    mapping = {
        Interval.MINUTE_1: "1min",
        Interval.MINUTE_5: "5min",
        Interval.MINUTE_15: "15min",
        Interval.HOUR_1: "1h",
        Interval.HOUR_4: "4h",
        Interval.DAY_1: "1d",
    }
    return mapping[interval]


def compute_adx(ohlc: pd.DataFrame, period: int) -> pd.Series:
    high = ohlc["high"]
    low = ohlc["low"]
    close = ohlc["close"]

    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

    tr = pd.concat(
        [
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    plus_di = 100.0 * (plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr)
    minus_di = 100.0 * (minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr)
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    return dx.ewm(alpha=1 / period, adjust=False).mean()


def load_resampled_ohlc(
    *,
    catalog: ParquetDataCatalog,
    instrument_id: Any,
    start: dt.date,
    end: dt.date,
    interval: Interval,
) -> pd.DataFrame:
    start_ns = int(pd.Timestamp(start.isoformat(), tz="UTC").value)
    end_ns = int(pd.Timestamp(end.isoformat(), tz="UTC").replace(hour=23, minute=59, second=59).value)
    bar_type = f"{instrument_id}-1-MINUTE-LAST-EXTERNAL"
    bars = catalog.bars(bar_types=[bar_type], start=start_ns, end=end_ns)
    if not bars:
        return pd.DataFrame(columns=["open", "high", "low", "close"])

    rows = [
        {
            "timestamp": pd.Timestamp(bar.ts_event, tz="UTC"),
            "open": float(bar.open),
            "high": float(bar.high),
            "low": float(bar.low),
            "close": float(bar.close),
        }
        for bar in bars
    ]
    df = pd.DataFrame(rows).set_index("timestamp").sort_index()
    if interval == Interval.MINUTE_1:
        return df

    rule = pandas_freq_for_interval(interval)
    return (
        df.resample(rule)
        .agg({"open": "first", "high": "max", "low": "min", "close": "last"})
        .dropna()
    )


def load_funding_window(
    *,
    features_dir: Path,
    raw_dir: Path,
    symbol: str,
    end: dt.date,
    lookback_days: int,
) -> pd.DataFrame:
    candidates = [
        features_dir / f"funding_rates_{symbol}.parquet",
        raw_dir / "funding" / f"{symbol}.csv",
    ]
    for path in candidates:
        if not path.exists():
            continue
        df = pd.read_parquet(path) if path.suffix == ".parquet" else pd.read_csv(path)
        if df.empty:
            continue
        window_end = pd.Timestamp(end.isoformat(), tz="UTC").replace(hour=23, minute=59, second=59)
        window_start = window_end - pd.Timedelta(days=lookback_days)
        ts_col = "timestamp" if "timestamp" in df.columns else "fundingTime"
        rate_col = "funding_rate" if "funding_rate" in df.columns else "fundingRate"
        if ts_col == "fundingTime":
            timestamps = pd.to_datetime(df[ts_col], utc=True, errors="coerce", unit="ms")
        else:
            timestamps = pd.to_datetime(df[ts_col], utc=True, errors="coerce")
        normalized = pd.DataFrame(
            {
                "timestamp": timestamps,
                "funding_rate": pd.to_numeric(df[rate_col], errors="coerce"),
            }
        ).dropna()
        return normalized[(normalized["timestamp"] >= window_start) & (normalized["timestamp"] <= window_end)]
    return pd.DataFrame(columns=["timestamp", "funding_rate"])


def evaluate_symbol_regime_from_data(
    *,
    symbol: str,
    ohlc: pd.DataFrame,
    funding_window: pd.DataFrame,
    config: dict[str, Any],
) -> SymbolRegimeSnapshot:
    if ohlc.empty or len(ohlc) < 80:
        return SymbolRegimeSnapshot(
            symbol=symbol,
            slope_ratio=0.0,
            ema_gap_ratio=0.0,
            adx=0.0,
            funding_mean=0.0,
            funding_abs_mean=0.0,
            weak_trend=False,
            overheated=False,
            regime_pass=True,
            reason="insufficient_data",
        )

    slope_ema_span = int(config.get("slope_ema_span", 24))
    gap_ema_span = int(config.get("gap_ema_span", 72))
    slope_lookback_bars = int(config.get("slope_lookback_bars", 24))
    adx_period = int(config.get("adx_period", 14))

    close = ohlc["close"]
    ema_fast = close.ewm(span=slope_ema_span, adjust=False).mean()
    ema_slow = close.ewm(span=gap_ema_span, adjust=False).mean()
    if len(ema_fast) <= slope_lookback_bars:
        return SymbolRegimeSnapshot(
            symbol=symbol,
            slope_ratio=0.0,
            ema_gap_ratio=0.0,
            adx=0.0,
            funding_mean=0.0,
            funding_abs_mean=0.0,
            weak_trend=False,
            overheated=False,
            regime_pass=True,
            reason="insufficient_data",
        )

    slope_ratio = float((ema_fast.iloc[-1] - ema_fast.iloc[-1 - slope_lookback_bars]) / close.iloc[-1])
    ema_gap_ratio = float((ema_fast.iloc[-1] - ema_slow.iloc[-1]) / close.iloc[-1])
    adx_value = float(compute_adx(ohlc, adx_period).iloc[-1])

    funding_mean = float(funding_window["funding_rate"].mean()) if not funding_window.empty else 0.0
    funding_abs_mean = float(funding_window["funding_rate"].abs().mean()) if not funding_window.empty else 0.0

    weak_trend = (
        abs(slope_ratio) < float(config.get("min_abs_slope_ratio", 0.005))
        and abs(ema_gap_ratio) < float(config.get("min_abs_gap_ratio", 0.002))
        and adx_value < float(config.get("min_adx", 25.0))
    )
    overheated = funding_abs_mean > float(config.get("max_abs_funding_rate", 0.00008))
    strong_slope_override = abs(slope_ratio) >= float(config.get("funding_slope_override_ratio", 0.007))

    if weak_trend:
        return SymbolRegimeSnapshot(
            symbol=symbol,
            slope_ratio=slope_ratio,
            ema_gap_ratio=ema_gap_ratio,
            adx=adx_value,
            funding_mean=funding_mean,
            funding_abs_mean=funding_abs_mean,
            weak_trend=True,
            overheated=overheated,
            regime_pass=False,
            reason="weak_trend",
        )
    if overheated and not strong_slope_override:
        return SymbolRegimeSnapshot(
            symbol=symbol,
            slope_ratio=slope_ratio,
            ema_gap_ratio=ema_gap_ratio,
            adx=adx_value,
            funding_mean=funding_mean,
            funding_abs_mean=funding_abs_mean,
            weak_trend=False,
            overheated=True,
            regime_pass=False,
            reason="overheated_funding",
        )

    return SymbolRegimeSnapshot(
        symbol=symbol,
        slope_ratio=slope_ratio,
        ema_gap_ratio=ema_gap_ratio,
        adx=adx_value,
        funding_mean=funding_mean,
        funding_abs_mean=funding_abs_mean,
        weak_trend=weak_trend,
        overheated=overheated,
        regime_pass=True,
        reason="pass",
    )


def evaluate_symbol_regime(
    *,
    catalog: ParquetDataCatalog,
    features_dir: Path,
    raw_dir: Path,
    instrument_id: Any,
    symbol: str,
    start: dt.date,
    end: dt.date,
    interval: Interval,
    config: dict[str, Any],
) -> SymbolRegimeSnapshot:
    ohlc = load_resampled_ohlc(
        catalog=catalog,
        instrument_id=instrument_id,
        start=start,
        end=end,
        interval=interval,
    )
    funding_window = load_funding_window(
        features_dir=features_dir,
        raw_dir=raw_dir,
        symbol=symbol,
        end=end,
        lookback_days=int(config.get("funding_lookback_days", 7)),
    )
    return evaluate_symbol_regime_from_data(
        symbol=symbol,
        ohlc=ohlc,
        funding_window=funding_window,
        config=config,
    )
