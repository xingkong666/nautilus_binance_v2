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
import threading
from dataclasses import dataclass, field
from typing import Any

import structlog
from nautilus_trader.adapters.binance import config as binance_config
from nautilus_trader.adapters.binance.common.enums import BinanceEnvironment
from nautilus_trader.adapters.binance.common.urls import get_http_base_url
from nautilus_trader.adapters.binance.config import (
    BinanceDataClientConfig,
    BinanceExecClientConfig,
    BinanceInstrumentProviderConfig,
)
from nautilus_trader.adapters.binance.factories import (
    BinanceLiveDataClientFactory,
    BinanceLiveExecClientFactory,
)
from nautilus_trader.adapters.binance.futures.http.account import BinanceFuturesAccountHttpAPI
from nautilus_trader.adapters.binance.http.client import BinanceHttpClient
from nautilus_trader.common.component import LiveClock
from nautilus_trader.config import (
    CacheConfig,
    LiveDataEngineConfig,
    LiveExecEngineConfig,
    LiveRiskEngineConfig,
    LoggingConfig,
    TradingNodeConfig,
)
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.live.node import TradingNode

from src.core.config import EnvSettings

logger = structlog.get_logger(__name__)
_DEFAULT_BINANCE_ACCOUNT_TYPE = getattr(
    getattr(binance_config, "BinanceAccountType", object),
    "USDT_FUTURES",
    "USDT_FUTURES",
)


# ---------------------------------------------------------------------------
# 配置
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
        cache: TradingNode cache 配置。
        instance_id: TradingNode 实例 ID，用于隔离 cache key。

    """

    api_key: str | None = None
    api_secret: str | None = None
    environment: BinanceEnvironment = BinanceEnvironment.LIVE
    account_type: Any = _DEFAULT_BINANCE_ACCOUNT_TYPE
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
    log_level: str = "INFO"
    cache: CacheConfig | None = None
    instance_id: UUID4 | None = None


# ---------------------------------------------------------------------------
# 适配器
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
        """初始化适配器，不创建网络连接。.

        Args:
            config: 适配器配置，见 BinanceAdapterConfig。

        """
        self.config = config
        self._node: TradingNode | None = None
        self._started = False
        self._pending_strategies: list[Any] = []
        self._hedge_mode: bool | None = None

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def node(self) -> TradingNode:
        """已构建的 TradingNode 实例.

        Raises:
            RuntimeError: 若尚未 build/start。

        """
        if self._node is None:
            raise RuntimeError("BinanceAdapter node not created. Call build_node()/run()/start() first.")
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

    def register_strategy(self, strategy: Any) -> None:
        """在启动前注册策略到 TradingNode.

        Args:
            strategy: Strategy instance to bind or inspect.
        """
        if self._node is not None:
            raise RuntimeError("Cannot register strategy after TradingNode is built.")
        instrument_id = getattr(getattr(strategy, "config", None), "instrument_id", None)
        if instrument_id is not None:
            instrument_text = str(instrument_id)
            if instrument_text not in self.config.instrument_ids:
                self.config.instrument_ids.append(instrument_text)
        self._pending_strategies.append(strategy)

    def build_node(self) -> TradingNode:
        """构建 TradingNode 并挂载预注册策略."""
        if self._node is not None:
            return self._node

        node_config = self._build_node_config()
        self._node = TradingNode(config=node_config)
        self._node.add_data_client_factory("BINANCE", BinanceLiveDataClientFactory)
        self._node.add_exec_client_factory("BINANCE", BinanceLiveExecClientFactory)

        for strategy in self._pending_strategies:
            self._node.trader.add_strategy(strategy)

        self._node.build()
        logger.info("binance_adapter_node_built", strategy_count=len(self._pending_strategies))
        return self._node

    def run(self) -> None:
        """同步阻塞运行 TradingNode."""
        node = self.build_node()
        self._started = True
        logger.info("binance_adapter_running")
        try:
            node.run()
        finally:
            self._started = False

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
        node = self.build_node()
        await node.run_async()

        self._started = True
        logger.info("binance_adapter_started")

    def request_stop(self) -> None:
        """请求 TradingNode 停止运行."""
        if self._node is None:
            return
        self._node.stop()

    def dispose(self) -> None:
        """释放 TradingNode 资源."""
        if self._node is None:
            return
        self._node.dispose()
        self._started = False
        logger.info("binance_adapter_disposed")

    async def stop(self) -> None:
        """优雅停止 TradingNode，断开所有连接并清理资源.

        Raises:
            RuntimeError: 若 start() 尚未调用。

        """
        if self._node is None:
            raise RuntimeError("BinanceAdapter not started.")

        logger.info("binance_adapter_stopping")
        try:
            if self._started:
                self.request_stop()
                await asyncio.sleep(0.5)  # 留给交易引擎内部清理时间
            self.dispose()
        except Exception as exc:
            logger.warning("binance_adapter_stop_error", error=str(exc))
        finally:
            self._started = False
            logger.info("binance_adapter_stopped")

    async def fetch_account_snapshot_async(self) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        """异步拉取 Binance 账户余额和持仓快照."""
        account_api = self._build_account_http_api()
        account_info = await account_api.query_futures_account_info(
            recv_window=str(self.config.recv_window_ms),
        )
        position_risks = await account_api.query_futures_position_risk(
            recv_window=str(self.config.recv_window_ms),
        )
        return (
            self._serialize_account_balances(account_info.assets),
            self._serialize_position_risks(position_risks),
        )

    def fetch_account_snapshot(self) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        """同步拉取 Binance 账户余额和持仓快照."""
        return self._run_async_blocking(self.fetch_account_snapshot_async())

    def fetch_balance(self) -> list[dict[str, str]]:
        """兼容接口：返回账户余额快照."""
        balances, _positions = self.fetch_account_snapshot()
        return balances

    def fetch_positions(self) -> list[dict[str, str]]:
        """兼容接口：返回持仓快照."""
        _balances, positions = self.fetch_account_snapshot()
        return positions

    async def fetch_open_orders_async(self, symbol: str | None = None) -> list[dict[str, str]]:
        """异步拉取 Binance 当前挂单.

        Args:
            symbol: Trading symbol to process.
        """
        account_api = self._build_account_http_api()
        open_orders = await account_api.query_open_orders(
            symbol=symbol,
            recv_window=str(self.config.recv_window_ms),
        )
        return self._serialize_open_orders(open_orders)

    def fetch_open_orders(self, symbol: str | None = None) -> list[dict[str, str]]:
        """同步拉取 Binance 当前挂单.

        Args:
            symbol: Trading symbol to process.
        """
        return self._run_async_blocking(self.fetch_open_orders_async(symbol=symbol))

    async def query_hedge_mode_async(self) -> bool:
        """异步查询 Binance Futures 是否启用 Hedge Mode."""
        account_api = self._build_account_http_api()
        response = await account_api.query_futures_hedge_mode(
            recv_window=str(self.config.recv_window_ms),
        )
        self._hedge_mode = bool(response.dualSidePosition)
        return self._hedge_mode

    def query_hedge_mode(self) -> bool:
        """同步查询 Binance Futures 是否启用 Hedge Mode."""
        return bool(self._run_async_blocking(self.query_hedge_mode_async()))

    def prepare_runtime_config(self) -> None:
        """按真实账户模式修正运行期配置."""
        try:
            self._hedge_mode = self.query_hedge_mode()
        except Exception as exc:
            logger.warning("binance_adapter_hedge_mode_probe_failed", error=str(exc))
            return

        if self._hedge_mode and self.config.use_reduce_only:
            self.config.use_reduce_only = False
            logger.warning(
                "binance_adapter_reduce_only_disabled_for_hedge_mode",
                environment=self.config.environment.value,
            )
        else:
            logger.info(
                "binance_adapter_runtime_config_ready",
                hedge_mode=self._hedge_mode,
                use_reduce_only=self.config.use_reduce_only,
            )

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
        env_value = os.environ.get(env_map[self.config.environment])
        if env_value:
            return env_value
        try:
            settings = EnvSettings()
        except Exception as exc:
            logger.error("env_settings_load_failed_api_key", error=str(exc), exc_info=True)
            return None
        if self.config.environment == BinanceEnvironment.TESTNET:
            return settings.binance_testnet_api_key or None
        if self.config.environment == BinanceEnvironment.DEMO:
            return settings.binance_demo_api_key or None
        return settings.binance_api_key or None

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
        env_value = os.environ.get(env_map[self.config.environment])
        if env_value:
            return env_value
        try:
            settings = EnvSettings()
        except Exception as exc:
            logger.error("env_settings_load_failed_api_secret", error=str(exc), exc_info=True)
            return None
        if self.config.environment == BinanceEnvironment.TESTNET:
            return settings.binance_testnet_api_secret or None
        if self.config.environment == BinanceEnvironment.DEMO:
            return settings.binance_demo_api_secret or None
        return settings.binance_api_secret or None

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
            load_ids=(frozenset(self.config.instrument_ids) if self.config.instrument_ids else None),
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
        futures_leverages = None
        if self.config.futures_leverages:
            futures_leverages = {
                binance_config.BinanceSymbol(sym): lev  # type: ignore[attr-defined]
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

        # 数据引擎默认配置
        data_engine_defaults: dict[str, Any] = {
            "time_bars_timestamp_on_close": True,
        }
        data_engine_defaults.update(self.config.data_engine)

        # 执行引擎默认配置
        exec_engine_defaults: dict[str, Any] = {
            "reconciliation": True,
            "reconciliation_lookback_mins": 1440,  # 最近 24 小时
        }
        exec_engine_defaults.update(self.config.exec_engine)

        # 风控引擎默认配置
        # 最大单笔名义金额留空，交易引擎默认无限制；
        # 若需限制，请在风控引擎配置中按合约标识格式传入，
        # 例如：{"BTCUSDT-PERP.BINANCE": "1000000"}
        risk_engine_defaults: dict[str, Any] = {
            "bypass": False,
            "max_order_submit_rate": "100/00:00:01",
            "max_order_modify_rate": "100/00:00:01",
        }
        risk_engine_defaults.update(self.config.risk_engine)

        # 日志默认配置：从配置日志级别读取，并启用桥接
        # 启用桥接会将交易引擎底层日志接入 Python日志 / 结构化日志管道
        logging_defaults: dict[str, Any] = {
            "log_level": self.config.log_level,
            "use_pyo3": True,
        }
        logging_defaults.update(self.config.logging)

        return TradingNodeConfig(
            trader_id="BINANCE-FUTURES-001",
            instance_id=self.config.instance_id,
            cache=self.config.cache,
            data_clients={"BINANCE": data_cfg},
            exec_clients={"BINANCE": exec_cfg},
            data_engine=LiveDataEngineConfig(**data_engine_defaults),
            exec_engine=LiveExecEngineConfig(**exec_engine_defaults),
            risk_engine=LiveRiskEngineConfig(**risk_engine_defaults),
            logging=LoggingConfig(**logging_defaults),
        )

    def _build_account_http_api(self) -> BinanceFuturesAccountHttpAPI:
        api_key = self._resolve_api_key()
        api_secret = self._resolve_api_secret()
        if not api_key or not api_secret:
            raise RuntimeError("Binance API credentials are required for account snapshot queries.")

        http_client = BinanceHttpClient(
            clock=LiveClock(),
            api_key=api_key,
            api_secret=api_secret,
            base_url=self.config.base_url_http
            or get_http_base_url(
                account_type=self.config.account_type,
                environment=self.config.environment,
                is_us=False,
            ),
            proxy_url=self.config.proxy_url,
        )
        return BinanceFuturesAccountHttpAPI(
            client=http_client,
            clock=LiveClock(),
            account_type=self.config.account_type,
        )

    @staticmethod
    def _serialize_account_balances(balances: list[Any]) -> list[dict[str, str]]:
        return [
            {
                "asset": str(balance.asset),
                "walletBalance": str(balance.walletBalance),
                "availableBalance": str(balance.availableBalance),
                "unrealizedProfit": str(balance.unrealizedProfit),
            }
            for balance in balances
            if str(balance.walletBalance) != "0"
        ]

    @staticmethod
    def _serialize_position_risks(position_risks: list[Any]) -> list[dict[str, str]]:
        return [
            {
                "symbol": str(position.symbol),
                "positionSide": str(getattr(position.positionSide, "value", position.positionSide)),
                "positionAmt": str(position.positionAmt),
                "entryPrice": str(position.entryPrice),
                "unrealizedProfit": str(position.unRealizedProfit),
                "leverage": str(position.leverage or "1"),
            }
            for position in position_risks
            if str(position.positionAmt) not in {"0", "0.0", "0.00000000"}
        ]

    @staticmethod
    def _serialize_open_orders(open_orders: list[Any]) -> list[dict[str, str]]:
        return [
            {
                "symbol": str(order.symbol),
                "clientOrderId": str(order.clientOrderId),
                "orderId": str(order.orderId),
                "status": str(getattr(order.status, "value", order.status or "")),
                "side": str(getattr(order.side, "value", order.side or "")),
                "type": str(getattr(order.type, "value", order.type or "")),
                "positionSide": str(order.positionSide or "BOTH"),
                "reduceOnly": str(bool(order.reduceOnly)),
            }
            for order in open_orders
        ]

    @staticmethod
    def _run_async_blocking(coro: Any) -> Any:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)

        result: dict[str, Any] = {}
        error: dict[str, BaseException] = {}

        def _runner() -> None:
            try:
                result["value"] = asyncio.run(coro)
            except BaseException as exc:  # pragma: no cover - 防御性分支
                error["value"] = exc

        thread = threading.Thread(target=_runner, name="BinanceAdapterAsyncBridge", daemon=True)
        thread.start()
        thread.join()

        if "value" in error:
            raise error["value"]

        return result.get("value")


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
    cache: CacheConfig | None = None,
    instance_id: UUID4 | None = None,
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
        cache: TradingNode cache 配置。
        instance_id: TradingNode 实例 ID，用于隔离 cache key。

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
        account_type=_DEFAULT_BINANCE_ACCOUNT_TYPE,
        instrument_ids=instrument_ids,
        futures_leverages=leverages,
        proxy_url=proxy_url,
        cache=cache,
        instance_id=instance_id,
    )
    return BinanceAdapter(cfg)
