"""回测执行引擎.

封装 NautilusTrader BacktestEngine，提供统一的回测入口。
支持单 symbol / 多 symbol，从 ParquetDataCatalog 加载数据。

流程:
    BacktestRunner.run()
        → 构建 BacktestEngine (venue / instrument / data)
        → 注册策略
        → engine.run()
        → 返回 BacktestResult + 报告数据
"""
# ruff: noqa: TC001,TC002

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import structlog
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.common.config import LoggingConfig
from nautilus_trader.config import BacktestEngineConfig
from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.instruments import CryptoPerpetual
from nautilus_trader.model.objects import Money
from nautilus_trader.persistence.catalog import ParquetDataCatalog

from src.backtest.costs import BacktestCostAnalyzer
from src.core.config import AppConfig
from src.core.enums import INTERVAL_TO_NAUTILUS, Interval
from src.strategy.base import BaseStrategy, BaseStrategyConfig

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


@dataclass
class BacktestConfig:
    """回测参数配置.

    Attributes:
        start: 回测起始日期（含）。
        end: 回测结束日期（含）。
        symbols: 参与回测的交易对列表，如 ["BTCUSDT"]。
        interval: K 线周期，默认 1m。
        starting_balance_usdt: 初始账户余额（USDT）。
        leverage: 账户杠杆倍数。
        trader_id: Nautilus trader_id 标识。
        bypass_logging: 是否关闭 Nautilus 内部日志（加速回测）。
        run_analysis: 是否在回测结束后运行性能分析。
    """

    start: dt.date
    end: dt.date
    symbols: list[str] = field(default_factory=lambda: ["BTCUSDT"])
    interval: Interval = Interval.MINUTE_1
    starting_balance_usdt: int = 10_000
    leverage: float = 10.0
    trader_id: str = "BACKTESTER-001"
    bypass_logging: bool = True
    run_analysis: bool = True


# ---------------------------------------------------------------------------
# 回测执行器
# ---------------------------------------------------------------------------


class BacktestRunner:
    """回测执行器.

    Usage:
        config = BacktestConfig(
            start=date(2024, 1, 1),
            end=date(2024, 3, 31),
            symbols=["BTCUSDT"],
        )
        runner = BacktestRunner(app_config, backtest_config)
        result = runner.run(strategy_cls, strategy_config)
    """

    VENUE = "BINANCE"

    def __init__(self, app_config: AppConfig, backtest_config: BacktestConfig) -> None:
        """初始化回测执行器.

        Args:
            app_config: 应用配置（含 catalog_dir 等路径）。
            backtest_config: 回测参数配置。
        """
        self._app_cfg = app_config
        self._bt_cfg = backtest_config
        self._catalog = ParquetDataCatalog(app_config.data.catalog_dir)

    def run(
        self,
        strategy_cls: type[BaseStrategy],
        strategy_config: BaseStrategyConfig,
    ) -> BacktestRunResult:
        """执行一次回测.

        Args:
            strategy_cls: 策略类（BaseStrategy 子类）。
            strategy_config: 策略配置实例。

        Returns:
            BacktestRunResult，含原始 BacktestResult 和报告数据。

        Raises:
            ValueError: catalog 中找不到所需 instrument 或数据为空。
            RuntimeError: BacktestEngine 运行异常。
        """
        bt = self._bt_cfg
        logger.info(
            "backtest_start",
            symbols=bt.symbols,
            interval=bt.interval.value,
            start=str(bt.start),
            end=str(bt.end),
            balance=bt.starting_balance_usdt,
        )

        return self.run_many(
            strategy_specs=[(strategy_cls, strategy_config)],
            metadata={"strategy_names": [strategy_cls.__name__]},
        )

    def run_many(
        self,
        strategy_specs: list[tuple[type[BaseStrategy], BaseStrategyConfig]],
        metadata: dict[str, Any] | None = None,
    ) -> BacktestRunResult:
        """执行多策略回测."""
        bt = self._bt_cfg
        if not strategy_specs:
            raise ValueError("strategy_specs 不能为空")

        engine = self._build_engine()

        instruments = self._load_instruments(bt.symbols)
        for inst in instruments:
            engine.add_instrument(inst)

        total_bars = self._add_bar_data(engine, instruments)
        if total_bars == 0:
            raise ValueError(f"No bar data found in catalog for {bt.symbols} [{bt.start} ~ {bt.end}]")

        logger.info("backtest_data_loaded", total_bars=total_bars)

        strategy_names: list[str] = []
        for strategy_cls, strategy_config in strategy_specs:
            engine.add_strategy(strategy=strategy_cls(config=strategy_config))
            strategy_names.append(strategy_cls.__name__)

        engine.sort_data()
        engine.run(start=self._to_datetime(bt.start), end=self._to_datetime(bt.end, end_of_day=True))

        result = engine.get_result()
        reports = self._collect_reports(engine)
        analysis = self._build_analysis(reports, result)

        logger.info(
            "backtest_done",
            elapsed_time=result.elapsed_time,
            iterations=result.iterations,
            total_orders=result.total_orders,
            total_positions=result.total_positions,
            strategy_count=len(strategy_specs),
        )

        engine.dispose()

        merged_metadata = {"strategy_names": strategy_names}
        if metadata:
            merged_metadata.update(metadata)

        return BacktestRunResult(
            result=result,
            reports=reports,
            config=bt,
            analysis=analysis,
            metadata=merged_metadata,
        )

    # ------ 构建引擎 ------

    def _build_engine(self) -> BacktestEngine:
        """构建并配置 BacktestEngine.

        Returns:
            配置好 venue 的 BacktestEngine 实例。
        """
        engine_cfg = BacktestEngineConfig(
            trader_id=self._bt_cfg.trader_id,
            logging=LoggingConfig(bypass_logging=self._bt_cfg.bypass_logging),
            run_analysis=self._bt_cfg.run_analysis,
        )
        engine = BacktestEngine(config=engine_cfg)

        engine.add_venue(
            venue=Venue(self.VENUE),
            oms_type=OmsType.HEDGING,
            account_type=AccountType.MARGIN,
            starting_balances=[Money(self._bt_cfg.starting_balance_usdt, USDT)],
            default_leverage=Decimal(str(self._bt_cfg.leverage)),
            bar_execution=True,
            bar_adaptive_high_low_ordering=True,
            # 1.223.0: trade_execution 默认值从 False 改为 True，
            # 显式设为 False 保持"只用 bar 驱动成交"的原有行为，
            # 避免回测引入 trade tick 双重触发导致基准漂移。
            trade_execution=False,
            # 1.223.0 新增：模拟 Binance 市价单先发 OrderAccepted 再成交的行为，
            # 使回测成交流程更贴近实盘事件序列。
            use_market_order_acks=True,
        )

        return engine

    # ------ 加载 instrument ------

    def _load_instruments(self, symbols: list[str]) -> list[CryptoPerpetual]:
        """从 catalog 加载 instrument 定义.

        Args:
            symbols: 交易对名称列表，如 ["BTCUSDT"]。

        Returns:
            对应的 CryptoPerpetual 列表。

        Raises:
            ValueError: 某 symbol 在 catalog 中不存在。
        """
        all_instruments: list[CryptoPerpetual] = self._catalog.instruments()
        instrument_map = {inst.raw_symbol.value: inst for inst in all_instruments}

        result = []
        for symbol in symbols:
            inst = instrument_map.get(symbol)
            if inst is None:
                raise ValueError(
                    f"Instrument '{symbol}' not found in catalog. "
                    f"Available: {list(instrument_map.keys())}"
                )
            result.append(inst)

        return result

    # ------ 加载 Bar 数据 ------

    def _add_bar_data(self, engine: BacktestEngine, instruments: list[CryptoPerpetual]) -> int:
        """从 catalog 加载指定时间范围的 Bar 数据并添加到引擎.

        Args:
            engine: 已初始化的 BacktestEngine。
            instruments: 要加载数据的 instrument 列表。

        Returns:
            成功加载的 Bar 总条数。
        """
        bt = self._bt_cfg

        start_ns = self._date_to_ns(bt.start)
        end_ns = self._date_to_ns(bt.end, end_of_day=True)

        total = 0
        for inst in instruments:
            source_interval = Interval.MINUTE_1 if bt.interval != Interval.MINUTE_1 else bt.interval
            source_nautilus_interval = INTERVAL_TO_NAUTILUS[source_interval]
            bar_type_str = f"{inst.id}-{source_nautilus_interval}-LAST-EXTERNAL"
            bars = self._catalog.bars(
                bar_types=[bar_type_str],
                start=start_ns,
                end=end_ns,
            )
            if not bars:
                logger.warning("no_bar_data", symbol=inst.raw_symbol.value, bar_type=bar_type_str)
                continue

            engine.add_data(bars)
            total += len(bars)
            event = "bar_data_loaded"
            if bt.interval != Interval.MINUTE_1:
                event = "bar_data_loaded_for_internal_aggregation"
            logger.info(
                event,
                symbol=inst.raw_symbol.value,
                source_bar_type=bar_type_str,
                target_interval=bt.interval.value,
                count=len(bars),
            )

        return total

    # ------ 收集报告 ------

    def _collect_reports(self, engine: BacktestEngine) -> dict[str, Any]:
        """收集回测报告（订单、成交、仓位、账户）.

        Args:
            engine: 回测完成后的 BacktestEngine 实例。

        Returns:
            包含各类报告 DataFrame 的字典，键为报告名称。
        """
        trader = engine.trader
        reports: dict[str, Any] = {}

        try:
            reports["orders"] = trader.generate_orders_report()
        except Exception:
            logger.warning("report_orders_failed")

        try:
            reports["order_fills"] = trader.generate_order_fills_report()
        except Exception:
            logger.warning("report_order_fills_failed")

        try:
            reports["positions"] = trader.generate_positions_report()
        except Exception:
            logger.warning("report_positions_failed")

        try:
            reports["account"] = trader.generate_account_report(Venue(self.VENUE))
        except Exception:
            logger.warning("report_account_failed")

        return reports

    def _build_analysis(self, reports: dict[str, Any], result: Any) -> dict[str, Any]:
        analysis: dict[str, Any] = {}

        pnl_stats: dict[str, Any] | None = None
        if isinstance(result.stats_pnls, dict):
            raw_pnl_stats = result.stats_pnls.get("USDT")
            if isinstance(raw_pnl_stats, dict):
                pnl_stats = raw_pnl_stats

        cost_analyzer = BacktestCostAnalyzer(
            execution_config=self._app_cfg.execution,
            raw_dir=self._app_cfg.data.raw_dir,
            features_dir=self._app_cfg.data.features_dir,
        )
        cost_analysis = cost_analyzer.analyze(
            reports=reports,
            starting_balance=self._bt_cfg.starting_balance_usdt,
            pnl_stats=pnl_stats,
        )
        if cost_analysis is not None:
            analysis["costs"] = cost_analysis.to_dict()

        return analysis

    # ------ 工具方法 ------

    @staticmethod
    def _to_datetime(date: dt.date, end_of_day: bool = False) -> dt.datetime:
        """将 dt.date 转换为 UTC datetime.

        Args:
            date: 目标日期。
            end_of_day: 若 True，时间设为当天 23:59:59；否则为 00:00:00。

        Returns:
            对应的 UTC datetime 对象。
        """
        if end_of_day:
            return dt.datetime.combine(date, dt.time(23, 59, 59), tzinfo=dt.UTC)
        return dt.datetime.combine(date, dt.time.min, tzinfo=dt.UTC)

    @staticmethod
    def _date_to_ns(date: dt.date, end_of_day: bool = False) -> int:
        """将 dt.date 转换为纳秒时间戳.

        Args:
            date: 目标日期。
            end_of_day: 若 True，时间设为当天末尾。

        Returns:
            Unix 纳秒时间戳（int）。
        """
        d = BacktestRunner._to_datetime(date, end_of_day)
        return int(d.timestamp() * 1_000_000_000)


# ---------------------------------------------------------------------------
# 结果封装
# ---------------------------------------------------------------------------


@dataclass
class BacktestRunResult:
    """回测结果封装.

    Attributes:
        result: Nautilus 原始 BacktestResult，含 stats_pnls / stats_returns 等统计。
        reports: 各类报告 DataFrame，键: orders / order_fills / positions / account。
        config: 本次回测的参数配置。
    """

    result: Any  # nautilus_trader.backtest.results.BacktestResult
    reports: dict[str, Any]
    config: BacktestConfig
    analysis: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
