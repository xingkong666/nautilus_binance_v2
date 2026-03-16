#!/usr/bin/env python3
"""下载 Binance USD-M 资金费率并落盘."""
# ruff: noqa: E402

from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from pathlib import Path

import httpx
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.config import load_app_config
from src.core.logging import setup_logging
from src.data.feature_store import FeatureStore
from src.data.funding import BINANCE_FUNDING_URL, datetime_to_ms, funding_output_paths, normalize_funding_rates


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments.

    Returns:
        argparse.Namespace: Parsed command-line arguments.
    """
    parser = argparse.ArgumentParser(description="下载 Binance USD-M funding rate")
    parser.add_argument("--symbol", required=True, help="交易对，如 BTCUSDT")
    parser.add_argument("--start", required=True, help="开始日期 YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="结束日期 YYYY-MM-DD")
    parser.add_argument("--env", default=None, help="环境配置")
    parser.add_argument("--limit", type=int, default=1000, help="单次请求条数，默认 1000")
    parser.add_argument("--sleep-ms", type=int, default=200, help="分页请求间隔，默认 200ms")
    return parser.parse_args()


def fetch_funding_rates(
    symbol: str,
    start: dt.datetime,
    end: dt.datetime,
    limit: int,
    sleep_ms: int,
) -> pd.DataFrame:
    """Fetch funding rates.

    Args:
        symbol: Trading symbol to process.
        start: Start value for the operation.
        end: End value for the operation.
        limit: Limit.
        sleep_ms: Sleep ms.

    Returns:
        pd.DataFrame: Dataframe produced by the operation.
    """
    rows: list[dict] = []
    start_ms = datetime_to_ms(start)
    end_ms = datetime_to_ms(end)

    with httpx.Client(timeout=30.0) as client:
        while start_ms < end_ms:
            resp = client.get(
                BINANCE_FUNDING_URL,
                params={
                    "symbol": symbol,
                    "startTime": start_ms,
                    "endTime": end_ms,
                    "limit": limit,
                },
            )
            resp.raise_for_status()
            chunk = resp.json()
            if not chunk:
                break
            rows.extend(chunk)
            last_ts = int(chunk[-1]["fundingTime"])
            start_ms = last_ts + 1
            if len(chunk) < limit:
                break
            time.sleep(sleep_ms / 1000.0)

    return normalize_funding_rates(rows)


def main() -> None:
    """Run the script entrypoint."""
    args = parse_args()
    setup_logging(level="INFO")
    config = load_app_config(env=args.env)

    start = dt.datetime.combine(dt.date.fromisoformat(args.start), dt.time.min, tzinfo=dt.UTC)
    end = dt.datetime.combine(dt.date.fromisoformat(args.end), dt.time.max, tzinfo=dt.UTC)
    if start > end:
        raise ValueError("start 不能大于 end")

    df = fetch_funding_rates(
        symbol=args.symbol.upper(),
        start=start,
        end=end,
        limit=args.limit,
        sleep_ms=args.sleep_ms,
    )
    raw_path, feature_path = funding_output_paths(
        raw_dir=config.data.raw_dir,
        features_dir=config.data.features_dir,
        symbol=args.symbol.upper(),
    )
    df.to_csv(raw_path, index=False)
    FeatureStore(config.data.features_dir).save_features(f"funding_rates_{args.symbol.upper()}", df)

    print(f"rows={len(df)}")
    print(f"raw_csv={raw_path}")
    print(f"feature_parquet={feature_path}")


if __name__ == "__main__":
    main()
