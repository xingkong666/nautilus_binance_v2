from __future__ import annotations

from pathlib import Path

from src.core.config import AppConfig, LiveConfig
from src.live import readiness as readiness_module
from src.live.readiness import (
    credential_checks,
    required_credential_env_names,
    resolve_live_symbol,
    resolve_strategy_config_path,
)


def test_required_credential_env_names_for_prod_live() -> None:
    config = AppConfig(env="prod", exchange={"environment": "LIVE"})
    assert required_credential_env_names(config) == (
        "BINANCE_API_KEY",
        "BINANCE_API_SECRET",
    )


def test_required_credential_env_names_for_testnet_default() -> None:
    config = AppConfig(env="dev")
    assert required_credential_env_names(config) == (
        "BINANCE_TESTNET_API_KEY",
        "BINANCE_TESTNET_API_SECRET",
    )


def test_resolve_strategy_config_path_prefers_override(tmp_path: Path) -> None:
    strategy_path = tmp_path / "strategy.yaml"
    strategy_path.write_text("strategy: {}\n", encoding="utf-8")
    config = AppConfig(live=LiveConfig(strategy_config="configs/strategies/ema_cross.yaml"))

    resolved = resolve_strategy_config_path(config, override=str(strategy_path), cwd=tmp_path)

    assert resolved == strategy_path.resolve()


def test_resolve_live_symbol_prefers_override() -> None:
    config = AppConfig(live=LiveConfig(symbol="BTCUSDT"))
    assert resolve_live_symbol(config, override="ETHUSDT") == "ETHUSDT"


def test_credential_checks_report_missing_values(monkeypatch) -> None:
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)

    class _EmptyEnvSettings:
        binance_api_key = ""
        binance_api_secret = ""
        binance_testnet_api_key = ""
        binance_testnet_api_secret = ""
        binance_demo_api_key = ""
        binance_demo_api_secret = ""

    monkeypatch.setattr(readiness_module, "EnvSettings", _EmptyEnvSettings)
    config = AppConfig(env="prod", exchange={"environment": "LIVE"})

    checks = credential_checks(config)

    assert [check.passed for check in checks] == [False, False]


def test_credential_checks_read_from_env_settings(monkeypatch) -> None:
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)

    class _StubEnvSettings:
        binance_api_key = "prod-key"
        binance_api_secret = "prod-secret"
        binance_testnet_api_key = ""
        binance_testnet_api_secret = ""
        binance_demo_api_key = ""
        binance_demo_api_secret = ""

    monkeypatch.setattr(readiness_module, "EnvSettings", _StubEnvSettings)
    config = AppConfig(env="prod", exchange={"environment": "LIVE"})

    checks = credential_checks(config)

    assert [check.passed for check in checks] == [True, True]
