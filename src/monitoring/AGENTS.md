# AGENTS.md — src/monitoring/

可观测性：Prometheus 指标、HTTP 健康接口、告警、事件 Watcher。

---

## 文件说明

| 文件 | 职责 |
|---|---|
| `metrics.py` | Prometheus 指标定义（计数器、仪表盘、直方图）。 |
| `prometheus_server.py` | `PrometheusServer` — 在配置的端口上启动 `prometheus_client` HTTP 导出器。 |
| `health_server.py` | `HealthServer` — 轻量级 HTTP 服务器，暴露 `/health` 接口（200 OK 或 503）。 |
| `alerting.py` | `AlertManager` + `build_alert_manager()` — 将 `RiskAlertEvent` 路由到各通知器。 |
| `watchers.py` | `BaseWatcher` + 具体 Watcher — 订阅 EventBus，在满足条件时触发告警。 |
| `notifier/base.py` | `BaseNotifier` 抽象基类。 |
| `notifier/telegram.py` | `TelegramNotifier` — 通过 Telegram Bot API 发送告警。 |
| `notifier/slack.py` | `SlackNotifier` — 通过 Slack Incoming Webhook 发送告警。 |

---

## Prometheus 指标（metrics.py）

在 `http://localhost:<prometheus_port>/metrics` 暴露的关键指标：

| 指标名 | 类型 | 标签 | 说明 |
|---|---|---|---|
| `EVENT_BUS_EVENTS` | Counter | `event_type` | 每种类型发布的事件总数 |
| `ORDERS_SUBMITTED` | Counter | `instrument`, `side` | 路由到交易所的订单数 |
| `ORDERS_REJECTED` | Counter | `instrument`, `reason` | 被风控/频率限制阻断的订单数 |
| `RISK_ALERTS` | Counter | `level`, `rule` | 触发的风控违规次数 |
| `POSITION_VALUE` | Gauge | `instrument` | 当前名义持仓价值 |
| `DRAWDOWN_PCT` | Gauge | — | 当前追踪回撤百分比 |

仅在配置中 `monitoring.enabled = true` 时启用。

---

## AlertManager

- 订阅 EventBus 上的 `EventType.RISK_ALERT`。
- 分发到所有已注册的 `BaseNotifier` 实例。
- `start()` / `stop()` 管理内部队列和工作线程。
- 通过 `configs/monitoring/alerts.yaml` → `alerting.*` 配置。

`build_alert_manager(event_bus, alerting_config, telegram_token, telegram_chat_id)` 根据配置组装 Telegram 和/或 Slack 通知器。

---

## Watcher（watchers.py）

Watcher 订阅特定 `EventType` 并在满足自定义条件时触发 `AlertManager`：

| Watcher | 触发条件 |
|---|---|
| `DrawdownWatcher` | 回撤越过 WARNING/CRITICAL 阈值 |
| `CircuitBreakerWatcher` | 熔断器触发 |
| `FillWatcher` | 意外成交（数量/方向不符） |
| `PositionWatcher` | 持仓规模超过配置阈值 |

`build_watchers(event_bus, alert_manager, alerting_config)` 创建并返回所有已配置的 Watcher。

---

## 健康服务器

`GET /health` 返回：
- `200 {"status": "ok"}` — 所有检查通过。
- `503 {"status": "degraded", "checks": {...}}` — 一项或多项检查失败。

包含的健康检查：Redis 连通性、交易所适配器连通性、回撤在限额内、熔断器状态。

---

## 新增告警

1. 在 `watchers.py` 中添加 `BaseWatcher` 子类。
2. 在 `__init__` 中订阅相关 `EventType`。
3. 触发时调用 `self._alert_manager.send(RiskAlertEvent(...))`。
4. 在 `build_watchers()` 中注册。
