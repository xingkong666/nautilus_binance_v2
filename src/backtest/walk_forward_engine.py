"""Walk-Forward 验证引擎.

将原 scripts/run_portfolio_walkforward.py 的核心逻辑封装为可测试、可复用的类。

典型用法::

    engine = WalkForwardEngine(
        app_config=app_config,
        factory=factory,
        portfolio_config=portfolio_cfg,
    )
    result = engine.run(output_dir=Path("experiments/walkforward/my_portfolio"))
    print(result.stability.passed, result.stability.consistency_rate)
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd
import structlog

from src.backtest.regime import SymbolRegimeSnapshot, evaluate_symbol_regime, regime_allows_strategy
from src.backtest.report import BacktestReporter
from src.backtest.runner import BacktestConfig, BacktestRunner
from src.backtest.walkforward import (
    WalkforwardWindow,
    combine_risk_score_weight,
    flatten_summary,
    generate_walkforward_windows,
    meets_min_active_strategies,
    resolve_min_active_strategies,
    scale_sizing_params,
    selection_passes,
    stitch_equity_curves,
)
from src.core.enums import Interval
from src.portfolio.allocator import PortfolioAllocator

if TYPE_CHECKING:
    from src.app.factory import AppFactory
    from src.core.config import AppConfig

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass
class WindowResult:
    """单个 WF 窗口的 IS/OOS 回测结果.

    Attributes:
        window: Walk-forward 窗口定义（时间段）。
        train_summary: IS 期回测摘要字典。
        test_summary: OOS 期回测摘要字典。
        train_equity_curve: IS 期权益曲线 DataFrame。
        test_equity_curve: OOS 期权益曲线 DataFrame。
        active_strategy_count: 本窗口参与 OOS 的策略数量。
        allocation_map: 策略 ID → 分配比例映射。
        selection_rows: 候选参数评估明细（用于 CSV 输出）。
        allocation_rows: 分配决策明细（用于 CSV 输出）。
        regime_rows: 市场状态评估明细（用于 CSV 输出）。

    """

    window: WalkforwardWindow
    train_summary: dict[str, Any]
    test_summary: dict[str, Any]
    train_equity_curve: pd.DataFrame
    test_equity_curve: pd.DataFrame
    active_strategy_count: int
    allocation_map: dict[str, float]
    selection_rows: list[dict[str, Any]] = field(default_factory=list)
    allocation_rows: list[dict[str, Any]] = field(default_factory=list)
    regime_rows: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class StabilityReport:
    """IS/OOS 一致性分析报告（过拟合检测）.

    Attributes:
        window_count: 总窗口数。
        is_oos_correlation: IS PnL% 与 OOS PnL% 的 Pearson 相关系数，[-1, 1]。
            正值表示 IS 好的窗口 OOS 也倾向于好（稳定信号）。
        consistency_rate: OOS PnL% > 0 的窗口占比，[0, 1]。
            0.5 表示半数以上窗口 OOS 盈利。
        overfitting_score: 1 - consistency_rate，越低越好。
        mean_is_pnl_pct: IS 期平均收益率（%）。
        mean_oos_pnl_pct: OOS 期平均收益率（%）。
        degradation_ratio: OOS均值 / IS均值，理想值接近 1.0。
            负值或 0 表示严重过拟合。
        passed: True 表示 consistency_rate >= threshold，认为策略通过稳定性检验。

    """

    window_count: int
    is_oos_correlation: float
    consistency_rate: float
    overfitting_score: float
    mean_is_pnl_pct: float
    mean_oos_pnl_pct: float
    degradation_ratio: float
    passed: bool


@dataclass
class WalkForwardResult:
    """Walk-forward 验证完整结果.

    Attributes:
        portfolio_name: 组合名称。
        windows: 每个窗口的 IS/OOS 结果列表。
        stability: IS/OOS 一致性稳定性报告。
        aggregate: 汇总指标字典（与 walkforward_aggregate.json 内容一致）。
        stitched_test_equity: 拼接后的样本外权益曲线 DataFrame。

    """

    portfolio_name: str
    windows: list[WindowResult]
    stability: StabilityReport
    aggregate: dict[str, Any]
    stitched_test_equity: pd.DataFrame


# ---------------------------------------------------------------------------
# 内部辅助函数（从脚本迁移，保持原有逻辑不变）
# ---------------------------------------------------------------------------


def _strategy_id(entry: dict[str, Any]) -> str:
    explicit = entry.get("strategy_id")
    if explicit:
        return str(explicit)
    return f"{entry['name']}:{entry['symbol']}"


def _candidate_param_sets(entry: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    base_params = dict(entry.get("params", {}))
    candidates = entry.get("param_candidates", [])
    if not candidates:
        return [("base", base_params)]
    result: list[tuple[str, dict[str, Any]]] = []
    for index, candidate in enumerate(candidates, start=1):
        merged = dict(base_params)
        merged.update(candidate.get("params", {}))
        candidate_id = str(candidate.get("id", f"candidate_{index:02d}"))
        result.append((candidate_id, merged))
    return result


def _coerce_metric(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float("-inf")
    if number != number:  # 南检查
        return float("-inf")
    return number


def _summary_metric(summary: dict[str, Any], metric_name: str) -> float:
    costs = summary.get("analysis", {}).get("costs", {})
    pnl = summary.get("pnl", {}).get("USDT", {})
    returns = summary.get("returns", {})

    for space in (costs, pnl, returns):
        if metric_name in space:
            return _coerce_metric(space[metric_name])

    aliases = {
        "pnl_pct_after_costs": costs.get("pnl_pct_after_costs"),
        "pnl_after_costs": costs.get("pnl_after_costs"),
        "pnl_pct": pnl.get("PnL% (total)"),
        "pnl_total": pnl.get("PnL (total)"),
        "profit_factor": returns.get("Profit Factor"),
        "sharpe": returns.get("Sharpe Ratio (252 days)"),
        "sortino": returns.get("Sortino Ratio (252 days)"),
    }
    return _coerce_metric(aliases.get(metric_name))


def _estimate_strategy_volatility(summary: dict[str, Any]) -> float:
    returns = summary.get("returns", {})
    raw = returns.get("Returns Volatility (252 days)", 1.0)
    value = _coerce_metric(raw)
    return value if value > 0 else 1.0


def _save_equity_curve(
    account_report: Any,
    output_path: Path,
    *,
    phase: str,
    window_index: int,
) -> pd.DataFrame:
    if not isinstance(account_report, pd.DataFrame) or account_report.empty or "total" not in account_report.columns:
        return pd.DataFrame(columns=["phase", "window_index", "step", "timestamp", "equity"])

    curve = pd.DataFrame(
        {
            "phase": phase,
            "window_index": window_index,
            "step": range(len(account_report)),
            "equity": pd.to_numeric(account_report["total"], errors="coerce"),
        }
    )
    if not isinstance(account_report.index, pd.RangeIndex):
        curve["timestamp"] = pd.to_datetime(account_report.index, utc=True, errors="coerce")
    elif "ts_event" in account_report.columns:
        curve["timestamp"] = pd.to_datetime(account_report["ts_event"], utc=True, errors="coerce")
    else:
        curve["timestamp"] = pd.NaT

    curve = curve.dropna(subset=["equity"]).reset_index(drop=True)
    if curve.empty:
        return curve

    curve["equity_norm"] = curve["equity"] / float(curve["equity"].iloc[0])
    curve.to_csv(output_path, index=False)
    return curve


def _build_flat_equity_curve(
    *,
    phase: str,
    window_index: int,
    start: dt.date,
    balance: int,
) -> pd.DataFrame:
    timestamp = pd.Timestamp(start.isoformat(), tz="UTC")
    return pd.DataFrame(
        [
            {
                "phase": phase,
                "window_index": window_index,
                "step": 0,
                "timestamp": timestamp,
                "equity": float(balance),
                "equity_norm": 1.0,
            }
        ]
    )


def _build_flat_summary(
    *,
    start: dt.date,
    end: dt.date,
    symbols: list[str],
    interval: Interval,
    strategy_names: list[str],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    return {
        "period": f"{start.isoformat()} ~ {end.isoformat()}",
        "symbols": symbols,
        "interval": interval.value,
        "total_orders": 0,
        "total_positions": 0,
        "pnl": {"USDT": {"PnL (total)": 0.0, "PnL% (total)": 0.0, "Win Rate": 0.0}},
        "returns": {
            "Profit Factor": 0.0,
            "Sharpe Ratio (252 days)": 0.0,
            "Sortino Ratio (252 days)": 0.0,
        },
        "analysis": {
            "costs": {
                "pnl_after_costs": 0.0,
                "pnl_pct_after_costs": 0.0,
                "modeled_slippage_cost": 0.0,
                "funding_cost": 0.0,
            }
        },
        "metadata": metadata | {"strategy_names": strategy_names},
    }


def _regime_snapshot_to_row(*, window_index: int, snapshot: SymbolRegimeSnapshot) -> dict[str, Any]:
    return {
        "window_index": window_index,
        "symbol": snapshot.symbol,
        "slope_ratio": snapshot.slope_ratio,
        "ema_gap_ratio": snapshot.ema_gap_ratio,
        "adx": snapshot.adx,
        "funding_mean": snapshot.funding_mean,
        "funding_abs_mean": snapshot.funding_abs_mean,
        "weak_trend": snapshot.weak_trend,
        "overheated": snapshot.overheated,
        "regime_pass": snapshot.regime_pass,
        "reason": snapshot.reason,
    }


def _regime_veto_symbols(
    *,
    symbol_regimes: dict[str, SymbolRegimeSnapshot],
    veto_strategy_names: list[str],
) -> str:
    if not veto_strategy_names:
        return ",".join(sorted(s.symbol for s in symbol_regimes.values() if not s.regime_pass))
    return ",".join(
        sorted(
            s.symbol
            for s in symbol_regimes.values()
            if any(
                not regime_allows_strategy(strategy_name=name, snapshot=s, veto_strategy_names=veto_strategy_names)
                for name in veto_strategy_names
            )
        )
    )


# ---------------------------------------------------------------------------
# 滚动前向引擎
# ---------------------------------------------------------------------------


class WalkForwardEngine:
    """Walk-forward 验证引擎.

    将 IS（训练）→ OOS（测试）的滑动窗口流程封装为可复用类，
    支持稳定性评分和可选的多进程并行执行。

    Args:
        app_config: 应用配置（含数据目录等路径）。
        factory: 策略工厂（用于构建策略实例）。
        portfolio_config: 组合 YAML 中的 portfolio 节点字典。
        parallel: True 时用 ProcessPoolExecutor 并行跑各窗口（默认 False）。
        max_workers: 并行进程数上限；None 时使用 CPU 核数。
        stability_threshold: consistency_rate 阈值，超过则 StabilityReport.passed=True（默认 0.5）。

    Example::

        engine = WalkForwardEngine(app_config, factory, portfolio_cfg)
        result = engine.run(output_dir=Path("experiments/walkforward/my_portfolio"))
        if result.stability.passed:
            print("策略通过稳定性检验")

    """

    def __init__(
        self,
        app_config: AppConfig,
        factory: AppFactory,
        portfolio_config: dict[str, Any],
        *,
        parallel: bool = False,
        max_workers: int | None = None,
        stability_threshold: float = 0.5,
    ) -> None:
        """初始化 WalkForwardEngine."""
        self._app_config = app_config
        self._factory = factory
        self._cfg = portfolio_config
        self._parallel = parallel
        self._max_workers = max_workers
        self._stability_threshold = stability_threshold

        # 解析组合级配置
        backtest_cfg = self._cfg.get("backtest", {})
        self._interval = Interval(str(backtest_cfg.get("interval", "1h")))
        self._balance = int(backtest_cfg.get("starting_balance_usdt", 10_000))
        self._leverage = float(backtest_cfg.get("leverage", 10.0))
        self._allocation_cfg: dict[str, Any] = self._cfg.get("allocation", {})
        self._strategy_entries: list[dict[str, Any]] = self._cfg.get("strategies", [])
        self._portfolio_name = str(self._cfg.get("name", "portfolio"))

        walk_cfg = self._cfg.get("walkforward", {})
        self._walk_cfg = walk_cfg
        self._selection_metric = str(walk_cfg.get("selection_metric", "pnl_pct_after_costs"))
        raw_min_score = walk_cfg.get("selection_min_score")
        self._selection_min_score: float | None = float(raw_min_score) if raw_min_score is not None else None
        raw_min_active = walk_cfg.get("min_active_strategies")
        self._min_active_strategies: int | None = int(raw_min_active) if raw_min_active is not None else None
        self._regime_filter_cfg: dict[str, Any] = walk_cfg.get("regime_filter", {})

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def generate_windows(self) -> list[WalkforwardWindow]:
        """生成滑动验证窗口列表（不执行回测）."""
        walk_cfg = self._walk_cfg
        start = dt.date.fromisoformat(str(walk_cfg["start"]))
        end = dt.date.fromisoformat(str(walk_cfg["end"]))
        return generate_walkforward_windows(
            start=start,
            end=end,
            train_months=int(walk_cfg.get("train_months", 6)),
            test_months=int(walk_cfg.get("test_months", 3)),
            step_months=int(walk_cfg.get("step_months", 3)),
        )

    def run(
        self,
        output_dir: Path | None = None,
        *,
        selection_min_score: float | None = None,
    ) -> WalkForwardResult:
        """执行完整 walk-forward 验证流程.

        Args:
            output_dir: 结果保存目录；None 时使用 experiments/walkforward/<portfolio_name>。
            selection_min_score: 覆盖 YAML 中的 selection_min_score（可选）。

        Returns:
            WalkForwardResult，含逐窗口结果、稳定性报告和汇总指标。

        Raises:
            ValueError: 策略为空或窗口生成失败。

        """
        if not self._strategy_entries:
            raise ValueError("没有启用的策略，无法执行 walk-forward")

        windows = self.generate_windows()
        if not windows:
            raise ValueError("未生成任何 walk-forward 窗口，请检查起止日期和窗口参数")

        effective_min_score = selection_min_score if selection_min_score is not None else self._selection_min_score

        if output_dir is None:
            output_dir = Path("experiments/walkforward") / self._portfolio_name
        output_dir.mkdir(parents=True, exist_ok=True)

        logger.info(
            "walkforward_start",
            portfolio=self._portfolio_name,
            windows=len(windows),
            parallel=self._parallel,
            selection_min_score=effective_min_score,
        )

        if self._parallel and len(windows) > 1:
            window_results = self._run_windows_parallel(windows, output_dir, effective_min_score)
        else:
            window_results = self._run_windows_serial(windows, output_dir, effective_min_score)

        stability = self._compute_stability(window_results)
        aggregate = self._build_aggregate(window_results, effective_min_score)
        stitched = self._build_stitched_equity(window_results)

        result = WalkForwardResult(
            portfolio_name=self._portfolio_name,
            windows=window_results,
            stability=stability,
            aggregate=aggregate,
            stitched_test_equity=stitched,
        )

        self._save_outputs(result, output_dir)

        logger.info(
            "walkforward_done",
            portfolio=self._portfolio_name,
            windows=len(window_results),
            consistency_rate=round(stability.consistency_rate, 3),
            overfitting_score=round(stability.overfitting_score, 3),
            passed=stability.passed,
        )

        return result

    # ------------------------------------------------------------------
    # 窗口执行
    # ------------------------------------------------------------------

    def _run_windows_serial(
        self,
        windows: list[WalkforwardWindow],
        output_dir: Path,
        selection_min_score: float | None,
    ) -> list[WindowResult]:
        results = []
        for window in windows:
            window_dir = output_dir / f"window_{window.index:02d}"
            window_dir.mkdir(parents=True, exist_ok=True)
            try:
                wr = self._run_window(window, window_dir, selection_min_score)
                results.append(wr)
            except Exception as exc:
                logger.error("walkforward_window_failed", window=window.index, error=str(exc), exc_info=True)
        return results

    def _run_windows_parallel(
        self,
        windows: list[WalkforwardWindow],
        output_dir: Path,
        selection_min_score: float | None,
    ) -> list[WindowResult]:
        """多进程并行执行各窗口（每窗口独立进程，异常隔离）."""
        results: list[WindowResult | None] = [None] * len(windows)

        try:
            with concurrent.futures.ProcessPoolExecutor(max_workers=self._max_workers) as executor:
                futures = {
                    executor.submit(
                        _run_window_worker,
                        self._app_config,
                        self._factory,
                        self._cfg,
                        window,
                        output_dir / f"window_{window.index:02d}",
                        selection_min_score,
                        self._selection_metric,
                        self._min_active_strategies,
                        self._regime_filter_cfg,
                        self._interval,
                        self._balance,
                        self._leverage,
                        self._allocation_cfg,
                        self._strategy_entries,
                    ): i
                    for i, window in enumerate(windows)
                }
                for future in concurrent.futures.as_completed(futures):
                    idx = futures[future]
                    try:
                        results[idx] = future.result()
                    except Exception as exc:
                        logger.error(
                            "walkforward_parallel_window_failed",
                            window=windows[idx].index,
                            error=str(exc),
                            exc_info=True,
                        )
        except Exception as exc:
            logger.warning(
                "walkforward_parallel_failed_falling_back_to_serial",
                error=str(exc),
            )
            return self._run_windows_serial(windows, output_dir, selection_min_score)

        return [r for r in results if r is not None]

    def _run_window(
        self,
        window: WalkforwardWindow,
        window_dir: Path,
        selection_min_score: float | None,
    ) -> WindowResult:
        """执行单个 WF 窗口的 IS 评估 → 分配决策 → OOS 回测."""
        window_dir.mkdir(parents=True, exist_ok=True)

        metadata_base = {
            "portfolio_name": self._portfolio_name,
            "window_index": window.index,
            "selection_min_score": selection_min_score,
            "min_active_strategies": self._min_active_strategies,
        }

        # --- 市场状态过滤 ---
        symbol_regimes: dict[str, SymbolRegimeSnapshot] = {}
        regime_rows: list[dict[str, Any]] = []
        veto_strategy_names = [str(n) for n in self._regime_filter_cfg.get("veto_strategy_names", [])]
        min_active_on_veto_raw = self._regime_filter_cfg.get("min_active_strategies_on_regime_veto")
        min_active_on_veto: int | None = int(min_active_on_veto_raw) if min_active_on_veto_raw is not None else None

        if self._regime_filter_cfg.get("enabled", False):
            from nautilus_trader.persistence.catalog import ParquetDataCatalog

            catalog = ParquetDataCatalog(self._app_config.data.catalog_dir)
            instrument_map = {inst.raw_symbol.value: inst for inst in catalog.instruments()}

            enabled_symbols = sorted({str(e["symbol"]) for e in self._strategy_entries if bool(e.get("enabled", True))})
            for symbol in enabled_symbols:
                instrument = instrument_map[symbol]
                snapshot = evaluate_symbol_regime(
                    catalog=catalog,
                    features_dir=Path(self._app_config.data.features_dir),
                    raw_dir=Path(self._app_config.data.raw_dir),
                    instrument_id=instrument.id,
                    symbol=symbol,
                    start=window.train_start,
                    end=window.train_end,
                    interval=self._interval,
                    config=self._regime_filter_cfg,
                )
                symbol_regimes[symbol] = snapshot
                regime_rows.append(_regime_snapshot_to_row(window_index=window.index, snapshot=snapshot))

        # --- 样本内策略评估 ---
        evaluated_rows: list[dict[str, Any]] = []
        active_entries: list[dict[str, Any]] = []
        volatility_map: dict[str, float] = {}
        score_map: dict[str, float] = {}
        selection_rows: list[dict[str, Any]] = []
        regime_veto_count = 0

        for entry in self._strategy_entries:
            if not bool(entry.get("enabled", True)):
                continue

            evaluated = _evaluate_strategy_entry(
                app_config=self._app_config,
                factory=self._factory,
                entry=entry,
                interval=self._interval,
                start=window.train_start,
                end=window.train_end,
                balance=self._balance,
                leverage=self._leverage,
                output_dir=window_dir,
                metadata=metadata_base,
                selection_metric=self._selection_metric,
                selection_min_score=selection_min_score,
            )

            evaluated_entry = {
                **entry,
                "params": evaluated["selected_params"],
                "regime_passed": evaluated["regime_passed"],
                "score": evaluated["score"],
                "volatility": evaluated["volatility"],
            }

            symbol_regime = symbol_regimes.get(str(entry["symbol"]))
            symbol_regime_pass = symbol_regime.regime_pass if symbol_regime is not None else True
            symbol_regime_reason = symbol_regime.reason if symbol_regime is not None else "disabled"
            strategy_regime_pass = regime_allows_strategy(
                strategy_name=str(entry["name"]),
                snapshot=symbol_regime,
                veto_strategy_names=veto_strategy_names,
            )
            evaluated_rows.append(evaluated_entry)
            selection_rows.append(
                {
                    "window_index": window.index,
                    "strategy_id": evaluated["strategy_id"],
                    "symbol": evaluated["symbol"],
                    "selected_candidate_id": evaluated["selected_candidate_id"],
                    "selection_metric": self._selection_metric,
                    "selection_min_score": selection_min_score,
                    "score": evaluated["score"],
                    "volatility": evaluated["volatility"],
                    "regime_passed": evaluated["regime_passed"],
                    "symbol_regime_pass": symbol_regime_pass,
                    "strategy_regime_pass": strategy_regime_pass,
                    "symbol_regime_reason": symbol_regime_reason,
                    "params_json": json.dumps(evaluated["selected_params"], ensure_ascii=False, sort_keys=True),
                }
            )

            if symbol_regime is not None and not strategy_regime_pass:
                regime_veto_count += 1

            if bool(evaluated["regime_passed"]) and strategy_regime_pass:
                active_entries.append(evaluated_entry)
                strategy_id = evaluated["strategy_id"]
                volatility_map[strategy_id] = float(evaluated["volatility"])
                score_map[strategy_id] = float(evaluated["score"])

        effective_min_active = resolve_min_active_strategies(
            min_active_strategies=self._min_active_strategies,
            min_active_strategies_on_regime_veto=min_active_on_veto,
            regime_veto_count=regime_veto_count,
        )
        if not meets_min_active_strategies(len(active_entries), effective_min_active):
            active_entries = []
            volatility_map = {}
            score_map = {}

        allocation_map, effective_weights, allocation_mode_used = _build_allocation_map(
            allocation_cfg=self._allocation_cfg,
            active_entries=active_entries,
            volatility_map=volatility_map,
            score_map=score_map,
            total_capital=self._balance,
        )

        # 记录分配明细
        allocation_rows: list[dict[str, Any]] = []
        for entry in evaluated_rows:
            s_id = _strategy_id(entry)
            sym_regime = symbol_regimes.get(str(entry["symbol"]))
            allocation_rows.append(
                {
                    "window_index": window.index,
                    "strategy_id": s_id,
                    "symbol": entry["symbol"],
                    "score": entry["score"],
                    "volatility": entry["volatility"],
                    "regime_passed": entry["regime_passed"],
                    "symbol_regime_pass": sym_regime.regime_pass if sym_regime is not None else True,
                    "strategy_regime_pass": regime_allows_strategy(
                        strategy_name=str(entry["name"]),
                        snapshot=sym_regime,
                        veto_strategy_names=veto_strategy_names,
                    ),
                    "symbol_regime_reason": sym_regime.reason if sym_regime is not None else "disabled",
                    "active_after_min_gate": s_id in allocation_map,
                    "effective_weight": effective_weights.get(s_id, 0.0),
                    "allocation_mode_used": allocation_mode_used,
                    "allocation_pct": allocation_map.get(s_id, 0.0),
                    "effective_min_active_strategies": effective_min_active,
                    "regime_veto_count": regime_veto_count,
                }
            )

        # --- 样本内 + 样本外回测 ---
        scaled_specs, scaled_symbols, strategy_ids = _build_strategy_specs(
            factory=self._factory,
            strategy_entries=active_entries,
            interval=self._interval,
            allocation_map=allocation_map,
        )
        strategy_names = [spec[0].__name__ for spec in scaled_specs]

        train_meta = metadata_base | {
            "phase": "train",
            "strategy_names": strategy_names,
            "strategy_ids": strategy_ids,
            "allocation_map": allocation_map,
            "active_strategy_count": len(active_entries),
            "allocation_mode_used": allocation_mode_used,
            "effective_min_active_strategies": effective_min_active,
            "regime_veto_count": regime_veto_count,
        }
        test_meta = metadata_base | {
            "phase": "test",
            "strategy_names": strategy_names,
            "strategy_ids": strategy_ids,
            "allocation_map": allocation_map,
            "active_strategy_count": len(active_entries),
            "allocation_mode_used": allocation_mode_used,
            "effective_min_active_strategies": effective_min_active,
            "regime_veto_count": regime_veto_count,
        }

        if scaled_specs:
            train_summary, train_curve = _run_phase(
                app_config=self._app_config,
                strategy_specs=scaled_specs,
                symbols=scaled_symbols,
                interval=self._interval,
                start=window.train_start,
                end=window.train_end,
                balance=self._balance,
                leverage=self._leverage,
                output_dir=window_dir,
                phase_name="train",
                metadata=train_meta,
            )
            test_summary, test_curve = _run_phase(
                app_config=self._app_config,
                strategy_specs=scaled_specs,
                symbols=scaled_symbols,
                interval=self._interval,
                start=window.test_start,
                end=window.test_end,
                balance=self._balance,
                leverage=self._leverage,
                output_dir=window_dir,
                phase_name="test",
                metadata=test_meta,
            )
        else:
            train_summary = _build_flat_summary(
                start=window.train_start,
                end=window.train_end,
                symbols=[],
                interval=self._interval,
                strategy_names=[],
                metadata=train_meta,
            )
            test_summary = _build_flat_summary(
                start=window.test_start,
                end=window.test_end,
                symbols=[],
                interval=self._interval,
                strategy_names=[],
                metadata=test_meta,
            )
            train_curve = _build_flat_equity_curve(
                phase="train",
                window_index=window.index,
                start=window.train_start,
                balance=self._balance,
            )
            test_curve = _build_flat_equity_curve(
                phase="test",
                window_index=window.index,
                start=window.test_start,
                balance=self._balance,
            )

        train_curve.to_csv(window_dir / "train_equity_curve.csv", index=False)
        test_curve.to_csv(window_dir / "test_equity_curve.csv", index=False)

        return WindowResult(
            window=window,
            train_summary=train_summary,
            test_summary=test_summary,
            train_equity_curve=train_curve,
            test_equity_curve=test_curve,
            active_strategy_count=len(active_entries),
            allocation_map=allocation_map,
            selection_rows=selection_rows,
            allocation_rows=allocation_rows,
            regime_rows=regime_rows,
        )

    # ------------------------------------------------------------------
    # 稳定性评分
    # ------------------------------------------------------------------

    def _compute_stability(self, windows: list[WindowResult]) -> StabilityReport:
        """计算 IS/OOS 一致性稳定性评分."""
        if not windows:
            return StabilityReport(
                window_count=0,
                is_oos_correlation=0.0,
                consistency_rate=0.0,
                overfitting_score=1.0,
                mean_is_pnl_pct=0.0,
                mean_oos_pnl_pct=0.0,
                degradation_ratio=0.0,
                passed=False,
            )

        is_pnls = [float(w.train_summary.get("pnl", {}).get("USDT", {}).get("PnL% (total)", 0.0) or 0.0) for w in windows]
        oos_pnls = [float(w.test_summary.get("pnl", {}).get("USDT", {}).get("PnL% (total)", 0.0) or 0.0) for w in windows]

        n = len(windows)
        mean_is = sum(is_pnls) / n
        mean_oos = sum(oos_pnls) / n

        # Pearson相关（手算，避免 scipy依赖）
        correlation = 0.0
        if n >= 2:
            cov = sum((a - mean_is) * (b - mean_oos) for a, b in zip(is_pnls, oos_pnls, strict=True)) / n
            std_is = (sum((a - mean_is) ** 2 for a in is_pnls) / n) ** 0.5
            std_oos = (sum((b - mean_oos) ** 2 for b in oos_pnls) / n) ** 0.5
            if std_is > 0 and std_oos > 0:
                correlation = cov / (std_is * std_oos)
                correlation = max(-1.0, min(1.0, correlation))  # 数值夹紧

        consistency_rate = sum(1 for x in oos_pnls if x > 0) / n
        overfitting_score = 1.0 - consistency_rate

        degradation_ratio = 0.0
        if mean_is != 0:
            degradation_ratio = mean_oos / mean_is

        return StabilityReport(
            window_count=n,
            is_oos_correlation=round(correlation, 4),
            consistency_rate=round(consistency_rate, 4),
            overfitting_score=round(overfitting_score, 4),
            mean_is_pnl_pct=round(mean_is, 4),
            mean_oos_pnl_pct=round(mean_oos, 4),
            degradation_ratio=round(degradation_ratio, 4),
            passed=consistency_rate >= self._stability_threshold,
        )

    # ------------------------------------------------------------------
    # 汇总与输出
    # ------------------------------------------------------------------

    def _build_aggregate(
        self,
        windows: list[WindowResult],
        selection_min_score: float | None,
    ) -> dict[str, Any]:
        if not windows:
            return {}

        rows = []
        for wr in windows:
            train_row = flatten_summary(wr.train_summary, phase="train", window_index=wr.window.index)
            train_row.update(
                {
                    "selection_min_score": selection_min_score,
                    "active_strategy_count": wr.active_strategy_count,
                    "regime_veto_count": sum(1 for r in wr.allocation_rows if int(r.get("regime_veto_count", 0)) > 0),
                }
            )
            rows.append(train_row)

            test_row = flatten_summary(wr.test_summary, phase="test", window_index=wr.window.index)
            test_row.update(
                {
                    "selection_min_score": selection_min_score,
                    "active_strategy_count": wr.active_strategy_count,
                    "regime_veto_count": sum(1 for r in wr.allocation_rows if int(r.get("regime_veto_count", 0)) > 0),
                }
            )
            rows.append(test_row)

        df = pd.DataFrame(rows)

        def _safe_mean(series: pd.Series) -> float:
            try:
                return float(series.mean())
            except Exception:
                return 0.0

        aggregate: dict[str, Any] = {
            "portfolio_name": self._portfolio_name,
            "windows": len(windows),
            "selection_metric": self._selection_metric,
            "selection_min_score": selection_min_score,
            "min_active_strategies": self._min_active_strategies,
            "train_mean_pnl_pct": _safe_mean(df.loc[df["phase"] == "train", "pnl_pct"]),
            "test_mean_pnl_pct": _safe_mean(df.loc[df["phase"] == "test", "pnl_pct"]),
            "train_mean_pnl_pct_after_costs": _safe_mean(df.loc[df["phase"] == "train", "pnl_pct_after_costs"]),
            "test_mean_pnl_pct_after_costs": _safe_mean(df.loc[df["phase"] == "test", "pnl_pct_after_costs"]),
            "mean_active_strategy_count_test": _safe_mean(df.loc[df["phase"] == "test", "active_strategy_count"]),
        }
        return aggregate

    def _build_stitched_equity(self, windows: list[WindowResult]) -> pd.DataFrame:
        test_curves = [wr.test_equity_curve for wr in windows]
        return stitch_equity_curves(test_curves, starting_balance=self._balance)

    def _save_outputs(self, result: WalkForwardResult, output_dir: Path) -> None:
        """将 WalkForwardResult 保存为与原脚本相同的文件集合."""
        rows: list[dict[str, Any]] = []
        allocation_rows: list[dict[str, Any]] = []
        selection_rows: list[dict[str, Any]] = []
        regime_rows: list[dict[str, Any]] = []

        for wr in result.windows:
            train_row = flatten_summary(wr.train_summary, phase="train", window_index=wr.window.index)
            train_row["active_strategy_count"] = wr.active_strategy_count
            rows.append(train_row)

            test_row = flatten_summary(wr.test_summary, phase="test", window_index=wr.window.index)
            test_row["active_strategy_count"] = wr.active_strategy_count
            rows.append(test_row)

            allocation_rows.extend(wr.allocation_rows)
            selection_rows.extend(wr.selection_rows)
            regime_rows.extend(wr.regime_rows)

        pd.DataFrame(rows).to_csv(output_dir / "walkforward_summary.csv", index=False)
        pd.DataFrame(allocation_rows).to_csv(output_dir / "walkforward_allocations.csv", index=False)
        pd.DataFrame(selection_rows).to_csv(output_dir / "walkforward_selected_params.csv", index=False)
        pd.DataFrame(regime_rows).to_csv(output_dir / "walkforward_regimes.csv", index=False)

        if not result.stitched_test_equity.empty:
            result.stitched_test_equity.to_csv(output_dir / "walkforward_test_equity_curve.csv", index=False)
            result.aggregate["test_final_stitched_equity"] = float(result.stitched_test_equity["stitched_equity"].iloc[-1])

        # 稳定性报告
        stability_dict = {
            "window_count": result.stability.window_count,
            "is_oos_correlation": result.stability.is_oos_correlation,
            "consistency_rate": result.stability.consistency_rate,
            "overfitting_score": result.stability.overfitting_score,
            "mean_is_pnl_pct": result.stability.mean_is_pnl_pct,
            "mean_oos_pnl_pct": result.stability.mean_oos_pnl_pct,
            "degradation_ratio": result.stability.degradation_ratio,
            "passed": result.stability.passed,
        }
        result.aggregate["stability"] = stability_dict
        (output_dir / "walkforward_aggregate.json").write_text(json.dumps(result.aggregate, ensure_ascii=False, indent=2))
        (output_dir / "walkforward_stability.json").write_text(json.dumps(stability_dict, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# 并行工作进程入口（模块级函数，泡菜安全）
# ---------------------------------------------------------------------------


def _run_window_worker(
    app_config: Any,
    factory: Any,
    portfolio_config: dict[str, Any],
    window: WalkforwardWindow,
    window_dir: Path,
    selection_min_score: float | None,
    selection_metric: str,
    min_active_strategies: int | None,
    regime_filter_cfg: dict[str, Any],
    interval: Interval,
    balance: int,
    leverage: float,
    allocation_cfg: dict[str, Any],
    strategy_entries: list[dict[str, Any]],
) -> WindowResult:
    """ProcessPoolExecutor 子进程入口（单窗口）."""
    engine = WalkForwardEngine(
        app_config=app_config,
        factory=factory,
        portfolio_config=portfolio_config,
    )
    # 覆盖子进程内部参数以与调用方一致
    engine._selection_metric = selection_metric  # noqa: SLF001
    engine._min_active_strategies = min_active_strategies  # noqa: SLF001
    engine._regime_filter_cfg = regime_filter_cfg  # noqa: SLF001
    engine._interval = interval  # noqa: SLF001
    engine._balance = balance  # noqa: SLF001
    engine._leverage = leverage  # noqa: SLF001
    engine._allocation_cfg = allocation_cfg  # noqa: SLF001
    engine._strategy_entries = strategy_entries  # noqa: SLF001
    window_dir.mkdir(parents=True, exist_ok=True)
    return engine._run_window(window, window_dir, selection_min_score)  # noqa: SLF001


# ---------------------------------------------------------------------------
# 迁移自脚本的辅助函数（内部使用）
# ---------------------------------------------------------------------------


def _run_phase(
    app_config: Any,
    strategy_specs: list[tuple[type, Any]],
    symbols: list[str],
    interval: Interval,
    start: dt.date,
    end: dt.date,
    balance: int,
    leverage: float,
    output_dir: Path,
    phase_name: str,
    metadata: dict[str, Any],
) -> tuple[dict[str, Any], pd.DataFrame]:
    bt_config = BacktestConfig(
        start=start,
        end=end,
        symbols=symbols,
        interval=interval,
        starting_balance_usdt=balance,
        leverage=leverage,
    )
    runner = BacktestRunner(app_config=app_config, backtest_config=bt_config)
    run_result = runner.run_many(strategy_specs=strategy_specs, metadata=metadata)
    reporter = BacktestReporter(run_result)
    phase_dir = output_dir / phase_name
    reporter.save(phase_dir)
    equity_curve = _save_equity_curve(
        run_result.reports.get("account"),
        phase_dir / "equity_curve.csv",
        phase=str(metadata.get("phase", phase_name)),
        window_index=int(metadata.get("window_index", 0)),
    )
    return reporter.summary(), equity_curve


def _evaluate_strategy_entry(
    app_config: Any,
    factory: Any,
    entry: dict[str, Any],
    interval: Interval,
    start: dt.date,
    end: dt.date,
    balance: int,
    leverage: float,
    output_dir: Path,
    metadata: dict[str, Any],
    selection_metric: str,
    selection_min_score: float | None,
) -> dict[str, Any]:
    strategy_id = _strategy_id(entry)
    symbol = str(entry["symbol"])
    best: dict[str, Any] | None = None
    candidate_rows: list[dict[str, Any]] = []

    for candidate_id, params in _candidate_param_sets(entry):
        strategy_cfg = {"name": entry["name"], "params": params}
        strategy_specs = [factory.create_strategy_from_config(strategy_cfg, symbol=symbol, interval=interval)]

        summary, _ = _run_phase(
            app_config=app_config,
            strategy_specs=strategy_specs,
            symbols=[symbol],
            interval=interval,
            start=start,
            end=end,
            balance=balance,
            leverage=leverage,
            output_dir=output_dir,
            phase_name=f"train_{strategy_id}_{candidate_id}",
            metadata=metadata
            | {
                "phase": "train_single",
                "strategy_id": strategy_id,
                "candidate_id": candidate_id,
            },
        )
        score = _summary_metric(summary, selection_metric)
        candidate_rows.append(
            {
                "strategy_id": strategy_id,
                "candidate_id": candidate_id,
                "symbol": symbol,
                "selection_metric": selection_metric,
                "score": score,
                "params_json": json.dumps(params, ensure_ascii=False, sort_keys=True),
            }
        )
        if best is None or score > float(best["score"]):
            best = {
                "candidate_id": candidate_id,
                "params": params,
                "summary": summary,
                "score": score,
            }

    if best is None:
        raise ValueError(f"策略 {strategy_id} 没有可用候选参数")

    pd.DataFrame(candidate_rows).to_csv(output_dir / f"candidate_scores_{strategy_id}.csv", index=False)
    best_score = float(best["score"])
    return {
        "strategy_id": strategy_id,
        "symbol": symbol,
        "selected_candidate_id": best["candidate_id"],
        "selected_params": best["params"],
        "selected_summary": best["summary"],
        "volatility": _estimate_strategy_volatility(best["summary"]),
        "score": best_score,
        "regime_passed": selection_passes(best_score, selection_min_score),
    }


def _build_strategy_specs(
    factory: Any,
    strategy_entries: list[dict[str, Any]],
    interval: Interval,
    allocation_map: dict[str, float] | None = None,
) -> tuple[list[tuple[type, Any]], list[str], list[str]]:
    strategy_specs: list[tuple[type, Any]] = []
    symbols: list[str] = []
    strategy_ids: list[str] = []

    for entry in strategy_entries:
        if not bool(entry.get("enabled", True)):
            continue
        symbol = str(entry["symbol"])
        strategy_id = _strategy_id(entry)
        allocation_pct = 1.0 if allocation_map is None else allocation_map.get(strategy_id, 0.0)
        strategy_cfg = {
            "name": entry["name"],
            "params": scale_sizing_params(dict(entry.get("params", {})), allocation_pct),
        }
        strategy_specs.append(factory.create_strategy_from_config(strategy_cfg, symbol=symbol, interval=interval))
        symbols.append(symbol)
        strategy_ids.append(strategy_id)

    return strategy_specs, list(dict.fromkeys(symbols)), strategy_ids


def _resolve_allocator_inputs(
    *,
    allocation_cfg: dict[str, Any],
    active_entries: list[dict[str, Any]],
    volatility_map: dict[str, float],
    score_map: dict[str, float],
) -> tuple[str, list[dict[str, Any]]]:
    base_mode = str(allocation_cfg.get("mode", "risk_parity"))
    weighting_cfg = allocation_cfg.get("score_weighting", {})
    weighting_method = str(weighting_cfg.get("method", "none"))

    if base_mode == "risk_parity" and weighting_method != "none":
        return "weight", [
            {
                "strategy_id": _strategy_id(entry),
                "enabled": bool(entry.get("enabled", True)),
                "weight": combine_risk_score_weight(
                    volatility=volatility_map.get(_strategy_id(entry), 1.0),
                    score=score_map.get(_strategy_id(entry), 0.0),
                    score_weighting_method=weighting_method,
                ),
                "max_allocation_pct": float(entry.get("max_allocation_pct", 0.0)),
            }
            for entry in active_entries
        ]

    if base_mode == "weight" and weighting_method != "none":
        return "weight", [
            {
                "strategy_id": _strategy_id(entry),
                "enabled": bool(entry.get("enabled", True)),
                "weight": float(entry.get("weight", 1.0))
                * combine_risk_score_weight(
                    volatility=1.0,
                    score=score_map.get(_strategy_id(entry), 0.0),
                    score_weighting_method=weighting_method,
                ),
                "max_allocation_pct": float(entry.get("max_allocation_pct", 0.0)),
            }
            for entry in active_entries
        ]

    return base_mode, [
        {
            "strategy_id": _strategy_id(entry),
            "enabled": bool(entry.get("enabled", True)),
            "weight": float(entry.get("weight", 1.0)),
            "max_allocation_pct": float(entry.get("max_allocation_pct", 0.0)),
        }
        for entry in active_entries
    ]


def _build_allocation_map(
    allocation_cfg: dict[str, Any],
    active_entries: list[dict[str, Any]],
    volatility_map: dict[str, float],
    score_map: dict[str, float],
    total_capital: int,
) -> tuple[dict[str, float], dict[str, float], str]:
    if not active_entries:
        return {}, {}, str(allocation_cfg.get("mode", "risk_parity"))

    allocator_mode, strategies = _resolve_allocator_inputs(
        allocation_cfg=allocation_cfg,
        active_entries=active_entries,
        volatility_map=volatility_map,
        score_map=score_map,
    )
    allocator = PortfolioAllocator(
        {
            "mode": allocator_mode,
            "reserve_pct": float(allocation_cfg.get("reserve_pct", 0.0)),
            "min_allocation": str(allocation_cfg.get("min_allocation", "100")),
            "strategies": strategies,
        }
    )
    if allocator_mode == "risk_parity":
        for strategy_id, volatility in volatility_map.items():
            if volatility > 0:
                allocator.update_volatility(strategy_id, volatility)

    allocations = allocator.allocate(Decimal(str(total_capital)))
    allocation_map = {s_id: result.allocation_pct for s_id, result in allocations.items()}
    effective_weights = {item["strategy_id"]: float(item.get("weight", 1.0)) for item in strategies}
    return allocation_map, effective_weights, allocator_mode
