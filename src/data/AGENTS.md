# AGENTS.md — src/data/

行情数据加载、特征工程、数据校验和版本管理。

---

## 文件说明

| 文件 | 职责 |
|---|---|
| `loaders.py` | `BinanceDataLoader` — 从 Binance REST API 下载 OHLCV K 线，写入 Parquet 目录。 |
| `feature_store.py` | `FeatureStore` — 计算并缓存衍生特征（RSI、ATR、ADX、布林带等）。 |
| `validators.py` | `DataValidator` — 检查数据质量：缺口、重复时间戳、价格异常。 |
| `funding.py` | `FundingRateLoader` — 拉取并缓存永续合约资金费率，用于成本归因。 |
| `versioning.py` | `DataVersionManager` — 给数据集快照打标签并附加元数据，保障可复现性。 |

---

## BinanceDataLoader

```python
loader = BinanceDataLoader(raw_dir=Path("data/raw"), catalog_dir=Path("data/processed/catalog"))
loader.download(symbol="BTCUSDT", interval="1m", start="2024-01-01", end="2024-06-30")
loader.ingest_to_catalog(symbol="BTCUSDT", interval="1m")
```

- 下载到 `data/raw/<symbol>/<interval>/`，格式为 CSV。
- 将原始 CSV 导入到 `data/processed/catalog/` 的 NautilusTrader `ParquetDataCatalog`。
- 自动检测已有数据，跳过重复下载（增量更新）。

入口脚本：`scripts/download_data.py`。

---

## FeatureStore

- 从 Parquet 目录读取 K 线数据。
- 使用 pandas 计算特征（ATR、EMA、RSI、ADX、布林带等）。
- 将结果以 Parquet 格式缓存到 `data/features/<symbol>/<interval>/`。
- 供 `WalkForwardOptimizer` 和 `RegimeDetector` 使用。

---

## DataValidator

`validate(df)` → `ValidationResult`，包含问题列表：
- 缺失 K 线（缺口检测）
- 重复时间戳
- OHLC 逻辑错误（最高价 < 最低价、开/收价超出范围）
- 极端价格尖刺（阈值可配置）

在导入原始数据前调用，以捕获 API 响应中的错误数据。

---

## 数据目录结构

```
data/
├── raw/
│   └── BTCUSDT/
│       └── 1m/          ← 从 Binance API 下载的 CSV 文件
├── processed/
│   └── catalog/         ← NautilusTrader ParquetDataCatalog
├── features/
│   └── BTCUSDT/
│       └── 1m/          ← 计算好的特征 Parquet 文件
└── versioned/           ← 已打标签的数据集快照
```

`data/` 下所有目录均已加入 `.gitignore`。
