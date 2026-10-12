"""ORB30 实时决策状态机: 与回测引擎 (ztp/orb.py) 同逻辑, 由逐根 5 分钟收盘 bar 驱动。

- 状态机只负责"信号/仓位目标/止损止盈价位", 不直接下单。
- 实盘中 TP/SL 由 runner 用实时报价监控, 触发后调用 notify_exit_fill() 通知本状态机。
- 重启后可用 prime() 从当日已有 bar 重建开盘区间状态 (不追补历史入场)。
"""

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from .orb import ORBConfig, _round_tick


@dataclass
class ORBLiveState:
    session_high: float = np.nan
    session_low: float = np.nan
    session_done: bool = False
    traded_today: bool = False
    bull: int = 0
    bear: int = 0
    pos_dir: int = 0
    pos_qty: int = 0
    entry_px: float = 0.0
    entry_i: int = -1
    exit_armed: bool = False
    exit_from: int = 1 << 60
    sl_px: float = np.nan
    tp_px: float = np.nan
    last_date: date | None = None


@dataclass
class StrategyAction:
    kind: str  # enter_long | enter_short | arm_tpsl | eod | info
    price: float = 0.0
    bar_index: int = -1
    text: str = ""


class ORBLiveStrategy:
    def __init__(self, cfg: ORBConfig, warmup: pd.DataFrame):
        self.cfg = cfg
        self.df = warmup.copy()
        self.st = ORBLiveState()
        self.closed_trades: list[dict] = []

    # ---------- 数据 ----------

    def append_bar(self, ts, o: float, h: float, l: float, c: float) -> int:
        row = pd.DataFrame(
            {"open": [o], "high": [h], "low": [l], "close": [c]}, index=[ts]
        )
        self.df = pd.concat([self.df[~self.df.index.isin(row.index)], row]).sort_index()
        return len(self.df) - 1

    def _sess(self, m: int) -> bool:
        start = 9 * 60 + 30
        return start <= m < start + self.cfg.or_minutes

    @staticmethod
    def _mod(ts) -> int:
        return ts.hour * 60 + ts.minute

    def _eod_passed(self, m: int) -> bool:
        return m >= self.cfg.eod_hour * 60 + self.cfg.eod_minute

    # ---------- 重启预热: 从当日已有 bar 重建开盘区间状态 ----------

    def prime(self):
        st = self.st
        today = self.df.index[-1].date()
        st.last_date = today
        subset = self.df[self.df.index.date == today]
        for ts, r in subset.iterrows():
            m = self._mod(ts)
            in_sess = self._sess(m)
            prev_ts = self.df.index[self.df.index.get_loc(ts) - 1] \
                if self.df.index.get_loc(ts) > 0 else None
            in_sess_prev = self._sess(self._mod(prev_ts)) if prev_ts is not None else False
            if in_sess and not in_sess_prev:
                st.session_high, st.session_low = r["high"], r["low"]
                st.session_done, st.traded_today, st.bull, st.bear = False, False, 0, 0
            if in_sess:
                st.session_high = max(st.session_high, r["high"])
                st.session_low = min(st.session_low, r["low"])
            if in_sess_prev and not in_sess:
                st.session_done = True
            if st.session_done:
                if r["close"] > st.session_high:
                    st.bull, st.bear = st.bull + 1, 0
                elif r["close"] < st.session_low:
                    st.bear, st.bull = st.bear + 1, 0
                else:
                    st.bull = st.bear = 0
                if (st.bull >= self.cfg.brk_candles or st.bear >= self.cfg.brk_candles) \
                        and ts.dayofweek in self.cfg.trade_days:
                    st.traded_today = True  # 历史已触发过入场, 不追补

    # ---------- 外部事件 ----------

    def notify_exit_fill(self, price: float, reason: str):
        st = self.st
        if st.pos_dir == 0:
            return
        self.closed_trades.append(
            {
                "direction": "LONG" if st.pos_dir > 0 else "SHORT",
                "entry_time": self.df.index[st.entry_i],
                "entry_price": st.entry_px,
                "exit_time": self.df.index[-1],
                "exit_price": price,
                "exit_reason": reason,
                "bars_held": len(self.df) - 1 - st.entry_i,
            }
        )
        st.pos_dir = 0
        st.pos_qty = 0
        st.exit_armed = False
        st.entry_i = -1

    # ---------- 每根收盘 bar 决策 ----------

    def on_bar(self) -> list[StrategyAction]:
        cfg = self.cfg
        st = self.st
        i = len(self.df) - 1
        ts = self.df.index[i]
        h = float(self.df["high"].iloc[i])
        l = float(self.df["low"].iloc[i])
        c = float(self.df["close"].iloc[i])

        actions: list[StrategyAction] = []
        st.last_date = ts.date()
        m = self._mod(ts)
        in_sess = self._sess(m)
        prev_ts = self.df.index[i - 1] if i > 0 else None
        in_sess_prev = self._sess(self._mod(prev_ts)) if prev_ts is not None else False
        day_ok = ts.dayofweek in cfg.trade_days
        eod = self._eod_passed(m)

        # 开盘区间构建
        if in_sess and not in_sess_prev:
            st.session_high, st.session_low = h, l
            st.session_done, st.traded_today, st.bull, st.bear = False, False, 0, 0
        if in_sess:
            st.session_high = max(st.session_high, h)
            st.session_low = min(st.session_low, l)
        if in_sess_prev and not in_sess:
            st.session_done = True

        # 突破计数
        if st.session_done:
            if c > st.session_high:
                st.bull, st.bear = st.bull + 1, 0
            elif c < st.session_low:
                st.bear, st.bull = st.bear + 1, 0
            else:
                st.bull = st.bear = 0

        # 入场
        if st.pos_dir == 0 and st.session_done and not st.traded_today and day_ok and not eod:
            if st.bull >= cfg.brk_candles:
                self._open(1, i, c, st.session_high, actions)
            elif st.bear >= cfg.brk_candles:
                self._open(-1, i, c, st.session_low, actions)

        # 强平
        if st.pos_dir != 0 and eod:
            self.notify_exit_fill(_round_tick(c, cfg.mintick), "EOD")
            actions.append(StrategyAction("eod", c, i))

        return actions

    def _open(self, direction: int, i: int, px: float, anchor: float,
              actions: list[StrategyAction]):
        st = self.st
        st.pos_dir = direction
        st.pos_qty = 0
        st.entry_px = _round_tick(px, self.cfg.mintick)
        st.entry_i = i
        st.traded_today = True
        if direction > 0:
            st.sl_px = _round_tick(anchor - self.cfg.sl_points, self.cfg.mintick)
            st.tp_px = _round_tick(anchor + self.cfg.tp_points, self.cfg.mintick)
        else:
            st.sl_px = _round_tick(anchor + self.cfg.sl_points, self.cfg.mintick)
            st.tp_px = _round_tick(anchor - self.cfg.tp_points, self.cfg.mintick)
        st.exit_armed = True
        st.exit_from = i
        actions.append(StrategyAction("enter_long" if direction > 0 else "enter_short", px, i))
        actions.append(
            StrategyAction("arm_tpsl", 0.0, i, f"TP={st.tp_px:.2f} SL={st.sl_px:.2f}")
        )

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
