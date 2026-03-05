# Vegas 隧道与执行安全增强（2026-03）

## 变更摘要

本次迭代完成了从策略实现到执行安全兜底、告警降噪、回测基线的整套落地：

1. 新增 Vegas 隧道策略并接入工厂、CLI、YAML。
2. 将“数量切分/步进对齐”抽象为 BaseStrategy 通用工具并统一复用。
3. 执行层新增最小下单步进守卫（OrderRouter 统一规范化/拒单）。
4. 数量异常通过 RiskAlert 事件进入监控告警链路。
5. RiskAlertWatcher 新增去重冷却（同 rule + instrument 冷却窗口）。
6. 增加回归基线与端到端回测冒烟结果快照。

---

## 关键功能变更

### 1) Vegas Tunnel 策略接入

- 新增策略文件：`src/strategy/vegas_tunnel.py`
- 新增策略配置模板：`configs/strategies/vegas_tunnel.yaml`
- 工厂接入：`src/app/factory.py`
- 回测脚本接入：`scripts/run_backtest.py`

核心规则（本次实现）：

- 入场：EMA12/EMA36 穿越 + 位于 EMA144/EMA169 隧道外侧
- 止盈：按隧道宽度 Fib 三档（1.0 / 1.618 / 2.618）分批
- 止损：ATR 初始止损，TP1 后保本上移
- 默认分仓：40/30/30

---

### 2) BaseStrategy 数量工具统一

文件：`src/strategy/base.py`

新增/明确了三类工具：

1. `_split_quantity_by_ratios_preserve_total(...)`
2. `_split_quantity_by_ratios_strict_step(...)`
3. `_resolve_order_quantity_decimal(...)`

语义说明：

- `preserve_total`：优先总量守恒，最后一段可包含步进尾差。
- `strict_step`：每段都严格满足最小步进，合计 `<= total_qty`。

接入策略：

- `VegasTunnelStrategy`：分仓走 `strict_step`
- `TurtleStrategy`：单位仓位/平仓量走统一数量入口
- `MicroScalpStrategy`：下单数量元数据走统一数量入口

---

### 3) 执行层数量安全兜底

文件：`src/execution/order_router.py`

新增行为：

1. 下单前按 `instrument.size_increment` 向下规范化数量。
2. 规范化后数量 `<= 0` 直接拒单（不 submit）。
3. 对 `MARKET/LIMIT` 统一生效。

---

### 4) 数量异常告警事件

文件：`src/execution/order_router.py`

新增 RiskAlertEvent 规则：

- `order_router_quantity_normalized`（`WARNING`）
- `order_router_quantity_below_step`（`ERROR`）
- `order_router_quantity_invalid`（`ERROR`）

这些事件会进入现有链路：

`EventBus -> RiskAlertWatcher -> AlertManager -> Telegram/Slack`

---

### 5) 告警去重与冷却

文件：`src/monitoring/watchers.py`, `configs/monitoring/alerts.yaml`

`RiskAlertWatcher` 增加去重冷却：

- 去重键：`rule_name + instrument_id`
- 冷却配置：`alerting.risk_alert_cooldown_seconds`
- 默认值：`60`

---

## 回测冒烟结果（端到端）

命令：

```bash
uv run python scripts/run_backtest.py \
  --env dev \
  --strategy vegas_tunnel \
  --symbols BTCUSDT \
  --start 2024-01-01 \
  --end 2024-06-30 \
  --save \
  --output-dir experiments/reports/vegas_tunnel_smoke_20240101_20240630
```

结果快照（Run ID: `e87209cd-9164-4b7e-9802-1a91e525f4bf`）：

- interval: `1h`
- iterations: `262080`
- total_orders: `224`
- total_positions: `54`
- PnL(total): `+585.1334 USDT`（`+5.8513%`）
- Win Rate: `0.5741`

产物目录：

- `experiments/reports/vegas_tunnel_smoke_20240101_20240630/summary.json`
- `orders.csv` / `order_fills.csv` / `positions.csv` / `account.csv`

---

## 新增/更新测试

主要测试文件：

- `tests/unit/test_vegas_tunnel_strategy.py`
- `tests/regression/test_vegas_tunnel_baseline.py`
- `tests/unit/test_base_strategy_sizing.py`
- `tests/unit/test_turtle_strategy.py`
- `tests/unit/test_order_router.py`
- `tests/unit/test_watchers.py`

覆盖点：

- Vegas 入场/分批/止损迁移/最小步进
- 数量切分语义（守恒 vs 严格步进）
- OrderRouter 数量规范化与拒单
- RiskAlertWatcher 去重冷却

---

## 上线检查清单

### 部署前（Pre-Deploy）

- [ ] 代码质量检查通过：
  - `uv run ruff check src/ tests/`
  - `uv run pytest tests/unit -q`
  - `uv run pytest tests/regression -q`
- [ ] 关键配置已核对：
  - `configs/strategies/vegas_tunnel.yaml` 参数与预期一致（EMA/TP/Fib/分仓）
  - `configs/monitoring/alerts.yaml` 中 `risk_alert_cooldown_seconds` 已设置（建议 `60`）
- [ ] 环境变量完整（至少）：
  - Binance API Key（对应环境）
  - Telegram/Slack 告警凭据（若启用）
- [ ] 数据可用性确认：
  - 回测区间内 `BTCUSDT` 数据完整
  - 实盘/仿真环境网络与交易所连通

### 部署中（Deploy）

- [ ] 启动命令使用 `bootstrap_context`/标准入口，不绕过 Container：
  - `uv run python -m src.app.bootstrap --env configs/env/dev.yaml`（或 prod）
- [ ] 启动后检查日志中以下关键事件：
  - `strategy_created`（`VegasTunnel`）
  - `watcher_registered`（含 `RiskAlertWatcher`）
  - `alert_manager_started`
- [ ] 观察首次信号生成与执行：
  - 无 `order_submit_failed`
  - 无 `quantity below step` 连续错误

### 部署后（Post-Deploy）

- [ ] 监控面板检查（前 30 分钟）：
  - 订单/成交指标有更新
  - 无异常告警风暴（RiskAlert 去重生效）
- [ ] 告警链路抽样验证：
  - 人工触发 1 条 `WARNING` 或 `ERROR`，确认 Telegram/Slack 可达
- [ ] 执行安全校验：
  - 抽样检查 `order_qty` 均满足 `size_increment`
  - 无“数量归一化后为 0”的拒单持续出现
- [ ] 回滚预案可执行：
  - 保留上一版本启动命令与配置快照
  - 可在 5 分钟内回滚并重启

### 快速验收命令（建议）

```bash
# 1) Vegas 回测冒烟（固定样本）
uv run python scripts/run_backtest.py \
  --env dev \
  --strategy vegas_tunnel \
  --symbols BTCUSDT \
  --start 2024-01-01 \
  --end 2024-06-30 \
  --save \
  --output-dir experiments/reports/vegas_tunnel_smoke_20240101_20240630

# 2) 执行与告警相关单测
uv run pytest tests/unit/test_order_router.py tests/unit/test_watchers.py -q
```

---

## 生产值班手册（时间线）

### T-30min（上线前 30 分钟）

- [ ] 确认发布窗口与回滚负责人在线。
- [ ] 执行一次快速健康检查：
  - `uv run pytest tests/unit/test_order_router.py tests/unit/test_watchers.py -q`
- [ ] 核对告警渠道可用（Telegram/Slack 至少 1 条测试消息成功）。
- [ ] 固定本次版本号、配置快照路径、回滚命令到值班记录。

### T-10min（上线前 10 分钟）

- [ ] 冻结策略参数变更（避免上线同时改参数）。
- [ ] 最终确认关键配置：
  - `vegas_tunnel.yaml`
  - `alerts.yaml`（`risk_alert_cooldown_seconds`）
- [ ] 清空非必要噪音告警，保证上线期间信号可读。

### T0（执行发布）

- [ ] 按标准命令启动：
  - `uv run python -m src.app.bootstrap --env configs/env/prod.yaml`
- [ ] 观察启动日志 1-2 分钟，确认：
  - `strategy_created: VegasTunnel`
  - `watcher_registered: RiskAlertWatcher`
  - `alert_manager_started`

### T+10min（发布后 10 分钟）

- [ ] 检查是否出现持续异常：
  - `order_submit_failed`
  - `order_router_quantity_below_step` 高频告警
- [ ] 抽样 1-3 条订单，确认 `order_qty` 满足最小步进。
- [ ] 记录当前状态：订单数、仓位数、告警数。

### T+60min（发布后 1 小时）

- [ ] 复查是否出现告警风暴（去重冷却是否生效）。
- [ ] 对比上线前后关键指标趋势（成交、拒单、告警）。
- [ ] 判定“稳定/需回滚”并在值班记录中结论化。

### 回滚触发条件（建议）

- [ ] 连续出现执行失败且 10 分钟内无法缓解。
- [ ] `order_router_quantity_below_step` 持续高频且影响实际成交。
- [ ] 告警系统异常（误抑制或风暴）导致无法监控风险。

### 回滚操作（建议）

- [ ] 切回上一稳定版本代码与配置快照。
- [ ] 重启服务并确认基础健康项恢复。
- [ ] 在值班记录中补充：
  - 触发原因
  - 回滚时间
  - 后续修复 owner 与 ETA
