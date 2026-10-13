"""ORB30 实时决策状态机: 与回测引擎 (ztp/orb.py) 同逻辑, 由逐根 5 分钟收盘 bar 驱动。

- 状态机只负责"信号/仓位目标/止损止盈价位", 不直接下单。
- 实盘中 TP/SL 由 runner 用实时报价监控, 触发后调用 notify_exit_fill() 通知本状态机。
- 重启后可用 prime() 从当日已有 bar 重建开盘区间状态 (不追补历史入场)。
"""

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from .orb import ORBConfig, _round_tick, prior_daily_atr


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
    # v2 过滤 + 移动止损
    day_open: float = np.nan
    prev_close: float = np.nan
    first_dir: int = 0
    init_sl_px: float = np.nan
    R: float = np.nan
    run_ext: float = np.nan
    last_date: date | None = None
    # v2.1 质变过滤: 前一日日线 ATR + 跳空是否在 OR 内回补
    atr_daily: float = np.nan
    or_gap_filled: bool = False


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

    def _prev_day_close(self, today) -> float:
        prev = self.df[self.df.index.date < today]
        return float(prev["close"].iloc[-1]) if len(prev) else float("nan")

    def _daily_atr(self, today, length: int = 14) -> float:
        """前一日为止的日线 ATR(14); 与 ztp/orb.py 引擎共用 prior_daily_atr; 不足则 NaN。"""
        if len(self.df) == 0:
            return float("nan")
        s = prior_daily_atr(self.df, self.cfg.tz, length)
        key = pd.Timestamp(today)
        key = key.tz_localize(s.index.tz) if s.index.tz is not None else key
        v = s.get(key, np.nan)
        return float(v) if v == v else float("nan")

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
                st.day_open = float(r["open"])
                st.first_dir = 1 if r["close"] >= r["open"] else -1
                st.prev_close = self._prev_day_close(today)
                st.atr_daily = self._daily_atr(today)
                st.or_gap_filled = False
            if in_sess:
                st.session_high = max(st.session_high, r["high"])
                st.session_low = min(st.session_low, r["low"])
                if st.prev_close == st.prev_close and r["low"] <= st.prev_close <= r["high"]:
                    st.or_gap_filled = True
            if in_sess_prev and not in_sess:
                st.session_done = True
            if st.session_done:
                if r["close"] > st.session_high:
                    st.bull, st.bear = st.bull + 1, 0
                elif r["close"] < st.session_low:
                    st.bear, st.bull = st.bear + 1, 0
                else:
                    st.bull = st.bear = 0
                long_sig = st.bull >= self.cfg.brk_candles
                short_sig = st.bear >= self.cfg.brk_candles
                if ts.dayofweek in self.cfg.trade_days and (long_sig or short_sig):
                    # 历史已出现突破信号 -> 不追补; first_signal_only 时无论过滤与否都锁当日
                    if self.cfg.first_signal_only \
                            or (long_sig and self._filter_ok(1, r["close"])) \
                            or (short_sig and self._filter_ok(-1, r["close"])):
                        st.traded_today = True

    def _filter_ok(self, direction: int, close: float) -> bool:
        """v2.1 过滤: 跳空方向 + 首根方向 + 顺势发力 + 跳空未回补。"""
        cfg = self.cfg
        st = self.st
        gap_known = st.prev_close == st.prev_close
        atr_ok = st.atr_daily == st.atr_daily and st.atr_daily > 0
        if direction > 0:
            gap_ok = (not cfg.use_gap_filter) or (not gap_known) or st.day_open > st.prev_close
            fd_ok = (not cfg.use_firstbar_filter) or st.first_dir > 0
            str_ok = (not cfg.use_strength_filter) or (not atr_ok) or \
                (close - st.day_open) >= cfg.strength_atr_mult * st.atr_daily
        else:
            gap_ok = (not cfg.use_gap_filter) or (not gap_known) or st.day_open < st.prev_close
            fd_ok = (not cfg.use_firstbar_filter) or st.first_dir < 0
            str_ok = (not cfg.use_strength_filter) or (not atr_ok) or \
                (st.day_open - close) >= cfg.strength_atr_mult * st.atr_daily
        gapfill_ok = (not cfg.use_gapfill_filter) or (not st.or_gap_filled)
        return gap_ok and fd_ok and str_ok and gapfill_ok

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
        o = float(self.df["open"].iloc[i])
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

        # 开盘区间构建 (v2: 记录开盘价/首根方向/昨日收盘; v2.1: 日ATR/跳空回补)
        if in_sess and not in_sess_prev:
            st.session_high, st.session_low = h, l
            st.session_done, st.traded_today, st.bull, st.bear = False, False, 0, 0
            st.day_open = o
            st.first_dir = 1 if c >= o else -1
            st.prev_close = self._prev_day_close(ts.date())
            st.atr_daily = self._daily_atr(ts.date())
            st.or_gap_filled = False
        if in_sess:
            st.session_high = max(st.session_high, h)
            st.session_low = min(st.session_low, l)
            if st.prev_close == st.prev_close and l <= st.prev_close <= h:
                st.or_gap_filled = True
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

        # 入场 (v2 过滤 + 当日首个突破锁定)
        if st.pos_dir == 0 and st.session_done and not st.traded_today and day_ok and not eod:
            long_sig = st.bull >= cfg.brk_candles
            short_sig = st.bear >= cfg.brk_candles
            if long_sig or short_sig:
                if cfg.first_signal_only:
                    st.traded_today = True          # 首个突破出现即锁当日
                if long_sig and self._filter_ok(1, c):
                    self._open(1, i, c, st.session_high, actions)
                elif short_sig and self._filter_ok(-1, c):
                    self._open(-1, i, c, st.session_low, actions)

        # 强平
        if st.pos_dir != 0 and eod:
            self.notify_exit_fill(_round_tick(c, cfg.mintick), "EOD")
            actions.append(StrategyAction("eod", c, i))

        # v2 移动止损: 用本根极值移动 (下一根生效)
        if st.pos_dir != 0 and cfg.use_trail and st.R == st.R:
            if st.pos_dir > 0:
                st.run_ext = max(st.run_ext, h)
                st.sl_px = max(st.init_sl_px,
                               _round_tick(st.run_ext - cfg.trail_mult * st.R, cfg.mintick))
            else:
                st.run_ext = min(st.run_ext, l)
                st.sl_px = min(st.init_sl_px,
                               _round_tick(st.run_ext + cfg.trail_mult * st.R, cfg.mintick))

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
        # v2: 初始止损/风险/极值 (移动止损锚点)
        st.init_sl_px = st.sl_px
        st.R = abs(st.entry_px - st.sl_px)
        st.run_ext = float(self.df["high"].iloc[i]) if direction > 0 else float(self.df["low"].iloc[i])
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
