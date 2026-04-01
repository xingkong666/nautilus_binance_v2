<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-01 | Updated: 2026-04-01 -->

# Data

## Purpose
历史数据管理层，负责从 Binance 下载 K 线数据、写入 NautilusTrader `ParquetDataCatalog`、数据质量验证、资金费率获取、衍生特征存储和数据版本管理。为回测和实盘预热提供数据基础。设计特点：断点续传（元数据记录已下载月份）、批量异步下载、严格质量校验（间隙检测、OHLCV 逻辑校验）。

## Key Files

| File | Description |
|------|-------------|
| `loaders.py` | `BaseBinanceDownloader` 及子类 — 从 Binance 下载合约历史 K 线（ZIP → Parquet），支持断点续传、批量下载、数据校验；写入 `ParquetDataCatalog` |
| `feature_store.py` | `FeatureStore` — 使用 Parquet 存储计算好的衍生特征（指标值、市场状态等），避免重复计算；按特征集名称管理 |
| `funding.py` | 资金费率工具 — 从 `https://fapi.binance.com/fapi/v1/fundingRate` 获取资金费率历史，`normalize_funding_rates()` 标准化响应为 DataFrame |
| `validators.py` | `validate_data_completeness()` + `validate_kline_dataframe()` — K 线数据质量校验：时间间隙检测（`DataGap`）、OHLCV 逻辑校验、缺失 Bar 统计；`DataValidationError` 异常 |
| `versioning.py` | `DataVersionManager` — 对处理后数据进行版本化（基于内容哈希），支持历史版本回滚和对比 |
| `__init__.py` | 模块公开导出 |

## For AI Agents

### Working In This Directory
- 下载数据前确保 `ParquetDataCatalog` 路径存在（由 `AppConfig` 配置）
- `BaseBinanceDownloader` 子类通过覆盖 `TRADER_TYPE` / `PATH_TEMPLATE` 实现合约/现货差异化
- 断点续传依赖元数据 JSON 文件记录已下载月份，删除元数据文件可强制重新下载
- `validate_kline_dataframe()` 在写入 Catalog 前调用；失败时抛出 `DataValidationError`，不写入损坏数据
- `FeatureStore` 的特征文件以特征集名称命名存储在 `features_dir/` 目录，使用 `save_features()` / `load_features()`
- `DataVersionManager` 用内容 SHA256 哈希标记版本，支持 `list_versions()` / `restore_version()`
- 资金费率数据供 `src.backtest.regime` 的 `SymbolRegimeSnapshot` 计算使用

### Testing Requirements
- 单元测试路径：`tests/unit/test_validators.py`、`tests/unit/test_feature_store.py`
- `validators.py` 函数可纯单元测试，构造含间隙的 DataFrame 验证 `DataGap` 检测
- `loaders.py` 测试需 mock `httpx.AsyncClient`（拦截 ZIP 下载请求）
- `FeatureStore` 测试使用 `tmp_path` pytest fixture 隔离文件系统
- 下载集成测试需网络访问 Binance API，仅在集成测试套件中运行

```bash
# 下载历史数据
uv run python scripts/download_data.py
```

### Common Patterns
```python
# 数据验证
from src.data.validators import validate_kline_dataframe, DataValidationError

try:
    validate_kline_dataframe(df, expected_interval_ms=60_000)
except DataValidationError as e:
    logger.error("data_validation_failed", error=str(e))

# 特征存储
from src.data.feature_store import FeatureStore
from pathlib import Path

store = FeatureStore(features_dir=Path("data/features"))
store.save_features("ema_cross_signals", df_features)
df = store.load_features("ema_cross_signals")

# 资金费率
from src.data.funding import normalize_funding_rates

rows = await fetch_funding_rates("BTCUSDT", start_ms, end_ms)
df_funding = normalize_funding_rates(rows)

# 数据版本化
from src.data.versioning import DataVersionManager

mgr = DataVersionManager(versioned_dir=Path("data/versioned"))
mgr.save_version("btcusdt_1m", df)
versions = mgr.list_versions("btcusdt_1m")
```

## Dependencies

### Internal
- `src.core.enums` — `Interval`、`INTERVAL_TO_NAUTILUS`、`TraderType`

### External
- `nautilus_trader.model` — `BarType`、`CryptoPerpetual`
- `nautilus_trader.persistence.catalog` — `ParquetDataCatalog`
- `nautilus_trader.persistence.wranglers_v2` — `BarDataWranglerV2`（K 线转换写入 Catalog）
- `httpx` — 异步 HTTP 下载（`loaders.py` 用 `AsyncClient`，`funding.py` 用同步客户端）
- `pandas` — DataFrame 数据处理
- `structlog` — 结构化日志
- `zipfile` / `hashlib` / `json` — ZIP 解压、内容哈希、元数据记录

<!-- MANUAL: -->
