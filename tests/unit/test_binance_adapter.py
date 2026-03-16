"""Tests for test binance adapter."""

from __future__ import annotations

from types import SimpleNamespace

from src.exchange import binance_adapter as adapter_module


class _FakeTrader:
    def __init__(self) -> None:
        self.strategies: list[object] = []

    def add_strategy(self, strategy: object) -> None:
        self.strategies.append(strategy)


class _FakeTradingNode:
    def __init__(self, config) -> None:
        self.config = config
        self.trader = _FakeTrader()
        self.data_factories: list[tuple[str, object]] = []
        self.exec_factories: list[tuple[str, object]] = []
        self.build_called = False

    def add_data_client_factory(self, venue: str, factory: object) -> None:
        self.data_factories.append((venue, factory))

    def add_exec_client_factory(self, venue: str, factory: object) -> None:
        self.exec_factories.append((venue, factory))

    def build(self) -> None:
        self.build_called = True


def test_register_strategy_updates_instrument_ids_and_builds_node(monkeypatch) -> None:
    """Verify that register strategy updates instrument IDs and builds node.

    Args:
        monkeypatch: Monkeypatch.
    """
    monkeypatch.setattr(adapter_module, "TradingNode", _FakeTradingNode)

    adapter = adapter_module.BinanceAdapter(adapter_module.BinanceAdapterConfig())
    fake_strategy = SimpleNamespace(
        config=SimpleNamespace(instrument_id="BTCUSDT-PERP.BINANCE"),
    )

    adapter.register_strategy(fake_strategy)
    node = adapter.build_node()

    assert adapter.config.instrument_ids == ["BTCUSDT-PERP.BINANCE"]
    assert node.trader.strategies == [fake_strategy]
    assert node.build_called is True


def test_register_strategy_after_build_raises(monkeypatch) -> None:
    """Verify that register strategy after build raises.

    Args:
        monkeypatch: Monkeypatch.
    """
    monkeypatch.setattr(adapter_module, "TradingNode", _FakeTradingNode)

    adapter = adapter_module.BinanceAdapter(adapter_module.BinanceAdapterConfig())
    adapter.build_node()

    try:
        adapter.register_strategy(SimpleNamespace(config=SimpleNamespace(instrument_id="BTCUSDT-PERP.BINANCE")))
    except RuntimeError as exc:
        assert "after TradingNode is built" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


class _FakeAccountAPI:
    async def query_futures_account_info(self, recv_window: str | None = None):
        return SimpleNamespace(
            assets=[
                SimpleNamespace(
                    asset="USDT",
                    walletBalance="1000",
                    availableBalance="900",
                    unrealizedProfit="12.5",
                ),
            ]
        )

    async def query_futures_position_risk(self, recv_window: str | None = None):
        return [
            SimpleNamespace(
                symbol="BTCUSDT",
                positionSide=SimpleNamespace(value="LONG"),
                positionAmt="0.01",
                entryPrice="50000",
                unRealizedProfit="15.5",
                leverage="5",
            ),
            SimpleNamespace(
                symbol="ETHUSDT",
                positionSide=SimpleNamespace(value="BOTH"),
                positionAmt="0",
                entryPrice="3000",
                unRealizedProfit="0",
                leverage="3",
            ),
        ]

    async def query_open_orders(self, symbol: str | None = None, recv_window: str | None = None):
        return [
            SimpleNamespace(
                symbol="BTCUSDT",
                clientOrderId="external-1",
                orderId=123456,
                status=SimpleNamespace(value="NEW"),
                side=SimpleNamespace(value="BUY"),
                type=SimpleNamespace(value="LIMIT"),
                positionSide="LONG",
                reduceOnly=False,
            )
        ]


def test_fetch_account_snapshot_sync_uses_account_api(monkeypatch) -> None:
    """Verify that fetch account snapshot sync uses account API.

    Args:
        monkeypatch: Monkeypatch.
    """
    adapter = adapter_module.BinanceAdapter(adapter_module.BinanceAdapterConfig(api_key="key", api_secret="secret"))
    monkeypatch.setattr(adapter, "_build_account_http_api", lambda: _FakeAccountAPI())

    balances, positions = adapter.fetch_account_snapshot()

    assert balances == [
        {
            "asset": "USDT",
            "walletBalance": "1000",
            "availableBalance": "900",
            "unrealizedProfit": "12.5",
        }
    ]
    assert positions == [
        {
            "symbol": "BTCUSDT",
            "positionSide": "LONG",
            "positionAmt": "0.01",
            "entryPrice": "50000",
            "unrealizedProfit": "15.5",
            "leverage": "5",
        }
    ]


def test_fetch_balance_and_positions_wrap_snapshot(monkeypatch) -> None:
    """Verify that fetch balance and positions wrap snapshot.

    Args:
        monkeypatch: Monkeypatch.
    """
    adapter = adapter_module.BinanceAdapter(adapter_module.BinanceAdapterConfig())
    monkeypatch.setattr(
        adapter,
        "fetch_account_snapshot",
        lambda: ([{"asset": "USDT"}], [{"symbol": "BTCUSDT"}]),
    )

    assert adapter.fetch_balance() == [{"asset": "USDT"}]
    assert adapter.fetch_positions() == [{"symbol": "BTCUSDT"}]


def test_fetch_open_orders_uses_account_api(monkeypatch) -> None:
    """Verify that fetch open orders uses account API.

    Args:
        monkeypatch: Monkeypatch.
    """
    adapter = adapter_module.BinanceAdapter(adapter_module.BinanceAdapterConfig(api_key="key", api_secret="secret"))
    monkeypatch.setattr(adapter, "_build_account_http_api", lambda: _FakeAccountAPI())

    open_orders = adapter.fetch_open_orders()

    assert open_orders == [
        {
            "symbol": "BTCUSDT",
            "clientOrderId": "external-1",
            "orderId": "123456",
            "status": "NEW",
            "side": "BUY",
            "type": "LIMIT",
            "positionSide": "LONG",
            "reduceOnly": "False",
        }
    ]


def test_resolve_api_credentials_fall_back_to_env_settings(monkeypatch) -> None:
    """Verify that resolve API credentials fall back to env settings.

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

    monkeypatch.setattr(adapter_module, "EnvSettings", _StubEnvSettings)

    adapter = adapter_module.BinanceAdapter(adapter_module.BinanceAdapterConfig())

    assert adapter._resolve_api_key() == "prod-key"
    assert adapter._resolve_api_secret() == "prod-secret"


def test_prepare_runtime_config_disables_reduce_only_in_hedge_mode(monkeypatch) -> None:
    """Verify that prepare runtime config disables reduce only in hedge mode.

    Args:
        monkeypatch: Monkeypatch.
    """
    adapter = adapter_module.BinanceAdapter(adapter_module.BinanceAdapterConfig(use_reduce_only=True))
    monkeypatch.setattr(adapter, "query_hedge_mode", lambda: True)

    adapter.prepare_runtime_config()

    assert adapter.config.use_reduce_only is False


def test_prepare_runtime_config_keeps_reduce_only_in_one_way_mode(monkeypatch) -> None:
    """Verify that prepare runtime config keeps reduce only in one way mode.

    Args:
        monkeypatch: Monkeypatch.
    """
    adapter = adapter_module.BinanceAdapter(adapter_module.BinanceAdapterConfig(use_reduce_only=True))
    monkeypatch.setattr(adapter, "query_hedge_mode", lambda: False)

    adapter.prepare_runtime_config()

    assert adapter.config.use_reduce_only is True
