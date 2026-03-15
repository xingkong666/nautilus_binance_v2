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
        self.prometheus_port = 9090


def _write_yaml(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_load_app_config_includes_exchange_live_and_strategies(monkeypatch, tmp_path: Path) -> None:
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
live:
  strategy_config: configs/strategies/vegas_tunnel.yaml
  symbol: BTCUSDT
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
    assert app_config.live.strategy_config == "configs/strategies/vegas_tunnel.yaml"
    assert app_config.live.symbol == "BTCUSDT"
    assert app_config.strategies["portfolio"]["mode"] == "equal"
