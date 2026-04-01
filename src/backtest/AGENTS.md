<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-01 | Updated: 2026-04-01 -->

# Backtest

## Purpose
回测执行与分析层，封装 NautilusTrader `BacktestEngine` 提供统一入口。支持单/多 symbol 回测、walk-forward 滚动样本外验证、市场状态过滤（Regime Filter）、成本与资金费率分析、报告生成。数据源为 `ParquetDataCatalog`，策略通过 `AppFactory` 注入。

## Key Files

| File | Description |
|------|-------------|
| `runner.py` | `BacktestRunner` + `BacktestConfig` + `BacktestRunResult` — 核心执行引擎；封装 NautilusTrader `BacktestEngine`，配置 venue / instrument / data，注册策略，返回 `BacktestRunResult` |
| `walkforward.py` | `WalkforwardWindow` + `generate_walkforward_windows()` — 生成滚动样本内/样本外窗口（按月滑动），供外部循环调用 `BacktestRunner` |
| `regime.py` | `SymbolRegimeSnapshot` + `regime_allows_strategy()` — 市场状态过滤；计算趋势强度（slope_ratio、EMA gap）、ADX、资金费率均值，判断当前市场是否适合运行该策略 |
| `report.py` | `BacktestReporter` — 将 `BacktestRunResult` 格式化为文本报告（收益、回撤、胜率、夏普等）并支持保存为 JSON |
| `costs.py` | `BacktestCostAnalyzer` + `CostAnalysis` — 成本与资金费率分析；整合 `CostModel`、`SlippageModel`，计算佣金/滑点/资金费用，输出扣费后 PnL |
| `__init__.py` | 模块公开导出：`BacktestRunner`、`BacktestConfig`、`BacktestRunResult` |

## For AI Agents

### Working In This Directory
- `BacktestRunner.run()` 流程：构建 `BacktestEngine`（venue/instrument/data）→ 注册策略 → `engine.run()` → 返回 `BacktestRunResult`
- 数据通过 `ParquetDataCatalog` 加载（路径由 `AppConfig` 配置），需先用 `scripts/download_data.py` 下载
- `BacktestConfig` 为 dataclass，包含 symbol、interval、start/end 日期、初始资金等参数
- Walk-forward 用法：`generate_walkforward_windows()` 返回 `WalkforwardWindow` 列表，外部循环对每个窗口调用 `BacktestRunner`
- Regime 过滤：`regime_allows_strategy()` 返回 `bool`，`snapshot=None` 时默认放行；`veto_strategy_names` 可按策略名豁免过滤
- `BacktestCostAnalyzer` 依赖 `src.execution.cost_model.CostModel` 和 `src.execution.slippage.SlippageModel`，需确保两者已正确配置
- 回测模式下策略的 `generate_signal()` 直接调用 `submit_order()`，不经过 `EventBus`（BaseStrategy 基类处理此分支）

### Testing Requirements
- 回测测试路径：`tests/regression/`（基准回测，防止性能退化）
- `BacktestRunner` 集成测试需要 ParquetDataCatalog 中有数据，建议使用小样本 fixture
- `generate_walkforward_windows()` 可纯单元测试（无外部依赖），验证窗口数量和日期正确性
- `regime.py` 中的 `regime_allows_strategy()` 可用固定 `SymbolRegimeSnapshot` 数据单元测试
- 运行：`uv run pytest tests/regression/ -v`

```bash
# 运行回测脚本
uv run python scripts/run_backtest.py \
  --config configs/strategies/ema_cross.yaml \
  --env configs/env/dev.yaml \
  --start 2024-01-01 --end 2024-06-30
```

### Common Patterns
```python
from src.backtest.runner import BacktestRunner, BacktestConfig
from src.core.enums import Interval

config = BacktestConfig(
    symbol="BTCUSDT",
    interval=Interval.MIN_1,
    start=dt.date(2024, 1, 1),
    end=dt.date(2024, 6, 30),
    initial_balance=Decimal("10000"),
)
runner = BacktestRunner(app_config, strategy_config)
result = runner.run(config)

# Walk-forward
from src.backtest.walkforward import generate_walkforward_windows

windows = generate_walkforward_windows(
    start=dt.date(2023, 1, 1), end=dt.date(2024, 12, 31),
    train_months=6, test_months=1, step_months=1,
)
for w in windows:
    result = runner.run(BacktestConfig(start=w.test_start, end=w.test_end, ...))

# 生成报告
from src.backtest.report import BacktestReporter
BacktestReporter(result).print_summary()
BacktestReporter(result).save_json(Path("reports/result.json"))
```

## Dependencies

### Internal
- `src.core.config` — `AppConfig`
- `src.core.enums` — `Interval`、`INTERVAL_TO_NAUTILUS`
- `src.core.nautilus_cache` — `build_nautilus_cache_settings`
- `src.strategy.base` — `BaseStrategy`、`BaseStrategyConfig`
- `src.execution.cost_model` — `CostModel`（`costs.py` 使用）
- `src.execution.slippage` — `SlippageModel`（`costs.py` 使用）

### External
- `nautilus_trader.backtest.engine` — `BacktestEngine`
- `nautilus_trader.config` — `BacktestEngineConfig`
- `nautilus_trader.persistence.catalog` — `ParquetDataCatalog`
- `nautilus_trader.model` — `CryptoPerpetual`、`Venue`、`Money`、`USDT`
- `pandas` — walk-forward 窗口和 regime 数据处理
- `structlog` — 结构化日志

<!-- MANUAL: -->
