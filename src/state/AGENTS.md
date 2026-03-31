# AGENTS.md — src/state/

崩溃恢复、成交持久化和状态对账。

---

## 文件说明

| 文件 | 职责 |
|---|---|
| `snapshot.py` | `SnapshotManager` — 将 `SystemSnapshot` 以 JSON 格式保存/加载到磁盘。 |
| `persistence.py` | `TradePersistence` — 通过 psycopg 将成交记录写入 PostgreSQL。 |
| `reconciliation.py` | `ReconciliationEngine` — 比对快照状态与交易所状态，检测偏差。 |
| `recovery.py` | `RecoveryManager` — 编排启动恢复：加载最新快照、执行对账、发布对账事件。 |

---

## SystemSnapshot（snapshot.py）

```python
@dataclass
class SystemSnapshot:
    timestamp_ns: int
    positions: list[PositionSnapshot]
    account_balance: str        # Decimal 以字符串存储
    open_orders: list[dict]
    metadata: dict[str, Any]
```

- 快照以 JSON 格式写入 `data/snapshots/<env>/` 目录。
- `SnapshotManager.save(snapshot)` — 原子写入（写临时文件后 rename）。
- `SnapshotManager.load_latest()` → `SystemSnapshot | None`。
- 快照目录按环境区分（`snapshots/dev`、`snapshots/prod`）。

---

## 恢复流程（bootstrap._bootstrap_live_state）

1. `adapter.fetch_account_snapshot()` → 原始余额 + 持仓
2. `adapter.fetch_open_orders()` → 原始挂单列表
3. 将持仓规范化后全部加入 `IgnoredInstrumentRegistry`
4. `RecoveryManager.recover(exchange_positions, account_balance)`：
   - 加载最新快照
   - 调用 `ReconciliationEngine.reconcile(snapshot, exchange_positions)`
   - 返回解析后的 `SystemSnapshot`，若无历史状态则返回 `None`
5. 将解析后的快照重新保存到磁盘

---

## TradePersistence

- PostgreSQL 连接 URL 来自 `AppConfig.data.database_url`（环境变量 `DATABASE_URL` 可覆盖）。
- `record_fill(fill_event)` — 在 `ORDER_FILLED` 事件时向 `trades` 表插入一行记录。
- `close()` — 关闭数据库连接池。
- 若启动时数据库不可达，则优雅地空操作（记录警告，不崩溃）。

---

## ReconciliationEngine

`reconcile(snapshot, exchange_positions)` → `ReconciliationResult`：
- 将每个快照持仓与实时交易所持仓比对。
- 标记状态：`MATCHED`、`MISSING_IN_EXCHANGE`、`MISSING_IN_SNAPSHOT`、`SIZE_MISMATCH`。
- 发布携带完整差异的 `EventType.RECONCILIATION` 事件。
- **不**自动平掉不匹配的持仓 — 该决策由运维人员做出。
