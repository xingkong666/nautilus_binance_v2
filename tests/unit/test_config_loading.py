"""Tests for test config loading."""

from __future__ import annotations

from pathlib import Path

from src.core import config as config_module


class _StubEnvSettings:
    def __init__(self) -> None:
        self.env = "prod"
        self.binance_api_key = ""
        self.binance_api_secret = ""
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


def test_load_app_config_includes_exchange_live_and_strategies(monkeypatch, tmp_path: Path) -> None:
    """Verify that load app config includes exchange live and strategies.

    Args:
        monkeypatch: Monkeypatch.
        tmp_path: Path for tmp.
    """
    configs_dir = tmp_path / "configs"
    monkeypatch.setattr(config_module, "CONFIGS_DIR", configs_dir)
    monkeypatch.setattr(config_module, "EnvSettings", _StubEnvSettings)

    _write_yaml(
        configs_dir / "env" / "prod.yaml",
        """
env: prod
exchange:
  environment: LIVE
  instrument_ids:
    - BTCUSDT-PERP.BINANCE
execution:
  submit_orders: false
cache:
  enabled: true
  database: redis
  backtest:
    use_instance_id: true
    flush_on_start: false
live:
  strategy_config: configs/strategies/vegas_tunnel.yaml
  symbol: BTCUSDT
  universe_top_n: 200
  exclude_stablecoin_bases: true
strategies:
  portfolio:
    mode: equal
""",
    )
    _write_yaml(configs_dir / "risk" / "global_risk.yaml", "risk:\n  mode: hard\n")
    _write_yaml(
        configs_dir / "execution" / "execution.yaml",
        "execution:\n  rate_limit:\n    max_orders_per_second: 5\n",
    )
    _write_yaml(configs_dir / "accounts" / "binance_futures.yaml", "account:\n  name: test\n")
    _write_yaml(configs_dir / "monitoring" / "alerts.yaml", "alerting:\n  enabled: true\n")

    app_config = config_module.load_app_config(env="prod")

    assert app_config.exchange["environment"] == "LIVE"
    assert app_config.exchange["instrument_ids"] == ["BTCUSDT-PERP.BINANCE"]
    assert app_config.execution.submit_orders is False
    assert app_config.cache.enabled is True
    assert app_config.cache.database == "redis"
    assert app_config.cache.backtest.use_instance_id is True
    assert app_config.cache.backtest.flush_on_start is False
    assert app_config.execution.rate_limit["max_orders_per_second"] == 5
    assert app_config.live.strategy_config == "configs/strategies/vegas_tunnel.yaml"
    assert app_config.live.symbol == "BTCUSDT"
    assert app_config.live.universe_top_n == 200
    assert app_config.live.exclude_stablecoin_bases is True
    assert app_config.strategies["portfolio"]["mode"] == "equal"


def test_load_app_config_env_vars_override_yaml(monkeypatch, tmp_path: Path) -> None:
    """Verify that env vars override YAML for runtime-sensitive config."""
    configs_dir = tmp_path / "configs"
    monkeypatch.setattr(config_module, "CONFIGS_DIR", configs_dir)

    class _EnvOverrideSettings(_StubEnvSettings):
        def __init__(self) -> None:
            super().__init__()
            self.database_url = "postgresql://runtime:secret@db:5432/live"
            self.redis_password = "runtime-pass"
            self.prometheus_port = 9200
            self.live_strategy_config = "configs/strategies/turtle.yaml"
            self.submit_orders = True
            self.exchange_environment = "DEMO"

    monkeypatch.setattr(config_module, "EnvSettings", _EnvOverrideSettings)

    _write_yaml(
        configs_dir / "env" / "prod.yaml",
        """
env: prod
data:
  database_url: postgresql://yaml:yaml@127.0.0.1:5432/yaml
redis:
  host: 127.0.0.1
  port: 6379
  password: yaml-pass
monitoring:
  enabled: true
  prometheus_port: 9100
cache:
  enabled: true
  database: redis
exchange:
  environment: LIVE
execution:
  submit_orders: false
live:
  strategy_config: configs/strategies/vegas_tunnel.yaml
""",
    )
    _write_yaml(configs_dir / "execution" / "execution.yaml", "execution:\n  rate_limit: {}\n")
    _write_yaml(configs_dir / "risk" / "global_risk.yaml", "risk:\n  mode: hard\n")
    _write_yaml(configs_dir / "accounts" / "binance_futures.yaml", "account:\n  name: test\n")
    _write_yaml(configs_dir / "monitoring" / "alerts.yaml", "alerting:\n  enabled: true\n")

    app_config = config_module.load_app_config(env="prod")

    assert app_config.data.database_url == "postgresql://runtime:secret@db:5432/live"
    assert app_config.redis.password == "runtime-pass"
    assert app_config.cache.enabled is True
    assert app_config.cache.database == "redis"
    assert app_config.monitoring.prometheus_port == 9200
    assert app_config.live.strategy_config == "configs/strategies/turtle.yaml"
    assert app_config.execution.submit_orders is True
    assert app_config.exchange["environment"] == "DEMO"
