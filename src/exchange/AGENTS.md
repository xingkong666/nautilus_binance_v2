# AGENTS.md — src/exchange/

Binance 合约适配器 — 将 NautilusTrader 与 Binance REST/WebSocket 对接。

---

## 文件说明

| 文件 | 职责 |
|---|---|
| `binance_adapter.py` | `BinanceAdapter` + `BinanceAdapterConfig` + `build_binance_adapter()` 辅助函数。 |

---

## BinanceAdapterConfig

| 字段 | 类型 | 说明 |
|---|---|---|
| `api_key` | `str \| None` | Binance API 密钥（实盘或测试网） |
| `api_secret` | `str \| None` | Binance API 密钥 Secret |
| `environment` | `BinanceEnvironment` | `LIVE`、`TESTNET` 或 `DEMO` |
| `instrument_ids` | `list[str]` | 预配置的交易对列表 |
| `futures_leverages` | `dict \| None` | 每个交易对的杠杆覆盖值 |
| `proxy_url` | `str \| None` | 受限网络的 HTTP/SOCKS5 代理 |
| `use_reduce_only` | `bool` | 附加 `reduce_only` 标志（双向持仓账户需禁用） |
| `use_position_ids` | `bool` | 双向持仓模式使用持仓 ID |
| `max_retries` | `int \| None` | 瞬态 API 错误的重试次数 |
| `base_url_http` | `str` | REST 基础 URL（根据环境自动设置） |

---

## BinanceAdapter 关键方法

| 方法 | 说明 |
|---|---|
| `prepare_runtime_config()` | 查询账户模式（单向/双向），自动调整 `use_reduce_only`。 |
| `register_strategy(strategy)` | 将策略挂载到底层 NautilusTrader `TradingNode`。 |
| `build_node()` | 用所有已注册策略和交易对构建 `TradingNode`。 |
| `run()` | **阻塞。** 启动节点事件循环；停止或出错时返回。 |
| `request_stop()` | 发送信号，使节点优雅停机。 |
| `fetch_account_snapshot()` | HTTP 请求 → `(余额, 持仓)` 用于启动状态引导。 |
| `fetch_open_orders()` | HTTP 请求 → 挂单列表，用于启动时填充忽略列表。 |

---

## 凭证解析优先级

从高到低：

1. `BinanceAdapterConfig` 中显式传入的 `api_key` / `api_secret`
2. 通过 `EnvSettings` 读取的环境变量：
   - `TESTNET` → `BINANCE_TESTNET_API_KEY` / `BINANCE_TESTNET_API_SECRET`
   - `DEMO` → `BINANCE_DEMO_API_KEY` / `BINANCE_DEMO_API_SECRET`
   - `LIVE` → `BINANCE_API_KEY` / `BINANCE_API_SECRET`

---

## 双向持仓模式处理

启动时 `prepare_runtime_config()` 调用 Binance 账户 API：
- 若账户为**双向持仓模式**：自动将 `use_reduce_only` 设为 `False`，避免下单被拒。
- 此检查在 `build_node()` 之前执行。

---

## build_binance_adapter() 辅助函数

快速构建，用于脚本和测试：

```python
adapter = build_binance_adapter(
    environment=BinanceEnvironment.TESTNET,
    symbols=["BTCUSDT"],
    leverages={"BTCUSDT": 10},
)
```

自动从环境变量读取 API 密钥。**不会**启动节点。
