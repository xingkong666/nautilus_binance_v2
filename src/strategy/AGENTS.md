# AGENTS.md — src/strategy/

信号生成层。**策略绝不直接调用 `submit_order()`。**

---

## 文件说明

| 文件 | 职责 |
|---|---|
| `base.py` | `BaseStrategy` + `BaseStrategyConfig`。所有策略的基类。 |
| `signal.py` | 信号构建与校验的辅助工具函数。 |
| `ema_cross.py` | `EMACrossStrategy` — EMA 交叉策略，可选 ADX/RSI 过滤器。 |
| `ema_pullback_atr.py` | `EMAPullbackATRStrategy` — EMA 趋势 + ATR 回调入场。 |
| `micro_scalp.py` | `MicroScalpStrategy` — 短线刷单，使用限价单。 |
| `rsi_strategy.py` | `RSIStrategy` — RSI 均值回归。 |
| `turtle.py` | `TurtleStrategy` — 唐奇安通道突破，ATR 止损 + 单位加仓金字塔。 |
| `vegas_tunnel.py` | `VegasTunnelStrategy` — EMA 通道突破，斐波那契分批止盈。 |

---

## BaseStrategy 职责

`BaseStrategy(Strategy)`（继承 NautilusTrader `Strategy`）：

- 在 `on_start()` 中订阅 `bar_type`。
- 每根新 K 线触发 `generate_signal(bar)` — 子类实现此方法。
- 将 `SignalEvent` 发布到 `EventBus`（不直接提交订单）。
- 管理基于 ATR 和百分比的止损/止盈订单。
- 处理 `on_stop()`：可选在策略停止时平掉所有持仓（`close_positions_on_stop=True`）。
- 提供 `_size_order()`，根据 `trade_size`、`margin_pct_per_trade`、`gross_exposure_pct_per_trade` 或 `capital_pct_per_trade` 计算下单数量。

---

## BaseStrategyConfig 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `instrument_id` | `InstrumentId` | Nautilus 交易对标识符 |
| `bar_type` | `BarType` | 订阅的 K 线类型 |
| `close_positions_on_stop` | `bool` | 策略停止时平仓（默认 `True`） |
| `trade_size` | `Decimal` | 固定数量兜底值 |
| `margin_pct_per_trade` | `float \| None` | 按保证金占权益比 × 杠杆定仓 |
| `gross_exposure_pct_per_trade` | `float \| None` | 按名义价值占权益比定仓 |
| `capital_pct_per_trade` | `float \| None` | 按资金占权益比定仓 |
| `sizing_leverage` | `float` | 保证金比定仓的杠杆乘数 |
| `atr_period` | `int` | ATR 周期（默认 14） |
| `atr_sl_multiplier` | `float \| None` | ATR 止损乘数 |
| `atr_tp_multiplier` | `float \| None` | ATR 止盈乘数 |
| `live_warmup_bars` | `int` | 实盘开始前跳过的预热 K 线数 |

---

## 新增策略步骤

1. 在 `src/strategy/<name>.py` 中创建 `<Name>Config(BaseStrategyConfig, frozen=True)` 和 `<Name>Strategy(BaseStrategy)`。
2. 实现 `generate_signal(self, bar: Bar) -> SignalDirection | None`。
3. 在 `bootstrap._STRATEGY_REGISTRY` 中注册，并在 `AppFactory` 中添加 `create_<name>_strategy()`。
4. 在 `AppFactory.create_strategy_from_config()` 中添加对应分支。
5. 在 `tests/unit/test_<name>_strategy.py` 中编写单元测试。
6. 在 `tests/regression/test_<name>_baseline.py` 中添加回归基准。

---

## 信号契约

```python
# 发布做多信号：
self._event_bus.publish(
    SignalEvent(
        instrument_id=str(self.config.instrument_id),
        direction=SignalDirection.LONG,
        strength=1.0,
        source=self.id.value,
    )
)
```

`generate_signal()` 返回 `SignalDirection.LONG`、`SignalDirection.SHORT`、`SignalDirection.FLAT` 或 `None`（本 K 线无信号）。基类负责调用 `EventBus.publish()`。

---

## 定仓优先级

`capital_pct_per_trade` > `gross_exposure_pct_per_trade` > `margin_pct_per_trade` > `trade_size`（固定数量）。
