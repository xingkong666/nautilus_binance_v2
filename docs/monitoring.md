# 监控 & 告警

## 架构概览

```
交易节点
  ├── PrometheusServer (:9090/metrics)   → Prometheus → Grafana
  ├── HealthServer     (:8080/health)    → 负载均衡 / K8s 探针
  ├── Alerting Engine                    → Telegram / Slack
  └── Watchers (定时巡检)
```

---

## Prometheus 指标

启动后访问 `http://localhost:9090/metrics` 查看所有指标。

### 核心指标

| 指标名 | 类型 | 说明 |
|---|---|---|
| `trading_orders_total` | Counter | 累计下单数，按 `side` / `instrument` 分组 |
| `trading_fills_total` | Counter | 累计成交数 |
| `trading_position_usd` | Gauge | 当前持仓名义价值 |
| `trading_unrealized_pnl_usd` | Gauge | 未实现盈亏 |
| `trading_daily_pnl_usd` | Gauge | 当日已实现盈亏 |
| `trading_drawdown_pct` | Gauge | 当前回撤百分比 |
| `trading_fill_latency_ms` | Histogram | 下单到成交延迟分布 |
| `trading_circuit_breaker_triggered_total` | Counter | 熔断触发次数 |
| `trading_risk_rejections_total` | Counter | 风控拒绝次数，按 `reason` 分组 |
| `trading_account_balance_usd` | Gauge | 账户余额 |
| `trading_node_uptime_seconds` | Gauge | 节点运行时长 |

---

## HealthCheck 端点

```
GET http://localhost:8080/health
```

返回示例：

```json
{
  "status": "healthy",
  "uptime_seconds": 3600,
  "components": {
    "exchange": "connected",
    "data_feed": "active",
    "risk": "armed",
    "supervisor": "running"
  },
  "account": {
    "balance_usd": 10000.0,
    "open_positions": 1,
    "daily_pnl_usd": 42.5,
    "drawdown_pct": 0.3
  }
}
```

当任何组件异常时 `status` 变为 `"degraded"` 或 `"unhealthy"`，HTTP 状态码返回 503。

---

## 告警配置

配置文件：`configs/monitoring/alerts.yaml`

### RiskAlert 去重冷却

`RiskAlertWatcher` 支持对重复告警做去重冷却，避免短时间告警风暴：

- 配置项：`alerting.risk_alert_cooldown_seconds`
- 去重键：`rule_name + instrument_id`
- 默认值：`60` 秒

示例：

```yaml
alerting:
  enabled: true
  risk_alert_cooldown_seconds: 60
```

### 告警级别

| 级别 | 含义 | 默认渠道 |
|---|---|---|
| `CRITICAL` | 需要立即处理（熔断 / 对账不一致） | Telegram |
| `ERROR` | 需要关注（回撤预警 / 仓位接近上限） | Telegram |
| `WARNING` | 可以稍后处理（延迟升高等） | 日志 |

### 内置告警规则

| 规则 | 级别 | 触发条件 |
|---|---|---|
| `circuit_breaker_triggered` | CRITICAL | 熔断触发时 |
| `drawdown_warning` | ERROR | 回撤 > 3.0% |
| `position_limit_warning` | ERROR | 持仓 > 最大仓位 80% |
| `order_fill_latency` | WARNING | 成交延迟 > 1000ms |
| `reconciliation_mismatch` | CRITICAL | 对账发现不一致 |

新增执行层数量安全规则：

| 规则 | 级别 | 触发条件 |
|---|---|---|
| `order_router_quantity_normalized` | WARNING | 下单数量被向下规范化到最小步进 |
| `order_router_quantity_below_step` | ERROR | 下单数量小于最小步进被拒绝 |
| `order_router_quantity_invalid` | ERROR | 下单数量非正值被拒绝 |

### 添加自定义告警规则

在 `configs/monitoring/alerts.yaml` 的 `rules` 列表中添加：

```yaml
- name: my_custom_rule
  level: ERROR
  condition: "my_metric > threshold"
  message: "⚠️ 自定义告警: {my_metric}"
```

---

## Telegram 告警配置

1. 创建 Telegram Bot（找 @BotFather）
2. 获取 `bot_token` 和 `chat_id`
3. 在 `.env` 中配置：

```dotenv
TELEGRAM_BOT_TOKEN=1234567890:ABCdef...
TELEGRAM_CHAT_ID=-1001234567890
```

4. 在 `configs/monitoring/alerts.yaml` 中确认 Telegram channel 已启用：

```yaml
channels:
  - type: telegram
    enabled: true
    levels: [CRITICAL, ERROR]
```

---

## Docker 监控栈

```bash
# 启动 Prometheus + Grafana + Alertmanager
docker compose up -d

# 查看状态
docker compose ps
```

| 服务 | 端口 | 说明 |
|---|---|---|
| Prometheus | 9090 | 指标采集 |
| Grafana | 3000 | 可视化面板（admin/admin） |
| Alertmanager | 9093 | 告警路由 |

### Prometheus 抓取配置

在 `configs/monitoring/prometheus.yml` 中配置抓取目标：

```yaml
scrape_configs:
  - job_name: 'trading-node'
    static_configs:
      - targets: ['host.docker.internal:9090']
    scrape_interval: 10s
```

---

## Watchers（定时巡检）

`src/monitoring/watchers.py` 中的后台任务，默认每 60 秒执行一次：

- **AccountWatcher** — 检查账户余额异常变动
- **PositionWatcher** — 检查仓位是否超限
- **LatencyWatcher** — 统计近期成交延迟
- **ReconciliationWatcher** — 触发定期对账（默认每 15 分钟）

巡检间隔可在 `container.py` 中配置。
