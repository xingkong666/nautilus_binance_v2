"""回测报告生成.

将 BacktestRunResult 格式化为可读的文本报告，并支持保存到文件。

主要功能：
    - 打印/保存核心指标（收益、回撤、胜率、夏普等）
    - 汇总订单、成交、仓位统计
    - 输出 JSON 格式供后续分析使用
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import structlog

from src.backtest.runner import BacktestRunResult

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# 报告生成器
# ---------------------------------------------------------------------------


class BacktestReporter:
    """回测报告生成器.

    Usage:
        reporter = BacktestReporter(result)
        reporter.print_summary()
        reporter.save(output_dir=Path("experiments/reports"))
    """

    def __init__(self, run_result: BacktestRunResult) -> None:
        """初始化报告生成器.

        Args:
            run_result: BacktestRunner.run() 返回的 BacktestRunResult 实例。
        """
        self._run = run_result
        self._result = run_result.result
        self._reports = run_result.reports
        self._config = run_result.config

    # ------ 核心汇总 ------

    def summary(self) -> dict[str, Any]:
        """提取核心指标，返回结构化字典.

        Returns:
            包含以下字段的字典：
                - run_id (str): 本次回测 run_id。
                - period (str): 回测时间段，如 "2024-01-01 ~ 2024-03-31"。
                - symbols (list[str]): 参与回测的交易对。
                - interval (str): K 线周期。
                - starting_balance (int): 初始余额（USDT）。
                - elapsed_time (float): 回测耗时（秒）。
                - iterations (int): 引擎迭代次数。
                - total_orders (int): 总订单数。
                - total_positions (int): 总仓位数。
                - pnl (dict): 各货币的 PnL 统计（来自 stats_pnls）。
                - returns (dict): 收益率统计（来自 stats_returns）。
        """
        r = self._result
        cfg = self._config

        return {
            "run_id": r.run_id,
            "period": f"{cfg.start} ~ {cfg.end}",
            "symbols": cfg.symbols,
            "interval": cfg.interval.value,
            "starting_balance": cfg.starting_balance_usdt,
            "elapsed_time": round(r.elapsed_time, 2),
            "iterations": r.iterations,
            "total_orders": r.total_orders,
            "total_positions": r.total_positions,
            "pnl": r.stats_pnls,
            "returns": r.stats_returns,
        }

    def print_summary(self) -> None:
        """将回测汇总信息格式化打印到 stdout."""
        s = self.summary()
        sep = "=" * 70

        print(sep)
        print("📊 回测报告")
        print(sep)
        print(f"  Run ID       : {s['run_id']}")
        print(f"  Period       : {s['period']}")
        print(f"  Symbols      : {', '.join(s['symbols'])}")
        print(f"  Interval     : {s['interval']}")
        print(f"  Init Balance : {s['starting_balance']:,} USDT")
        print(f"  Elapsed      : {s['elapsed_time']}s")
        print(f"  Iterations   : {s['iterations']:,}")
        print(sep)
        print(f"  Total Orders   : {s['total_orders']}")
        print(f"  Total Positions: {s['total_positions']}")
        print(sep)

        # PnL 统计
        if s["pnl"]:
            print("  📈 PnL Statistics:")
            for currency, stats in s["pnl"].items():
                print(f"    [{currency}]")
                for k, v in stats.items():
                    if isinstance(v, float):
                        print(f"      {k:<35}: {v:.4f}")
                    else:
                        print(f"      {k:<35}: {v}")
        print(sep)

        # Returns 统计
        if s["returns"]:
            print("  📉 Returns Statistics:")
            for k, v in s["returns"].items():
                if isinstance(v, float):
                    print(f"    {k:<37}: {v:.4f}")
                else:
                    print(f"    {k:<37}: {v}")
        print(sep)

        # 订单/仓位报告摘要
        self._print_report_summary()

    def _print_report_summary(self) -> None:
        """打印订单、仓位报告的行数摘要.

        仅输出条数，完整明细可通过 save() 写入文件查看。
        """
        for name, df in self._reports.items():
            if df is not None and hasattr(df, "__len__"):
                print(f"  {name:<20}: {len(df)} rows")

    # ------ 保存报告 ------

    def save(self, output_dir: Path | None = None) -> Path:
        """将回测报告保存到文件（JSON + CSV）.

        保存内容：
            - summary.json：核心指标汇总。
            - orders.csv：所有订单记录（如有）。
            - order_fills.csv：成交记录（如有）。
            - positions.csv：仓位记录（如有）。
            - account.csv：账户流水（如有）。

        Args:
            output_dir: 报告保存目录；为 None 时使用
                ``experiments/reports/<run_id>/``。

        Returns:
            报告保存目录的 Path 对象。
        """
        r = self._result
        if output_dir is None:
            output_dir = Path("experiments/reports") / r.run_id

        output_dir.mkdir(parents=True, exist_ok=True)

        # 保存 JSON 汇总
        summary_path = output_dir / "summary.json"
        summary_data = self._serializable_summary()
        summary_path.write_text(json.dumps(summary_data, ensure_ascii=False, indent=2))
        logger.info("report_saved", path=str(summary_path))

        # 保存各 DataFrame 报告
        for name, df in self._reports.items():
            if df is not None and hasattr(df, "to_csv") and len(df) > 0:
                csv_path = output_dir / f"{name}.csv"
                df.to_csv(csv_path, index=False)
                logger.info("report_csv_saved", name=name, rows=len(df), path=str(csv_path))

        return output_dir

    def _serializable_summary(self) -> dict[str, Any]:
        """将 summary() 的值转换为 JSON 可序列化格式.

        Returns:
            可直接传入 json.dumps() 的字典。
        """
        s = self.summary()
        # dt.date 转字符串
        s["generated_at"] = dt.datetime.now(dt.UTC).isoformat()
        # float -> round 4 位
        if s["pnl"]:
            for currency in s["pnl"]:
                s["pnl"][currency] = {
                    k: round(v, 4) if isinstance(v, float) else v
                    for k, v in s["pnl"][currency].items()
                }
        if s["returns"]:
            s["returns"] = {
                k: round(v, 4) if isinstance(v, float) else v
                for k, v in s["returns"].items()
            }
        return s
