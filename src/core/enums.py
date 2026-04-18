"""枚举类型."""

from __future__ import annotations

from enum import Enum, unique


@unique
class TraderType(Enum):
    """交易类型."""

    FUTURES = "futures"
    SPOT = "spot"


@unique
class Interval(Enum):
    """K 线时间间隔 (Binance 格式)."""

    MINUTE_1 = "1m"
    MINUTE_3 = "3m"
    MINUTE_5 = "5m"
    MINUTE_15 = "15m"
    MINUTE_30 = "30m"
    HOUR_1 = "1h"
    HOUR_2 = "2h"
    HOUR_4 = "4h"
    HOUR_12 = "12h"
    DAY_1 = "1d"
    WEEK_1 = "1w"
    MONTH_1 = "1M"


# Binance 时间间隔 → 毫秒映射  # 修复-2
INTERVAL_TO_MS: dict[Interval, int] = {
    Interval.MINUTE_1: 60_000,
    Interval.MINUTE_3: 180_000,
    Interval.MINUTE_5: 300_000,
    Interval.MINUTE_15: 900_000,
    Interval.MINUTE_30: 1_800_000,
    Interval.HOUR_1: 3_600_000,
    Interval.HOUR_2: 7_200_000,
    Interval.HOUR_4: 14_400_000,
    Interval.HOUR_12: 43_200_000,
    Interval.DAY_1: 86_400_000,
    Interval.WEEK_1: 604_800_000,
    Interval.MONTH_1: 2_592_000_000,  # 近似 30 天
}


# Binance 时间间隔 → Nautilus BarType 周期映射
INTERVAL_TO_NAUTILUS: dict[Interval, str] = {
    Interval.MINUTE_1: "1-MINUTE",
    Interval.MINUTE_3: "3-MINUTE",
    Interval.MINUTE_5: "5-MINUTE",
    Interval.MINUTE_15: "15-MINUTE",
    Interval.MINUTE_30: "30-MINUTE",
    Interval.HOUR_1: "1-HOUR",
    Interval.HOUR_2: "2-HOUR",
    Interval.HOUR_4: "4-HOUR",
    Interval.HOUR_12: "12-HOUR",
    Interval.DAY_1: "1-DAY",
    Interval.WEEK_1: "1-WEEK",
    Interval.MONTH_1: "1-MONTH",
}


# 默认下载的交易对
DEFAULT_INSTRUMENTS: list[str] = [
    "BTCUSDT",
    "ETHUSDT",
]
