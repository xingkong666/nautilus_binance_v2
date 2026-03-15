from __future__ import annotations

import datetime as dt
from pathlib import Path

from src.data.funding import datetime_to_ms, funding_output_paths, normalize_funding_rates


def test_normalize_funding_rates_sorts_and_deduplicates() -> None:
    df = normalize_funding_rates(
        [
            {"symbol": "BTCUSDT", "fundingTime": 2_000, "fundingRate": "0.0002", "markPrice": "101"},
            {"symbol": "BTCUSDT", "fundingTime": 1_000, "fundingRate": "0.0001", "markPrice": "100"},
            {"symbol": "BTCUSDT", "fundingTime": 1_000, "fundingRate": "0.0001", "markPrice": "100"},
        ]
    )

    assert len(df) == 2
    assert list(df["funding_rate"]) == [0.0001, 0.0002]


def test_datetime_to_ms_assumes_utc() -> None:
    value = datetime_to_ms(dt.datetime(1970, 1, 1, 0, 0, 1))
    assert value == 1000


def test_funding_output_paths_create_expected_names(tmp_path: Path) -> None:
    raw_path, feature_path = funding_output_paths(tmp_path / "raw", tmp_path / "features", "BTCUSDT")
    assert raw_path == tmp_path / "raw" / "funding" / "BTCUSDT.csv"
    assert feature_path == tmp_path / "features" / "funding_rates_BTCUSDT.parquet"
