from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest

from src.data.loaders import BinanceFuturesDownloader, KlineCatalogLoader
from src.data.validators import DataValidationError, validate_kline_dataframe


def _sample_df() -> pd.DataFrame:
    """构造一份最小可用的 K 线样本数据.

    Returns:
        含 open_time/open/high/low/close/volume 的 DataFrame
    """
    return pd.DataFrame(
        {
            "open_time": [1_600_000_000_000, 1_600_000_060_000],
            "open": [1, 2],
            "high": [2, 3],
            "low": [1, 2],
            "close": [2, 3],
            "volume": [10, 12],
        }
    )


def test_validate_kline_dataframe_ok() -> None:
    """验证: 合法数据应通过校验.

    Returns:
        None
    """
    df = _sample_df()
    validate_kline_dataframe(df)


def test_validate_kline_dataframe_missing_col() -> None:
    """验证: 缺失必要列应抛出异常.

    Returns:
        None
    """
    df = _sample_df().drop(columns=["open"])
    with pytest.raises(DataValidationError):
        validate_kline_dataframe(df)


def test_validate_kline_dataframe_duplicate_ts() -> None:
    """验证: 重复时间戳应抛出异常.

    Returns:
        None
    """
    df = _sample_df()
    df.loc[1, "open_time"] = df.loc[0, "open_time"]
    with pytest.raises(DataValidationError):
        validate_kline_dataframe(df)


def test_downloader_skip_existing(tmp_path: Path) -> None:
    """验证: 已存在的 CSV 会被识别为可跳过.

    Args:
        tmp_path: pytest 提供的临时目录

    Returns:
        None
    """
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    downloader = BinanceFuturesDownloader(raw_dir)

    date = dt.date(2025, 11, 1)
    csv_dir = raw_dir / "futures" / "BTCUSDT"
    csv_dir.mkdir(parents=True, exist_ok=True)
    csv_path = csv_dir / f"BTCUSDT-1m-{date.isoformat()}.csv"
    csv_path.write_text("ok")

    path = downloader._validate_existing_csv(csv_path)
    assert path is True


def test_read_and_normalize(tmp_path: Path) -> None:
    """验证: CSV 读取后会补齐 ts_event 列.

    Args:
        tmp_path: pytest 提供的临时目录

    Returns:
        None
    """
    catalog_dir = tmp_path / "catalog"
    loader = KlineCatalogLoader(catalog_dir)

    csv_path = tmp_path / "sample.csv"
    df = _sample_df()
    df.to_csv(csv_path, index=False)

    result = loader._read_and_normalize(csv_path)
    assert "ts_event" in result.columns
