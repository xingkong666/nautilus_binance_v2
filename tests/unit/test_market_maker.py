"""Market maker hedge-only behavior tests."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from nautilus_trader.model.data import BarType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId, PositionId

from src.core.config import load_yaml
from src.strategy.market_maker import ActiveMarketMaker, InventoryLot, LotStatus, MarketMakerConfig

INSTRUMENT_ID = InstrumentId.from_str("BTCUSDT-PERP.BINANCE")
BAR_TYPE = BarType.from_str("BTCUSDT-PERP.BINANCE-1-MINUTE-LAST-INTERNAL")
ROOT = Path(__file__).resolve().parents[2]


class _DummyQty:
    def __init__(self, value: Decimal) -> None:
        self._value = value

    def as_decimal(self) -> Decimal:
        return self._value


class _DummyInstrument:
    def __init__(self) -> None:
        self.size_increment = Decimal("0.01")
        self.price_increment = Decimal("0.1")

    def make_price(self, value: float) -> float:
        return value

    def make_qty(self, value: Decimal) -> _DummyQty:
        return _DummyQty(value)


class _DummyOrder:
    def __init__(self, client_order_id: ClientOrderId) -> None:
        self.client_order_id = client_order_id


def make_strategy(**overrides: float | int | bool | Decimal | None) -> ActiveMarketMaker:
    """Build a market maker strategy for unit tests.

    Args:
        **overrides: Config overrides.

    Returns:
        ActiveMarketMaker: Strategy instance.
    """
    cfg = MarketMakerConfig(
        instrument_id=INSTRUMENT_ID,
        bar_type=BAR_TYPE,
        **overrides,
    )
    strategy = ActiveMarketMaker(config=cfg)
    strategy.instrument = _DummyInstrument()  # type: ignore[assignment]
    return strategy


def make_lot(
    lot_id: str,
    side: OrderSide,
    entry_price: float,
    qty: str,
    position_id: PositionId | None = None,
) -> InventoryLot:
    """Build an inventory lot for unit tests.

    Args:
        lot_id: Stable lot identifier.
        side: Entry side.
        entry_price: Fill price.
        qty: Remaining quantity.
        position_id: Optional bound position id.

    Returns:
        InventoryLot: Result.
    """
    quantity = Decimal(qty)
    return InventoryLot(
        lot_id=lot_id,
        quote_order_id=ClientOrderId(f"quote-{lot_id}"),
        side=side,
        entry_price=entry_price,
        filled_qty=quantity,
        remaining_qty=quantity,
        position_id=position_id,
    )


def test_inventory_snapshot_aggregates_long_and_short_lots() -> None:
    """Inventory snapshot should aggregate long/short lots independently."""
    strategy = make_strategy(max_position_usd=1000.0)
    strategy._inventory_lots = {
        "long": make_lot("long", OrderSide.BUY, 100.0, "2.0"),
        "short": make_lot("short", OrderSide.SELL, 50.0, "4.0"),
        "closed": InventoryLot(
            lot_id="closed",
            quote_order_id=ClientOrderId("quote-closed"),
            side=OrderSide.BUY,
            entry_price=10.0,
            filled_qty=Decimal("1.0"),
            remaining_qty=Decimal("0"),
            status=LotStatus.CLOSED,
        ),
    }

    snapshot = strategy._inventory_snapshot()

    assert snapshot["long_usd"] == 200.0
    assert snapshot["short_usd"] == 200.0
    assert snapshot["gross_usd"] == 400.0
    assert snapshot["long_qty"] == 2.0
    assert snapshot["short_qty"] == 4.0
    assert snapshot["gross_ratio"] == 0.4
    assert snapshot["imbalance"] == 0.0


def test_calc_quote_sizes_blocks_bid_when_long_side_is_overweight() -> None:
    """Long-heavy inventory should stop adding more bid-side exposure."""
    strategy = make_strategy(max_position_usd=1000.0, one_side_only_limit=0.85)
    strategy._inventory_lots = {
        "long": make_lot("long", OrderSide.BUY, 100.0, "9.0"),
    }
    strategy._last_dir_val = 0.0

    bid_qty, ask_qty = strategy._calc_quote_sizes(Decimal("1.00"))

    assert bid_qty == Decimal("0")
    assert ask_qty > Decimal("0")


def test_calc_quote_sizes_blocks_only_adverse_side() -> None:
    """Adverse-side gating should zero only the requested side."""
    strategy = make_strategy()
    strategy._inventory_lots = {}
    strategy._last_dir_val = 0.0

    bid_qty, ask_qty = strategy._calc_quote_sizes(Decimal("1.00"), adverse_side="BUY")
    assert bid_qty == Decimal("0")
    assert ask_qty == Decimal("1.0")

    bid_qty, ask_qty = strategy._calc_quote_sizes(Decimal("1.00"), adverse_side="SELL")
    assert bid_qty == Decimal("1.0")
    assert ask_qty == Decimal("0")


def test_submit_quote_binds_side_specific_position_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Quote orders should bind LONG/SHORT position ids by side."""
    strategy = make_strategy(post_only=True)
    submitted: list[tuple[ClientOrderId, PositionId | None]] = []

    def limit_factory(**kwargs: object) -> _DummyOrder:
        side = kwargs["order_side"]
        suffix = "bid" if side == OrderSide.BUY else "ask"
        return _DummyOrder(ClientOrderId(f"quote-{suffix}"))

    monkeypatch.setattr(
        ActiveMarketMaker,
        "order_factory",
        property(lambda self: SimpleNamespace(limit=limit_factory)),
    )
    monkeypatch.setattr(
        ActiveMarketMaker,
        "submit_order",
        lambda self, order, position_id=None: submitted.append((order.client_order_id, position_id)),
    )

    bid_id = strategy._submit_quote(OrderSide.BUY, price=100.0, qty=Decimal("0.10"))
    ask_id = strategy._submit_quote(OrderSide.SELL, price=101.0, qty=Decimal("0.10"))

    assert bid_id == ClientOrderId("quote-bid")
    assert ask_id == ClientOrderId("quote-ask")
    assert submitted == [
        (ClientOrderId("quote-bid"), PositionId(f"{INSTRUMENT_ID}-LONG")),
        (ClientOrderId("quote-ask"), PositionId(f"{INSTRUMENT_ID}-SHORT")),
    ]


def test_resolve_lot_position_id_prefers_matching_side_position(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lot protection should resolve the matching hedge-side position id."""
    strategy = make_strategy()
    monkeypatch.setattr(
        ActiveMarketMaker,
        "cache",
        property(
            lambda self: SimpleNamespace(
                positions_open=lambda instrument_id=None: [
                    SimpleNamespace(instrument_id=INSTRUMENT_ID, is_long=False, id=PositionId(f"{INSTRUMENT_ID}-SHORT")),
                    SimpleNamespace(instrument_id=INSTRUMENT_ID, is_long=True, id=PositionId(f"{INSTRUMENT_ID}-LONG")),
                ]
            )
        ),
    )
    lot = make_lot("long", OrderSide.BUY, 100.0, "0.50")

    position_id = strategy._resolve_lot_position_id(lot)

    assert position_id == PositionId(f"{INSTRUMENT_ID}-LONG")
    assert lot.position_id == PositionId(f"{INSTRUMENT_ID}-LONG")


def test_place_reduce_order_requires_and_binds_position_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reduce orders should bind the resolved hedge-side position id."""
    strategy = make_strategy(reduce_post_only=False)
    recorded: list[tuple[ClientOrderId, PositionId | None]] = []
    strategy._reduce_to_lot.clear()

    monkeypatch.setattr(
        ActiveMarketMaker,
        "order_factory",
        property(lambda self: SimpleNamespace(limit=lambda **kwargs: _DummyOrder(ClientOrderId("reduce-1")))),
    )
    monkeypatch.setattr(
        ActiveMarketMaker,
        "submit_order",
        lambda self, order, position_id=None: recorded.append((order.client_order_id, position_id)),
    )
    monkeypatch.setattr(
        ActiveMarketMaker,
        "_resolve_lot_position_id",
        lambda self, lot: PositionId(f"{INSTRUMENT_ID}-LONG"),
    )
    monkeypatch.setattr(ActiveMarketMaker, "_utc_now", lambda self: None)

    lot = make_lot("long", OrderSide.BUY, 100.0, "0.50")
    reduce_id = strategy._place_reduce_order(lot)

    assert reduce_id == ClientOrderId("reduce-1")
    assert recorded == [(ClientOrderId("reduce-1"), PositionId(f"{INSTRUMENT_ID}-LONG"))]
    assert lot.reduce_order_id == ClientOrderId("reduce-1")
    assert lot.status == LotStatus.PENDING_PROTECT
    assert strategy._reduce_to_lot[ClientOrderId("reduce-1")] == "long"


def test_market_maker_configs_are_hedge_only() -> None:
    """Strategy and account configs should align on hedge-only behavior."""
    strategy_cfg = load_yaml(ROOT / "configs/strategies/market_maker.yaml")
    account_cfg = load_yaml(ROOT / "configs/accounts/binance_futures.yaml")
    strategy_text = (ROOT / "configs/strategies/market_maker.yaml").read_text()

    assert strategy_cfg["strategy"]["class_path"] == "strategy.market_maker:ActiveMarketMaker"
    forbidden_netting_label = "单" + "向" + "净" + "仓"
    assert forbidden_netting_label not in strategy_text
    assert account_cfg["account"]["oms_type"] == "HEDGING"
