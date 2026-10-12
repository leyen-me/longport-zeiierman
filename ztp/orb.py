"""ORB30 (开盘区间突破) 的 TradingView 语义回测复刻。

对应 tradingview/ORB30.pine:

  ① 开盘区间: 09:30 (美东) 起 or_minutes 分钟的最高/最低 (默认 30 分钟)
  ② 突破确认: 区间结束后, 连续 brk_candles 根收盘站上/跌破边界
  ③ 入场:     突破 -> 顺势做多/做空, 每日仅 1 笔; 收盘价市价成交
  ④ 出场:     固定点数止损/止盈, 价格锚定在"区间边界"(不是成交价):
                多头 SL = ORhigh - sl_points, TP = ORhigh + tp_points
                空头 SL = ORlow  + sl_points, TP = ORlow  - tp_points
  ⑤ 强平:     当 ET 时间 >= eod, 持仓一律收盘平仓; 之后禁止开仓

撮合语义与 ztp.engine 一致 (process_orders_on_close=true):
  - 市价单 (入场/强平) 以信号 bar 的收盘价成交
  - strategy.exit 的 stop/limit 自下一根 bar 起生效; 盘中触发时:
      * 跳空越过触发价 -> 开盘价成交
      * 同根 bar 同时可触发 TP/SL -> 按 bar 方向假设路径(阳线 O->L->H->C)
  - 价格按 mintick 取整
"""

from dataclasses import dataclass

import math

import numpy as np
import pandas as pd


@dataclass
class ORBConfig:
    symbol: str = "SPY.US"

    # Pine: tradeMonday..tradeFriday —— 0=Mon .. 4=Fri
    trade_days: tuple = (0, 1, 2)

    # 开盘区间
    or_minutes: int = 30            # "开盘区间时长"
    brk_candles: int = 2            # "连续突破K线数"

    # 仓位 (固定股数, 与 TV 回测一致)
    qty: int = 80

    # 风险 (固定点数)
    sl_points: float = 1.1
    tp_points: float = 2.1

    # 强平 (ET)
    eod_hour: int = 12
    eod_minute: int = 0

    tz: str = "America/New_York"
    mintick: float = 0.01
    initial_capital: float = 100_000.0


def _round_tick(px: float, tick: float) -> float:
    """按最小变动价位取整 (四舍五入 .5 进位, 对齐 TradingView 而非 Python 银行家进位)。"""
    n = round(1.0 / tick)
    return math.floor(px * n + 0.5) / n


def load_bars_csv(path: str) -> pd.DataFrame:
    """读取 TradingView "Export chart data" 的 5min CSV (time,open,high,low,close[,Volume])。

    支持 Unix 秒/毫秒时间戳或 ISO 字符串; 支持 glob 与目录; 仅保留 09:30-16:00 ET。
    """
    from .paths import ROOT

    hits = sorted(ROOT.glob(path)) if any(ch in path for ch in "*?[") else [ROOT / path]
    if len(hits) == 1 and hits[0].is_dir():
        hits = sorted(hits[0].glob("*.csv"))
    frames = []
    for f in hits:
        d = pd.read_csv(f)
        d = d.rename(columns={c: c.strip().lower() for c in d.columns})
        tcol = next(c for c in d.columns if c in ("time", "datetime", "date"))
        tvals = d[tcol]
        if pd.api.types.is_numeric_dtype(tvals):
            unit = "ms" if tvals.astype("float").median() > 1e12 else "s"
            idx = pd.to_datetime(tvals, unit=unit, utc=True).dt.tz_convert("America/New_York")
        else:
            idx = pd.to_datetime(tvals, utc=True).dt.tz_convert("America/New_York")
        d = d.set_index(idx)
        keep = {"open": "first", "high": "first", "low": "first", "close": "first"}
        if "volume" in d.columns:
            keep["volume"] = "first"
        frames.append(d[list(keep)].astype(float))
    df = pd.concat(frames).loc[lambda x: ~x.index.duplicated()].sort_index()
    mod = df.index.hour * 60 + df.index.minute
    return df[(mod >= 570) & (mod < 960)]


def _minute_of_day(index: pd.DatetimeIndex, tz: str) -> np.ndarray:
    local = index.tz_convert(tz)
    return (local.hour * 60 + local.minute).to_numpy()


def run(df: pd.DataFrame, cfg: ORBConfig) -> pd.DataFrame:
    """df: 5 分钟 OHLCV, 索引为美东 tz-aware DatetimeIndex。"""
    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    times = df.index
    n = len(df)

    mod = _minute_of_day(times, cfg.tz)
    dow = times.tz_convert(cfg.tz).dayofweek.to_numpy()
    dates = times.tz_convert(cfg.tz).date

    or_start = 9 * 60 + 30
    or_end = or_start + cfg.or_minutes
    in_sess = (mod >= or_start) & (mod < or_end)
    eod_min = cfg.eod_hour * 60 + cfg.eod_minute
    eod_passed = mod >= eod_min
    day_ok = np.isin(dow, cfg.trade_days)

    new_day = np.zeros(n, dtype=bool)
    new_day[1:] = dates[1:] != dates[:-1]

    equity = cfg.initial_capital
    pos = 0
    qty = 0
    entry_px = 0.0
    entry_i = -1
    exit_active = False
    exit_from = 1 << 60
    sl_px = tp_px = np.nan

    session_high = session_low = np.nan
    session_done = False
    traded_today = False
    bull = 0
    bear = 0

    trades: list[dict] = []
    cur = None

    def open_trade(direction: int, i: int, anchor: float):
        nonlocal equity, pos, qty, entry_px, entry_i, exit_active, exit_from, sl_px, tp_px, cur
        px = _round_tick(c[i], cfg.mintick)
        q = int(cfg.qty)
        if q <= 0:
            return
        pos = direction
        qty = q
        entry_px = px
        entry_i = i
        if direction > 0:
            sl_px = _round_tick(anchor - cfg.sl_points, cfg.mintick)
            tp_px = _round_tick(anchor + cfg.tp_points, cfg.mintick)
        else:
            sl_px = _round_tick(anchor + cfg.sl_points, cfg.mintick)
            tp_px = _round_tick(anchor - cfg.tp_points, cfg.mintick)
        exit_active = True
        exit_from = i + 1
        cur = {
            "direction": "LONG" if direction > 0 else "SHORT",
            "entry_time": times[i],
            "entry_price": px,
            "anchor": anchor,
            "qty": q,
            "sl": sl_px,
            "tp": tp_px,
        }

    def close_trade(i: int, px: float, reason: str):
        nonlocal equity, pos, qty, cur, exit_active, entry_i
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
        entry_i = -1

    for i in range(n):
        # A) 盘中 stop/limit 成交 (自入场下一根 bar 起生效)
        if pos != 0 and exit_active and i >= exit_from:
            fill = reason = None
            if pos > 0:
                if o[i] <= sl_px:
                    fill, reason = o[i], "SL"
                elif o[i] >= tp_px:
                    fill, reason = o[i], "TP"
                elif l[i] <= sl_px and h[i] >= tp_px:
                    bull_bar = c[i] >= o[i]
                    fill, reason = (sl_px, "SL") if bull_bar else (tp_px, "TP")
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
                    bull_bar = c[i] >= o[i]
                    fill, reason = (tp_px, "TP") if bull_bar else (sl_px, "SL")
                elif h[i] >= sl_px:
                    fill, reason = sl_px, "SL"
                elif l[i] <= tp_px:
                    fill, reason = tp_px, "TP"
            if fill is not None:
                close_trade(i, fill, reason)

        # B) 构建开盘区间 (Pine: 会话内重置/累积, 会话结束锁定)
        if in_sess[i] and (i == 0 or not in_sess[i - 1]):
            session_high = h[i]
            session_low = l[i]
            session_done = False
            traded_today = False
            bull = bear = 0
        if in_sess[i]:
            session_high = max(session_high, h[i])
            session_low = min(session_low, l[i])
        if i > 0 and in_sess[i - 1] and not in_sess[i]:
            session_done = True

        # C) 突破计数
        if session_done:
            if c[i] > session_high:
                bull += 1
                bear = 0
            elif c[i] < session_low:
                bear += 1
                bull = 0
            else:
                bull = bear = 0

        # D) 入场 (每日 1 笔, 过了强平时间不再开仓)
        if (
            pos == 0
            and session_done
            and not traded_today
            and day_ok[i]
            and not eod_passed[i]
        ):
            if bull >= cfg.brk_candles:
                open_trade(1, i, session_high)
                traded_today = True
            elif bear >= cfg.brk_candles:
                open_trade(-1, i, session_low)
                traded_today = True

        # 入场当根: 若收盘已越过止盈, TV 会在同一根收盘即刻成交 (bars=0)
        if pos != 0 and ((pos > 0 and c[i] >= tp_px) or (pos < 0 and c[i] <= tp_px)):
            close_trade(i, _round_tick(c[i], cfg.mintick), "TP")

        # E) 强平: 过点仍持仓则平; 跨日兜底
        if pos != 0 and (eod_passed[i] or new_day[i]):
            close_trade(i, _round_tick(c[i], cfg.mintick), "EOD")

    return pd.DataFrame(trades)
