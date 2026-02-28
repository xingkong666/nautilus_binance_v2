"""数据质量验证.

对加载的 K 线数据进行完整性和质量检查.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import structlog

logger = structlog.get_logger()


class DataValidationError(Exception):
    """数据验证失败异常.

    当 K 线数据不满足完整性或逻辑校验规则时抛出.
    """


def validate_kline_dataframe(df: pd.DataFrame) -> None:
    """验证 K 线 DataFrame 的数据质量.

    检查项:
    - 必要列存在
    - 无空值
    - OHLC 逻辑 (high >= low, high >= open/close, low <= open/close)
    - 时间戳单调递增
    - 无重复时间戳

    Args:
        df: K 线 DataFrame

    Raises:
        DataValidationError: 验证失败
    """
    required_cols = ["open_time", "open", "high", "low", "close", "volume"]

    # 1. 必要列检查
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        raise DataValidationError(f"缺少必要列: {missing}")

    # 2. 空值检查
    null_counts = df[required_cols].isnull().sum()
    if null_counts.any():
        nulls = null_counts[null_counts > 0].to_dict()
        logger.warning("data_has_nulls", null_counts=nulls)
        raise DataValidationError(f"存在空值: {nulls}")

    # 3. OHLC 逻辑检查
    invalid_high = (df["high"] < df["low"]).sum()
    if invalid_high > 0:
        logger.warning("invalid_ohlc", high_lt_low_count=int(invalid_high))
        raise DataValidationError(f"OHLC 异常: {invalid_high} 条 high < low")

    invalid_high_open = (df["high"] < df["open"]).sum()
    invalid_high_close = (df["high"] < df["close"]).sum()
    invalid_low_open = (df["low"] > df["open"]).sum()
    invalid_low_close = (df["low"] > df["close"]).sum()

    total_invalid = int(invalid_high_open + invalid_high_close + invalid_low_open + invalid_low_close)
    if total_invalid > 0:
        logger.warning("ohlc_logic_violation", count=total_invalid)

    # 4. 时间戳单调递增
    if not df["open_time"].is_monotonic_increasing:
        logger.warning("timestamps_not_monotonic")

    # 5. 重复时间戳
    duplicates = df["open_time"].duplicated().sum()
    if duplicates > 0:
        logger.warning("duplicate_timestamps", count=int(duplicates))
        raise DataValidationError(f"重复时间戳: {duplicates} 条")

    logger.debug("data_validation_passed", rows=len(df))


def validate_data_completeness(
    df: pd.DataFrame,
    expected_interval_ms: int = 60_000,
    max_gap_tolerance: int = 3,
) -> list[dict]:
    """检查数据连续性, 发现缺失的时间段.

    Args:
        df: K 线 DataFrame
        expected_interval_ms: 预期时间间隔 (毫秒)
        max_gap_tolerance: 最大容忍的连续缺失数

    Returns:
        缺失时间段列表
    """
    gaps = []
    if len(df) < 2:
        return gaps

    diffs = df["open_time"].diff().dropna()
    abnormal = diffs[diffs > expected_interval_ms * max_gap_tolerance]

    for idx in abnormal.index:
        gap = {
            "from_ts": int(df["open_time"].iloc[idx - 1]),
            "to_ts": int(df["open_time"].iloc[idx]),
            "missing_bars": int(diffs.iloc[idx] / expected_interval_ms) - 1,
        }
        gaps.append(gap)

    if gaps:
        logger.warning("data_gaps_found", gap_count=len(gaps), gaps=gaps)

    return gaps


def validate_cross_day_continuity(
    csv_paths: list,
    expected_interval_ms: int = 60_000,
) -> list[str]:
    """检查跨天数据连续性, 合并多个 CSV 文件后验证日间缺口.

    将所有 CSV 合并后按时间戳排序, 找出超过容忍阈值的缺口,
    并推断出具体缺失的日期.

    Args:
        csv_paths: CSV 文件路径列表, 建议按日期升序排列.
        expected_interval_ms: 预期 K 线时间间隔 (毫秒), 默认 60000 (1 分钟).

    Returns:
        缺失日期列表, 格式为 YYYY-MM-DD; 无缺口时返回空列表.
    """
    from src.data.loaders import KlineCatalogLoader

    if not csv_paths:
        return []

    # 合并所有 CSV
    frames = []
    for csv_path in csv_paths:
        try:
            df_raw = pd.read_csv(csv_path, header=0)
            if "open_time" not in df_raw.columns:
                df_raw = pd.read_csv(
                    csv_path,
                    header=None,
                    names=KlineCatalogLoader.KLINE_COLUMNS,
                )
            frames.append(df_raw)
        except Exception:
            logger.exception("cross_day_read_error", csv=str(csv_path))

    if not frames:
        return []

    merged = pd.concat(frames, ignore_index=True).sort_values("open_time").reset_index(drop=True)

    # 检查日间缺口
    gaps = validate_data_completeness(merged, expected_interval_ms, max_gap_tolerance=3)

    missing_dates: list[str] = []
    for gap in gaps:
        from_dt = dt.datetime.fromtimestamp(gap["from_ts"] / 1000, tz=dt.timezone.utc)
        to_dt = dt.datetime.fromtimestamp(gap["to_ts"] / 1000, tz=dt.timezone.utc)
        # 找出缺失的日期
        current = from_dt.date() + dt.timedelta(days=1)
        while current < to_dt.date():
            date_str = current.strftime("%Y-%m-%d")
            if date_str not in missing_dates:
                missing_dates.append(date_str)
            current += dt.timedelta(days=1)

    if missing_dates:
        logger.warning(
            "cross_day_gaps_found",
            missing_dates=missing_dates,
            total_missing=len(missing_dates),
        )
    else:
        logger.info("cross_day_continuity_ok", total_files=len(csv_paths))

    return missing_dates
