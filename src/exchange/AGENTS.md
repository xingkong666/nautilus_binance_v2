<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-01 | Updated: 2026-04-01 -->

# Exchange

## Purpose
交易所接入层，封装 NautilusTrader 的 Binance Futures 客户端（`BinanceLiveDataClientFactory` + `BinanceLiveExecClientFactory`），提供统一的 `BinanceAdapter` 接口。负责 `TradingNode` 的配置和生命周期管理，支持 `LIVE / TESTNET` 环境切换。架构位置：`OrderRouter → BinanceAdapter → NautilusTrader BinanceFutures{Data,Exec}Client`。

## Key Files

| File | Description |
|------|-------------|
| `binance_adapter.py` | `BinanceAdapter` + `BinanceAdapterConfig` — 封装 `TradingNode`，注册 DataClient 和 ExecClient 工厂，暴露 `start()` / `stop()` 异步接口；`adapter.node` 可访问底层 `TradingNode` |
| `__init__.py` | 模块公开导出：`BinanceAdapter`、`BinanceAdapterConfig` |

## For AI Agents

### Working In This Directory
- **`start()` / `stop()` 均为 `async`**，必须在异步上下文中 `await` 调用
- **调用顺序**：`await adapter.start()` → 使用 → `await adapter.stop()` → `container.teardown()`（必须先 stop adapter 再 teardown）
- 环境配置使用 `BinanceEnvironment` 枚举（`LIVE` / `TESTNET`），推荐方式（NautilusTrader 1.223.0+）
- API Key 优先读取环境变量（`.env`），`BinanceAdapterConfig` 中可配置 YAML fallback
- `BinanceAdapterConfig` 为 dataclass，字段：`api_key`、`api_secret`、`environment: BinanceEnvironment`
- Testnet 基础 URL：通过 `get_http_base_url(BinanceEnvironment.TESTNET)` 获取
- `TradingNode` 实例通过 `adapter.node` 访问，可直接订阅数据或提交订单（通常由 NautilusTrader 策略机制自动处理）
- `BinanceFuturesAccountHttpAPI` 用于直接 REST 调用（如 `account_sync.py` 获取余额），绕过 EventBus 机制

### Testing Requirements
- 集成测试使用 testnet 配置（`configs/env/dev.yaml`）
- Smoke 测试：`uv run python scripts/smoke_testnet.py`（行情接收 → 市价单 → 成交 → 停止）
- 单元测试 mock `TradingNode` 验证 `start()` / `stop()` 调用顺序
- 测试时确保 `.env` 中已配置 testnet API Key：`BINANCE_API_KEY` / `BINANCE_API_SECRET`

```bash
# Testnet 冒烟测试
uv run python scripts/smoke_testnet.py
```

### Common Patterns
```python
from src.exchange.binance_adapter import BinanceAdapter, BinanceAdapterConfig
from nautilus_trader.adapters.binance.common.enums import BinanceEnvironment

# 配置（推荐方式）
cfg = BinanceAdapterConfig(
    api_key="YOUR_KEY",      # 或省略，从环境变量读取
    api_secret="YOUR_SECRET",
    environment=BinanceEnvironment.TESTNET,
)

# 生命周期
adapter = BinanceAdapter(cfg)
await adapter.start()

node = adapter.node  # 访问底层 TradingNode

# 关闭（必须在 container.teardown() 之前）
await adapter.stop()
await container.teardown()
```

## Dependencies

### Internal
- `src.core.config` — `AppConfig`（API Key、环境配置读取）

### External
- `nautilus_trader.adapters.binance` — `BinanceLiveDataClientFactory`、`BinanceLiveExecClientFactory`
- `nautilus_trader.adapters.binance.common.enums` — `BinanceEnvironment`
- `nautilus_trader.adapters.binance.config` — `BinanceDataClientConfig`、`BinanceExecClientConfig`、`BinanceInstrumentProviderConfig`
- `nautilus_trader.adapters.binance.futures.http.account` — `BinanceFuturesAccountHttpAPI`
- `nautilus_trader.adapters.binance.http.client` — `BinanceHttpClient`
- `nautilus_trader.adapters.binance.common.urls` — `get_http_base_url`
- `structlog` — 结构化日志
- `asyncio` / `threading` — 异步生命周期管理

<!-- MANUAL: -->
