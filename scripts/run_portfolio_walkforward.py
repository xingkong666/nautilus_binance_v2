#!/usr/bin/env python3
"""运行组合策略的 walk-forward 验证.

薄封装脚本：解析 CLI 参数、加载配置，然后委托给 WalkForwardEngine 执行。
核心逻辑见 src/backtest/walk_forward_engine.py。
"""
# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.app.factory import AppFactory
from src.backtest.walk_forward_engine import WalkForwardEngine
from src.backtest.walkforward import generate_walkforward_windows
from src.core.config import load_app_config, load_yaml
from src.core.logging import setup_logging


class _ContainerStub:
    def __init__(self, config: Any) -> None:
        self.config = config
        self.binance_adapter = None


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
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
    parser.add_argument(
        "--parallel",
        action="store_true",
        default=False,
        help="启用多进程并行窗口执行（实验性）",
    )
    return parser.parse_args()


def _format_score_label(value: float | None) -> str:
    if value is None:
        return "none"
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text.replace("-", "m").replace(".", "p")


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

    walk_cfg = portfolio_cfg.get("walkforward", {})
    selection_min_score_grid_raw = walk_cfg.get("selection_min_score_grid", [])
    selection_min_score_grid = [float(v) for v in selection_min_score_grid_raw]
    selection_min_score_raw = walk_cfg.get("selection_min_score")
    selection_min_score_default: float | None = (
        float(selection_min_score_raw) if selection_min_score_raw is not None else None
    )

    base_output_dir = Path(args.output_dir) if args.output_dir else Path("experiments/walkforward") / portfolio_name
    base_output_dir.mkdir(parents=True, exist_ok=True)

    # Support selection_min_score grid sweep (backward-compatible)
    scenario_thresholds: list[float | None] = selection_min_score_grid or [selection_min_score_default]

    # Pre-compute windows once (used only for display; Engine generates them internally)
    backtest_cfg = portfolio_cfg.get("backtest", {})
    import datetime as dt

    wf_start = dt.date.fromisoformat(str(walk_cfg["start"]))
    wf_end = dt.date.fromisoformat(str(walk_cfg["end"]))
    windows_preview = generate_walkforward_windows(
        start=wf_start,
        end=wf_end,
        train_months=int(walk_cfg.get("train_months", 6)),
        test_months=int(walk_cfg.get("test_months", 3)),
        step_months=int(walk_cfg.get("step_months", 3)),
    )
    if not windows_preview:
        raise ValueError("未生成任何 walk-forward 窗口，请检查起止日期和窗口参数")

    grid_rows: list[dict[str, Any]] = []

    for threshold in scenario_thresholds:
        scenario_dir = base_output_dir
        if len(scenario_thresholds) > 1:
            scenario_dir = base_output_dir / f"score_{_format_score_label(threshold)}"
            scenario_dir.mkdir(parents=True, exist_ok=True)

        # Override selection_min_score per-scenario
        scenario_cfg = dict(portfolio_cfg)
        if "walkforward" in scenario_cfg:
            scenario_wf = dict(scenario_cfg["walkforward"])
            scenario_wf["selection_min_score"] = threshold
            scenario_cfg = {**scenario_cfg, "walkforward": scenario_wf}

        engine = WalkForwardEngine(
            app_config=app_config,
            factory=factory,
            portfolio_config=scenario_cfg,
            parallel=args.parallel,
        )
        result = engine.run(output_dir=scenario_dir, selection_min_score=threshold)
        aggregate = result.aggregate
        aggregate["output_dir"] = str(scenario_dir)
        grid_rows.append(aggregate)

        leverage = float(backtest_cfg.get("leverage", app_config.account.max_leverage))
        _ = leverage  # available for future display
        print(f"组合名称: {portfolio_name}")
        print(f"窗口数量: {len(result.windows)}")
        print(f"结果目录: {scenario_dir}")
        print(f"selection_min_score: {threshold}")
        print(f"样本外平均收益率: {aggregate['test_mean_pnl_pct']:.4f}%")
        print(f"样本外成本后平均收益率: {aggregate['test_mean_pnl_pct_after_costs']:.4f}%")
        if "test_final_stitched_equity" in aggregate:
            print(f"样本外拼接权益终值: {aggregate['test_final_stitched_equity']:.4f}")
        print(f"稳定性: consistency={result.stability.consistency_rate:.3f}, passed={result.stability.passed}")

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
