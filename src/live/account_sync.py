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
from collections.abc import Callable
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

import structlog

from src.core.events import Event, EventType
from src.live.account_models import (
    AccountBalance,
    AccountSnapshotProvider,
    PositionSnapshot,
    RawSnapshotProvider,
    SyncResult,
)
from src.risk.real_time import RealTimeRiskMonitor
from src.state.reconciliation import ReconciliationEngine, ReconciliationResult
from src.state.snapshot import SystemSnapshot

if TYPE_CHECKING:
    from src.app.container import Container
    from src.cache.redis_client import RedisClient

logger = structlog.get_logger(__name__)

# 重新导出数据模型，确保现有导入如
# `from src.live.account_sync import AccountBalance` 继续可用。
__all__ = [
    "AccountSync",
    "AccountBalance",
    "PositionSnapshot",
    "SyncResult",
    "AccountSnapshotProvider",
    "RawSnapshotProvider",
]


# ---------------------------------------------------------------------------
# 账户同步
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
        exchange_snapshot_provider: AccountSnapshotProvider | None = None,
    ) -> None:
        """初始化 AccountSync.

        Args:
            container: 应用依赖容器，提供 event_bus / persistence 等服务。
            interval_sec: 两次同步之间的间隔秒数，默认 30。
            redis_client: 可选 Redis 客户端，用于缓存余额/持仓快照。
            exchange_snapshot_provider: 可选交易所快照提供器，用于替代默认账户查询实现。

        """
        self._container = container
        self._interval = interval_sec
        self._redis = redis_client if redis_client is not None else getattr(container, "redis_client", None)
        self._exchange_snapshot_provider = exchange_snapshot_provider
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._last_result: SyncResult | None = None
        self._reconciler = ReconciliationEngine(container.event_bus)
        self._real_time_risk_monitor = cast(
            RealTimeRiskMonitor | None,
            getattr(container, "real_time_risk_monitor", None),
        )
        self._risk_monitor_initialized = False

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

            except (ConnectionError, OSError, TimeoutError):
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
            balances, positions = self._fetch_from_exchange()
            self._mark_external_open_orders()

            # 与本地状态层对账
            reconciliation = self._reconcile_with_local(positions)

            duration_ms = (time.monotonic() - t0) * 1000
            result = SyncResult(
                success=True,
                balances=balances,
                positions=positions,
                duration_ms=duration_ms,
                reconciliation_matched=reconciliation.matched if reconciliation is not None else None,
                mismatch_count=len(reconciliation.mismatches) if reconciliation is not None else 0,
            )

            # 发布对账事件
            self._update_real_time_risk(balances)
            self._publish_reconciliation(result)
            return result

        except Exception as exc:
            duration_ms = (time.monotonic() - t0) * 1000
            return SyncResult(
                success=False,
                error=str(exc),
                duration_ms=duration_ms,
            )

    def _mark_external_open_orders(self) -> None:
        adapter = getattr(self._container, "binance_adapter", None)
        ignored_registry = getattr(self._container, "ignored_instruments", None)
        if adapter is None or ignored_registry is None:
            return

        fetch_open_orders = getattr(adapter, "fetch_open_orders", None)
        if not callable(fetch_open_orders):
            return

        try:
            exchange_open_orders = cast(Callable[[], list[dict[str, Any]]], fetch_open_orders)()
        except (ConnectionError, TimeoutError, OSError) as exc:
            logger.warning("account_sync_open_orders_fetch_connection_error", error=str(exc))
            return
        except Exception as exc:
            logger.error("account_sync_open_orders_fetch_failed", error=str(exc), exc_info=True)
            return

        known_client_order_ids = self._known_open_client_order_ids()
        for order in exchange_open_orders:
            client_order_id = str(order.get("clientOrderId", "")).strip()
            if client_order_id and client_order_id in known_client_order_ids:
                continue

            symbol = str(order.get("symbol", "")).strip()
            if not symbol:
                continue
            ignored_registry.ignore(
                instrument_id=f"{symbol}-PERP.BINANCE",
                reason="external_open_order_detected_during_sync",
                source="account_sync",
                details={"client_order_id": client_order_id},
            )

    def _known_open_client_order_ids(self) -> set[str]:
        adapter = getattr(self._container, "binance_adapter", None)
        if adapter is None:
            return set()

        try:
            node = adapter.node
        except Exception as exc:
            logger.error("adapter_node_access_failed", error=str(exc), exc_info=True)
            return set()
        if node is None:
            return set()

        cache = getattr(node, "cache", None)
        if cache is None:
            return set()

        client_order_ids_open = getattr(cache, "client_order_ids_open", None)
        if not callable(client_order_ids_open):
            return set()

        try:
            return {str(client_order_id) for client_order_id in cast(Callable[[], list[Any]], client_order_ids_open)()}
        except Exception as exc:
            logger.error("account_sync_known_open_orders_load_failed", error=str(exc), exc_info=True)
            return set()

    def _fetch_from_exchange(
        self,
    ) -> tuple[list[AccountBalance], list[PositionSnapshot]]:
        """从交易所拉取账户余额和持仓.

        Returns:
            (balances, positions) 元组。

        Raises:
            Exception: 交易所 API 调用失败时向上抛出。

        """
        provider = self._exchange_snapshot_provider or self._resolve_container_snapshot_provider()
        if provider is None:
            raise RuntimeError("exchange_snapshot_provider_unavailable")
        return provider()

    def _reconcile_with_local(
        self,
        positions: list[PositionSnapshot],
    ) -> ReconciliationResult | None:
        """将交易所快照与本地 state 进行对账.

        Args:
            positions: 从交易所拉取的持仓列表。

        """
        local_positions = self._load_local_positions()
        exchange_positions = self._to_reconciliation_positions(positions)

        result = self._reconciler.reconcile(
            local_positions=local_positions,
            exchange_positions=exchange_positions,
        )
        self._mark_ignored_instruments(result)
        logger.info(
            "account_sync_reconciled",
            matched=result.matched,
            mismatch_count=len(result.mismatches),
            local_position_count=len(local_positions),
            exchange_position_count=len(exchange_positions),
        )
        return result

    def _mark_ignored_instruments(self, result: ReconciliationResult) -> None:
        ignored_registry = getattr(self._container, "ignored_instruments", None)
        if ignored_registry is None:
            return

        exchange_instruments = {
            str(position.get("instrument_id", "")).strip(): position
            for position in result.exchange_positions
            if str(position.get("instrument_id", "")).strip()
        }
        for instrument_id, position in exchange_instruments.items():
            ignored_registry.ignore(
                instrument_id=instrument_id,
                reason="exchange_position_detected_during_sync",
                source="account_sync",
                details={"side": str(position.get("side", "BOTH"))},
            )

        for mismatch in result.mismatches:
            instrument_id = str(mismatch.get("instrument_id", "")).strip()
            if not instrument_id:
                continue
            ignored_registry.ignore(
                instrument_id=instrument_id,
                reason=f"reconciliation_{mismatch.get('type', 'mismatch')}",
                source="account_sync",
                details={"side": str(mismatch.get("side", "BOTH"))},
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
            "reconciliation_matched": result.reconciliation_matched,
            "mismatch_count": result.mismatch_count,
        }

        # 将余额汇总加入载荷
        usdt = next((b for b in result.balances if b.asset == "USDT"), None)
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
        except Exception as exc:
            logger.error("reconciliation_event_publish_failed", error=str(exc), exc_info=True)

        # 写入缓存（过期时间 = 同步间隔 + 5 秒安全余量）
        self._cache_to_redis(result)

    def _update_real_time_risk(self, balances: list[AccountBalance]) -> None:
        """用最新账户权益驱动实时风控监控器."""
        if self._real_time_risk_monitor is None:
            return

        usdt = next((balance for balance in balances if balance.asset == "USDT"), None)
        if usdt is None or usdt.wallet_balance <= 0:
            return

        current_equity = usdt.wallet_balance
        if not self._risk_monitor_initialized:
            self._real_time_risk_monitor.initialize(current_equity)
            self._risk_monitor_initialized = True

        self._real_time_risk_monitor.update(current_equity)

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

    def _resolve_container_snapshot_provider(self) -> AccountSnapshotProvider | None:
        adapter = self._container.binance_adapter
        if adapter is None:
            return None

        fetch_account_snapshot = getattr(adapter, "fetch_account_snapshot", None)
        if callable(fetch_account_snapshot):
            return self._wrap_raw_snapshot_provider(cast(RawSnapshotProvider, fetch_account_snapshot))

        fetch_balance = getattr(adapter, "fetch_balance", None)
        fetch_positions = getattr(adapter, "fetch_positions", None)
        if callable(fetch_balance) and callable(fetch_positions):
            typed_fetch_balance = cast(Callable[[], list[dict[str, Any]]], fetch_balance)
            typed_fetch_positions = cast(Callable[[], list[dict[str, Any]]], fetch_positions)
            return self._wrap_raw_snapshot_provider(lambda: (typed_fetch_balance(), typed_fetch_positions()))

        return None

    def _wrap_raw_snapshot_provider(
        self,
        raw_provider: RawSnapshotProvider,
    ) -> AccountSnapshotProvider:
        def _provider() -> tuple[list[AccountBalance], list[PositionSnapshot]]:
            raw_balances, raw_positions = raw_provider()
            return (
                self._normalize_raw_balances(raw_balances),
                self._normalize_raw_positions(raw_positions),
            )

        return _provider

    @staticmethod
    def _normalize_raw_balances(raw_balances: list[dict[str, Any]]) -> list[AccountBalance]:
        return [
            AccountBalance(
                asset=str(balance["asset"]),
                wallet_balance=Decimal(str(balance["walletBalance"])),
                available_balance=Decimal(str(balance["availableBalance"])),
                unrealized_pnl=Decimal(str(balance.get("unrealizedProfit", 0))),
            )
            for balance in raw_balances
            if Decimal(str(balance.get("walletBalance", 0))) != 0
        ]

    @staticmethod
    def _normalize_raw_positions(raw_positions: list[dict[str, Any]]) -> list[PositionSnapshot]:
        return [
            PositionSnapshot(
                symbol=str(position["symbol"]),
                side=str(position.get("positionSide", "BOTH")),
                quantity=Decimal(str(abs(float(position["positionAmt"])))),
                entry_price=Decimal(str(position.get("entryPrice", 0))),
                unrealized_pnl=Decimal(str(position.get("unrealizedProfit", 0))),
                leverage=int(position.get("leverage", 1)),
            )
            for position in raw_positions
            if Decimal(str(position.get("positionAmt", 0))) != 0
        ]

    def _load_local_positions(self) -> list[dict[str, str]]:
        """读取本地当前仓位.

        优先从 NautilusTrader 内存 cache 实时读取（精确），
        cache 不可用时 fallback 到磁盘快照（最多 60s 延迟）。
        """
        cache_positions = self._load_positions_from_cache()
        if cache_positions is not None:
            return cache_positions

        # fallback: 磁盘快照
        snapshot = self._load_latest_snapshot()
        if snapshot is None:
            return []
        return [
            {
                "instrument_id": position.instrument_id,
                "side": position.side,
                "quantity": position.quantity,
                "avg_entry_price": position.avg_entry_price,
                "unrealized_pnl": position.unrealized_pnl,
                "realized_pnl": position.realized_pnl,
            }
            for position in snapshot.positions
        ]

    def _load_positions_from_cache(self) -> list[dict[str, str]] | None:
        """从 NautilusTrader node.cache 实时读取开仓位.

        Returns:
            仓位列表，cache 不可用时返回 None（触发 fallback）。
        """
        adapter = getattr(self._container, "binance_adapter", None)
        if adapter is None:
            return None
        node = getattr(adapter, "node", None)
        if node is None:
            return None
        cache = getattr(node, "cache", None)
        if cache is None:
            return None

        positions_open = getattr(cache, "positions_open", None)
        if not callable(positions_open):
            return None

        try:
            result = []
            for pos in cast(Callable[[], list[Any]], positions_open)():
                side = "LONG" if getattr(pos, "is_long", False) else "SHORT"
                result.append(
                    {
                        "instrument_id": str(pos.instrument_id),
                        "side": side,
                        "quantity": str(abs(float(pos.quantity))),
                        "avg_entry_price": str(getattr(pos, "avg_px_open", 0)),
                        "unrealized_pnl": str(getattr(pos, "unrealized_pnl", 0)),
                        "realized_pnl": str(getattr(pos, "realized_pnl", 0)),
                    }
                )
            return result
        except Exception as exc:
            logger.warning("account_sync_cache_positions_load_failed", error=str(exc))
            return None

    def _load_latest_snapshot(self) -> SystemSnapshot | None:
        try:
            return self._container.snapshot_manager.load_latest()
        except Exception as exc:
            logger.error("account_sync_snapshot_load_failed", error=str(exc), exc_info=True)
            return None

    @staticmethod
    def _to_reconciliation_positions(
        positions: list[PositionSnapshot],
    ) -> list[dict[str, str]]:
        return [
            {
                "instrument_id": f"{position.symbol}-PERP.BINANCE",
                "side": position.side,
                "quantity": str(position.quantity),
                "entry_price": str(position.entry_price),
                "unrealized_pnl": str(position.unrealized_pnl),
                "leverage": str(position.leverage),
            }
            for position in positions
        ]
