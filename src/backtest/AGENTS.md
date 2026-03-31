# AGENTS.md — src/backtest/

NautilusTrader 回测引擎封装，含滚动优化、市场状态检测和报告生成。

---

## 文件说明

| 文件 | 职责 |
|---|---|
| `runner.py` | `BacktestRunner` + `BacktestConfig` — 构建引擎、从 Parquet 目录加载数据、运行策略。 |
| `walkforward.py` | `WalkForwardOptimizer` — 滚动样本内/样本外参数搜索。 |
| `regime.py` | `RegimeDetector` — 基于 ADX + ATR/收盘价比率，将市场分类为趋势/震荡/高波动。 |
| `report.py` | `BacktestReport` — 回测后指标：夏普比率、索提诺比率、最大回撤、胜率、盈亏比。 |
| `costs.py` | `BacktestCostAnalyzer` — 按成交归因手续费/资金费/滑点成本。 |

---

## BacktestConfig

```python
@dataclass
class BacktestConfig:
    start: dt.date
    end: dt.date
    symbols: list[str]           # 例如 ["BTCUSDT"]
    interval: Interval           # 默认 MINUTE_1
    starting_balance_usdt: int   # 默认来自 AppConfig.account.starting_balance
    leverage: float              # 默认来自 AppConfig.account.max_leverage
    trader_id: str               # 默认 "BACKTESTER-001"
    bypass_logging: bool         # 默认 True（加速运行）
    run_analysis: bool           # 默认 True
```

---

## BacktestRunner.run()

```python
result = runner.run(strategy_cls, strategy_config)
```

1. 构建带 BINANCE 场所、USDT 账户、配置杠杆的 `BacktestEngine`。
2. 从 `AppConfig.data.catalog_dir` 的 `ParquetDataCatalog` 加载合约和 K 线数据。
3. 向引擎添加策略。
4. 调用 `engine.run()`。
5. 返回含 `engine`、`stats`、`report` 的 `BacktestResult`。

**数据必须预先通过 `scripts/download_data.py` 下载到 `data/raw/`，并导入到 `data/processed/catalog/` 的 Parquet 目录中。**

---

## WalkForwardOptimizer

- 将完整日期范围划分为滚动窗口：`in_sample_days`、`out_of_sample_days`。
- 针对每个窗口在参数网格上运行 `BacktestRunner.run()`。
- 选取样本内最优参数，在 OOS 窗口上评估。
- 返回含每窗口指标和最优参数集的 `WalkForwardResult`。

---

## RegimeDetector

- 使用 ADX + ATR/收盘价比率将每根 K 线分类为 `TRENDING`、`RANGING` 或 `VOLATILE`。
- 用于条件性启用/禁用策略或调整仓位大小。
- 可以喂入历史 K 线，用于回测市场状态条件策略。

---

## 运行回测

```bash
# EMA 交叉策略
uv run python scripts/run_backtest.py --strategy ema_cross --symbols BTCUSDT \
  --start 2024-01-01 --end 2024-06-30 --interval 15m --fast-ema 10 --slow-ema 20

# 海龟策略
uv run python scripts/run_backtest.py --strategy turtle --symbols BTCUSDT \
  --start 2024-01-01 --end 2024-06-30 --entry-period 20 --exit-period 10

# 维加斯通道策略
uv run python scripts/run_backtest.py --strategy vegas_tunnel --symbols BTCUSDT \
  --start 2024-01-01 --end 2024-06-30
```

---

## 关键不变量

- 回测数据从 Parquet 目录只读 — 运行期间禁止写入。
- `bypass_logging=True` 禁用 Nautilus 内部控制台输出，保持开启以提升速度。
- `tests/regression/` 中的回归基准在未重新运行完整回测前禁止收紧阈值。
