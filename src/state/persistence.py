"""持久化层.

将交易状态持久化到 PostgreSQL 数据库。
"""

from __future__ import annotations

import json
import time
from typing import Any

import psycopg
import structlog

logger = structlog.get_logger()


class TradePersistence:
    """交易记录持久化 (PostgreSQL)."""

    def __init__(self, database_url: str) -> None:
        """Initialize the trade persistence.

        Args:
            database_url: Database url.
        """
        self._database_url = database_url
        self._conn = psycopg.connect(database_url, autocommit=False)
        self._init_tables()

    def _init_tables(self) -> None:
        """初始化表结构."""
        with self._conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    timestamp_ns BIGINT NOT NULL,
                    instrument_id TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    price TEXT NOT NULL,
                    order_id TEXT,
                    strategy_id TEXT,
                    pnl TEXT,
                    fees TEXT,
                    metadata TEXT
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    timestamp_ns BIGINT NOT NULL,
                    event_type TEXT NOT NULL,
                    source TEXT,
                    payload TEXT
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(timestamp_ns)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_trades_instrument ON trades(instrument_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(timestamp_ns)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type)")
        self._conn.commit()

    def record_trade(
        self,
        instrument_id: str,
        side: str,
        quantity: str,
        price: str,
        order_id: str = "",
        strategy_id: str = "",
        pnl: str = "0",
        fees: str = "0",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """记录一笔交易.

        Args:
            instrument_id: Instrument identifier to target.
            side: Side.
            quantity: Order quantity to use.
            price: Price.
            order_id: Identifier for order.
            strategy_id: Strategy identifier associated with the order.
            pnl: Pnl.
            fees: Fees.
            metadata: Additional metadata attached to the run.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """INSERT INTO trades (timestamp_ns, instrument_id, side, quantity, price,
                   order_id, strategy_id, pnl, fees, metadata)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    time.time_ns(),
                    instrument_id,
                    side,
                    quantity,
                    price,
                    order_id,
                    strategy_id,
                    pnl,
                    fees,
                    json.dumps(metadata or {}),
                ),
            )
        self._conn.commit()

    def record_event(self, event_type: str, source: str = "", payload: dict[str, Any] | None = None) -> None:
        """记录一个事件.

        Args:
            event_type: Event type.
            source: Source.
            payload: Payload.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO events (timestamp_ns, event_type, source, payload) VALUES (%s, %s, %s, %s)",
                (time.time_ns(), event_type, source, json.dumps(payload or {})),
            )
        self._conn.commit()

    def close(self) -> None:
        """关闭数据库连接."""
        self._conn.close()
