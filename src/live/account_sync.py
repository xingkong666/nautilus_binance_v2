"""账户状态同步 (AccountSync).

定期从 Binance REST API 拉取最新账户余额和持仓，
与本地 state 层进行对账，并通过 EventBus 发布 RECONCILIATION 事件。

设计原则：
  - 独立后台线程，不阻塞主进程
  - 软失败：单次同步失败只记录日志，不中断进程
  - 每次同步结果通过 EventBus 广播，供监控/风控订阅
  - 同步成功后将余额/持仓写入 Redis 缓存（TTL = interval_sec + 5）

典型用法::

    sync = AccountSync(container, interval_sec=30)
    sync.start()
    ...
    sync.stop()
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog

from src.core.events import Event, EventType

if TYPE_CHECKING:
    from src.app.container import Container
    from src.cache.redis_client import RedisClient

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


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
    timestamp_ns: int = field(default_factory=time.time_ns)


# ---------------------------------------------------------------------------
# AccountSync
# ---------------------------------------------------------------------------


class AccountSync:
    """定期同步 Binance 账户状态的后台服务.

    每隔 ``interval_sec`` 秒执行一次账户快照拉取，
    与本地 state 对账后向 EventBus 发布 RECONCILIATION 事件。

    Attributes:
        interval_sec: 同步间隔（秒），默认 30。

    Example::

        sync = AccountSync(container, interval_sec=30)
        sync.start()
        sync.stop()
    """

    def __init__(
        self,
        container: Container,
        interval_sec: float = 30.0,
        redis_client: RedisClient | None = None,
    ) -> None:
        """初始化 AccountSync.

        Args:
            container: 应用依赖容器，提供 event_bus / persistence 等服务。
            interval_sec: 两次同步之间的间隔秒数，默认 30。
            redis_client: 可选 Redis 客户端，用于缓存余额/持仓快照。
        """
        self._container = container
        self._interval = interval_sec
        self._redis = redis_client
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._last_result: SyncResult | None = None

        # 统计
        self._sync_count = 0
        self._error_count = 0

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    @property
    def last_result(self) -> SyncResult | None:
        """返回最近一次同步结果（线程安全只读）.

        Returns:
            SyncResult 或 None（未执行过同步时）。
        """
        return self._last_result

    @property
    def is_running(self) -> bool:
        """返回后台线程是否正在运行.

        Returns:
            True 表示后台线程已启动且未停止。
        """
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """启动后台同步线程.

        Raises:
            RuntimeError: 已在运行时再次调用 start()。
        """
        if self.is_running:
            raise RuntimeError("AccountSync is already running")

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="AccountSync",
            daemon=True,
        )
        self._thread.start()
        logger.info("account_sync_started", interval_sec=self._interval)

    def stop(self, timeout: float = 10.0) -> None:
        """停止后台同步线程.

        Args:
            timeout: 最长等待秒数，超时后返回（线程仍可能在跑）。
        """
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        logger.info("account_sync_stopped", sync_count=self._sync_count)

    def sync_once(self) -> SyncResult:
        """立即执行一次同步（可在主线程调用，用于测试或强制刷新）.

        Returns:
            本次同步的 SyncResult。
        """
        return self._do_sync()

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        """后台线程主循环：按间隔执行 _do_sync()."""
        logger.info("account_sync_loop_started")

        while not self._stop_event.is_set():
            try:
                result = self._do_sync()
                self._last_result = result

                if result.success:
                    self._sync_count += 1
                    logger.debug(
                        "account_sync_ok",
                        positions=len(result.positions),
                        duration_ms=round(result.duration_ms, 1),
                    )
                else:
                    self._error_count += 1
                    logger.warning("account_sync_failed", error=result.error)

            except Exception:
                self._error_count += 1
                logger.exception("account_sync_unexpected_error")

            # 等待下次触发（支持提前唤醒）
            self._stop_event.wait(timeout=self._interval)

        logger.info("account_sync_loop_stopped")

    def _do_sync(self) -> SyncResult:
        """执行一次完整的账户状态同步.

        流程：
          1. 调用 exchange adapter 拉取余额/持仓（当前用 mock 占位）
          2. 与本地 state 对账
          3. 通过 EventBus 发布 RECONCILIATION 事件

        Returns:
            SyncResult，包含本次同步结果。
        """
        t0 = time.monotonic()
        try:
            # TODO: 接入真实 BinanceAdapter 后替换此处
            balances, positions = self._fetch_from_exchange()

            # 与本地 state 对账
            self._reconcile_with_local(balances, positions)

            duration_ms = (time.monotonic() - t0) * 1000
            result = SyncResult(
                success=True,
                balances=balances,
                positions=positions,
                duration_ms=duration_ms,
            )

            # 发布对账事件
            self._publish_reconciliation(result)
            return result

        except Exception as exc:
            duration_ms = (time.monotonic() - t0) * 1000
            return SyncResult(
                success=False,
                error=str(exc),
                duration_ms=duration_ms,
            )

    def _fetch_from_exchange(
        self,
    ) -> tuple[list[AccountBalance], list[PositionSnapshot]]:
        """从交易所拉取账户余额和持仓.

        当 exchange adapter 尚未实现时，返回空列表（软降级）。

        Returns:
            (balances, positions) 元组。

        Raises:
            Exception: 交易所 API 调用失败时向上抛出。
        """
        try:
            from src.exchange.binance_adapter import BinanceAdapter  # noqa: PLC0415

            adapter: BinanceAdapter = BinanceAdapter.get_instance()  # type: ignore[attr-defined]
            raw_balance = adapter.fetch_balance()  # type: ignore[attr-defined]
            raw_positions = adapter.fetch_positions()  # type: ignore[attr-defined]

            balances = [
                AccountBalance(
                    asset=b["asset"],
                    wallet_balance=Decimal(str(b["walletBalance"])),
                    available_balance=Decimal(str(b["availableBalance"])),
                    unrealized_pnl=Decimal(str(b.get("unrealizedProfit", 0))),
                )
                for b in raw_balance
                if Decimal(str(b.get("walletBalance", 0))) != 0
            ]

            positions = [
                PositionSnapshot(
                    symbol=p["symbol"],
                    side=p.get("positionSide", "BOTH"),
                    quantity=Decimal(str(abs(float(p["positionAmt"])))),
                    entry_price=Decimal(str(p.get("entryPrice", 0))),
                    unrealized_pnl=Decimal(str(p.get("unrealizedProfit", 0))),
                    leverage=int(p.get("leverage", 1)),
                )
                for p in raw_positions
                if Decimal(str(p.get("positionAmt", 0))) != 0
            ]

            return balances, positions

        except ImportError:
            # exchange adapter 尚未实现，软降级
            logger.debug("binance_adapter_not_available_using_mock")
            return [], []

    def _reconcile_with_local(
        self,
        balances: list[AccountBalance],
        positions: list[PositionSnapshot],
    ) -> None:
        """将交易所快照与本地 state 进行对账.

        当前实现：仅记录日志；后续接入 state.reconciliation 模块做差异检测。

        Args:
            balances: 从交易所拉取的余额列表。
            positions: 从交易所拉取的持仓列表。
        """
        # TODO: 接入 src.state.reconciliation 做本地持仓差异检测
        if balances:
            usdt = next(
                (b for b in balances if b.asset == "USDT"), None
            )
            if usdt:
                logger.debug(
                    "account_balance",
                    wallet=float(usdt.wallet_balance),
                    available=float(usdt.available_balance),
                    unrealized_pnl=float(usdt.unrealized_pnl),
                )

        if positions:
            logger.debug(
                "open_positions",
                count=len(positions),
                symbols=[p.symbol for p in positions],
            )

    def _publish_reconciliation(self, result: SyncResult) -> None:
        """将同步结果发布为 RECONCILIATION 事件，并缓存到 Redis.

        Args:
            result: 本次同步的 SyncResult。
        """
        payload: dict[str, Any] = {
            "sync_count": self._sync_count,
            "balance_count": len(result.balances),
            "position_count": len(result.positions),
            "duration_ms": result.duration_ms,
        }

        # 将余额汇总加入 payload
        usdt = next(
            (b for b in result.balances if b.asset == "USDT"), None
        )
        if usdt:
            payload["usdt_wallet"] = float(usdt.wallet_balance)
            payload["usdt_available"] = float(usdt.available_balance)

        event = Event(
            event_type=EventType.RECONCILIATION,
            source="account_sync",
            payload=payload,
        )

        try:
            self._container.event_bus.publish(event)
        except Exception:
            logger.exception("reconciliation_event_publish_failed")

        # 写入 Redis 缓存（TTL = interval + 5s 安全余量）
        self._cache_to_redis(result)

    def _cache_to_redis(self, result: SyncResult) -> None:
        """将余额和持仓快照写入 Redis 缓存.

        Args:
            result: 本次同步的 SyncResult。
        """
        if self._redis is None or not self._redis.is_available:
            return

        ttl = int(self._interval) + 5

        try:
            # 缓存余额
            for b in result.balances:
                key = f"nautilus:account:balance:{b.asset}"
                self._redis.hset(
                    key,
                    {
                        "wallet_balance": str(b.wallet_balance),
                        "available_balance": str(b.available_balance),
                        "unrealized_pnl": str(b.unrealized_pnl),
                        "timestamp_ns": str(b.timestamp_ns),
                    },
                )
                self._redis.expire(key, ttl)

            # 缓存持仓
            for p in result.positions:
                key = f"nautilus:account:position:{p.symbol}:{p.side}"
                self._redis.hset(
                    key,
                    {
                        "quantity": str(p.quantity),
                        "entry_price": str(p.entry_price),
                        "unrealized_pnl": str(p.unrealized_pnl),
                        "leverage": str(p.leverage),
                        "timestamp_ns": str(p.timestamp_ns),
                    },
                )
                self._redis.expire(key, ttl)

            logger.debug(
                "account_sync_cached_to_redis",
                balance_count=len(result.balances),
                position_count=len(result.positions),
                ttl=ttl,
            )
        except Exception as exc:
            logger.warning("account_sync_redis_cache_failed", error=str(exc))
