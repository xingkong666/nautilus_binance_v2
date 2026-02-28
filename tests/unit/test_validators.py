"""数据验证器单元测试.

覆盖 validate_kline_dataframe 的各种边界情况:
- 合法数据通过
- 缺少列报错
- 空值报错
- OHLC 逻辑错误报错
- 重复时间戳报错
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.data.validators import DataValidationError, validate_kline_dataframe


def _make_df(**overrides: object) -> pd.DataFrame:
    """创建标准测试用 K 线 DataFrame.

    Args:
        **overrides: 覆盖默认列的字段值, key 为列名, value 为列数据.

    Returns:
        包含完整 OHLCV 字段的 DataFrame.
    """
    data: dict[str, object] = {
        "open_time": [1000, 2000, 3000],
        "open": [100.0, 101.0, 102.0],
        "high": [105.0, 106.0, 107.0],
        "low": [95.0, 96.0, 97.0],
        "close": [103.0, 104.0, 105.0],
        "volume": [10.0, 20.0, 30.0],
    }
    data.update(overrides)
    return pd.DataFrame(data)


def test_valid_data() -> None:
    """验证: 合法 OHLCV 数据应通过所有检查, 不抛出异常.

    Returns:
        None
    """
    df = _make_df()
    validate_kline_dataframe(df)


def test_missing_columns() -> None:
    """验证: 缺少必要列时应抛出 DataValidationError.

    Returns:
        None
    """
    df = pd.DataFrame({"open_time": [1], "open": [100]})
    with pytest.raises(DataValidationError, match="缺少必要列"):
        validate_kline_dataframe(df)


def test_null_values() -> None:
    """验证: 含有空值时应抛出 DataValidationError.

    Returns:
        None
    """
    df = _make_df(open=[100.0, None, 102.0])
    with pytest.raises(DataValidationError, match="存在空值"):
        validate_kline_dataframe(df)


def test_ohlc_high_less_than_low() -> None:
    """验证: high < low 违反 OHLC 逻辑时应抛出 DataValidationError.

    Args:
        无

    Returns:
        None
    """
    df = _make_df(high=[90.0, 106.0, 107.0])  # 第一条 high(90) < low(95)
    with pytest.raises(DataValidationError, match="OHLC 异常"):
        validate_kline_dataframe(df)


def test_duplicate_timestamps() -> None:
    """验证: 重复时间戳应抛出 DataValidationError.

    Returns:
        None
    """
    df = _make_df(open_time=[1000, 1000, 3000])
    with pytest.raises(DataValidationError, match="重复时间戳"):
        validate_kline_dataframe(df)
