<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-01 | Updated: 2026-04-01 -->

# configs/monitoring/

## Purpose
Monitoring and alerting configuration for the production observability stack. `alerts.yaml` defines alert rules evaluated by `AlertManager` and dispatched via `Notifier` (Telegram/Slack); it also documents known monitoring gaps where YAML rules exist but corresponding `Watcher` code has not yet been implemented. `prometheus.yml` configures Prometheus scrape targets (trading system at `:9100/metrics`) and the Alertmanager integration at `:9093`.

## Key Files

| File | Description |
|------|-------------|
| `alerts.yaml` | Alert channel config (Telegram/Slack), named alert rules with conditions and message templates, inline gap documentation |
| `prometheus.yml` | Prometheus global settings (15s scrape interval), Alertmanager target, rule file paths, scrape job for trading system and Prometheus self-monitoring |

## For AI Agents

### Working In This Directory
- Monitoring is **disabled by default** in `dev.yaml` (`monitoring.enabled: false`); it activates in `prod.yaml`.
- Alert channel credentials (`bot_token`, `chat_id`, Slack `webhook_url`) must come from `.env`—never hardcode in YAML.
- `risk_alert_cooldown_seconds: 60` deduplicates repeated alerts for the same rule + instrument pair.
- **Known monitoring gaps** (documented in `alerts.yaml` comments): `position_limit_warning`, `daily_pnl_milestone`, and `strategy_health` rules have no backing `Watcher` code yet—they require new `Watcher` classes subscribing to `PositionEvent`, `AccountEvent`, and heartbeat events respectively.
- Phase 2 adjustment: change `drawdown_warning` condition from `> 10.0` to `> 13.0`.

### Common Patterns
```yaml
# alerts.yaml rule structure
rules:
  - name: circuit_breaker_triggered
    level: CRITICAL
    message: "🚨 熔断触发: {reason}"

  - name: drawdown_warning
    level: ERROR
    condition: "drawdown_pct > 10.0"
    message: "⚠️ 回撤预警: {drawdown_pct:.1f}%"
```
- Alert `level` values: `CRITICAL`, `ERROR`, `WARNING`, `INFO`.
- Telegram channel only receives `CRITICAL` and `ERROR`; Slack (disabled) would receive `CRITICAL` only.
- Prometheus scrape target for the trading system is `:9100/metrics` (set in `dev.yaml` as `monitoring.prometheus_port: 9100`).

## Dependencies

### Internal
- `src.monitoring.alert_manager.AlertManager` — evaluates rules from `alerts.yaml`
- `src.monitoring.notifier.Notifier` — dispatches to Telegram/Slack channels
- `src.monitoring.watchers.*` — watcher classes that publish `RiskAlert` events
- `src.core.events.EventBus` — `EVENT_BUS_EVENTS` counter auto-incremented per event type
- `src.core.config.load_app_config` — merges into `AppConfig.monitoring` and `AppConfig.alerting`

### External
- Prometheus (`:9090`) — scrapes `:9100/metrics`
- Grafana (`:3000`) — dashboards backed by Prometheus + PostgreSQL
- Alertmanager (`:9093`) — receives rules from `prometheus.yml`
- Telegram Bot API / Slack Webhooks — outbound notification channels

<!-- MANUAL: -->
