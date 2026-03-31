# AGENTS.md — tests/

测试套件：332 个测试，分布在单元测试、集成测试和回归测试三个层级。

---

## 目录结构

```
tests/
├── unit/          # 快速隔离测试 — 无外部依赖，不使用真实 NautilusTrader 引擎
├── integration/   # 多组件测试 — 组装 Container、EventBus、真实管道
└── regression/    # 基准回测测试 — 指标阈值；未重新运行回测前禁止收紧
```

---

## 运行测试

```bash
# 全部测试
uv run pytest

# 按层级
uv run pytest tests/unit/ -v
uv run pytest tests/integration/ -v
uv run pytest tests/regression/ -v

# 单个文件
uv run pytest tests/unit/test_order_router.py -v

# 单个测试用例
uv run pytest tests/unit/test_order_router.py::test_route_market_order -v

# 带覆盖率
uv run pytest --cov=src --cov-report=html
```

所有测试使用 `asyncio_mode = "auto"` — 无需 `@pytest.mark.asyncio` 装饰器。

---

## 单元测试（`tests/unit/`）

| 文件 | 测试内容 |
|---|---|
| `test_order_router.py` | OrderRouter 路由、空跑、多策略分发 |
| `test_signal_processor.py` | SignalProcessor 管道、频率限制、忽略交易对 |
| `test_allocator.py` | PortfolioAllocator 的 equal/weight/risk_parity 模式 |
| `test_config_loading.py` | YAML 分层、环境变量覆盖、deep_merge |
| `test_ema_cross_filters.py` | EMACrossStrategy 信号逻辑（mock K 线） |
| `test_ema_pullback_atr.py` | EMAPullbackATRStrategy 入场/出场条件 |
| `test_turtle_strategy.py` | 海龟策略突破入场、加仓、止损逻辑 |
| `test_vegas_tunnel_strategy.py` | 维加斯通道入场、斐波那契分批止盈 |
| `test_micro_scalp_strategy.py` | MicroScalp 限价单逻辑、冷却期 |
| `test_base_strategy_sizing.py` | 定仓模式：固定/保证金%/名义%/资金% |
| `test_reconciliation.py` | ReconciliationEngine 各类差异情形 |
| `test_validators.py` | DataValidator 缺口/尖刺/重复检测 |
| `test_watchers.py` | 监控 Watcher 在正确事件上触发 |
| `test_supervisor.py` | LiveSupervisor 状态转换 |
| `test_live_readiness.py` | ensure_live_readiness 门禁条件 |
| `test_live_warmup.py` | preload_strategies_warmup K 线喂入 |
| `test_account_sync.py` | AccountSync 偏差检测 |
| `test_bootstrap_live.py` | bootstrap_live_state 持仓规范化 |
| `test_binance_adapter.py` | BinanceAdapter 配置/凭证解析 |
| `test_factory_strategy_loader.py` | AppFactory.create_strategy_from_config 分发 |
| `test_notifier_telegram.py` | TelegramNotifier 消息格式化 |
| `test_loaders.py` | BinanceDataLoader 下载/导入逻辑 |
| `test_funding.py` | FundingRateLoader 拉取/缓存 |
| `test_regime.py` | RegimeDetector 分类 |
| `test_walkforward.py` | WalkForwardOptimizer 窗口切分 |
| `test_backtest_runner_cache.py` | BacktestRunner 目录缓存设置 |
| `test_backtest_costs.py` | BacktestCostAnalyzer 手续费归因 |
| `test_nautilus_cache.py` | build_nautilus_cache_settings 实盘/回测模式 |
| `test_downloader.py` | download_data 脚本参数处理 |

---

## 集成测试（`tests/integration/`）

| 文件 | 测试内容 |
|---|---|
| `test_container_build.py` | 完整 Container.build() + teardown；验证所有服务已初始化 |
| `test_event_pipeline.py` | SignalEvent → OrderRouter → mock Strategy 端到端 |
| `test_risk_integration.py` | PreTradeRisk + CircuitBreaker + DrawdownController 联动 |
| `test_multi_strategy_integration.py` | 同一 EventBus 上多个策略，路由隔离 |

集成测试可使用真实的 `EventBus`、真实的 `Container`（含 mock 交易所），但不发起真实网络请求。

---

## 回归测试（`tests/regression/`）

| 文件 | 基准指标 |
|---|---|
| `test_ema_cross_baseline.py` | 在固定 BTC 数据集上：夏普比率 ≥ 阈值，最大回撤 ≤ 阈值 |
| `test_rsi_baseline.py` | 在固定 BTC 数据集上：胜率、盈亏比 |
| `test_turtle_baseline.py` | 在 4h BTC 数据集上：夏普比率、盈亏比、最大回撤 |
| `test_vegas_tunnel_baseline.py` | 在 1h BTC 数据集上：夏普比率、盈亏比、最大回撤 |

**未重新运行完整回测并更新 `conftest.py` 基准前，禁止收紧回归阈值。** 这些测试用于防止重构时无意间导致策略性能退化。

---

## 测试规范

- `EventBus` fixture 必须 `yield` 并在 teardown 中调用 `bus.clear()`。
- 对 NautilusTrader 的 `Strategy`、`Instrument`、`Bar` 对象使用 `unittest.mock.MagicMock`。
- 测试命名：`test_<被测行为>` 的描述性 snake_case。
- 单元测试和集成测试中禁止真实网络/交易所调用。
- 回归测试使用本地 `data/processed/catalog/` 的 Parquet 数据 — CI 必须预先填充该数据。
