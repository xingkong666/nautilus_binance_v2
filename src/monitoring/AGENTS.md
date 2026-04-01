<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-01 | Updated: 2026-04-01 -->

# Monitoring

## Purpose
可观测性层，提供 Prometheus 指标暴露、HTTP 健康检查端点、告警规则引擎和多渠道通知分发。`monitoring.enabled=true` 时由 `Container` 在启动时激活。告警链路：`EventBus` 事件 → `Watchers`（规则判断）→ `AlertManager`（路由分发）→ `Notifier`（Telegram/Slack 发送）。所有 Prometheus 指标集中定义在 `metrics.py`，由各模块直接 import 使用。

## Key Files

| File | Description |
|------|-------------|
| `metrics.py` | 集中定义所有 Prometheus 指标（Counter / Gauge / Histogram）；模块间共享，直接 import 使用 |
| `alerting.py` | `AlertManager` — 订阅 EventBus 中的 `RiskAlertEvent`，广播给所有已注册的 `BaseNotifier`；通知器内部按 `min_level` 过滤 |
| `health_server.py` | `HealthServer` + `HealthStatus` — 后台 HTTP 服务，暴露 `/health` 和 `/ready` 端点（默认 `:8080`）；`HealthStatus` 聚合各子系统健康状态 |
| `prometheus_server.py` | `PrometheusServer` — 后台线程启动 `prometheus_client.start_http_server`，默认端口 `:9090` |
| `watchers.py` | `BaseWatcher` 及子类（`RiskAlertWatcher`、`DrawdownWatcher`、`FillLatencyWatcher`）— 告警规则引擎，订阅 EventBus 事件，满足条件时调用 `AlertManager.send_direct()` |
| `__init__.py` | 模块公开导出：`AlertManager`、`PrometheusServer`、`HealthServer` |

## Subdirectories

- `notifier/` — 多渠道通知实现（Telegram、Slack），见 [notifier/AGENTS.md](notifier/AGENTS.md)

## For AI Agents

### Working In This Directory

**Prometheus 指标（`metrics.py` 中定义）：**

| 指标名 | 类型 | 标签 | 说明 |
|--------|------|------|------|
| `ORDERS_TOTAL` | Counter | `instrument, side, order_type` | 订单总数 |
| `FILLS_TOTAL` | Counter | `instrument, side` | 成交总数 |
| `FILL_LATENCY` | Histogram | — | 订单成交延迟（秒），buckets: 0.01~10s |
| `PNL_TOTAL` | Gauge | — | 总 PnL (USDT) |
| `PNL_DAILY` | Gauge | — | 当日 PnL (USDT) |
| `POSITION_SIZE` | Gauge | `instrument, side` | 当前仓位大小 |
| `DRAWDOWN_PCT` | Gauge | — | 当前回撤百分比 |
| `RISK_CHECKS_TOTAL` | Counter | — | 风控检查总次数 |
| `EVENT_BUS_EVENTS` | Counter | `event_type` | EventBus 全局事件计数（全局 handler 自动累加） |

- 新增指标直接在 `metrics.py` 添加，遵循现有命名前缀 `trading_`
- `Watcher` 扩展：继承 `BaseWatcher`，在 `__init__` 中订阅目标 `EventType`，在 handler 中调用 `self._alert_manager.send_direct()`
- `AlertManager.start()` 挂载 EventBus 订阅，`stop()` 取消订阅；先调用 `add_notifier()` 再调用 `start()`
- 告警级别（`AlertLevel`）：`WARNING=1 < ERROR=2 < CRITICAL=3`；各 Notifier 配置 `min_level` 过滤低优先级消息

### Testing Requirements
- 单元测试路径：`tests/unit/test_alerting.py`、`tests/unit/test_watchers.py`
- `AlertManager` 测试时 mock `EventBus` 和 `BaseNotifier`（验证 `send()` 被正确调用）
- `HealthServer` 测试：启动后对 `http://localhost:{port}/health` 发 GET 请求验证响应
- Prometheus 指标测试：使用 `prometheus_client.REGISTRY` 或 `CollectorRegistry` 隔离

### Common Patterns
```python
# 在任意模块记录订单
from src.monitoring.metrics import ORDERS_TOTAL, FILL_LATENCY

ORDERS_TOTAL.labels(instrument="BTCUSDT", side="buy", order_type="market").inc()

with FILL_LATENCY.time():
    await submit_order(...)

# 构建 AlertManager 并添加通知器
from src.monitoring.alerting import AlertManager
from src.monitoring.notifier.telegram import TelegramNotifier

manager = AlertManager(event_bus)
manager.add_notifier(TelegramNotifier(token="...", chat_id="..."))
manager.start()

# 扩展 Watcher
from src.monitoring.watchers import BaseWatcher

class MyWatcher(BaseWatcher):
    def __init__(self, event_bus, alert_manager):
        super().__init__(event_bus, alert_manager)
        event_bus.subscribe(EventType.ORDER_REJECTED, self._on_reject)

    def _on_reject(self, event):
        self._alert_manager.send_direct(AlertMessage(...))
```

## Dependencies

### Internal
- `src.core.events` — `EventBus`、`EventType`、`RiskAlertEvent`
- `src.monitoring.notifier.base` — `BaseNotifier`、`AlertLevel`、`AlertMessage`

### External
- `prometheus_client` — `Counter`、`Gauge`、`Histogram`、`start_http_server`
- `structlog` — 结构化日志
- `threading` — `HealthServer`、`PrometheusServer` 均运行在后台线程
- `http.server` — `HealthServer` 基于标准库 HTTP 服务器实现

<!-- MANUAL: -->
