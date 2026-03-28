"""依赖容器.

集中管理所有服务实例的生命周期，作为整个应用的单一依赖来源。
各模块通过 Container 获取依赖，避免到处传参。

访问方式:
    container = Container(config)
    container.build()
    event_bus = container.event_bus
    risk_manager = container.pre_trade_risk
"""

from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING, Any

import structlog
from nautilus_trader.adapters.binance.common.enums import BinanceEnvironment

from src.cache.redis_client import RedisClient
from src.core.config import EnvSettings
from src.core.events import EventBus
from src.exchange.binance_adapter import BinanceAdapter, BinanceAdapterConfig
from src.execution.fill_handler import FillHandler
from src.execution.ignored_instruments import IgnoredInstrumentRegistry
from src.execution.order_router import OrderRouter
from src.execution.rate_limiter import RateLimiter
from src.execution.signal_processor import SignalProcessor
from src.monitoring.alerting import AlertManager, build_alert_manager
from src.monitoring.health_server import HealthServer
from src.monitoring.metrics import EVENT_BUS_EVENTS
from src.monitoring.prometheus_server import PrometheusServer
from src.monitoring.watchers import BaseWatcher, build_watchers
from src.portfolio.allocator import PortfolioAllocator
from src.risk.circuit_breaker import CircuitBreaker
from src.risk.drawdown_control import DrawdownController
from src.risk.position_sizer import PositionSizer
from src.risk.pre_trade import PreTradeRiskManager
from src.state.persistence import TradePersistence
from src.state.snapshot import SnapshotManager

if TYPE_CHECKING:
    from src.core.config import AppConfig

logger = structlog.get_logger()


class Container:
    """应用依赖容器.

    持有所有服务的单例实例，提供统一的生命周期管理（build / teardown）。
    设计原则：所有有状态的服务都在 build() 中初始化，在 teardown() 中清理。
    """

    def __init__(self, config: AppConfig) -> None:
        """初始化容器，但不创建任何服务实例（延迟到 build()）.

        Args:
            config: 应用配置，包含所有子模块配置。

        """
        self._config = config
        self._built = False

        # 服务实例（build 后可用）
        self._redis_client: RedisClient | None = None
        self._event_bus: EventBus | None = None
        self._persistence: TradePersistence | None = None
        self._snapshot_manager: SnapshotManager | None = None
        self._rate_limiter: RateLimiter | None = None
        self._ignored_instruments: IgnoredInstrumentRegistry | None = None
        self._position_sizer: PositionSizer | None = None
        self._pre_trade_risk: PreTradeRiskManager | None = None
        self._circuit_breaker: CircuitBreaker | None = None
        self._drawdown_controller: DrawdownController | None = None
        self._fill_handler: FillHandler | None = None
        self._order_router: OrderRouter | None = None
        self._signal_processor: SignalProcessor | None = None
        self._alert_manager: AlertManager | None = None
        self._watchers: list[BaseWatcher] = []
        self._health_server: HealthServer | None = None
        self._prometheus_server: PrometheusServer | None = None
        self._portfolio_allocator: PortfolioAllocator | None = None
        self._binance_adapter: BinanceAdapter | None = None

    # ------ 生命周期 ------

    def build(self) -> Container:
        """实例化并连接所有服务.

        按照依赖顺序初始化：基础设施 → 风控 → 执行 → 监控。

        Returns:
            self，支持链式调用。

        """
        if self._built:
            logger.warning("container_already_built")
            return self

        cfg = self._config
        logger.info("container_building", env=cfg.env)

        # 1. 基础设施
        self._event_bus = self._build_event_bus()
        self._persistence = TradePersistence(database_url=cfg.data.database_url)
        self._snapshot_manager = SnapshotManager(snapshot_dir=cfg.data.catalog_dir.parent / "snapshots" / cfg.env)

        # 初始化 Redis（失败时打 WARNING 不中断启动）
        try:
            self._redis_client = RedisClient(cfg.redis)
            if not self._redis_client.is_available:
                logger.warning("redis_unavailable_degraded_mode")
        except Exception as exc:
            logger.warning("redis_init_failed", error=str(exc))
            self._redis_client = None

        # 2. 执行层
        self._rate_limiter = RateLimiter(cfg.execution.rate_limit, redis_client=self._redis_client)
        self._ignored_instruments = IgnoredInstrumentRegistry(event_bus=self._event_bus)
        self._position_sizer = PositionSizer(config=cfg.execution.algo if cfg.execution.algo else {"mode": "fixed"})

        # 3. 风控层
        risk_cfg = cfg.risk
        self._pre_trade_risk = PreTradeRiskManager(
            event_bus=self._event_bus,
            config=risk_cfg.pre_trade,
        )
        self._circuit_breaker = CircuitBreaker(
            event_bus=self._event_bus,
            config=risk_cfg.circuit_breaker,
            redis_client=self._redis_client,
        )
        rt_cfg = risk_cfg.real_time
        self._drawdown_controller = DrawdownController(
            warning_pct=float(rt_cfg.get("trailing_drawdown_pct", 3.0)),
            critical_pct=float(rt_cfg.get("max_drawdown_pct", 5.0)),
        )

        # 4. 资金分配
        portfolio_cfg = cfg.strategies.get("portfolio", {})
        if portfolio_cfg:
            strategy_list = portfolio_cfg.get("strategies", [])
            # 从 strategies 配置下收集所有策略的 allocation 配置
            alloc_strategies = []
            for sid, scfg in cfg.strategies.items():
                if sid == "portfolio":
                    continue
                alloc = scfg.get("allocation", {}) if isinstance(scfg, dict) else {}
                alloc_strategies.append(
                    {
                        "strategy_id": sid,
                        "weight": float(alloc.get("weight", 1.0)),
                        "max_allocation_pct": float(alloc.get("max_allocation_pct", 0.0)),
                        "enabled": bool(alloc.get("enabled", True)),
                    }
                )
            # portfolio 块下的 strategies 列表优先级更高（可覆盖）
            if strategy_list:
                alloc_strategies = strategy_list

            if alloc_strategies:
                self._portfolio_allocator = PortfolioAllocator(
                    {
                        "mode": portfolio_cfg.get("mode", "equal"),
                        "reserve_pct": float(portfolio_cfg.get("reserve_pct", 5.0)),
                        "min_allocation": str(portfolio_cfg.get("min_allocation", "100")),
                        "strategies": alloc_strategies,
                    }
                )
                logger.info(
                    "portfolio_allocator_registered",
                    mode=portfolio_cfg.get("mode", "equal"),
                    strategy_count=len(alloc_strategies),
                )

        # 5. 交易所适配器（仅实盘环境，dev/testnet 可跳过）
        exchange_cfg = cfg.exchange or cfg.strategies.get("exchange", {})
        if exchange_cfg or cfg.env in ("prod", "staging"):
            env_settings = self._get_env_settings()
            # 优先读 YAML 中的 environment 字段，其次按 env 推断
            # prod → LIVE，其余（dev/staging/testnet） → TESTNET
            env_str = exchange_cfg.get("environment", "TESTNET" if cfg.env != "prod" else "LIVE")
            try:
                binance_env = BinanceEnvironment[env_str.upper()]
            except KeyError:
                logger.warning("unknown_binance_environment", value=env_str, fallback="TESTNET")
                binance_env = BinanceEnvironment.TESTNET
            default_api_key, default_api_secret = self._resolve_binance_credentials(env_settings, binance_env)

            self._binance_adapter = BinanceAdapter(
                BinanceAdapterConfig(
                    api_key=exchange_cfg.get("api_key") or default_api_key or None,
                    api_secret=exchange_cfg.get("api_secret") or default_api_secret or None,
                    environment=binance_env,
                    instrument_ids=exchange_cfg.get("instrument_ids", []),
                    futures_leverages=exchange_cfg.get("futures_leverages"),
                    proxy_url=exchange_cfg.get("proxy_url"),
                    use_reduce_only=bool(exchange_cfg.get("use_reduce_only", True)),
                    use_position_ids=bool(exchange_cfg.get("use_position_ids", True)),
                    max_retries=exchange_cfg.get("max_retries"),
                    retry_delay_initial_ms=exchange_cfg.get("retry_delay_initial_ms"),
                    retry_delay_max_ms=exchange_cfg.get("retry_delay_max_ms"),
                )
            )
            logger.info(
                "binance_adapter_registered",
                environment=self._binance_adapter.config.environment.value,
                env=cfg.env,
            )

        # 6. 成交处理
        self._fill_handler = FillHandler(
            event_bus=self._event_bus,
            persistence=self._persistence,
        )
        self._order_router = OrderRouter(
            event_bus=self._event_bus,
            submit_orders=cfg.execution.submit_orders,
        )
        self._signal_processor = SignalProcessor(
            event_bus=self._event_bus,
            order_router=self._order_router,
            pre_trade_risk=self._pre_trade_risk,
            rate_limiter=self._rate_limiter,
            ignored_instruments=self._ignored_instruments,
        )

        # 7. 告警
        alerting_cfg = cfg.monitoring.alerting
        env_settings = self._get_env_settings()
        self._alert_manager = build_alert_manager(
            event_bus=self._event_bus,
            alerting_config=alerting_cfg,
            telegram_token=env_settings.get("telegram_bot_token", ""),
            telegram_chat_id=env_settings.get("telegram_chat_id", ""),
        )
        self._watchers = build_watchers(self._event_bus, self._alert_manager, alerting_cfg)
        self._alert_manager.start()

        # 8. 监控
        if cfg.monitoring.enabled:
            self._prometheus_server = PrometheusServer(port=cfg.monitoring.prometheus_port)
            self._prometheus_server.start()
            logger.info("prometheus_exporter_started", port=cfg.monitoring.prometheus_port)
            self._health_server = HealthServer(port=8080)
            self._health_server.start()
            logger.info("health_server_started", port=8080)

        self._built = True
        logger.info("container_built", env=cfg.env)
        return self

    def teardown(self) -> None:
        """清理所有服务，释放资源.

        按照依赖逆序清理：监控 → 执行 → 基础设施。
        """
        logger.info("container_teardown")

        if self._alert_manager is not None:
            try:
                self._alert_manager.stop()
            except Exception:
                logger.exception("alert_manager_stop_failed")

        if self._health_server is not None:
            try:
                self._health_server.stop()
            except Exception:
                logger.exception("health_server_stop_failed")

        if self._prometheus_server is not None:
            try:
                self._prometheus_server.stop()
            except Exception:
                logger.exception("prometheus_server_stop_failed")

        if self._binance_adapter and self._binance_adapter.is_started:
            # adapter.stop() 是 async；同步 teardown 中仅记录警告，
            # 调用方应在事件循环中先 await adapter.stop() 再调用 teardown()。
            logger.warning(
                "binance_adapter_not_stopped",
                hint="call `await container.binance_adapter.stop()` before teardown()",
            )

        if self._persistence:
            try:
                self._persistence.close()
            except Exception:
                logger.exception("persistence_close_failed")

        if self._redis_client is not None:
            try:
                self._redis_client.close()
            except Exception:
                logger.exception("redis_close_failed")

        if self._event_bus is not None:
            self._event_bus.clear()

        self._built = False
        logger.info("container_teardown_done")

    def _ensure_built(self) -> None:
        """确保容器已调用 build()，否则抛出异常.

        Raises:
            RuntimeError: 容器尚未初始化。

        """
        if not self._built:
            raise RuntimeError("Container not built. Call container.build() first.")

    # ------ EventBus 构建 ------

    def _get_env_settings(self) -> dict[str, str]:
        """读取环境变量中的敏感配置（Token、密钥等）.

        Returns:
            包含 telegram_bot_token / telegram_chat_id 等字段的字典。

        """
        try:
            s = EnvSettings()
            return {
                "binance_api_key": s.binance_api_key,
                "binance_api_secret": s.binance_api_secret,
                "binance_testnet_api_key": s.binance_testnet_api_key,
                "binance_testnet_api_secret": s.binance_testnet_api_secret,
                "binance_demo_api_key": s.binance_demo_api_key,
                "binance_demo_api_secret": s.binance_demo_api_secret,
                "telegram_bot_token": s.telegram_bot_token,
                "telegram_chat_id": s.telegram_chat_id,
            }
        except Exception:
            return {}

    @staticmethod
    def _resolve_binance_credentials(
        env_settings: dict[str, str],
        environment: BinanceEnvironment,
    ) -> tuple[str, str]:
        if environment == BinanceEnvironment.TESTNET:
            return (
                env_settings.get("binance_testnet_api_key", ""),
                env_settings.get("binance_testnet_api_secret", ""),
            )
        if environment == BinanceEnvironment.DEMO:
            return (
                env_settings.get("binance_demo_api_key", ""),
                env_settings.get("binance_demo_api_secret", ""),
            )
        return (
            env_settings.get("binance_api_key", ""),
            env_settings.get("binance_api_secret", ""),
        )

    def _build_event_bus(self) -> EventBus:
        """构建 EventBus 并挂载全局监控 handler.

        Returns:
            配置好的 EventBus 实例。

        """
        bus = EventBus()

        # 全局事件计数（Prometheus）
        if self._config.monitoring.enabled:

            def _metrics_handler(event: Any) -> None:
                with suppress(Exception):
                    EVENT_BUS_EVENTS.labels(event_type=event.event_type.value).inc()

            bus.subscribe_all(_metrics_handler)

        return bus

    # ------ 属性访问 ------

    @property
    def config(self) -> AppConfig:
        """返回应用配置."""
        return self._config

    @property
    def redis_client(self) -> RedisClient | None:
        """返回 Redis 客户端实例（Redis 不可用时为 None）.

        Raises:
            RuntimeError: 容器未 build。

        """
        self._ensure_built()
        return self._redis_client

    @property
    def event_bus(self) -> EventBus:
        """返回事件总线实例.

        Raises:
            RuntimeError: 容器未 build。

        """
        self._ensure_built()
        assert self._event_bus is not None
        return self._event_bus

    @property
    def persistence(self) -> TradePersistence:
        """返回交易持久化实例.

        Raises:
            RuntimeError: 容器未 build。

        """
        self._ensure_built()
        assert self._persistence is not None
        return self._persistence

    @property
    def snapshot_manager(self) -> SnapshotManager:
        """返回状态快照管理器.

        Raises:
            RuntimeError: 容器未 build。

        """
        self._ensure_built()
        assert self._snapshot_manager is not None
        return self._snapshot_manager

    @property
    def rate_limiter(self) -> RateLimiter:
        """返回速率限制器.

        Raises:
            RuntimeError: 容器未 build。

        """
        self._ensure_built()
        assert self._rate_limiter is not None
        return self._rate_limiter

    @property
    def position_sizer(self) -> PositionSizer:
        """返回仓位计算器.

        Raises:
            RuntimeError: 容器未 build。

        """
        self._ensure_built()
        assert self._position_sizer is not None
        return self._position_sizer

    @property
    def ignored_instruments(self) -> IgnoredInstrumentRegistry:
        """返回运行期交易对忽略注册表."""
        self._ensure_built()
        assert self._ignored_instruments is not None
        return self._ignored_instruments

    @property
    def pre_trade_risk(self) -> PreTradeRiskManager:
        """返回事前风控管理器.

        Raises:
            RuntimeError: 容器未 build。

        """
        self._ensure_built()
        assert self._pre_trade_risk is not None
        return self._pre_trade_risk

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        """返回熔断器.

        Raises:
            RuntimeError: 容器未 build。

        """
        self._ensure_built()
        assert self._circuit_breaker is not None
        return self._circuit_breaker

    @property
    def drawdown_controller(self) -> DrawdownController:
        """返回回撤控制器.

        Raises:
            RuntimeError: 容器未 build。

        """
        self._ensure_built()
        assert self._drawdown_controller is not None
        return self._drawdown_controller

    @property
    def fill_handler(self) -> FillHandler:
        """返回成交处理器.

        Raises:
            RuntimeError: 容器未 build。

        """
        self._ensure_built()
        assert self._fill_handler is not None
        return self._fill_handler

    @property
    def alert_manager(self) -> AlertManager:
        """返回告警管理器.

        Raises:
            RuntimeError: 容器未 build。

        """
        self._ensure_built()
        assert self._alert_manager is not None
        return self._alert_manager

    @property
    def order_router(self) -> OrderRouter:
        """返回订单路由器.

        Raises:
            RuntimeError: 容器未 build。

        """
        self._ensure_built()
        assert self._order_router is not None
        return self._order_router

    @property
    def signal_processor(self) -> SignalProcessor:
        """返回信号处理器.

        Raises:
            RuntimeError: 容器未 build。

        """
        self._ensure_built()
        assert self._signal_processor is not None
        return self._signal_processor

    @property
    def health_server(self) -> HealthServer | None:
        """返回健康检查服务（monitoring.enabled=False 时为 None）.

        Raises:
            RuntimeError: 容器未 build。

        """
        self._ensure_built()
        return self._health_server

    @property
    def prometheus_server(self) -> PrometheusServer | None:
        """返回 Prometheus exporter 服务（monitoring.enabled=False 时为 None）."""
        self._ensure_built()
        return self._prometheus_server

    @property
    def portfolio_allocator(self) -> PortfolioAllocator | None:
        """返回多策略资金分配器.

        若 AppConfig.strategies 中未配置 "portfolio" 节，则返回 None（单策略模式）。

        Raises:
            RuntimeError: 容器未 build。

        """
        self._ensure_built()
        return self._portfolio_allocator

    @property
    def binance_adapter(self) -> BinanceAdapter | None:
        """返回 Binance 交易所适配器.

        仅在 prod/staging 环境或配置了 exchange 节时非 None。
        启动实盘前需 `await container.binance_adapter.start()`。

        Raises:
            RuntimeError: 容器未 build。

        """
        self._ensure_built()
        return self._binance_adapter
