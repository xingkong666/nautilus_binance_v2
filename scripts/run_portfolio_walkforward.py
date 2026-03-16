#!/usr/bin/env python3
"""运行组合策略的 walk-forward 验证."""
# ruff: noqa: E402

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
from nautilus_trader.persistence.catalog import ParquetDataCatalog

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.app.factory import AppFactory
from src.backtest.regime import SymbolRegimeSnapshot, evaluate_symbol_regime, regime_allows_strategy
from src.backtest.report import BacktestReporter
from src.backtest.runner import BacktestConfig, BacktestRunner
from src.backtest.walkforward import (
    combine_risk_score_weight,
    flatten_summary,
    generate_walkforward_windows,
    meets_min_active_strategies,
    resolve_min_active_strategies,
    scale_sizing_params,
    selection_passes,
    stitch_equity_curves,
)
from src.core.config import load_app_config, load_yaml
from src.core.enums import Interval
from src.core.logging import setup_logging
from src.portfolio.allocator import PortfolioAllocator


class _ContainerStub:
    def __init__(self, config: Any) -> None:
        self.config = config
        self.binance_adapter = None


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments.

    Returns:
        argparse.Namespace: Parsed command-line arguments.
    """
    parser = argparse.ArgumentParser(description="组合策略 walk-forward 验证")
    parser.add_argument(
        "--config",
        default="configs/strategies/vegas_ema_combo_multi.yaml",
        help="组合配置 YAML",
    )
    parser.add_argument("--env", default=None, help="环境配置，默认使用 .env 中 env")
    parser.add_argument(
        "--output-dir",
        default=None,
        help="输出目录，默认 experiments/walkforward/<portfolio_name>",
    )
    return parser.parse_args()


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


def _build_strategy_specs(
    factory: AppFactory,
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


def _coerce_metric(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float("-inf")
    if number != number:
        return float("-inf")
    return number


def _summary_metric(summary: dict[str, Any], metric_name: str) -> float:
    costs = summary.get("analysis", {}).get("costs", {})
    pnl = summary.get("pnl", {}).get("USDT", {})
    returns = summary.get("returns", {})

    if metric_name in costs:
        return _coerce_metric(costs.get(metric_name))
    if metric_name in pnl:
        return _coerce_metric(pnl.get(metric_name))
    if metric_name in returns:
        return _coerce_metric(returns.get(metric_name))

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
    if value <= 0:
        return 1.0
    return value


def _evaluate_strategy_entry(
    app_config: Any,
    factory: AppFactory,
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
    regime_passed = selection_passes(best_score, selection_min_score)
    return {
        "strategy_id": strategy_id,
        "symbol": symbol,
        "selected_candidate_id": best["candidate_id"],
        "selected_params": best["params"],
        "selected_summary": best["summary"],
        "volatility": _estimate_strategy_volatility(best["summary"]),
        "score": best_score,
        "regime_passed": regime_passed,
    }


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
        strategies = []
        for entry in active_entries:
            strategy_id = _strategy_id(entry)
            combined_weight = combine_risk_score_weight(
                volatility=volatility_map.get(strategy_id, 1.0),
                score=score_map.get(strategy_id, 0.0),
                score_weighting_method=weighting_method,
            )
            strategies.append(
                {
                    "strategy_id": strategy_id,
                    "enabled": bool(entry.get("enabled", True)),
                    "weight": combined_weight,
                    "max_allocation_pct": float(entry.get("max_allocation_pct", 0.0)),
                }
            )
        return "weight", strategies

    if base_mode == "weight" and weighting_method != "none":
        strategies = []
        for entry in active_entries:
            strategy_id = _strategy_id(entry)
            strategies.append(
                {
                    "strategy_id": strategy_id,
                    "enabled": bool(entry.get("enabled", True)),
                    "weight": float(entry.get("weight", 1.0))
                    * combine_risk_score_weight(
                        volatility=1.0,
                        score=score_map.get(strategy_id, 0.0),
                        score_weighting_method=weighting_method,
                    ),
                    "max_allocation_pct": float(entry.get("max_allocation_pct", 0.0)),
                }
            )
        return "weight", strategies

    strategies = [
        {
            "strategy_id": _strategy_id(entry),
            "enabled": bool(entry.get("enabled", True)),
            "weight": float(entry.get("weight", 1.0)),
            "max_allocation_pct": float(entry.get("max_allocation_pct", 0.0)),
        }
        for entry in active_entries
    ]
    return base_mode, strategies


def _build_allocation_map(
    allocation_cfg: dict[str, Any],
    selected_entries: list[dict[str, Any]],
    volatility_map: dict[str, float],
    score_map: dict[str, float],
    total_capital: int,
) -> tuple[dict[str, float], dict[str, float], str]:
    if not selected_entries:
        return {}, {}, str(allocation_cfg.get("mode", "risk_parity"))

    allocator_mode, strategies = _resolve_allocator_inputs(
        allocation_cfg=allocation_cfg,
        active_entries=selected_entries,
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
    allocation_map = {strategy_id: result.allocation_pct for strategy_id, result in allocations.items()}
    effective_weights = {item["strategy_id"]: float(item.get("weight", 1.0)) for item in strategies}
    return allocation_map, effective_weights, allocator_mode


def _format_score_label(value: float | None) -> str:
    if value is None:
        return "none"
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text.replace("-", "m").replace(".", "p")


def _regime_snapshot_to_row(
    *,
    window_index: int,
    snapshot: SymbolRegimeSnapshot,
) -> dict[str, Any]:
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
        return ",".join(sorted(snapshot.symbol for snapshot in symbol_regimes.values() if not snapshot.regime_pass))
    return ",".join(
        sorted(
            snapshot.symbol
            for snapshot in symbol_regimes.values()
            if any(
                not regime_allows_strategy(
                    strategy_name=strategy_name,
                    snapshot=snapshot,
                    veto_strategy_names=veto_strategy_names,
                )
                for strategy_name in veto_strategy_names
            )
        )
    )


def _run_walkforward_scenario(
    *,
    app_config: Any,
    factory: AppFactory,
    catalog: ParquetDataCatalog,
    instrument_map: dict[str, Any],
    portfolio_name: str,
    config_path: Path,
    strategy_entries: list[dict[str, Any]],
    allocation_cfg: dict[str, Any],
    interval: Interval,
    balance: int,
    leverage: float,
    windows: list[Any],
    selection_metric: str,
    selection_min_score: float | None,
    min_active_strategies: int | None,
    regime_filter_cfg: dict[str, Any],
    output_dir: Path,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    allocation_rows: list[dict[str, Any]] = []
    selection_rows: list[dict[str, Any]] = []
    regime_rows: list[dict[str, Any]] = []
    stitched_test_curves: list[pd.DataFrame] = []
    veto_strategy_names = [str(name) for name in regime_filter_cfg.get("veto_strategy_names", [])]
    min_active_strategies_on_regime_veto_raw = regime_filter_cfg.get("min_active_strategies_on_regime_veto")
    min_active_strategies_on_regime_veto = (
        int(min_active_strategies_on_regime_veto_raw) if min_active_strategies_on_regime_veto_raw is not None else None
    )

    for window in windows:
        window_dir = output_dir / f"window_{window.index:02d}"
        window_dir.mkdir(parents=True, exist_ok=True)
        metadata = {
            "portfolio_name": portfolio_name,
            "window_index": window.index,
            "config_path": str(config_path),
            "selection_min_score": selection_min_score,
            "min_active_strategies": min_active_strategies,
        }

        evaluated_rows: list[dict[str, Any]] = []
        active_entries: list[dict[str, Any]] = []
        volatility_map: dict[str, float] = {}
        score_map: dict[str, float] = {}
        symbol_regimes: dict[str, SymbolRegimeSnapshot] = {}
        regime_veto_count = 0

        if regime_filter_cfg.get("enabled", False):
            enabled_symbols = sorted(
                {str(entry["symbol"]) for entry in strategy_entries if bool(entry.get("enabled", True))}
            )
            for symbol in enabled_symbols:
                instrument = instrument_map[symbol]
                snapshot = evaluate_symbol_regime(
                    catalog=catalog,
                    features_dir=Path(app_config.data.features_dir),
                    raw_dir=Path(app_config.data.raw_dir),
                    instrument_id=instrument.id,
                    symbol=symbol,
                    start=window.train_start,
                    end=window.train_end,
                    interval=interval,
                    config=regime_filter_cfg,
                )
                symbol_regimes[symbol] = snapshot
                regime_rows.append(_regime_snapshot_to_row(window_index=window.index, snapshot=snapshot))

        for entry in strategy_entries:
            if not bool(entry.get("enabled", True)):
                continue

            evaluated = _evaluate_strategy_entry(
                app_config=app_config,
                factory=factory,
                entry=entry,
                interval=interval,
                start=window.train_start,
                end=window.train_end,
                balance=balance,
                leverage=leverage,
                output_dir=window_dir,
                metadata=metadata,
                selection_metric=selection_metric,
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
                    "selection_metric": selection_metric,
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

        effective_min_active_strategies = resolve_min_active_strategies(
            min_active_strategies=min_active_strategies,
            min_active_strategies_on_regime_veto=min_active_strategies_on_regime_veto,
            regime_veto_count=regime_veto_count,
        )
        if not meets_min_active_strategies(len(active_entries), effective_min_active_strategies):
            active_entries = []
            volatility_map = {}
            score_map = {}

        allocation_map, effective_weights, allocation_mode_used = _build_allocation_map(
            allocation_cfg=allocation_cfg,
            selected_entries=active_entries,
            volatility_map=volatility_map,
            score_map=score_map,
            total_capital=balance,
        )
        for entry in evaluated_rows:
            strategy_id = _strategy_id(entry)
            allocation_rows.append(
                {
                    "window_index": window.index,
                    "strategy_id": strategy_id,
                    "symbol": entry["symbol"],
                    "score": entry["score"],
                    "volatility": entry["volatility"],
                    "regime_passed": entry["regime_passed"],
                    "symbol_regime_pass": symbol_regimes.get(str(entry["symbol"])).regime_pass
                    if str(entry["symbol"]) in symbol_regimes
                    else True,
                    "strategy_regime_pass": regime_allows_strategy(
                        strategy_name=str(entry["name"]),
                        snapshot=symbol_regimes.get(str(entry["symbol"])),
                        veto_strategy_names=veto_strategy_names,
                    ),
                    "symbol_regime_reason": symbol_regimes.get(str(entry["symbol"])).reason
                    if str(entry["symbol"]) in symbol_regimes
                    else "disabled",
                    "active_after_min_gate": strategy_id in allocation_map,
                    "effective_weight": effective_weights.get(strategy_id, 0.0),
                    "allocation_mode_used": allocation_mode_used,
                    "allocation_pct": allocation_map.get(strategy_id, 0.0) if strategy_id in allocation_map else 0.0,
                    "effective_min_active_strategies": effective_min_active_strategies,
                    "regime_veto_count": regime_veto_count,
                }
            )

        scaled_specs, scaled_symbols, strategy_ids = _build_strategy_specs(
            factory,
            active_entries,
            interval,
            allocation_map=allocation_map,
        )
        strategy_names = [spec[0].__name__ for spec in scaled_specs]

        train_metadata = metadata | {
            "phase": "train",
            "strategy_names": strategy_names,
            "strategy_ids": strategy_ids,
            "allocation_map": allocation_map,
            "active_strategy_count": len(active_entries),
            "allocation_mode_used": allocation_mode_used,
            "effective_min_active_strategies": effective_min_active_strategies,
            "regime_veto_count": regime_veto_count,
        }
        test_metadata = metadata | {
            "phase": "test",
            "strategy_names": strategy_names,
            "strategy_ids": strategy_ids,
            "allocation_map": allocation_map,
            "active_strategy_count": len(active_entries),
            "allocation_mode_used": allocation_mode_used,
            "effective_min_active_strategies": effective_min_active_strategies,
            "regime_veto_count": regime_veto_count,
        }

        if scaled_specs:
            train_summary, train_curve = _run_phase(
                app_config=app_config,
                strategy_specs=scaled_specs,
                symbols=scaled_symbols,
                interval=interval,
                start=window.train_start,
                end=window.train_end,
                balance=balance,
                leverage=leverage,
                output_dir=window_dir,
                phase_name="train",
                metadata=train_metadata,
            )
            test_summary, test_curve = _run_phase(
                app_config=app_config,
                strategy_specs=scaled_specs,
                symbols=scaled_symbols,
                interval=interval,
                start=window.test_start,
                end=window.test_end,
                balance=balance,
                leverage=leverage,
                output_dir=window_dir,
                phase_name="test",
                metadata=test_metadata,
            )
        else:
            train_summary = _build_flat_summary(
                start=window.train_start,
                end=window.train_end,
                symbols=[],
                interval=interval,
                strategy_names=[],
                metadata=train_metadata,
            )
            test_summary = _build_flat_summary(
                start=window.test_start,
                end=window.test_end,
                symbols=[],
                interval=interval,
                strategy_names=[],
                metadata=test_metadata,
            )
            train_curve = _build_flat_equity_curve(
                phase="train",
                window_index=window.index,
                start=window.train_start,
                balance=balance,
            )
            test_curve = _build_flat_equity_curve(
                phase="test",
                window_index=window.index,
                start=window.test_start,
                balance=balance,
            )

        train_row = flatten_summary(train_summary, phase="train", window_index=window.index)
        train_row["selection_min_score"] = selection_min_score
        train_row["active_strategy_count"] = len(active_entries)
        train_row["effective_min_active_strategies"] = effective_min_active_strategies
        train_row["regime_veto_count"] = regime_veto_count
        train_row["regime_veto_symbols"] = _regime_veto_symbols(
            symbol_regimes=symbol_regimes,
            veto_strategy_names=veto_strategy_names,
        )
        rows.append(train_row)

        test_row = flatten_summary(test_summary, phase="test", window_index=window.index)
        test_row["selection_min_score"] = selection_min_score
        test_row["active_strategy_count"] = len(active_entries)
        test_row["effective_min_active_strategies"] = effective_min_active_strategies
        test_row["regime_veto_count"] = regime_veto_count
        test_row["regime_veto_symbols"] = _regime_veto_symbols(
            symbol_regimes=symbol_regimes,
            veto_strategy_names=veto_strategy_names,
        )
        rows.append(test_row)

        stitched_test_curves.append(test_curve)
        train_curve.to_csv(window_dir / "train_equity_curve.csv", index=False)
        test_curve.to_csv(window_dir / "test_equity_curve.csv", index=False)

    summary_df = pd.DataFrame(rows)
    summary_df.to_csv(output_dir / "walkforward_summary.csv", index=False)
    pd.DataFrame(allocation_rows).to_csv(output_dir / "walkforward_allocations.csv", index=False)
    pd.DataFrame(selection_rows).to_csv(output_dir / "walkforward_selected_params.csv", index=False)
    pd.DataFrame(regime_rows).to_csv(output_dir / "walkforward_regimes.csv", index=False)

    stitched_test_df = stitch_equity_curves(stitched_test_curves, starting_balance=balance)
    if not stitched_test_df.empty:
        stitched_test_df.to_csv(output_dir / "walkforward_test_equity_curve.csv", index=False)

    aggregate = {
        "portfolio_name": portfolio_name,
        "config_path": str(config_path),
        "windows": len(windows),
        "selection_metric": selection_metric,
        "selection_min_score": selection_min_score,
        "min_active_strategies": min_active_strategies,
        "train_mean_pnl_pct": float(summary_df.loc[summary_df["phase"] == "train", "pnl_pct"].mean()),
        "test_mean_pnl_pct": float(summary_df.loc[summary_df["phase"] == "test", "pnl_pct"].mean()),
        "train_mean_pnl_pct_after_costs": float(
            summary_df.loc[summary_df["phase"] == "train", "pnl_pct_after_costs"].mean()
        ),
        "test_mean_pnl_pct_after_costs": float(
            summary_df.loc[summary_df["phase"] == "test", "pnl_pct_after_costs"].mean()
        ),
        "mean_active_strategy_count_test": float(
            summary_df.loc[summary_df["phase"] == "test", "active_strategy_count"].mean()
        ),
    }
    if not stitched_test_df.empty:
        aggregate["test_final_stitched_equity"] = float(stitched_test_df["stitched_equity"].iloc[-1])

    (output_dir / "walkforward_aggregate.json").write_text(json.dumps(aggregate, ensure_ascii=False, indent=2))
    return aggregate


def main() -> None:
    """Run the script entrypoint."""
    args = parse_args()
    setup_logging(level="WARNING")

    config_path = Path(args.config)
    raw_cfg = load_yaml(config_path)
    portfolio_cfg = raw_cfg.get("portfolio", raw_cfg)
    portfolio_name = str(portfolio_cfg.get("name", config_path.stem))

    app_config = load_app_config(env=args.env)
    factory = AppFactory(container=_ContainerStub(app_config))
    catalog = ParquetDataCatalog(app_config.data.catalog_dir)
    instrument_map = {inst.raw_symbol.value: inst for inst in catalog.instruments()}

    backtest_cfg = portfolio_cfg.get("backtest", {})
    interval = Interval(str(backtest_cfg.get("interval", "1h")))
    balance = int(backtest_cfg.get("starting_balance_usdt", 10_000))
    leverage = float(backtest_cfg.get("leverage", app_config.account.max_leverage))
    allocation_cfg = portfolio_cfg.get("allocation", {})

    strategy_entries = portfolio_cfg.get("strategies", [])
    if not strategy_entries:
        raise ValueError("没有启用的策略，无法执行 walk-forward")

    walk_cfg = portfolio_cfg.get("walkforward", {})
    start = dt.date.fromisoformat(str(walk_cfg["start"]))
    end = dt.date.fromisoformat(str(walk_cfg["end"]))
    train_months = int(walk_cfg.get("train_months", 6))
    test_months = int(walk_cfg.get("test_months", 3))
    step_months = int(walk_cfg.get("step_months", 3))
    selection_metric = str(walk_cfg.get("selection_metric", "pnl_pct_after_costs"))
    selection_min_score_raw = walk_cfg.get("selection_min_score")
    selection_min_score = float(selection_min_score_raw) if selection_min_score_raw is not None else None
    selection_min_score_grid_raw = walk_cfg.get("selection_min_score_grid", [])
    selection_min_score_grid = [float(value) for value in selection_min_score_grid_raw]
    min_active_strategies_raw = walk_cfg.get("min_active_strategies")
    min_active_strategies = int(min_active_strategies_raw) if min_active_strategies_raw is not None else None
    regime_filter_cfg = walk_cfg.get("regime_filter", {})

    windows = generate_walkforward_windows(
        start=start,
        end=end,
        train_months=train_months,
        test_months=test_months,
        step_months=step_months,
    )
    if not windows:
        raise ValueError("未生成任何 walk-forward 窗口，请检查起止日期和窗口参数")

    base_output_dir = Path(args.output_dir) if args.output_dir else Path("experiments/walkforward") / portfolio_name
    base_output_dir.mkdir(parents=True, exist_ok=True)

    scenario_thresholds = selection_min_score_grid or [selection_min_score]

    grid_rows: list[dict[str, Any]] = []
    for threshold in scenario_thresholds:
        scenario_dir = base_output_dir
        if len(scenario_thresholds) > 1:
            scenario_dir = base_output_dir / f"score_{_format_score_label(threshold)}"
            scenario_dir.mkdir(parents=True, exist_ok=True)

        aggregate = _run_walkforward_scenario(
            app_config=app_config,
            factory=factory,
            catalog=catalog,
            instrument_map=instrument_map,
            portfolio_name=portfolio_name,
            config_path=config_path,
            strategy_entries=strategy_entries,
            allocation_cfg=allocation_cfg,
            interval=interval,
            balance=balance,
            leverage=leverage,
            windows=windows,
            selection_metric=selection_metric,
            selection_min_score=threshold,
            min_active_strategies=min_active_strategies,
            regime_filter_cfg=regime_filter_cfg,
            output_dir=scenario_dir,
        )
        aggregate["output_dir"] = str(scenario_dir)
        grid_rows.append(aggregate)

        print(f"组合名称: {portfolio_name}")
        print(f"窗口数量: {len(windows)}")
        print(f"结果目录: {scenario_dir}")
        print(f"selection_min_score: {threshold}")
        print(f"样本外平均收益率: {aggregate['test_mean_pnl_pct']:.4f}%")
        print(f"样本外成本后平均收益率: {aggregate['test_mean_pnl_pct_after_costs']:.4f}%")
        if "test_final_stitched_equity" in aggregate:
            print(f"样本外拼接权益终值: {aggregate['test_final_stitched_equity']:.4f}")

    if len(grid_rows) > 1:
        grid_df = pd.DataFrame(grid_rows).sort_values(
            by=["test_mean_pnl_pct_after_costs", "test_final_stitched_equity"],
            ascending=[False, False],
        )
        grid_df.to_csv(base_output_dir / "walkforward_grid_summary.csv", index=False)
        (base_output_dir / "walkforward_grid_summary.json").write_text(
            json.dumps(grid_rows, ensure_ascii=False, indent=2)
        )


if __name__ == "__main__":
    main()
