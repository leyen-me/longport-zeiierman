from dataclasses import dataclass


@dataclass
class ZTPConfig:
    symbol: str = "SPY.US"

    # Pine: signalMode / fl / pl / ps / sl / ml / ts / db / zn / sn / sw / mw / th
    signal_mode: str = "Cross"  # Exhaustion | Release | Cross
    pulse_range: int = 21       # fl
    pulse_stoch: int = 9        # pl
    pulse_smooth: int = 7       # ps
    trend_range: int = 55       # sl
    macro_trend: int = 120      # ml
    trend_smooth: int = 5       # ts
    trend_persistence: float = 9.0  # db
    exhaustion_zone: int = 20   # zn
    sensitivity: int = 7        # sn
    sw: float = 0.18
    mw: float = 0.72
    th: float = 0.92

    # Pine: 方向 / 交易时段 / 交易频率
    allow_long: bool = True
    allow_short: bool = True
    trade_session: tuple = ("09:30", "12:30")      # 信号发生时段
    pullback_session: tuple = ("09:30", "12:30")   # 回调入场时段
    force_close_session: tuple = ("15:45", "16:00")  # 强制平仓时段
    max_trades_per_day: int = 2

    # Pine: 回调入场
    use_pullback: bool = True
    pullback_ema_len: int = 12
    max_pullback_bars: int = 9  # 0 = 不限制

    # Pine: 止损止盈
    tpsl_mode: str = "ATR"  # ATR | PCT
    take_profit_percent: float = 0.5
    stop_loss_percent: float = 0.25
    atr_len: int = 14
    atr_tp_mult: float = 4.2
    atr_sl_mult: float = 1.5

    # TradingView 策略属性
    initial_capital: float = 1_000_000.0
    qty_pct_of_equity: float = 10.0
    mintick: float = 0.01  # syminfo.mintick for SPY
