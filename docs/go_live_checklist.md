# 实盘上线 Checklist

## 必须完成

1. `.env` 已配置 `BINANCE_API_KEY` / `BINANCE_API_SECRET` / `DATABASE_URL` / `REDIS_*`。
2. `configs/env/prod.yaml` 中 `exchange.environment=LIVE` 且 `live.strategy_config` 指向本次策略。
3. 首次上线前保持 `SUBMIT_ORDERS=false`，先完成 dry-run 启动验证。
4. 执行 `uv run pytest -q`，结果必须全绿。
5. 执行 `uv run python scripts/check_live_readiness.py --env prod --strategy-config <strategy> --check-account-snapshot`。
6. 监控已就绪：`http://localhost:9100/metrics`、`http://localhost:8080/health`、Grafana、Telegram 告警。
7. 启动前确认账户内不存在外部持仓和外部挂单；若存在，理解并接受 ignored 保护行为。

## Canary 顺序

1. 使用 `SUBMIT_ORDERS=false CONFIRM_LIVE=YES scripts/run_live_prod.sh` 启动一次，确认策略、订阅、恢复、监控全部正常。
2. 观察 10-15 分钟，确认 `/health` 正常、Prometheus 有指标、无连续 `RISK_ALERT`。
3. 切换为最小仓位参数，并设置 `SUBMIT_ORDERS=true`。
4. 再次启动，先只放行一个交易对做小资金 canary。
5. 首笔真实成交后核对 Binance 订单、仓位、快照和持久化记录是否一致。
6. 稳定运行一段时间后，再扩大到完整 universe。

## 常驻启动

推荐使用：

```bash
sudo cp deploy/systemd/nautilus-live.service /etc/systemd/system/nautilus-live.service
sudo systemctl daemon-reload
sudo systemctl enable --now nautilus-live.service
```

手工启动：

```bash
CONFIRM_LIVE=YES SUBMIT_ORDERS=false scripts/run_live_prod.sh
```

真实放单前再显式开启：

```bash
CONFIRM_LIVE=YES SUBMIT_ORDERS=true scripts/run_live_prod.sh
```

## 回滚

1. 立即执行 `sudo systemctl stop nautilus-live.service` 或向进程发送 `SIGTERM`。
2. 保留日志、快照和交易所订单记录，不要直接删状态目录。
3. 若 Binance 仍有仓位或挂单，先人工确认是否需要平仓/撤单，再决定是否重启系统。
