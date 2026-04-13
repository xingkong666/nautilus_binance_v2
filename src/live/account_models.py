"""账户状态数据模型.

定义账户同步相关的数据类和类型别名，供 AccountSync 及外部模块使用。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass
class AccountBalance:
    """账户余额快照.

    Attributes:
        asset: 资产符号，如 "USDT"。
        wallet_balance: 钱包余额（含未实现盈亏）。
        available_balance: 可用余额（可下单部分）。
        unrealized_pnl: 当前未实现盈亏。
        timestamp_ns: 快照时间戳（纳秒）。

    """

    asset: str
    wallet_balance: Decimal
    available_balance: Decimal
    unrealized_pnl: Decimal
    timestamp_ns: int = field(default_factory=time.time_ns)


@dataclass
class PositionSnapshot:
    """单个合约持仓快照.

    Attributes:
        symbol: 交易对符号，如 "BTCUSDT"。
        side: 持仓方向，"LONG" / "SHORT" / "BOTH"。
        quantity: 持仓数量（正数）。
        entry_price: 均价。
        unrealized_pnl: 未实现盈亏。
        leverage: 当前杠杆倍数。
        timestamp_ns: 快照时间戳（纳秒）。

    """

    symbol: str
    side: str
    quantity: Decimal
    entry_price: Decimal
    unrealized_pnl: Decimal
    leverage: int
    timestamp_ns: int = field(default_factory=time.time_ns)


@dataclass
class SyncResult:
    """单次账户同步结果.

    Attributes:
        success: 是否同步成功。
        balances: 余额快照列表（仅成功时有效）。
        positions: 持仓快照列表（仅成功时有效）。
        error: 失败原因（仅失败时有效）。
        duration_ms: 本次同步耗时（毫秒）。
        timestamp_ns: 同步完成时间戳。

    """

    success: bool
    balances: list[AccountBalance] = field(default_factory=list)
    positions: list[PositionSnapshot] = field(default_factory=list)
    error: str = ""
    duration_ms: float = 0.0
    reconciliation_matched: bool | None = None
    mismatch_count: int = 0
    timestamp_ns: int = field(default_factory=time.time_ns)


AccountSnapshotProvider = Callable[
    [],
    tuple[list[AccountBalance], list[PositionSnapshot]],
]
RawSnapshotProvider = Callable[
    [],
    tuple[list[dict[str, Any]], list[dict[str, Any]]],
]
