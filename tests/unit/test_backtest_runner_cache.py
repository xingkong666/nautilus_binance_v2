"""Tests for backtest runner cache wiring."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from src.backtest import runner as runner_module
from src.core import config as config_module


class _StubEnvSettings:
    def __init__(self) -> None:
        self.env = "dev"
        self.binance_api_key = ""
        self.binance_api_secret = ""
        self.binance_testnet_api_key = ""
        self.binance_testnet_api_secret = ""
        self.binance_demo_api_key = ""
        self.binance_demo_api_secret = ""
        self.telegram_bot_token = ""
        self.telegram_chat_id = ""
        self.prometheus_port = None
        self.database_url = ""
        self.redis_host = ""
        self.redis_port = None
        self.redis_password = ""
        self.redis_db = None
        self.live_strategy_config = ""
        self.submit_orders = None
        self.exchange_environment = ""


class _FakeBacktestEngine:
    def __init__(self, config) -> None:
        self.config = config
        self.venues: list[dict[str, object]] = []

    def add_venue(self, **kwargs) -> None:
        self.venues.append(kwargs)


def _write_yaml(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _load_app_config(monkeypatch, tmp_path: Path) -> config_module.AppConfig:
    configs_dir = tmp_path / "configs"
    monkeypatch.setattr(config_module, "CONFIGS_DIR", configs_dir)
    monkeypatch.setattr(config_module, "EnvSettings", _StubEnvSettings)
    _write_yaml(
        configs_dir / "env" / "dev.yaml",
        """
env: dev
data:
  catalog_dir: data/processed/catalog
redis:
  host: 127.0.0.1
  port: 6379
cache:
  enabled: true
  database: redis
  backtest:
    use_instance_id: true
    flush_on_start: false
""",
    )
    _write_yaml(configs_dir / "execution" / "execution.yaml", "execution:\n  rate_limit: {}\n")
    _write_yaml(configs_dir / "risk" / "global_risk.yaml", "risk:\n  mode: soft\n")
    _write_yaml(configs_dir / "accounts" / "binance_futures.yaml", "account:\n  name: test\n")
    _write_yaml(configs_dir / "monitoring" / "alerts.yaml", "alerting:\n  enabled: true\n")
    return config_module.load_app_config(env="dev")


def test_build_engine_applies_backtest_cache(monkeypatch, tmp_path: Path) -> None:
    """Verify that backtest runner injects Redis cache into BacktestEngineConfig."""
    app_config = _load_app_config(monkeypatch, tmp_path)
    monkeypatch.setattr(runner_module, "ParquetDataCatalog", lambda path: object())
    monkeypatch.setattr(runner_module, "BacktestEngine", _FakeBacktestEngine)

    backtest_config = runner_module.BacktestConfig(
        start=dt.date(2024, 1, 1),
        end=dt.date(2024, 1, 31),
    )
    runner = runner_module.BacktestRunner(app_config, backtest_config)

    engine = runner._build_engine()

    assert engine.config.cache is not None
    assert engine.config.cache.database is not None
    assert engine.config.cache.database.type == "redis"
    assert engine.config.cache.flush_on_start is False
    assert engine.config.cache.use_instance_id is True
    assert engine.config.instance_id is not None
