<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-01 | Updated: 2026-04-01 -->

# Cache

## Purpose
Redis 缓存层，提供带连接池和静默降级的 `RedisClient` 封装。Redis 不可用时所有操作返回 `None`/`False`，保证主交易链路不因 Redis 故障中断。主要用途：熔断器状态持久化、账户余额/持仓快照（短 TTL）、限流滑动窗口和实时风控指标共享。

## Key Files

| File | Description |
|------|-------------|
| `redis_client.py` | `RedisClient` — 带连接池（`ConnectionPool`）和降级的 Redis 封装；所有操作捕获 `RedisError` 静默处理 |
| `__init__.py` | 模块公开导出：`RedisClient` |

## For AI Agents

### Working In This Directory

**Key 命名规范（前缀 `nautilus:`）：**

| Key | 类型 | TTL | 用途 |
|-----|------|-----|------|
| `nautilus:cb:state` | Hash | 无 | 熔断器状态 |
| `nautilus:account:balance:{asset}` | Hash | 30s | 账户余额快照 |
| `nautilus:account:position:{symbol}:{side}` | Hash | 30s | 持仓快照 |
| `nautilus:rl:orders:second` | ZSET | 2s | 限流滑动窗口（秒级） |
| `nautilus:rl:orders:minute` | ZSET | 120s | 限流滑动窗口（分钟级） |
| `nautilus:risk:metrics` | Hash | 无 TTL | 实时风控指标 |

- `RedisClient` 由 `Container` 在基础设施构建阶段初始化，通过 `container.redis` 访问
- 降级策略：`RedisError` 捕获后记录 structlog warning，返回 `None`/`False`/空值，主流程继续
- 新增 key 时遵守 `nautilus:` 前缀和上表命名规范，避免与其他应用冲突
- ZSET 限流窗口使用时间戳作为 score，查询时用 `ZRANGEBYSCORE` 滑动统计
- `RedisConfig` 从 `AppConfig` 读取（host、port、db、password、pool_size 等）

### Testing Requirements
- 单元测试 mock `Redis`（`unittest.mock.patch`）验证调用参数和降级行为
- 降级测试：让 `Redis.__init__` 抛出 `ConnectionError`，验证后续所有操作返回安全默认值
- 集成测试需 Docker 中 Redis 服务运行：`docker compose up -d redis`，默认端口 `:6379`

### Common Patterns
```python
# 从 Container 获取客户端（推荐）
redis = container.redis  # RedisClient 实例

# 账户余额写入（带 TTL）
redis.hset("nautilus:account:balance:USDT", mapping={"free": "9500.0", "locked": "500.0"})
redis.expire("nautilus:account:balance:USDT", 35)  # interval_sec + 5

# 风控指标读取
metrics = redis.hgetall("nautilus:risk:metrics")

# 熔断器状态
redis.hset("nautilus:cb:state", mapping={"status": "HALTED", "triggered_at": "1234567890"})

# 降级示例（Redis 不可用时 get 返回 None）
value = redis.get("nautilus:cb:state")  # None if Redis down
if value is None:
    # 使用内存状态作为 fallback
    ...
```

## Dependencies

### Internal
- `src.core.config` — `RedisConfig`（连接参数）

### External
- `redis` — `Redis`、`ConnectionPool`、`RedisError`（`redis-py`）
- `structlog` — 结构化日志（降级时记录 warning）

<!-- MANUAL: -->
