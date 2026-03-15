"""资金费率数据工具."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pandas as pd

BINANCE_FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"


def datetime_to_ms(value: dt.datetime) -> int:
    """UTC datetime 转毫秒时间戳."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return int(value.timestamp() * 1000)


def normalize_funding_rates(rows: list[dict[str, Any]]) -> pd.DataFrame:
    """标准化 Binance fundingRate 响应."""
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=["symbol", "timestamp", "funding_rate", "mark_price"])

    timestamp_col = "fundingTime" if "fundingTime" in df.columns else "timestamp"
    rate_col = "fundingRate" if "fundingRate" in df.columns else "funding_rate"
    mark_col = "markPrice" if "markPrice" in df.columns else "mark_price"

    normalized = pd.DataFrame(
        {
            "symbol": df.get("symbol", ""),
            "timestamp": pd.to_datetime(df[timestamp_col], unit="ms", utc=True, errors="coerce"),
            "funding_rate": pd.to_numeric(df[rate_col], errors="coerce"),
            "mark_price": pd.to_numeric(df[mark_col], errors="coerce") if mark_col in df.columns else None,
        }
    )
    if "mark_price" not in normalized.columns:
        normalized["mark_price"] = None
    normalized = normalized.dropna(subset=["timestamp", "funding_rate"])
    normalized = normalized.sort_values("timestamp").drop_duplicates(subset=["timestamp"]).reset_index(drop=True)
    return normalized


def funding_output_paths(raw_dir: Path, features_dir: Path, symbol: str) -> tuple[Path, Path]:
    """返回 funding 原始 CSV 与特征 parquet 路径."""
    raw_path = raw_dir / "funding" / f"{symbol}.csv"
    feature_path = features_dir / f"funding_rates_{symbol}.parquet"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    feature_path.parent.mkdir(parents=True, exist_ok=True)
    return raw_path, feature_path
