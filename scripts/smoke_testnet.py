"""Binance Testnet 冒烟测试脚本.

流程：
1. 加载 .env 环境变量
2. 用 BinanceEnvironment.TESTNET 构建 TradingNode
3. 注入一个 SmokeStrategy（NautilusTrader Strategy 子类）
4. SmokeStrategy.on_start() 内：订阅 BTCUSDT QuoteTick
5. SmokeStrategy.on_quote_tick() 内：收到首个 tick → 挂市价单 → 等成交
6. 成交后打印结果，调用 node.stop()

用法::

    cd /root/workSpace/nautilus_binance_v2
    source .venv/bin/activate
    python scripts/smoke_testnet.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# ── 路径 & 环境变量 ──────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

env_file = ROOT / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
    print(f"✅ 已加载 .env: {env_file}")

# ── NautilusTrader imports ───────────────────────────────────────────────────
from nautilus_trader.adapters.binance.common.enums import BinanceEnvironment
from nautilus_trader.adapters.binance.config import (
    BinanceAccountType,
    BinanceDataClientConfig,
    BinanceExecClientConfig,
    BinanceInstrumentProviderConfig,
)
from nautilus_trader.adapters.binance.factories import (
    BinanceLiveDataClientFactory,
    BinanceLiveExecClientFactory,
)
from nautilus_trader.config import (
    LiveDataEngineConfig,
    LiveExecEngineConfig,
    LiveRiskEngineConfig,
    LoggingConfig,
    StrategyConfig,
    TradingNodeConfig,
)
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId, ClientOrderId
from nautilus_trader.model.objects import Quantity
from nautilus_trader.trading.strategy import Strategy

# ── 配置 ─────────────────────────────────────────────────────────────────────
SYMBOL = "BTCUSDT-PERP.BINANCE"
ORDER_QTY = "0.002"   # BTC 约 68k，0.002 × 68k = 136 USDT > 最小名义价值 100 USDT


# ── 冒烟策略 ──────────────────────────────────────────────────────────────────

class SmokeConfig(StrategyConfig, frozen=True):
    instrument_id: str = SYMBOL
    order_qty: str = ORDER_QTY


class SmokeStrategy(Strategy):
    """最小冒烟策略：收到首个 tick 后下市价单，成交后停止节点."""

    def __init__(self, config: SmokeConfig) -> None:
        super().__init__(config)
        self.instrument_id = InstrumentId.from_str(config.instrument_id)
        self.order_qty = config.order_qty
        self._order_submitted = False
        self._done = False

    def on_start(self) -> None:
        """订阅合约行情."""
        instrument = self.cache.instrument(self.instrument_id)
        if instrument is None:
            self.log.error(f"合约 {self.instrument_id} 未找到，策略退出")
            self.stop()
            return

        self.log.info(f"✅ 合约已加载: {instrument.id}")
        self.log.info(f"   最小下单量: {instrument.size_increment}")
        self.log.info(f"   价格精度:   {instrument.price_increment}")
        self.subscribe_quote_ticks(self.instrument_id)
        self.log.info("⏳ 等待首个行情 tick ...")

    def on_quote_tick(self, tick: QuoteTick) -> None:
        """收到首个 tick 后下单，之后忽略."""
        if self._order_submitted:
            return

        self._order_submitted = True
        self.log.info(f"✅ 首个 Tick: bid={tick.bid_price}  ask={tick.ask_price}")
        self.log.info(f"📤 提交市价买单 {self.order_qty} BTC ...")

        order = self.order_factory.market(
            instrument_id=self.instrument_id,
            order_side=OrderSide.BUY,
            quantity=Quantity.from_str(self.order_qty),
            time_in_force=TimeInForce.GTC,
            reduce_only=False,
        )
        self.submit_order(order)
        self.log.info(f"   订单已提交: {order.client_order_id}")

    def on_order_filled(self, event) -> None:
        """成交事件处理."""
        if self._done:
            return
        self._done = True

        self.log.info("=" * 50)
        self.log.info("🎉 成交成功！")
        self.log.info(f"   Client Order ID : {event.client_order_id}")
        self.log.info(f"   Venue Order ID  : {event.venue_order_id}")
        self.log.info(f"   成交均价        : {event.last_px}")
        self.log.info(f"   成交量          : {event.last_qty}")
        self.log.info(f"   成交时间 (ns)   : {event.ts_event}")
        self.log.info("=" * 50)
        self.log.info("🛑 冒烟完成，2s 后停止节点 ...")

        # 用 asyncio 调度停止，避免在事件回调里直接调用阻塞操作
        loop = asyncio.get_event_loop()
        loop.call_later(2.0, loop.stop)


# ── 构建节点 ──────────────────────────────────────────────────────────────────

def build_node() -> TradingNode:
    api_key = os.environ.get("BINANCE_TESTNET_API_KEY")
    api_secret = os.environ.get("BINANCE_TESTNET_API_SECRET")

    instrument_provider = BinanceInstrumentProviderConfig(
        load_ids=frozenset([SYMBOL]),
    )

    data_cfg = BinanceDataClientConfig(
        api_key=api_key,
        api_secret=api_secret,
        account_type=BinanceAccountType.USDT_FUTURES,
        environment=BinanceEnvironment.TESTNET,
        instrument_provider=instrument_provider,
    )

    exec_cfg = BinanceExecClientConfig(
        api_key=api_key,
        api_secret=api_secret,
        account_type=BinanceAccountType.USDT_FUTURES,
        environment=BinanceEnvironment.TESTNET,
        use_reduce_only=True,
        use_position_ids=True,
        instrument_provider=instrument_provider,
    )

    node_config = TradingNodeConfig(
        trader_id="SMOKE-TESTNET-001",
        data_clients={"BINANCE": data_cfg},
        exec_clients={"BINANCE": exec_cfg},
        data_engine=LiveDataEngineConfig(time_bars_timestamp_on_close=True),
        exec_engine=LiveExecEngineConfig(
            reconciliation=True,
            reconciliation_lookback_mins=60,
        ),
        risk_engine=LiveRiskEngineConfig(
            bypass=False,
            max_order_submit_rate="100/00:00:01",
            max_order_modify_rate="100/00:00:01",
        ),
        logging=LoggingConfig(log_level="INFO"),
    )

    node = TradingNode(config=node_config)
    node.add_data_client_factory("BINANCE", BinanceLiveDataClientFactory)
    node.add_exec_client_factory("BINANCE", BinanceLiveExecClientFactory)

    # 手动注册策略（避免 ImportableStrategyConfig 路径问题）
    strategy = SmokeStrategy(config=SmokeConfig(
        strategy_id="SMOKE-001",
        instrument_id=SYMBOL,
        order_qty=ORDER_QTY,
    ))
    node.trader.add_strategy(strategy)
    node.build()
    return node


# ── 主入口 ────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 60)
    print("🚀 Binance Futures Testnet 冒烟测试 v2")
    print(f"   合约: {SYMBOL}")
    print(f"   环境: TESTNET  (testnet.binancefuture.com)")
    print(f"   下单量: {ORDER_QTY} BTC")
    print("=" * 60)

    node = build_node()
    try:
        node.run()   # 阻塞直到 loop.stop() 被调用
    except KeyboardInterrupt:
        print("\n⚠️  用户中断")
    finally:
        try:
            node.stop()
            node.dispose()
        except Exception as e:
            print(f"停止时报错（可忽略）: {e}")

    print("\n" + "=" * 60)
    print("冒烟测试结束")
    print("=" * 60)


if __name__ == "__main__":
    main()
