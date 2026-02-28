"""Binance 合约交易所适配器.

封装 NautilusTrader 的 Binance Futures 客户端，提供统一的交易所接入层。
负责：
- TradingNode 的配置和生命周期管理
- DataClient 和 ExecutionClient 的工厂注册
- 对外暴露简洁的 start / stop 接口

架构位置:
    Strategy → OrderRouter → BinanceAdapter → NautilusTrader BinanceFutures{Data,Exec}Client

使用示例::

    from src.exchange.binance_adapter import BinanceAdapter, BinanceAdapterConfig

    # 使用 BinanceEnvironment 枚举（推荐，1.223.0+）
    cfg = BinanceAdapterConfig(
        api_key="YOUR_KEY",
        api_secret="YOUR_SECRET",
        environment=BinanceEnvironment.TESTNET,
    )
    adapter = BinanceAdapter(cfg)
    await adapter.start()
    node = adapter.node  # TradingNode 实例
    await adapter.stop()
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import Any

import structlog
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
    TradingNodeConfig,
)
from nautilus_trader.live.node import TradingNode

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class BinanceAdapterConfig:
    """Binance 适配器配置.

    Attributes:
        api_key: Binance API 公钥。为 None 时从环境变量自动读取
            （LIVE: BINANCE_API_KEY，TESTNET: BINANCE_TESTNET_API_KEY，
            DEMO: BINANCE_DEMO_API_KEY）。
        api_secret: Binance API 私钥。规则同上。
        environment: 交易所环境，BinanceEnvironment.LIVE / TESTNET / DEMO。
            默认 LIVE（正式环境）。推荐使用此字段替代已弃用的 testnet。
        account_type: 账户类型，默认 USDT_FUTURES（U 本位合约）。
        base_url_http: HTTP base URL 覆盖，None 则使用官方地址。
        base_url_ws: WebSocket base URL 覆盖，None 则使用官方地址。
        proxy_url: HTTP 代理 URL，格式 "http://host:port"，无代理传 None。
        futures_leverages: 各合约初始杠杆，格式 {"BTCUSDT": 10}。
        use_reduce_only: 是否在下单时附带 reduce_only 参数，默认 True。
        use_position_ids: 是否使用 Binance Futures hedging position ID，默认 True。
        recv_window_ms: HTTP 请求接收窗口（毫秒），默认 5000。
        max_retries: 提交/取消/改单最大重试次数，None 表示不重试。
        retry_delay_initial_ms: 首次重试延迟（毫秒）。
        retry_delay_max_ms: 重试最大延迟（毫秒）。
        update_instruments_interval_mins: 合约信息刷新间隔（分钟），默认 60。
        instrument_ids: 要订阅的合约 ID 列表，格式 ["BTCUSDT-PERP.BINANCE"]。
            若为空列表则不预加载，由策略按需订阅。
        load_all_instruments: 是否预加载所有合约，默认 False（按需加载）。
        data_engine: DataEngine 配置覆盖。
        exec_engine: ExecEngine 配置覆盖。
        risk_engine: RiskEngine 配置覆盖。
        logging: Nautilus logging 配置覆盖。
    """

    api_key: str | None = None
    api_secret: str | None = None
    environment: BinanceEnvironment = BinanceEnvironment.LIVE
    account_type: BinanceAccountType = BinanceAccountType.USDT_FUTURES
    base_url_http: str | None = None
    base_url_ws: str | None = None
    proxy_url: str | None = None
    futures_leverages: dict[str, int] | None = None
    use_reduce_only: bool = True
    use_position_ids: bool = True
    recv_window_ms: int = 5_000
    max_retries: int | None = None
    retry_delay_initial_ms: int | None = None
    retry_delay_max_ms: int | None = None
    update_instruments_interval_mins: int = 60
    instrument_ids: list[str] = field(default_factory=list)
    load_all_instruments: bool = False
    data_engine: dict[str, Any] = field(default_factory=dict)
    exec_engine: dict[str, Any] = field(default_factory=dict)
    risk_engine: dict[str, Any] = field(default_factory=dict)
    logging: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class BinanceAdapter:
    """Binance 合约交易所适配器.

    封装 NautilusTrader TradingNode 的初始化、配置注入和生命周期管理。
    外部模块（Container / LiveSupervisor）通过此类与 Binance 交互。

    Attributes:
        config: 适配器配置。
        node: 已构建的 TradingNode 实例（start 后可用）。

    Example::

        cfg = BinanceAdapterConfig(environment=BinanceEnvironment.TESTNET)
        adapter = BinanceAdapter(cfg)
        await adapter.start()
        node = adapter.node
        await adapter.stop()
    """

    def __init__(self, config: BinanceAdapterConfig) -> None:
        """初始化适配器，不创建网络连接。

        Args:
            config: 适配器配置，见 BinanceAdapterConfig。
        """
        self.config = config
        self._node: TradingNode | None = None
        self._started = False

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def node(self) -> TradingNode:
        """已启动的 TradingNode 实例.

        Raises:
            RuntimeError: 若 start() 尚未调用或未成功完成。
        """
        if self._node is None:
            raise RuntimeError("BinanceAdapter not started. Call await adapter.start() first.")
        return self._node

    @property
    def is_started(self) -> bool:
        """是否已成功启动."""
        return self._started

    @property
    def is_testnet(self) -> bool:
        """是否连接 Testnet 环境（兼容旧代码的便捷属性）."""
        return self.config.environment == BinanceEnvironment.TESTNET

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """构建并启动 TradingNode，连接 Binance 数据和执行通道.

        会依次执行：
        1. 构建 TradingNodeConfig（注入 data/exec client 配置）
        2. 注册 BinanceLiveDataClientFactory + BinanceLiveExecClientFactory
        3. node.build() → node.run_async()

        Raises:
            RuntimeError: 若已处于启动状态。
            Exception: NautilusTrader 内部连接失败时透传。
        """
        if self._started:
            raise RuntimeError("BinanceAdapter is already started.")

        logger.info(
            "binance_adapter_starting",
            environment=self.config.environment.value,
            account_type=self.config.account_type.value,
        )

        node_config = self._build_node_config()
        self._node = TradingNode(config=node_config)

        # 注册工厂
        self._node.add_data_client_factory("BINANCE", BinanceLiveDataClientFactory)
        self._node.add_exec_client_factory("BINANCE", BinanceLiveExecClientFactory)

        self._node.build()
        logger.info("binance_adapter_node_built")

        await self._node.run_async()

        self._started = True
        logger.info("binance_adapter_started")

    async def stop(self) -> None:
        """优雅停止 TradingNode，断开所有连接并清理资源.

        Raises:
            RuntimeError: 若 start() 尚未调用。
        """
        if self._node is None:
            raise RuntimeError("BinanceAdapter not started.")

        logger.info("binance_adapter_stopping")
        try:
            self._node.stop()
            await asyncio.sleep(0.5)  # 留给 NT 内部清理时间
            self._node.dispose()
        except Exception as exc:
            logger.warning("binance_adapter_stop_error", error=str(exc))
        finally:
            self._started = False
            logger.info("binance_adapter_stopped")

    # ------------------------------------------------------------------
    # 内部：解析凭证
    # ------------------------------------------------------------------

    def _resolve_api_key(self) -> str | None:
        """解析 API Key，优先 config，其次按环境读取标准环境变量.

        1.223.0 起 Binance 客户端统一使用 BINANCE_API_KEY /
        BINANCE_TESTNET_API_KEY / BINANCE_DEMO_API_KEY。

        Returns:
            API Key 字符串，若未配置返回 None（由 NT 客户端自行读取）。
        """
        if self.config.api_key:
            return self.config.api_key
        env_map = {
            BinanceEnvironment.LIVE: "BINANCE_API_KEY",
            BinanceEnvironment.TESTNET: "BINANCE_TESTNET_API_KEY",
            BinanceEnvironment.DEMO: "BINANCE_DEMO_API_KEY",
        }
        return os.environ.get(env_map[self.config.environment])

    def _resolve_api_secret(self) -> str | None:
        """解析 API Secret，优先 config，其次按环境读取标准环境变量.

        Returns:
            API Secret 字符串，若未配置返回 None（由 NT 客户端自行读取）。
        """
        if self.config.api_secret:
            return self.config.api_secret
        env_map = {
            BinanceEnvironment.LIVE: "BINANCE_API_SECRET",
            BinanceEnvironment.TESTNET: "BINANCE_TESTNET_API_SECRET",
            BinanceEnvironment.DEMO: "BINANCE_DEMO_API_SECRET",
        }
        return os.environ.get(env_map[self.config.environment])

    # ------------------------------------------------------------------
    # 内部：构建配置
    # ------------------------------------------------------------------

    def _build_instrument_provider_config(self) -> BinanceInstrumentProviderConfig:
        """构建合约信息提供者配置.

        Returns:
            BinanceInstrumentProviderConfig 实例。
        """
        return BinanceInstrumentProviderConfig(
            load_all=self.config.load_all_instruments,
            load_ids=(
                frozenset(self.config.instrument_ids) if self.config.instrument_ids else None
            ),
        )

    def _build_data_client_config(self) -> BinanceDataClientConfig:
        """构建 DataClient 配置.

        使用 environment 字段替代已弃用的 testnet 布尔值（1.223.0+）。
        key_type 不再传递，由 NT 自动检测（1.223.0 deprecated）。

        Returns:
            BinanceDataClientConfig 实例。
        """
        return BinanceDataClientConfig(
            api_key=self._resolve_api_key(),
            api_secret=self._resolve_api_secret(),
            account_type=self.config.account_type,
            environment=self.config.environment,
            base_url_http=self.config.base_url_http,
            base_url_ws=self.config.base_url_ws,
            proxy_url=self.config.proxy_url,
            update_instruments_interval_mins=self.config.update_instruments_interval_mins,
            instrument_provider=self._build_instrument_provider_config(),
        )

    def _build_exec_client_config(self) -> BinanceExecClientConfig:
        """构建 ExecutionClient 配置.

        使用 environment 字段替代已弃用的 testnet 布尔值（1.223.0+）。
        key_type 不再传递，由 NT 自动从 api_secret 格式检测（1.223.0+）。

        Returns:
            BinanceExecClientConfig 实例。
        """
        from nautilus_trader.adapters.binance.config import BinanceSymbol

        futures_leverages = None
        if self.config.futures_leverages:
            futures_leverages = {
                BinanceSymbol(sym): lev
                for sym, lev in self.config.futures_leverages.items()
            }

        return BinanceExecClientConfig(
            api_key=self._resolve_api_key(),
            api_secret=self._resolve_api_secret(),
            account_type=self.config.account_type,
            environment=self.config.environment,
            base_url_http=self.config.base_url_http,
            base_url_ws=self.config.base_url_ws,
            proxy_url=self.config.proxy_url,
            use_reduce_only=self.config.use_reduce_only,
            use_position_ids=self.config.use_position_ids,
            recv_window_ms=self.config.recv_window_ms,
            max_retries=self.config.max_retries,
            retry_delay_initial_ms=self.config.retry_delay_initial_ms,
            retry_delay_max_ms=self.config.retry_delay_max_ms,
            futures_leverages=futures_leverages,
            instrument_provider=self._build_instrument_provider_config(),
        )

    def _build_node_config(self) -> TradingNodeConfig:
        """组装完整的 TradingNodeConfig.

        Returns:
            TradingNodeConfig 实例，可直接传入 TradingNode()。
        """
        data_cfg = self._build_data_client_config()
        exec_cfg = self._build_exec_client_config()

        # DataEngine 默认配置
        data_engine_defaults: dict[str, Any] = {
            "time_bars_timestamp_on_close": True,
        }
        data_engine_defaults.update(self.config.data_engine)

        # ExecEngine 默认配置
        exec_engine_defaults: dict[str, Any] = {
            "reconciliation": True,
            "reconciliation_lookback_mins": 1440,  # 最近 24h
        }
        exec_engine_defaults.update(self.config.exec_engine)

        # RiskEngine 默认配置
        # max_notional_per_order 留空，NT 默认无限制；
        # 若需限制请在 config.risk_engine 中按 instrument_id 格式传入，
        # 例如：{"BTCUSDT-PERP.BINANCE": "1000000"}
        risk_engine_defaults: dict[str, Any] = {
            "bypass": False,
            "max_order_submit_rate": "100/00:00:01",
            "max_order_modify_rate": "100/00:00:01",
        }
        risk_engine_defaults.update(self.config.risk_engine)

        # Logging 默认配置
        logging_defaults: dict[str, Any] = {
            "log_level": "INFO",
        }
        logging_defaults.update(self.config.logging)

        return TradingNodeConfig(
            trader_id="BINANCE-FUTURES-001",
            data_clients={"BINANCE": data_cfg},
            exec_clients={"BINANCE": exec_cfg},
            data_engine=LiveDataEngineConfig(**data_engine_defaults),
            exec_engine=LiveExecEngineConfig(**exec_engine_defaults),
            risk_engine=LiveRiskEngineConfig(**risk_engine_defaults),
            logging=LoggingConfig(**logging_defaults),
        )


# ---------------------------------------------------------------------------
# 工厂函数（便捷入口）
# ---------------------------------------------------------------------------


def build_binance_adapter(
    api_key: str | None = None,
    api_secret: str | None = None,
    environment: BinanceEnvironment = BinanceEnvironment.LIVE,
    symbols: list[str] | None = None,
    leverages: dict[str, int] | None = None,
    proxy_url: str | None = None,
) -> BinanceAdapter:
    """快速构建 BinanceAdapter 的工厂函数.

    Args:
        api_key: Binance API 公钥，None 时从环境变量读取。
        api_secret: Binance API 私钥，None 时从环境变量读取。
        environment: 交易所环境，默认 BinanceEnvironment.LIVE。
            使用 BinanceEnvironment.TESTNET 连接测试网。
        symbols: 要预加载的合约符号列表，格式 ["BTCUSDT"]。
            会自动转换为 Nautilus instrument_id 格式。
        leverages: 各合约杠杆，格式 {"BTCUSDT": 10}。
        proxy_url: HTTP 代理 URL，无代理传 None。

    Returns:
        已配置但未启动的 BinanceAdapter 实例。

    Example::

        adapter = build_binance_adapter(
            environment=BinanceEnvironment.TESTNET,
            symbols=["BTCUSDT", "ETHUSDT"],
            leverages={"BTCUSDT": 10, "ETHUSDT": 5},
        )
        await adapter.start()
    """
    instrument_ids: list[str] = []
    if symbols:
        for sym in symbols:
            if "-PERP.BINANCE" not in sym and ".BINANCE" not in sym:
                instrument_ids.append(f"{sym}-PERP.BINANCE")
            else:
                instrument_ids.append(sym)

    cfg = BinanceAdapterConfig(
        api_key=api_key,
        api_secret=api_secret,
        environment=environment,
        account_type=BinanceAccountType.USDT_FUTURES,
        instrument_ids=instrument_ids,
        futures_leverages=leverages,
        proxy_url=proxy_url,
    )
    return BinanceAdapter(cfg)
