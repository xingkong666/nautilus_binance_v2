# 200%+ 年化研究蓝图（2026-03）

## 先说结论

当前仓库还没有任何一套单策略、单标的、固定仓位回测，能够证明“稳定年化 200%+”已经成立。

但这个项目的架构已经足够支撑一套更现实的高增长研究路线：

1. 以 `VegasTunnelStrategy` 作为趋势突破主引擎。
2. 以 `EMAPullbackATRStrategy` 作为趋势回撤补仓/补段引擎。
3. 放弃把 `MicroScalpStrategy` 当主收益源，在成本建模接入前只保留为候选。
4. 把目标从“单策略神参数”改成“状态切换 + 多标的 + 风险预算 + 更高资金利用率”。

如果要把 200%+ 年化作为研究目标，而不是口号，必须同时满足三件事：

- 信号有正期望。
- 资金利用率足够高。
- 回测已经纳入手续费、滑点、资金费率，并通过样本外验证。

当前仓库第一项已有雏形，后两项仍然明显不足。

## 仓库现状判断

### 1. 当前最值得继续做的策略

#### `VegasTunnelStrategy`

- `experiments/reports/vegas_tunnel_smoke_20240101_20240630/summary.json`
  - 2024-01-01 到 2024-06-30
  - BTCUSDT
  - 1h
  - `PnL% = 5.8513`
- `experiments/reports/58c1bc9b-1cf4-498c-bdec-b3fe7e1cb9ad/summary.json`
  - 2024-07-01 到 2024-12-31
  - BTCUSDT
  - 1h
  - `PnL% = 3.0639`
  - `Sharpe = 2.2945`
  - `PF = 1.4942`

结论：这是仓库里目前最像“主趋势收益引擎”的策略。

#### `EMAPullbackATRStrategy`

已有扫描结果显示，ADX 过滤是关键变量：

- `experiments/sweep/ema_pullback_adx_scan_20260301T155251Z.csv`
  - 某组样本内结果里 `adx_threshold=25` 时
  - `PnL% = 9.1295`
  - `Sharpe = 2.4015`
  - `PF = 1.4391`
- `experiments/sweep/ema_pullback_adx_scan_2025_20260301T155922Z.csv`
  - 2025 样本外只有 `adx_threshold=35`
  - `PnL% = 1.1101`
  - 其余阈值普遍为负

结论：这个策略有 alpha，但高度依赖市场状态，不适合裸跑，必须配状态过滤。

### 2. 当前不适合当主力的策略

#### `MicroScalpStrategy`

- `experiments/reports/863f185c-211d-426c-b28f-e950b71bca9a/summary.json`
  - 2024-01-01 到 2024-03-31
  - BTCUSDT
  - 1m
  - `PnL% = -13.9459`
  - `total_orders = 12424`

结论：当前版本的 1m 高频逻辑在现有回测口径下被交易成本和噪声严重侵蚀，不应承载 200% 目标。

### 3. 当前回测的两个硬缺口

#### 缺口 A：成本模型没有真正进入回测主引擎

仓库里有：

- `src/execution/cost_model.py`
- `src/execution/slippage.py`

但当前 `src/backtest/runner.py` 里没有把手续费、滑点、资金费率统一打进回测结果。

这意味着当前大多数收益结论都偏乐观，尤其对短周期策略更明显。

#### 缺口 B：仓位利用率偏低，且 sizing 还不是杠杆感知的

当前大多数实验仍然使用固定：

- `trade_size = 0.01`

而 `BaseStrategy._resolve_qty_from_capital_pct(...)` 的实现是：

- 以账户总权益百分比换算名义仓位
- 没有把账户杠杆作为仓位放大器纳入 sizing 语义

所以现在的很多 `PnL%` 更像“轻仓信号质量测试”，不是“满配部署后的收益上限测试”。

## 我建议的 200% 研究路线

## 策略结构

### Sleeve A：趋势突破主引擎

策略：

- `VegasTunnelStrategy`

职责：

- 捕捉 1h 级别的大段趋势延续
- 在强趋势环境里提供主要利润来源

建议标的：

- BTCUSDT
- ETHUSDT
- SOLUSDT
- BNBUSDT

初始参数：

- `fast_ema_period = 12`
- `slow_ema_period = 36`
- `tunnel_ema_period_1 = 144`
- `tunnel_ema_period_2 = 169`
- `stop_atr_multiplier = 1.0`
- `tp_fib = [1.0, 1.618, 2.618]`
- `tp_split = [0.4, 0.3, 0.3]`
- `interval = 1h`

### Sleeve B：趋势回撤补段引擎

策略：

- `EMAPullbackATRStrategy`

职责：

- 在趋势已确认后，只做顺势回撤再启动
- 弥补 Vegas 只能等均线再穿越的入场滞后

建议参数起点：

- `fast_ema_period = 20`
- `slow_ema_period = 50`
- `pullback_atr_multiplier = 1.0`
- `min_trend_gap_ratio = 0.0003`
- `signal_cooldown_bars = 3`
- `adx_period = 14`
- `adx_threshold = 25` 作为样本内起点
- `adx_threshold = 35` 作为样本外保守版候选
- `interval = 1h`

### Sleeve C：短线套利/反转候选

策略：

- 暂不正式启用 `MicroScalpStrategy`

只在以下条件全部满足后再启用：

- 回测已接入真实手续费
- 回测已接入滑点
- 至少用 `maker/taker` 两种情景压测
- 订单寿命、撤单率、追价行为在实盘仿真中通过

## 市场状态切换

200%+ 年化不能靠“永远开着同一套趋势策略”完成，必须做状态切换。

建议把状态机拆成三档：

### Trend-On

条件：

- 1h `ADX >= 25`
- `ATR / Close` 位于过去 90 天中高分位
- 资金费率绝对值不过热
- 价格相对标记价格无异常偏离

动作：

- 开启 `VegasTunnel`
- 开启 `EMAPullbackATR`
- 提高趋势 sleeve 风险预算

### Trend-Crowded

条件：

- 趋势仍在，但资金费率显著偏正或偏负
- 同时 ATR 扩张过快

动作：

- 保留趋势方向，但下调新开仓额度
- 只允许回撤型入场，不追新突破

### Range/Chop

条件：

- `ADX < 20`
- `ATR / Close` 低位
- 趋势策略近期连续假突破

动作：

- 关闭趋势 sleeve
- 仅保留观察，不上短线高频主策略

## 资金与风险预算

如果继续用当前仓库里最常见的固定 `trade_size=0.01`，200%+ 年化没有讨论价值。

建议改成组合层的风险预算：

- 总体可部署资金：`80%`
- 储备资金：`20%`
- 分配模式：`risk_parity`

建议初始分配：

- `VegasTunnel`：`50%`
- `EMAPullbackATR`：`30%`
- 现金储备：`20%`

单策略约束：

- 单策略最大分配不超过总权益 `35%`
- 单标的最大分配不超过总权益 `20%`
- 单次新增仓位风险不超过总权益 `0.75%`
- 日内净值回撤 `2.5%` 触发降档
- 组合回撤 `6%` 触发 `reduce_only`

## 必须新增的工程项

### 1. 给回测接入真实成本

优先级：最高

需要接入：

- maker/taker 手续费
- 固定与成交量相关滑点
- 资金费率

在这件事完成前，不应该把任何回测当成 200% 候选。

### 2. 增加杠杆感知 sizing

当前 `capital_pct_per_trade` 更像现货式名义仓位比例。

建议新增明确语义的字段，二选一即可：

- `gross_exposure_pct_per_trade`
- `risk_pct_per_trade`

否则策略 alpha 可能不错，但永远吃不到足够的资本效率。

### 3. 增加状态过滤特征

建议在 `src/data/feature_store.py` 附近新增以下特征缓存：

- `funding_rate`
- `mark_price_basis`
- `atr_percentile`
- `rolling_adx`

这些特征不是为了“预测顶底”，而是为了关掉最容易亏钱的交易窗口。

### 4. 样本外与滚动验证

最少分四段：

- 2022-01-01 到 2022-12-31
- 2023-01-01 到 2023-12-31
- 2024-01-01 到 2024-12-31
- 2025-01-01 到 2025-12-31

通过条件建议设为：

- 每个自然年 `PF > 1.15`
- 每个自然年 `Max Drawdown < 18%`
- 任一年不出现单年大幅负收益
- 参数不因年度切换而大幅漂移

## 我建议的落地顺序

1. 先把 `VegasTunnel + EMAPullbackATR` 做成组合研究主线。
2. 接入手续费、滑点、资金费率到回测。
3. 增加状态过滤，不再裸跑趋势策略。
4. 把 sizing 从固定币数切到风险预算。
5. 再做多标的与风险平价，而不是先去堆更多策略。

## 最终判断

这个仓库可以研究“冲击 200%+ 年化”的方案，但当前还不具备证明该目标可达的证据链。

当前最正确的做法不是继续微调均线参数，而是：

- 用 `VegasTunnel` 做主趋势，
- 用 `EMAPullbackATR` 做趋势补段，
- 用状态机决定什么时候开、什么时候关，
- 用成本感知回测和风险预算决定能不能放大仓位。

如果这些步骤做完，才有资格讨论 200%+ 年化是不是可重复。

## 2026-03-15 研究进展

截至 2026-03-15，上面列出的几项关键工程已经有了可运行基线：

- 回测结果已接入手续费、滑点、资金费率分析。
- sizing 已支持 `margin_pct_per_trade` + `sizing_leverage` 的杠杆感知语义。
- 已有组合 walk-forward 脚本、风险平价分配、分数加权、样本外拼接权益曲线。
- 已接入 Binance funding 下载与本地特征落盘。

### 当前最佳配置

当前最佳配置不是单策略，而是 BTC/ETH 双标的双策略组合：

- 配置文件：`configs/strategies/vegas_ema_combo_multi_grid.yaml`
- 最优场景：`selection_min_score = 0`
- 分配模式：`risk_parity + sqrt(score)`
- 组合门槛：`min_active_strategies = 2`

组合包含 4 条腿：

- `vegas_btc`
- `pullback_btc`
- `vegas_eth`
- `pullback_eth`

对应样本外聚合结果：

- `experiments/walkforward/vegas_ema_combo_multi_grid/score_0/walkforward_aggregate.json`
- 样本外成本后平均收益率：`15.4811%`
- 样本外拼接权益终值：`21472.3272`
- 以 `2024-07-01` 到 `2025-12-31` 的样本外区间推算，年化约 `66.4381%`

结论：

- 这是当前仓库里最强的真实 walk-forward 研究基线。
- 但它离“稳定年化 200%+”仍然有很大距离。

### 已验证但未胜出的方案

#### 1. Symbol-level hard veto

- 配置文件：`configs/strategies/vegas_ema_combo_multi_best_regime.yaml`
- 结果目录：`experiments/walkforward/vegas_ema_combo_multi_best_regime`

结果：

- 样本外成本后平均收益率：`14.6486%`
- 拼接权益终值：`20482.1158`

结论：

- 直接按 symbol 整体关闭策略过于粗糙。
- 它会连带砍掉原本还有效的回撤腿。

#### 2. Strategy-level veto

- 配置文件：`configs/strategies/vegas_ema_combo_multi_best_strategy_regime.yaml`
- 结果目录：`experiments/walkforward/vegas_ema_combo_multi_best_strategy_regime`

结果：

- 样本外成本后平均收益率：`15.1773%`
- 拼接权益终值：`21225.6823`

结论：

- 方向比 symbol-level veto 更合理。
- 但仍未超过当前最佳基线。

#### 3. Strategy-level veto + relaxed gate

- 配置文件：`configs/strategies/vegas_ema_combo_multi_best_strategy_regime_relaxed_gate.yaml`
- 结果目录：`experiments/walkforward/vegas_ema_combo_multi_best_strategy_regime_relaxed_gate`

结果：

- 样本外成本后平均收益率：`14.1107%`
- 拼接权益终值：`19927.3453`

结论：

- 放宽组合门槛虽然避免了空仓窗口，但保留下来的单腿并没有足够 alpha。
- `window 3` 从空仓变成了亏损交易，说明问题不只是 gate 太严。

### 当前判断

目前最有研究价值的主线仍然是：

1. 保留 `vegas_ema_combo_multi_grid.yaml` 的 `score_0` 作为最佳基线。
2. 不再继续推进 hard veto 方案。
3. 下一阶段优先测试“regime 只影响权重，不直接关停”的 allocation penalty。
