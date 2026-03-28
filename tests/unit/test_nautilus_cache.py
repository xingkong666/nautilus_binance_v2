"""Tests for NautilusTrader cache wiring."""

from __future__ import annotations

from pathlib import Path

from src.core import config as config_module
from src.core.nautilus_cache import build_nautilus_cache_settings


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


def _write_yaml(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _load_app_config(monkeypatch, tmp_path: Path, cache_section: str) -> config_module.AppConfig:
    configs_dir = tmp_path / "configs"
    monkeypatch.setattr(config_module, "CONFIGS_DIR", configs_dir)
    monkeypatch.setattr(config_module, "EnvSettings", _StubEnvSettings)
    _write_yaml(
        configs_dir / "env" / "dev.yaml",
        f"""
env: dev
redis:
  host: 127.0.0.1
  port: 6379
{cache_section}
""",
    )
    _write_yaml(configs_dir / "execution" / "execution.yaml", "execution:\n  rate_limit: {}\n")
    _write_yaml(configs_dir / "risk" / "global_risk.yaml", "risk:\n  mode: soft\n")
    _write_yaml(configs_dir / "accounts" / "binance_futures.yaml", "account:\n  name: test\n")
    _write_yaml(configs_dir / "monitoring" / "alerts.yaml", "alerting:\n  enabled: true\n")
    return config_module.load_app_config(env="dev")


def test_live_cache_uses_redis_without_instance_id(monkeypatch, tmp_path: Path) -> None:
    """Verify that live cache uses Redis and shared keys by default."""
    app_config = _load_app_config(
        monkeypatch,
        tmp_path,
        """
cache:
  enabled: true
  database: redis
""",
    )

    settings = build_nautilus_cache_settings(app_config, mode="live")

    assert settings.cache is not None
    assert settings.instance_id is None
    assert settings.cache.database is not None
    assert settings.cache.database.type == "redis"
    assert settings.cache.database.host == "127.0.0.1"
    assert settings.cache.use_instance_id is False
    assert settings.cache.flush_on_start is False


def test_backtest_cache_isolated_without_flush(monkeypatch, tmp_path: Path) -> None:
    """Verify that backtest cache uses isolated instance ID without flushing Redis."""
    app_config = _load_app_config(
        monkeypatch,
        tmp_path,
        """
cache:
  enabled: true
  database: redis
  backtest:
    use_instance_id: true
    flush_on_start: false
    drop_instruments_on_reset: true
""",
    )

    settings = build_nautilus_cache_settings(app_config, mode="backtest")

    assert settings.cache is not None
    assert settings.instance_id is not None
    assert settings.cache.use_instance_id is True
    assert settings.cache.flush_on_start is False
    assert settings.cache.drop_instruments_on_reset is True


def test_disabled_cache_returns_none(monkeypatch, tmp_path: Path) -> None:
    """Verify that disabled cache bypasses Nautilus cache config."""
    app_config = _load_app_config(
        monkeypatch,
        tmp_path,
        """
cache:
  enabled: false
""",
    )

    settings = build_nautilus_cache_settings(app_config, mode="live")

    assert settings.cache is None
    assert settings.instance_id is None
