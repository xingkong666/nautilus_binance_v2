# Strategy Architecture Optimization — Spec

## Goal
优化策略架构，优先使用 NautilusTrader 内置函数和接口，减少重复代码，提升可维护性。

## Problem Analysis

### 1. 重复 _AdxState dataclass（HIGH）
两个策略各有约100行完全重复的 Wilder ADX 实现。
NT DirectionalMovement 不计算全 ADX，因此自定义逻辑必要，但需统一。
方案：创建 src/core/indicators.py，WilderAdx(Indicator) 子类。

### 2. Turtle 手动 Donchian deque（MEDIUM）
TurtleStrategy 用 deque[float] 手动维护高低点。
NT 内置 DonchianChannel(period) 提供 .upper_band/.lower_band/.initialized。
方案：用两个 DonchianChannel 实例替代两个 deque。

### 3. Turtle/Vegas _position_side 字符串（LOW-MEDIUM）
两策略用字符串跟踪仓位方向，与 NT portfolio 并行维护。
NT 提供 portfolio.is_flat/is_net_long/is_net_short。
方案：移除 _position_side 字符串，改用 NT portfolio 查询。
限制：_units_held/_last_add_price/_remaining_qty 无NT等价物，保留。

### 4. MicroScalp limit 价格未用 instrument.make_price()（MEDIUM）
_calc_limit_price 返回原始 Decimal，未经 tick-size 规整。
方案：改用 instrument.make_price() 规整。

## Files
- CREATE: src/core/indicators.py (WilderAdx)
- MODIFY: src/strategy/ema_pullback_atr.py
- MODIFY: src/strategy/micro_scalp.py
- MODIFY: src/strategy/turtle.py
- MODIFY: src/strategy/vegas_tunnel.py
- CREATE: tests/unit/test_wilder_adx.py
