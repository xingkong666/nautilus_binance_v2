"""回归测试公共 fixtures.

提供内存回测引擎、合成 Bar 数据构造器等工具，
让回归测试完全独立于外部文件系统和真实 catalog。
"""

from __future__ import annotations

import datetime as dt
import math
from decimal import Decimal
from typing import Any

import pytest
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.common.config import LoggingConfig
from nautilus_trader.config import BacktestEngineConfig
from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.test_kit.providers import TestInstrumentProvider

from src.backtest.runner import BacktestConfig, BacktestRunResult
from src.strategy.ema_cross import EMACrossConfig, EMACrossStrategy


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

BTCUSDT = TestInstrumentProvider.btcusdt_perp_binance()
BAR_TYPE = BarType.from_str("BTCUSDT-PERP.BINANCE-1-MINUTE-LAST-EXTERNAL")
STARTING_BALANCE = 10_000

# 基准回测日期（固定，确保每次结果一致）
START_DATE = dt.date(2024, 1, 1)
END_DATE = dt.date(2024, 1, 1)  # 单日，用合成数据


# ---------------------------------------------------------------------------
# Bar 生成器
# ---------------------------------------------------------------------------


def make_sine_bars(
    n: int = 200,
    base_price: float = 50_000.0,
    amplitude: float = 500.0,
    period: float = 30.0,
    start_ts_ns: int | None = None,
    interval_seconds: int = 60,
) -> list[Bar]:
    """生成基于正弦波的合成 Bar 序列.

    使用确定性正弦波确保每次生成结果完全一致，
    适合作为回归基准数据集。

    Args:
        n: Bar 数量。
        base_price: 基准价格（USDT）。
        amplitude: 价格振幅（USDT）。
        period: 正弦波周期（Bar 数），控制交叉频率。
        start_ts_ns: 起始时间戳（纳秒），None 时使用 2024-01-01 UTC。
        interval_seconds: 每根 Bar 间隔秒数，默认 60（1分钟）。

    Returns:
        按时间顺序排列的 Bar 列表。
    """
    if start_ts_ns is None:
        start_ts_ns = int(
            dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc).timestamp() * 1_000_000_000
        )

    bars = []
    for i in range(n):
        ts = start_ts_ns + i * interval_seconds * 1_000_000_000
        open_price = base_price + amplitude * math.sin(i / period * math.pi)
        close_price = base_price + amplitude * math.sin((i + 1) / period * math.pi)
        high_price = max(open_price, close_price) + 15.0
        low_price = min(open_price, close_price) - 15.0

        bars.append(
            Bar(
                bar_type=BAR_TYPE,
                open=Price.from_str(f"{open_price:.1f}"),
                high=Price.from_str(f"{high_price:.1f}"),
                low=Price.from_str(f"{low_price:.1f}"),
                close=Price.from_str(f"{close_price:.1f}"),
                volume=Quantity.from_str("1.000"),
                ts_event=ts,
                ts_init=ts,
            )
        )
    return bars


def make_trend_bars(
    n: int = 200,
    start_price: float = 50_000.0,
    step: float = 50.0,
    noise: float = 10.0,
) -> list[Bar]:
    """生成线性趋势 Bar 序列（固定步长 + 小噪声）.

    Args:
        n: Bar 数量。
        start_price: 起始价格。
        step: 每根 Bar 价格步长（正=上涨趋势）。
        noise: 每根 Bar 高低价噪声范围（USDT）。

    Returns:
        按时间顺序排列的 Bar 列表。
    """
    start_ts_ns = int(
        dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc).timestamp() * 1_000_000_000
    )
    bars = []
    price = start_price
    for i in range(n):
        ts = start_ts_ns + i * 60 * 1_000_000_000
        close = price + step
        bars.append(
            Bar(
                bar_type=BAR_TYPE,
                open=Price.from_str(f"{price:.1f}"),
                high=Price.from_str(f"{max(price, close) + noise:.1f}"),
                low=Price.from_str(f"{min(price, close) - noise:.1f}"),
                close=Price.from_str(f"{close:.1f}"),
                volume=Quantity.from_str("1.000"),
                ts_event=ts,
                ts_init=ts,
            )
        )
        price = close
    return bars


# ---------------------------------------------------------------------------
# 引擎构造
# ---------------------------------------------------------------------------


def build_engine(starting_balance: int = STARTING_BALANCE) -> BacktestEngine:
    """构造已配置 venue 和 instrument 的 BacktestEngine.

    Args:
        starting_balance: 初始账户余额（USDT）。

    Returns:
        已调用 add_venue + add_instrument 的 BacktestEngine 实例（未 run）。
    """
    engine_cfg = BacktestEngineConfig(
        trader_id="REGRESSION-001",
        logging=LoggingConfig(bypass_logging=True),
        run_analysis=False,
    )
    engine = BacktestEngine(config=engine_cfg)
    engine.add_venue(
        venue=Venue("BINANCE"),
        oms_type=OmsType.HEDGING,
        account_type=AccountType.MARGIN,
        starting_balances=[Money(starting_balance, USDT)],
        default_leverage=Decimal("10"),
        bar_execution=True,
        # 1.223.0: 显式设 False 保持纯 bar 驱动行为，防止基准漂移
        trade_execution=False,
        use_market_order_acks=True,
    )
    engine.add_instrument(BTCUSDT)
    return engine


def run_ema_cross(
    bars: list[Bar],
    fast_period: int = 5,
    slow_period: int = 20,
    trade_size: str = "0.010",
    starting_balance: int = STARTING_BALANCE,
) -> dict[str, Any]:
    """运行 EMA 交叉策略回测，返回关键指标字典.

    Args:
        bars: 合成 Bar 数据列表。
        fast_period: 快线 EMA 周期。
        slow_period: 慢线 EMA 周期。
        trade_size: 每次下单量（币数字符串）。
        starting_balance: 初始余额（USDT）。

    Returns:
        包含以下字段的字典：
            - iterations (int)
            - total_orders (int)
            - total_positions (int)
            - starting_balance (int)
    """
    engine = build_engine(starting_balance)
    engine.add_data(bars)

    strategy_cfg = EMACrossConfig(
        instrument_id=BTCUSDT.id,
        bar_type=BAR_TYPE,
        fast_ema_period=fast_period,
        slow_ema_period=slow_period,
        trade_size=Decimal(trade_size),
    )
    engine.add_strategy(EMACrossStrategy(config=strategy_cfg))
    engine.sort_data()
    engine.run()
    result = engine.get_result()

    metrics = {
        "iterations": result.iterations,
        "total_orders": result.total_orders,
        "total_positions": result.total_positions,
        "starting_balance": starting_balance,
    }
    engine.dispose()
    return metrics


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def sine_bars():
    """会话级别 fixture：正弦波 Bar 序列（200根，固定参数，可复用）。"""
    return make_sine_bars(n=200, base_price=50_000.0, amplitude=500.0, period=30.0)


@pytest.fixture(scope="session")
def trend_bars_up():
    """会话级别 fixture：上涨趋势 Bar 序列（200根）。"""
    return make_trend_bars(n=200, start_price=50_000.0, step=50.0)


@pytest.fixture(scope="session")
def trend_bars_down():
    """会话级别 fixture：下跌趋势 Bar 序列（200根）。"""
    return make_trend_bars(n=200, start_price=60_000.0, step=-50.0)
