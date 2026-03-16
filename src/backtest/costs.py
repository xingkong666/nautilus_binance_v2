"""回测成本与资金费率分析."""

from __future__ import annotations

import ast
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import pandas as pd

from src.execution.cost_model import CostModel
from src.execution.slippage import SlippageModel


@dataclass(frozen=True)
class CostAnalysis:
    """回测成本分析结果."""

    commissions_total: Decimal
    commissions_source: str
    modeled_fee_cost: Decimal
    modeled_slippage_cost: Decimal
    funding_cost: Decimal
    additional_cost_applied: Decimal
    pnl_total: Decimal
    pnl_after_costs: Decimal
    pnl_pct_after_costs: float
    ending_balance_after_costs: Decimal
    fills_count: int
    funding_events_used: int

    def to_dict(self) -> dict[str, Any]:
        """Convert the object to dict.

        Returns:
            dict[str, Any]: Dictionary representation of the result.
        """
        data = asdict(self)
        for key, value in list(data.items()):
            if isinstance(value, Decimal):
                data[key] = float(value)
        return data


class BacktestCostAnalyzer:
    """基于回测报表做手续费、滑点和资金费率分析."""

    def __init__(
        self,
        execution_config: Any,
        raw_dir: Path,
        features_dir: Path,
    ) -> None:
        """Initialize the backtest cost analyzer.

        Args:
            execution_config: Configuration for execution.
            raw_dir: Directory for raw.
            features_dir: Directory for features.
        """
        self._execution_config = execution_config
        self._raw_dir = raw_dir
        self._features_dir = features_dir
        self._cost_model = CostModel(getattr(execution_config, "cost", {}) or {})
        self._slippage_model = SlippageModel(getattr(execution_config, "slippage", {}) or {})
        self._funding_config = getattr(execution_config, "funding", {}) or {}

    def analyze(
        self,
        reports: dict[str, Any],
        starting_balance: int,
        pnl_stats: dict[str, Any] | None,
    ) -> CostAnalysis | None:
        """Run analyze.

        Args:
            reports: Reports.
            starting_balance: Starting balance.
            pnl_stats: PnL stats.

        Returns:
            CostAnalysis: Result of analyze.
        """
        fills = self._as_dataframe(reports.get("order_fills"))
        positions = self._as_dataframe(reports.get("positions"))
        if fills is None and positions is None:
            return None

        commissions_total = self._commissions_total(fills)
        modeled_fee_cost = self._modeled_fee_cost(fills)
        modeled_slippage_cost = self._modeled_slippage_cost(fills)
        funding_cost, funding_events_used = self._funding_cost(positions)

        pnl_total = self._extract_total_pnl(pnl_stats)

        # Nautilus 账户报表已反映 commissions，因此只有在 commissions 缺失时才补扣 modeled fee。
        fee_cost_applied = Decimal("0") if commissions_total > 0 else modeled_fee_cost
        additional_cost_applied = fee_cost_applied + modeled_slippage_cost + funding_cost
        pnl_after_costs = pnl_total - additional_cost_applied
        ending_balance_after_costs = Decimal(str(starting_balance)) + pnl_after_costs
        pnl_pct_after_costs = 0.0
        if starting_balance > 0:
            pnl_pct_after_costs = float((pnl_after_costs / Decimal(str(starting_balance))) * Decimal("100"))

        return CostAnalysis(
            commissions_total=commissions_total,
            commissions_source="reported" if commissions_total > 0 else "modeled",
            modeled_fee_cost=modeled_fee_cost,
            modeled_slippage_cost=modeled_slippage_cost,
            funding_cost=funding_cost,
            additional_cost_applied=additional_cost_applied,
            pnl_total=pnl_total,
            pnl_after_costs=pnl_after_costs,
            pnl_pct_after_costs=pnl_pct_after_costs,
            ending_balance_after_costs=ending_balance_after_costs,
            fills_count=0 if fills is None else len(fills),
            funding_events_used=funding_events_used,
        )

    @staticmethod
    def _as_dataframe(value: Any) -> pd.DataFrame | None:
        return value if isinstance(value, pd.DataFrame) and not value.empty else None

    def _commissions_total(self, fills: pd.DataFrame | None) -> Decimal:
        if fills is None or "commissions" not in fills.columns:
            return Decimal("0")
        total = Decimal("0")
        for raw in fills["commissions"].tolist():
            total += self._parse_commission_cell(raw)
        return total

    def _modeled_fee_cost(self, fills: pd.DataFrame | None) -> Decimal:
        if fills is None:
            return Decimal("0")

        total = Decimal("0")
        for row in fills.to_dict(orient="records"):
            qty = self._decimal_or_zero(row.get("filled_qty", row.get("quantity")))
            price = self._decimal_or_zero(row.get("avg_px"))
            if qty <= 0 or price <= 0:
                continue
            total += self._cost_model.estimate_cost(
                quantity=qty,
                price=price,
                is_maker=str(row.get("liquidity_side", "")).upper() == "MAKER",
                slippage_bps=Decimal("0"),
            )
        return total

    def _modeled_slippage_cost(self, fills: pd.DataFrame | None) -> Decimal:
        if fills is None:
            return Decimal("0")

        total = Decimal("0")
        for row in fills.to_dict(orient="records"):
            qty = self._decimal_or_zero(row.get("filled_qty", row.get("quantity")))
            price = self._decimal_or_zero(row.get("avg_px"))
            if qty <= 0 or price <= 0:
                continue
            slippage_bps = self._slippage_model.estimate_slippage_bps(quantity=qty, price=price)
            total += qty * price * slippage_bps / Decimal("10000")
        return total

    def _funding_cost(self, positions: pd.DataFrame | None) -> tuple[Decimal, int]:
        if positions is None or positions.empty:
            return Decimal("0"), 0

        total = Decimal("0")
        events_used = 0
        grouped = positions.groupby("instrument_id")
        for instrument_id, group in grouped:
            funding = self._load_funding_rates(str(instrument_id))
            if funding is None or funding.empty:
                continue

            for row in group.to_dict(orient="records"):
                opened_at = pd.to_datetime(row.get("ts_opened"), utc=True, errors="coerce")
                closed_at = pd.to_datetime(row.get("ts_closed"), utc=True, errors="coerce")
                if pd.isna(opened_at) or pd.isna(closed_at):
                    continue

                qty = self._decimal_or_zero(row.get("peak_qty", row.get("quantity")))
                if qty <= 0:
                    continue

                side_text = str(row.get("entry", "")).upper()
                side_sign = Decimal("1") if side_text == "BUY" else Decimal("-1")
                fallback_price = self._decimal_or_zero(row.get("avg_px_open"))
                mask = (funding["timestamp"] >= opened_at) & (funding["timestamp"] <= closed_at)
                window = funding.loc[mask]
                if window.empty:
                    continue

                for _, funding_row in window.iterrows():
                    rate = self._decimal_or_zero(funding_row.get("funding_rate"))
                    reference_price = self._decimal_or_zero(funding_row.get("mark_price"))
                    if reference_price <= 0:
                        reference_price = fallback_price
                    if reference_price <= 0:
                        continue
                    total += qty * reference_price * rate * side_sign
                    events_used += 1

        return total, events_used

    def _load_funding_rates(self, instrument_id: str) -> pd.DataFrame | None:
        if not self._funding_config.get("enabled", True):
            return None

        symbol = instrument_id.split("-")[0]
        candidates = [
            self._features_dir / f"funding_rates_{symbol}.parquet",
            self._features_dir / f"funding_rates_{symbol.lower()}.parquet",
            self._raw_dir / "funding" / f"{symbol}.csv",
            self._raw_dir / "funding" / f"{symbol.lower()}.csv",
        ]
        for path in candidates:
            if not path.exists():
                continue
            df = pd.read_parquet(path) if path.suffix == ".parquet" else pd.read_csv(path)
            normalized = self._normalize_funding_dataframe(df)
            if normalized is not None and not normalized.empty:
                return normalized
        return None

    @staticmethod
    def _normalize_funding_dataframe(df: pd.DataFrame) -> pd.DataFrame | None:
        timestamp_col = next(
            (col for col in ("timestamp", "ts_event", "funding_time", "time") if col in df.columns),
            None,
        )
        rate_col = next(
            (col for col in ("funding_rate", "rate") if col in df.columns),
            None,
        )
        if timestamp_col is None or rate_col is None:
            return None

        mark_col = next((col for col in ("mark_price", "price") if col in df.columns), None)
        normalized = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(df[timestamp_col], utc=True, errors="coerce"),
                "funding_rate": pd.to_numeric(df[rate_col], errors="coerce"),
                "mark_price": pd.to_numeric(df[mark_col], errors="coerce") if mark_col else None,
            }
        )
        if "mark_price" not in normalized.columns:
            normalized["mark_price"] = None
        normalized = normalized.dropna(subset=["timestamp", "funding_rate"])
        return normalized.sort_values("timestamp").reset_index(drop=True)

    @staticmethod
    def _extract_total_pnl(pnl_stats: dict[str, Any] | None) -> Decimal:
        if not pnl_stats:
            return Decimal("0")
        pnl_value = pnl_stats.get("PnL (total)", 0)
        return BacktestCostAnalyzer._decimal_or_zero(pnl_value)

    @staticmethod
    def _parse_commission_cell(raw: Any) -> Decimal:
        if raw is None:
            return Decimal("0")
        if isinstance(raw, list):
            items = raw
        else:
            text = str(raw).strip()
            if not text:
                return Decimal("0")
            try:
                parsed = ast.literal_eval(text)
                items = parsed if isinstance(parsed, list) else [parsed]
            except (SyntaxError, ValueError):
                items = [text]

        total = Decimal("0")
        for item in items:
            text = str(item).strip()
            if not text:
                continue
            parts = text.split()
            total += BacktestCostAnalyzer._decimal_or_zero(parts[0])
        return total

    @staticmethod
    def _decimal_or_zero(raw: Any) -> Decimal:
        try:
            value = Decimal(str(raw))
        except (InvalidOperation, ValueError, TypeError):
            return Decimal("0")
        if not value.is_finite():
            return Decimal("0")
        return value
