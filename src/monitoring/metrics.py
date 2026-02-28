"""Prometheus 指标定义.

集中管理所有监控指标.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, Summary


# ---------------------------------------------------------------------------
# 交易指标
# ---------------------------------------------------------------------------

ORDERS_TOTAL = Counter(
    "trading_orders_total",
    "订单总数",
    ["instrument", "side", "order_type"],
)

FILLS_TOTAL = Counter(
    "trading_fills_total",
    "成交总数",
    ["instrument", "side"],
)

FILL_LATENCY = Histogram(
    "trading_fill_latency_seconds",
    "订单成交延迟",
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
)

PNL_TOTAL = Gauge(
    "trading_pnl_total_usdt",
    "总 PnL (USDT)",
)

PNL_DAILY = Gauge(
    "trading_pnl_daily_usdt",
    "当日 PnL (USDT)",
)

POSITION_SIZE = Gauge(
    "trading_position_size",
    "当前仓位大小",
    ["instrument", "side"],
)

# ---------------------------------------------------------------------------
# 风控指标
# ---------------------------------------------------------------------------

DRAWDOWN_PCT = Gauge(
    "risk_drawdown_pct",
    "当前回撤百分比",
)

RISK_CHECKS_TOTAL = Counter(
    "risk_checks_total",
    "风控检查总数",
    ["check_type", "result"],
)

CIRCUIT_BREAKER_TRIGGERED = Counter(
    "risk_circuit_breaker_triggered_total",
    "熔断触发次数",
    ["trigger_type"],
)

# ---------------------------------------------------------------------------
# 系统指标
# ---------------------------------------------------------------------------

HEARTBEAT = Gauge(
    "system_heartbeat_timestamp",
    "最近心跳时间戳",
)

RECONCILIATION_STATUS = Gauge(
    "system_reconciliation_status",
    "对账状态 (1=正常, 0=异常)",
)

EVENT_BUS_EVENTS = Counter(
    "system_event_bus_events_total",
    "事件总线事件总数",
    ["event_type"],
)
