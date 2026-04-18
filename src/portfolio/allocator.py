"""多策略资金分配器.

负责在多个策略之间分配总资金, 计算每个策略可用的资金上限,
并提供仓位再平衡的 OrderIntent 列表.

支持三种分配模式:
- equal: 等额分配
- weight: 按权重分配
- risk_parity: 按风险平价分配 (等风险贡献)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from typing import Any

import structlog

from src.execution.order_intent import OrderIntent
from src.risk.drawdown_control import DrawdownController

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# 配置数据类
# ---------------------------------------------------------------------------


@dataclass
class StrategyAllocation:
    """单个策略的分配配置.

    Attributes:
        strategy_id: 策略唯一标识。
        weight: 相对权重（等额模式下忽略）。
        max_allocation_pct: 该策略可用资金上限占总资金的百分比，0 表示不限。
        enabled: 是否参与分配。

    """

    strategy_id: str
    weight: float = 1.0
    max_allocation_pct: float = 0.0  # 0 = 不限制
    enabled: bool = True


@dataclass
class AllocationResult:
    """单个策略的分配结果.

    Attributes:
        strategy_id: 策略唯一标识。
        allocated_capital: 分配的资金金额（USDT）。
        allocation_pct: 分配比例（0-1）。
        available_capital: 去除已用保证金后的可用资金。

    """

    strategy_id: str
    allocated_capital: Decimal
    allocation_pct: float
    available_capital: Decimal


@dataclass
class PortfolioSnapshot:
    """当前持仓快照，用于再平衡计算.

    Attributes:
        strategy_id: 策略唯一标识。
        instrument_id: 交易对。
        current_quantity: 当前持仓量（正=多头，负=空头，0=无仓）。
        current_price: 当前价格（用于估算持仓价值）。
        margin_used: 当前占用保证金。

    """

    strategy_id: str
    instrument_id: str
    current_quantity: Decimal
    current_price: Decimal
    margin_used: Decimal = field(default_factory=Decimal)


# ---------------------------------------------------------------------------
# 核心分配器
# ---------------------------------------------------------------------------


class PortfolioAllocator:
    """多策略资金分配器.

    根据配置的分配模式和策略权重，计算每个策略的可用资金。
    支持等额分配、权重分配和风险平价三种模式。

    Examples:
        >>> config = {
        ...     "mode": "weight",
        ...     "total_capital": "10000",
        ...     "reserve_pct": 10.0,
        ...     "strategies": [
        ...         {"strategy_id": "ema_cross", "weight": 2.0},
        ...         {"strategy_id": "mean_revert", "weight": 1.0},
        ...     ],
        ... }
        >>> allocator = PortfolioAllocator(config)
        >>> results = allocator.allocate(Decimal("10000"))

    """

    def __init__(
        self,
        config: dict[str, Any],
        drawdown_controller: DrawdownController | None = None,
    ) -> None:
        """初始化分配器.

        Args:
            config: 分配配置字典，包含以下字段：
                - mode (str): 分配模式，"equal" / "weight" / "risk_parity"，默认 "equal"。
                - reserve_pct (float): 预留资金比例（%），默认 5.0。
                - min_allocation (str): 单策略最低分配金额（USDT），默认 "100"。
                - strategies (list[dict]): 策略分配配置列表。
            drawdown_controller: 回撤控制器，用于动态调整仓位大小。

        Raises:
            ValueError: 若策略列表为空或模式不合法。

        """
        valid_modes = {"equal", "weight", "risk_parity"}
        self._mode: str = config.get("mode", "equal")
        if self._mode not in valid_modes:
            raise ValueError(f"无效分配模式 '{self._mode}'，合法值: {valid_modes}")

        self._reserve_pct: float = float(config.get("reserve_pct", 5.0))
        self._min_allocation: Decimal = Decimal(str(config.get("min_allocation", "100")))

        raw_strategies: list[dict[str, Any]] = config.get("strategies", [])
        if not raw_strategies:
            raise ValueError("strategies 列表不能为空")

        self._strategies: dict[str, StrategyAllocation] = {
            s["strategy_id"]: StrategyAllocation(
                strategy_id=s["strategy_id"],
                weight=float(s.get("weight", 1.0)),
                max_allocation_pct=float(s.get("max_allocation_pct", 0.0)),
                enabled=bool(s.get("enabled", True)),
            )
            for s in raw_strategies
        }

        # 策略维度的风险波动率（risk_parity 模式使用），外部通过 update_volatility 注入
        self._volatilities: dict[str, float] = {}

        # 回撤控制器
        self._drawdown_controller = drawdown_controller

        logger.info(
            "portfolio_allocator_initialized",
            mode=self._mode,
            reserve_pct=self._reserve_pct,
            strategy_count=len(self._strategies),
        )

    # ------------------------------------------------------------------
    # 公共 应用程序编程接口
    # ------------------------------------------------------------------

    def allocate(self, total_capital: Decimal) -> dict[str, AllocationResult]:
        """计算所有策略的资金分配结果.

        Args:
            total_capital: 当前账户总权益（USDT）。

        Returns:
            以 strategy_id 为键的分配结果字典。

        Raises:
            ValueError: 若没有可用的启用策略。

        """
        enabled = [s for s in self._strategies.values() if s.enabled]
        if not enabled:
            raise ValueError("没有已启用的策略，无法分配资金")

        # 预留储备金
        deployable = total_capital * Decimal(str((100.0 - self._reserve_pct) / 100.0))

        # 应用回撤控制的仓位乘数
        dd_multiplier = Decimal("1.0")
        if self._drawdown_controller is not None:
            raw_mult = self._drawdown_controller.get_size_multiplier(total_capital)
            dd_multiplier = Decimal(str(raw_mult))
            if raw_mult < 1.0:
                logger.warning("drawdown_multiplier_applied", multiplier=raw_mult)
        deployable = deployable * dd_multiplier

        if self._mode == "equal":
            raw_weights = {s.strategy_id: 1.0 for s in enabled}
        elif self._mode == "weight":
            raw_weights = {s.strategy_id: s.weight for s in enabled}
        elif self._mode == "risk_parity":
            raw_weights = self._risk_parity_weights(enabled)
        else:
            raw_weights = {s.strategy_id: 1.0 for s in enabled}

        # 归一化权重
        total_weight = sum(raw_weights.values())
        if total_weight <= 0:
            total_weight = 1.0
        norm_weights = {sid: w / total_weight for sid, w in raw_weights.items()}

        results: dict[str, AllocationResult] = {}
        for strat in enabled:
            sid = strat.strategy_id
            pct = norm_weights[sid]
            allocated = deployable * Decimal(str(pct))

            # 单策略上限
            if strat.max_allocation_pct > 0:
                cap = total_capital * Decimal(str(strat.max_allocation_pct / 100.0))
                allocated = min(allocated, cap)

            # 最低资金门槛：低于门槛则跳过（不分配）
            if allocated < self._min_allocation:
                logger.warning(
                    "allocation_below_minimum",
                    strategy_id=sid,
                    allocated=str(allocated),
                    min_allocation=str(self._min_allocation),
                )
                allocated = Decimal("0")
                pct = 0.0

            results[sid] = AllocationResult(
                strategy_id=sid,
                allocated_capital=allocated.quantize(Decimal("0.01"), rounding=ROUND_DOWN),
                allocation_pct=pct,
                available_capital=allocated.quantize(Decimal("0.01"), rounding=ROUND_DOWN),
            )

        logger.info(
            "allocation_complete",
            total_capital=str(total_capital),
            deployable=str(deployable),
            allocations={sid: str(r.allocated_capital) for sid, r in results.items()},
        )
        return results

    def get_available_capital(
        self,
        strategy_id: str,
        total_capital: Decimal,
        margin_used: Decimal = Decimal("0"),
    ) -> Decimal:
        """查询指定策略的可用资金.

        Args:
            strategy_id: 策略唯一标识。
            total_capital: 当前账户总权益（USDT）。
            margin_used: 该策略当前已占用的保证金（USDT）。

        Returns:
            该策略当前还可以使用的资金（USDT），最小为 0。

        Raises:
            KeyError: 若 strategy_id 不在配置中。

        """
        if strategy_id not in self._strategies:
            raise KeyError(f"未知策略 '{strategy_id}'")

        results = self.allocate(total_capital)
        if strategy_id not in results:
            return Decimal("0")

        available = results[strategy_id].allocated_capital - margin_used
        return max(available, Decimal("0"))

    def rebalance(
        self,
        snapshots: list[PortfolioSnapshot],
        total_capital: Decimal,
        price_precision: int = 2,
        qty_precision: int = 4,
        close_unknown: bool = True,
    ) -> list[OrderIntent]:
        """生成再平衡所需的 OrderIntent 列表.

        比较当前持仓价值与目标分配资金，生成增减仓指令。
        只在偏差超过阈值（5%）时才产生再平衡订单，避免频繁小额操作。

        Args:
            snapshots: 当前各策略持仓快照列表。
            total_capital: 当前账户总权益（USDT）。
            price_precision: 价格精度（小数位数），用于格式化日志。
            qty_precision: 数量精度（小数位数），用于四舍五入。
            close_unknown: 是否对未出现在目标分配中的策略仓位执行全平。

        Returns:
            需要执行的 OrderIntent 列表（可能为空）。

        """
        target_allocations = self.allocate(total_capital)
        intents: list[OrderIntent] = []
        rebalance_threshold = Decimal("0.05")  # 5% 偏差才触发

        for snap in snapshots:
            sid = snap.strategy_id

            # 策略不在分配结果中（已禁用或未知）且 close_unknown=True→ 全平
            if sid not in target_allocations:
                if close_unknown and snap.current_quantity != Decimal("0"):
                    intents.append(
                        OrderIntent(
                            instrument_id=snap.instrument_id,
                            side="SELL" if snap.current_quantity > 0 else "BUY",
                            quantity=abs(snap.current_quantity),
                            order_type="MARKET",
                            reduce_only=True,
                            strategy_id=sid,
                            metadata={"reason": "rebalance_disabled_or_unknown"},
                        )
                    )
                continue

            target_capital = target_allocations[sid].allocated_capital
            current_value = abs(snap.current_quantity) * snap.current_price

            if target_capital == Decimal("0"):
                # 目标为零 → 全平
                if snap.current_quantity != Decimal("0"):
                    intents.append(
                        OrderIntent(
                            instrument_id=snap.instrument_id,
                            side="SELL" if snap.current_quantity > 0 else "BUY",
                            quantity=abs(snap.current_quantity),
                            order_type="MARKET",
                            reduce_only=True,
                            strategy_id=sid,
                            metadata={"reason": "rebalance_zero_target"},
                        )
                    )
                continue

            if current_value == Decimal("0") or snap.current_price == Decimal("0"):
                continue

            deviation = abs(current_value - target_capital) / target_capital
            if deviation <= rebalance_threshold:
                logger.debug(
                    "rebalance_skipped_within_threshold",
                    strategy_id=sid,
                    deviation=f"{float(deviation):.2%}",
                )
                continue

            delta_capital = target_capital - current_value
            delta_qty = (delta_capital / snap.current_price).quantize(
                Decimal(f"1e-{qty_precision}"), rounding=ROUND_DOWN
            )

            if delta_qty == Decimal("0"):
                continue

            side = "BUY" if delta_qty > 0 else "SELL"
            intents.append(
                OrderIntent(
                    instrument_id=snap.instrument_id,
                    side=side,
                    quantity=abs(delta_qty),
                    order_type="MARKET",
                    strategy_id=sid,
                    metadata={
                        "reason": "rebalance",
                        "current_value": str(current_value),
                        "target_capital": str(target_capital),
                        "deviation": f"{float(deviation):.2%}",
                    },
                )
            )

            logger.info(
                "rebalance_order_generated",
                strategy_id=sid,
                instrument_id=snap.instrument_id,
                side=side,
                qty=str(abs(delta_qty)),
                deviation=f"{float(deviation):.2%}",
            )

        return intents

    def update_volatility(self, strategy_id: str, volatility: float) -> None:
        """更新策略的历史波动率（供 risk_parity 模式使用）.

        Args:
            strategy_id: 策略唯一标识。
            volatility: 年化波动率（正数），例如 0.2 表示 20%。

        Raises:
            ValueError: 若 volatility <= 0。

        """
        if volatility <= 0:
            raise ValueError(f"波动率必须为正数，收到: {volatility}")
        self._volatilities[strategy_id] = volatility
        logger.debug("volatility_updated", strategy_id=strategy_id, volatility=volatility)

    def update_strategy_enabled(self, strategy_id: str, enabled: bool) -> None:
        """动态启用或禁用策略（运行时调整，不影响配置文件）.

        Args:
            strategy_id: 策略唯一标识。
            enabled: True 为启用，False 为禁用。

        Raises:
            KeyError: 若 strategy_id 不在配置中。

        """
        if strategy_id not in self._strategies:
            raise KeyError(f"未知策略 '{strategy_id}'")
        self._strategies[strategy_id].enabled = enabled
        logger.info("strategy_enabled_updated", strategy_id=strategy_id, enabled=enabled)

    def summary(self, total_capital: Decimal) -> str:
        """返回当前分配方案的文本摘要（调试/日志用途）.

        Args:
            total_capital: 当前账户总权益（USDT）。

        Returns:
            多行字符串，包含每个策略的分配比例和金额。

        """
        results = self.allocate(total_capital)
        deployable = total_capital * Decimal(str((100 - self._reserve_pct) / 100))
        lines = [
            f"PortfolioAllocator [{self._mode}] — 总资金: {total_capital} USDT",
            f"  储备: {self._reserve_pct}%  可部署: {deployable:.2f} USDT",
            "-" * 60,
        ]
        for sid, r in results.items():
            lines.append(f"  {sid:<30} {r.allocation_pct:>6.1%}   {r.allocated_capital:>12} USDT")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 私有辅助函数
    # ------------------------------------------------------------------

    def _risk_parity_weights(self, strategies: list[StrategyAllocation]) -> dict[str, float]:
        """计算风险平价权重.

        每个策略的权重与其波动率成反比（等风险贡献）。
        若某策略没有波动率数据，则退化为等权重。

        Args:
            strategies: 启用的策略列表。

        Returns:
            以 strategy_id 为键的原始权重字典（未归一化）。

        """
        weights: dict[str, float] = {}
        for strat in strategies:
            vol = self._volatilities.get(strat.strategy_id, 0.0)
            if vol > 0:
                weights[strat.strategy_id] = 1.0 / vol
            else:
                # 无波动率数据 → 等权重（使用所有策略的均值倒数兜底）
                weights[strat.strategy_id] = 1.0

        # 如果所有策略都没有波动率数据，退化为等权重
        if all(v == 1.0 for v in weights.values()):
            logger.warning(
                "risk_parity_fallback_to_equal",
                reason="no volatility data available",
            )

        return weights
