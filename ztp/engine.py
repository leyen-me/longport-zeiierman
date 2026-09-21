"""TradingView 策略回测引擎的精确复刻。

撮合语义 (对应 strategy 声明: process_orders_on_close=true, default_qty_type=percent_of_equity):
1. 信号 bar 收盘时评估指标, 市价单(入场/强平)以该 bar 收盘价成交
2. strategy.exit 的 stop/limit 单在挂单的下一根 bar 起生效, 盘中触发:
   - 跳空越过触发价 -> 以开盘价成交
   - 同一根 bar 同时可触发 TP 与 SL -> 按 bar 方向假设路径:
     阳线 O->L->H->C (多头先触发 SL / 空头先触发 TP), 阴线 O->H->L->C
3. 仓位 = floor(权益 * qty_pct / 成交价), 权益含已实现盈亏
4. TP/SL 价格基于开仓后第一根 bar 收盘时的 ATR 固定 (entryAtr 不漂移)
5. 回调入场: 信号 bar 设置 pending, close 下穿/上穿 EMA 时入场;
   注意 Pine 中入场判断在挂单放弃判断之前, 故信号后第 max_pullback_bars+1 根仍可入场
"""

import numpy as np
import pandas as pd

from .config import ZTPConfig
from . import signals as zt
from . import tvfuncs as tv


def _round_tick(px: float, tick: float) -> float:
    n = round(1.0 / tick)
    return round(px * n) / n


def _session_mask(times, start: str, end: str) -> np.ndarray:
    sh, sm = map(int, start.split(":"))
    eh, em = map(int, end.split(":"))
    mins = times.hour * 60 + times.minute
    return (mins >= sh * 60 + sm) & (mins < eh * 60 + em)


def run(df: pd.DataFrame, cfg: ZTPConfig, sig: dict) -> pd.DataFrame:
    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    times = df.index
    n = len(df)

    ema_pb = tv.ema(c, cfg.pullback_ema_len)
    atr = tv.atr(h, l, c, cfg.atr_len)

    in_session = _session_mask(times, *("09:30", "16:00"))
    trade_session = _session_mask(times, *cfg.trade_session)
    pullback_session = _session_mask(times, *cfg.pullback_session)
    force_session = _session_mask(times, *cfg.force_close_session)

    dates = times.date
    new_day = np.zeros(n, dtype=bool)
    new_day[1:] = dates[1:] != dates[:-1]

    xup = sig["xup"]
    xdn = sig["xdn"]

    equity = cfg.initial_capital
    pos = 0
    qty = 0
    entry_px = 0.0
    entry_i = -1
    entry_atr = None
    exit_active = False
    exit_from = 1 << 60
    sl_px = tp_px = np.nan
    trades_today = 0
    pend_long = pend_short = False
    pl_bar = ps_bar = -1
    trades: list[dict] = []
    cur = None

    def open_trade(direction: int, i: int):
        nonlocal equity, pos, qty, entry_px, entry_i, trades_today, cur
        nonlocal pend_long, pend_short
        price = c[i]
        q = int((cfg.qty_pct_of_equity / 100.0 * equity) // price)
        if q <= 0:
            return
        pos = direction
        qty = q
        entry_px = price
        entry_i = i
        trades_today += 1
        if direction > 0:
            pend_long = False
        else:
            pend_short = False
        cur = {
            "direction": "LONG" if direction > 0 else "SHORT",
            "entry_time": times[i],
            "entry_price": price,
            "qty": q,
        }

    def close_trade(i: int, px: float, reason: str):
        nonlocal equity, pos, qty, cur, exit_active, entry_atr, entry_i
        pnl = qty * (px - entry_px) if pos > 0 else qty * (entry_px - px)
        equity += pnl
        trades.append(
            {
                **cur,
                "exit_time": times[i],
                "exit_price": px,
                "pnl": pnl,
                "exit_reason": reason,
                "bars_held": i - entry_i,
                "equity_after": equity,
            }
        )
        pos = 0
        qty = 0
        cur = None
        exit_active = False
        entry_atr = None
        entry_i = -1

    for i in range(n):
        if new_day[i]:
            trades_today = 0
            pend_long = False
            pend_short = False

        # A) 盘中 stop/limit 成交 (挂单自下一根 bar 起生效)
        if pos != 0 and exit_active and i >= exit_from:
            fill, reason = None, None
            if pos > 0:
                if o[i] <= sl_px:
                    fill, reason = o[i], "SL"
                elif o[i] >= tp_px:
                    fill, reason = o[i], "TP"
                elif l[i] <= sl_px and h[i] >= tp_px:
                    bull = c[i] >= o[i]
                    fill, reason = (sl_px, "SL") if bull else (tp_px, "TP")
                elif l[i] <= sl_px:
                    fill, reason = sl_px, "SL"
                elif h[i] >= tp_px:
                    fill, reason = tp_px, "TP"
            else:
                if o[i] >= sl_px:
                    fill, reason = o[i], "SL"
                elif o[i] <= tp_px:
                    fill, reason = o[i], "TP"
                elif h[i] >= sl_px and l[i] <= tp_px:
                    bull = c[i] >= o[i]
                    fill, reason = (tp_px, "TP") if bull else (sl_px, "SL")
                elif h[i] >= sl_px:
                    fill, reason = sl_px, "SL"
                elif l[i] <= tp_px:
                    fill, reason = tp_px, "TP"
            if fill is not None:
                close_trade(i, fill, reason)

        pos_script = pos

        # B) Pine 脚本逻辑 (bar 收盘时点)
        daily_ok = cfg.max_trades_per_day == 0 or trades_today < cfg.max_trades_per_day
        flat = pos_script == 0

        long_sig = (
            cfg.allow_long
            and daily_ok
            and flat
            and not pend_long
            and trade_session[i]
            and in_session[i]
            and xup[i]
        )
        short_sig = (
            cfg.allow_short
            and daily_ok
            and flat
            and not pend_short
            and trade_session[i]
            and in_session[i]
            and xdn[i]
        )

        if cfg.use_pullback:
            if long_sig:
                pend_long = True
                pl_bar = i
            if short_sig:
                pend_short = True
                ps_bar = i

            if i > 0 and np.isfinite(ema_pb[i]) and np.isfinite(ema_pb[i - 1]):
                touch_long = c[i] < ema_pb[i] and c[i - 1] >= ema_pb[i - 1]
                touch_short = c[i] > ema_pb[i] and c[i - 1] <= ema_pb[i - 1]
            else:
                touch_long = touch_short = False

            if (
                pend_long
                and daily_ok
                and flat
                and pullback_session[i]
                and in_session[i]
                and touch_long
            ):
                open_trade(1, i)
            if (
                pend_short
                and daily_ok
                and flat
                and pullback_session[i]
                and in_session[i]
                and touch_short
            ):
                open_trade(-1, i)

            if pend_long and (
                not pullback_session[i]
                or (cfg.max_pullback_bars > 0 and i - pl_bar > cfg.max_pullback_bars)
            ):
                pend_long = False
            if pend_short and (
                not pullback_session[i]
                or (cfg.max_pullback_bars > 0 and i - ps_bar > cfg.max_pullback_bars)
            ):
                pend_short = False
        else:
            if long_sig:
                open_trade(1, i)
            if short_sig:
                open_trade(-1, i)

        # 开仓后第一根 bar: 记录 entryAtr (Pine L319)
        if pos_script != 0 and entry_atr is None and entry_i == i - 1:
            entry_atr = atr[i]

        # 挂 TP/SL, 下一根 bar 生效 (TV 将委托价四舍五入到 mintick)
        if pos_script > 0 and entry_atr is not None:
            sl_px = _round_tick(entry_px - cfg.atr_sl_mult * entry_atr, cfg.mintick)
            tp_px = _round_tick(entry_px + cfg.atr_tp_mult * entry_atr, cfg.mintick)
            exit_active = True
            exit_from = i + 1
        elif pos_script < 0 and entry_atr is not None:
            sl_px = _round_tick(entry_px + cfg.atr_sl_mult * entry_atr, cfg.mintick)
            tp_px = _round_tick(entry_px - cfg.atr_tp_mult * entry_atr, cfg.mintick)
            exit_active = True
            exit_from = i + 1

        # 强制平仓 (Pine L336)
        if force_session[i] and pos != 0:
            close_trade(i, c[i], "EOD")
            pend_long = False
            pend_short = False

    return pd.DataFrame(trades)


def run_full(df: pd.DataFrame, cfg: ZTPConfig):
    sig = zt.compute(
        df["close"].to_numpy(float),
        df["high"].to_numpy(float),
        df["low"].to_numpy(float),
        cfg,
    )
    trades = run(df, cfg, sig)
    return trades, sig
