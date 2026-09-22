"""实时决策状态机: 与回测引擎 (ztp/engine.py) 完全同逻辑, 但由逐根收盘 bar 驱动。

差别:
- 回测引擎在 bar 内部模拟 stop/limit 成交; 实盘中 TP/SL 由 runner 用实时报价监控,
  触发后调用 notify_exit_fill() 通知本状态机。
- 其余 (信号/回调入场/超时/每日次数/entryAtr 时点/强平) 与回测逐行对应。
"""

from dataclasses import dataclass, field
from datetime import date

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


@dataclass
class LiveState:
    pos_dir: int = 0
    pos_qty: int = 0
    entry_px: float = 0.0
    entry_i: int = -1
    entry_atr: float | None = None
    exit_armed: bool = False
    exit_from: int = 1 << 60
    sl_px: float = np.nan
    tp_px: float = np.nan
    trades_today: int = 0
    pend_long: bool = False
    pend_short: bool = False
    pl_bar: int = -1
    ps_bar: int = -1
    last_date: date | None = None


@dataclass
class StrategyAction:
    kind: str  # enter_long | enter_short | eod | info
    price: float = 0.0
    bar_index: int = -1
    text: str = ""


class LiveStrategy:
    def __init__(self, cfg: ZTPConfig, warmup: pd.DataFrame):
        self.cfg = cfg
        self.df = warmup.copy()
        self.st = LiveState()
        self.closed_trades: list[dict] = []

    # ---------- 数据与指标 ----------

    def append_bar(self, ts, o: float, h: float, l: float, c: float) -> int:
        row = pd.DataFrame(
            {"open": [o], "high": [h], "low": [l], "close": [c]}, index=[ts]
        )
        self.df = pd.concat([self.df[~self.df.index.isin(row.index)], row]).sort_index()
        return len(self.df) - 1

    def _indicators(self):
        cfg = self.cfg
        o = self.df["open"].to_numpy(float)
        h = self.df["high"].to_numpy(float)
        l = self.df["low"].to_numpy(float)
        c = self.df["close"].to_numpy(float)
        sig = zt.compute(c, h, l, cfg)
        extras = {
            "ema_pb": tv.ema(c, cfg.pullback_ema_len),
            "atr": tv.atr(h, l, c, cfg.atr_len),
        }
        return sig, extras

    def _session_masks(self):
        times = self.df.index
        cfg = self.cfg
        return (
            _session_mask(times, "09:30", "16:00"),
            _session_mask(times, *cfg.trade_session),
            _session_mask(times, *cfg.pullback_session),
            _session_mask(times, *cfg.force_close_session),
        )

    # ---------- 外部事件 ----------

    def notify_exit_fill(self, price: float, reason: str):
        st = self.st
        if st.pos_dir == 0:
            return
        pnl = st.pos_qty * (price - st.entry_px) if st.pos_dir > 0 else st.pos_qty * (st.entry_px - price)
        self.closed_trades.append(
            {
                "direction": "LONG" if st.pos_dir > 0 else "SHORT",
                "entry_time": self.df.index[st.entry_i],
                "entry_price": st.entry_px,
                "exit_time": self.df.index[-1],
                "exit_price": price,
                "pnl_underlying_per_share": round(pnl / max(st.pos_qty, 1), 4),
                "exit_reason": reason,
                "bars_held": len(self.df) - 1 - st.entry_i,
            }
        )
        st.pos_dir = 0
        st.pos_qty = 0
        st.exit_armed = False
        st.entry_atr = None
        st.entry_i = -1

    # ---------- 每根收盘 bar 的决策 ----------

    def on_bar(self) -> list[StrategyAction]:
        cfg = self.cfg
        st = self.st
        i = len(self.df) - 1
        ts = self.df.index[i]
        c = self.df["close"].to_numpy(float)[-1]

        actions: list[StrategyAction] = []
        cur_date = ts.date()
        if st.last_date is not None and cur_date != st.last_date:
            st.trades_today = 0
            st.pend_long = False
            st.pend_short = False
        st.last_date = cur_date

        sig, extras = self._indicators()
        in_sess, trade_sess, pull_sess, force_sess = self._session_masks()
        ema_pb, atr = extras["ema_pb"], extras["atr"]
        daily_ok = cfg.max_trades_per_day == 0 or st.trades_today < cfg.max_trades_per_day
        flat = st.pos_dir == 0

        long_sig = (
            cfg.allow_long and daily_ok and flat and not st.pend_long
            and trade_sess[i] and in_sess[i] and sig["xup"][i]
        )
        short_sig = (
            cfg.allow_short and daily_ok and flat and not st.pend_short
            and trade_sess[i] and in_sess[i] and sig["xdn"][i]
        )

        if cfg.use_pullback:
            if long_sig:
                st.pend_long = True
                st.pl_bar = i
            if short_sig:
                st.pend_short = True
                st.ps_bar = i

            if i > 0 and np.isfinite(ema_pb[i]) and np.isfinite(ema_pb[i - 1]):
                touch_long = c < ema_pb[i] and self.df["close"].to_numpy(float)[i - 1] >= ema_pb[i - 1]
                touch_short = c > ema_pb[i] and self.df["close"].to_numpy(float)[i - 1] <= ema_pb[i - 1]
            else:
                touch_long = touch_short = False

            if st.pend_long and daily_ok and flat and pull_sess[i] and in_sess[i] and touch_long:
                st.pos_dir = 1
                st.pos_qty = 0
                st.entry_px = c
                st.entry_i = i
                st.trades_today += 1
                st.pend_long = False
                actions.append(StrategyAction("enter_long", c, i))
            if st.pend_short and daily_ok and flat and pull_sess[i] and in_sess[i] and touch_short:
                st.pos_dir = -1
                st.pos_qty = 0
                st.entry_px = c
                st.entry_i = i
                st.trades_today += 1
                st.pend_short = False
                actions.append(StrategyAction("enter_short", c, i))

            if st.pend_long and (
                not pull_sess[i] or (cfg.max_pullback_bars > 0 and i - st.pl_bar > cfg.max_pullback_bars)
            ):
                st.pend_long = False
            if st.pend_short and (
                not pull_sess[i] or (cfg.max_pullback_bars > 0 and i - st.ps_bar > cfg.max_pullback_bars)
            ):
                st.pend_short = False
        else:
            if long_sig:
                st.pos_dir = 1
                st.entry_px = c
                st.entry_i = i
                st.trades_today += 1
                actions.append(StrategyAction("enter_long", c, i))
            if short_sig:
                st.pos_dir = -1
                st.entry_px = c
                st.entry_i = i
                st.trades_today += 1
                actions.append(StrategyAction("enter_short", c, i))

        pos_script = st.pos_dir

        if pos_script != 0 and st.entry_atr is None and st.entry_i == i - 1:
            st.entry_atr = atr[i]

        if pos_script > 0 and st.entry_atr is not None:
            st.sl_px = _round_tick(st.entry_px - cfg.atr_sl_mult * st.entry_atr, cfg.mintick)
            st.tp_px = _round_tick(st.entry_px + cfg.atr_tp_mult * st.entry_atr, cfg.mintick)
            if not st.exit_armed:
                st.exit_armed = True
                st.exit_from = i + 1
                actions.append(
                    StrategyAction("arm_tpsl", 0.0, i, f"TP={st.tp_px:.2f} SL={st.sl_px:.2f}")
                )
        elif pos_script < 0 and st.entry_atr is not None:
            st.sl_px = _round_tick(st.entry_px + cfg.atr_sl_mult * st.entry_atr, cfg.mintick)
            st.tp_px = _round_tick(st.entry_px - cfg.atr_tp_mult * st.entry_atr, cfg.mintick)
            if not st.exit_armed:
                st.exit_armed = True
                st.exit_from = i + 1
                actions.append(
                    StrategyAction("arm_tpsl", 0.0, i, f"TP={st.tp_px:.2f} SL={st.sl_px:.2f}")
                )

        if force_sess[i] and st.pos_dir != 0:
            px = self.df["close"].to_numpy(float)[-1]
            self.notify_exit_fill(px, "EOD")
            st.pend_long = False
            st.pend_short = False
            actions.append(StrategyAction("eod", px, i))

        return actions

    # ---------- TP/SL 盘中触发 (由 runner 用实时价调用) ----------

    def check_tpsl(self, price: float) -> StrategyAction | None:
        st = self.st
        if st.pos_dir == 0 or not st.exit_armed:
            return None
        if len(self.df) < st.exit_from:
            return None
        if st.pos_dir > 0:
            if price <= st.sl_px:
                return StrategyAction("exit_sl", price)
            if price >= st.tp_px:
                return StrategyAction("exit_tp", price)
        else:
            if price >= st.sl_px:
                return StrategyAction("exit_sl", price)
            if price <= st.tp_px:
                return StrategyAction("exit_tp", price)
        return None
