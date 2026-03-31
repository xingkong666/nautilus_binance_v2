# AGENTS.md — src/cache/

Redis 客户端封装 — 可选基础设施；系统在无 Redis 时自动降级运行。

---

## 文件说明

| 文件 | 职责 |
|---|---|
| `redis_client.py` | `RedisClient` — 对 `redis-py` 的轻量封装；供 `RateLimiter` 和 `CircuitBreaker` 使用。 |

---

## RedisClient

```python
client = RedisClient(config)   # config: RedisConfig
client.is_available            # 初始化连接失败时为 False
client.get(key) -> str | None
client.set(key, value, ex=ttl_seconds)
client.incr(key) -> int
client.close()
```

- 连接在构造时尝试建立；失败时将 `is_available` 设为 `False`，不抛出异常。
- `Container.build()` 在连接失败时记录 `redis_unavailable_degraded_mode` 日志，并将 `_redis_client` 设为 `None`。
- 所有使用方（`RateLimiter`、`CircuitBreaker`）检查客户端是否为 `None`，并回退到进程内状态。

---

## 配置（core/config.py 中的 RedisConfig）

| 字段 | 默认值 | 环境变量覆盖 |
|---|---|---|
| `host` | `127.0.0.1` | `REDIS_HOST` |
| `port` | `6379` | `REDIS_PORT` |
| `password` | `""` | `REDIS_PASSWORD` |
| `db` | `0` | `REDIS_DB` |
| `socket_timeout` | `2.0` 秒 | — |
| `socket_connect_timeout` | `2.0` 秒 | — |

---

## 使用说明

- Redis 是**可选的** — 系统在无 Redis 时可完整运行（降级模式）。
- Redis 可用时，支持跨进程频率限制和分布式熔断器状态（多实例部署的关键）。
- NautilusTrader 自身的缓存也可使用 Redis（通过 `AppConfig.cache` 配置），与此处的 `RedisClient` 是独立的。
