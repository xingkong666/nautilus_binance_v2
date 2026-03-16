"""Tests for test live readiness."""

from __future__ import annotations

from pathlib import Path

from src.core.config import AppConfig, LiveConfig
from src.live import readiness as readiness_module
from src.live.readiness import (
    credential_checks,
    required_credential_env_names,
    resolve_live_symbol,
    resolve_live_symbols,
    resolve_strategy_config_path,
)


def test_required_credential_env_names_for_prod_live() -> None:
    """Verify that required credential env names for prod live."""
    config = AppConfig(env="prod", exchange={"environment": "LIVE"})
    assert required_credential_env_names(config) == (
        "BINANCE_API_KEY",
        "BINANCE_API_SECRET",
    )


def test_required_credential_env_names_for_testnet_default() -> None:
    """Verify that required credential env names for testnet default."""
    config = AppConfig(env="dev")
    assert required_credential_env_names(config) == (
        "BINANCE_TESTNET_API_KEY",
        "BINANCE_TESTNET_API_SECRET",
    )


def test_resolve_strategy_config_path_prefers_override(tmp_path: Path) -> None:
    """Verify that resolve strategy config path prefers override.

    Args:
        tmp_path: Path for tmp.
    """
    strategy_path = tmp_path / "strategy.yaml"
    strategy_path.write_text("strategy: {}\n", encoding="utf-8")
    config = AppConfig(live=LiveConfig(strategy_config="configs/strategies/ema_cross.yaml"))

    resolved = resolve_strategy_config_path(config, override=str(strategy_path), cwd=tmp_path)

    assert resolved == strategy_path.resolve()


def test_resolve_live_symbol_prefers_override() -> None:
    """Verify that resolve live symbol prefers override."""
    config = AppConfig(live=LiveConfig(symbol="BTCUSDT"))
    assert resolve_live_symbol(config, override="ETHUSDT") == "ETHUSDT"


def test_resolve_live_symbols_prefers_symbols_override() -> None:
    """Verify that resolve live symbols prefers symbols override."""
    config = AppConfig(live=LiveConfig(symbol="BTCUSDT", symbols=["ETHUSDT"]))

    resolved = resolve_live_symbols(config, symbols_override=["SOLUSDT", "ETHUSDT", "SOLUSDT"])

    assert resolved == ["SOLUSDT", "ETHUSDT"]


def test_resolve_live_symbols_uses_configured_symbols_before_single_symbol() -> None:
    """Verify that resolve live symbols uses configured symbols before single symbol."""
    config = AppConfig(live=LiveConfig(symbol="BTCUSDT", symbols=["ETHUSDT", "SOLUSDT"]))

    resolved = resolve_live_symbols(config)

    assert resolved == ["ETHUSDT", "SOLUSDT"]


def test_resolve_live_symbols_falls_back_to_ranked_universe_and_excludes_stablecoins(tmp_path: Path) -> None:
    """Verify that resolve live symbols falls back to ranked universe and excludes stablecoins.

    Args:
        tmp_path: Path for tmp.
    """
    instruments_path = tmp_path / "instruments.yaml"
    instruments_path.write_text(
        """
instruments:
  BTCUSDT:
    provider: btcusdt_perp_binance
    market_cap_rank: 1
  USDCUSDT:
    provider: usdcusdt_perp_binance
    market_cap_rank: 2
  ETHUSDT:
    provider: ethusdt_perp_binance
    market_cap_rank: 3
  SOLUSDT:
    provider: solusdt_perp_binance
    market_cap_rank: 4
""",
        encoding="utf-8",
    )
    config = AppConfig(
        live=LiveConfig(
            universe_top_n=3,
            exclude_stablecoin_bases=True,
        )
    )

    resolved = resolve_live_symbols(config, instruments_config_path=instruments_path)

    assert resolved == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


def test_credential_checks_report_missing_values(monkeypatch) -> None:
    """Verify that credential checks report missing values.

    Args:
        monkeypatch: Monkeypatch.
    """
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
    """Verify that credential checks read from env settings.

    Args:
        monkeypatch: Monkeypatch.
    """
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
