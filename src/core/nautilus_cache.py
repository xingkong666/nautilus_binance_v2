"""NautilusTrader cache 配置构造."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from nautilus_trader.common.config import DatabaseConfig
from nautilus_trader.config import CacheConfig
from nautilus_trader.core.uuid import UUID4

from src.core.config import AppConfig, CacheModeConfig


@dataclass(frozen=True)
class ResolvedCacheSettings:
    """解析后的 cache 配置结果."""

    cache: CacheConfig | None
    instance_id: UUID4 | None


def build_nautilus_cache_settings(
    app_config: AppConfig,
    mode: Literal["live", "backtest"],
) -> ResolvedCacheSettings:
    """构造指定运行模式的 NautilusTrader cache 配置."""
    cache_cfg = app_config.cache
    if not cache_cfg.enabled:
        return ResolvedCacheSettings(cache=None, instance_id=None)

    mode_cfg = _resolve_mode_config(cache_cfg.live, cache_cfg.backtest, mode)
    instance_id = UUID4() if mode_cfg.use_instance_id else None
    database = DatabaseConfig(
        type=cache_cfg.database,
        host=app_config.redis.host,
        port=app_config.redis.port,
        password=app_config.redis.password or None,
        timeout=cache_cfg.database_timeout,
    )
    cache = CacheConfig(
        database=database,
        encoding=cache_cfg.encoding,
        timestamps_as_iso8601=cache_cfg.timestamps_as_iso8601,
        persist_account_events=cache_cfg.persist_account_events,
        buffer_interval_ms=cache_cfg.buffer_interval_ms,
        bulk_read_batch_size=cache_cfg.bulk_read_batch_size,
        use_trader_prefix=cache_cfg.use_trader_prefix,
        use_instance_id=mode_cfg.use_instance_id,
        flush_on_start=mode_cfg.flush_on_start,
        drop_instruments_on_reset=mode_cfg.drop_instruments_on_reset,
        tick_capacity=cache_cfg.tick_capacity,
        bar_capacity=cache_cfg.bar_capacity,
    )
    return ResolvedCacheSettings(cache=cache, instance_id=instance_id)


def _resolve_mode_config(
    live_cfg: CacheModeConfig,
    backtest_cfg: CacheModeConfig,
    mode: Literal["live", "backtest"],
) -> CacheModeConfig:
    mode_cfg = live_cfg if mode == "live" else backtest_cfg
    return CacheModeConfig(
        use_instance_id=bool(mode_cfg.use_instance_id),
        flush_on_start=bool(mode_cfg.flush_on_start),
        drop_instruments_on_reset=bool(mode_cfg.drop_instruments_on_reset),
    )
