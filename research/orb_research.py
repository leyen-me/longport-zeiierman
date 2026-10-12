"""ORB30 研究台: 事件抽取 + 多种入场/出场/过滤变体的统一回测。

数据: data_cache/SPY_US_non_min2.parquet -> 重采样 5min。
基线语义严格对齐 ztp.orb (Mon-Wed, OR=30min, brk=2, SL=1.1, TP=2.1, ET12:00 强平)。
"""

import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

TZ = "America/New_York"
OR_START = 9 * 60 + 30
OR_MIN = 30
EOD_MIN = 12 * 60
SL_PTS = 1.1
TP_PTS = 2.1
BRK = 2
TRADE_DOW = (0, 1, 2)
MINTICK = 0.01
QTY = 80


def load_5min():
    from ztp.paths import ROOT
    d = pd.read_parquet(ROOT / "data_cache/SPY_US_non_min2.parquet")
    df = (
        d.resample("5min", label="left", closed="left")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna()
    )
    mod = df.index.hour * 60 + df.index.minute
    return df[(mod >= 570) & (mod < 960)]


def rnd(px, tick=MINTICK):
    import math
    n = round(1.0 / tick)
    return math.floor(px * n + 0.5) / n


@dataclass
class DayData:
    date: np.ndarray
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    mod: np.ndarray
    dow: np.ndarray
    ts: pd.DatetimeIndex


def prep(df):
    local = df.index.tz_convert(TZ)
    return DayData(
        date=np.array(local.date),
        o=df["open"].to_numpy(float),
        h=df["high"].to_numpy(float),
        l=df["low"].to_numpy(float),
        c=df["close"].to_numpy(float),
        mod=(local.hour * 60 + local.minute).to_numpy(),
        dow=local.dayofweek.to_numpy(),
        ts=df.index,
    )


def day_slices(D):
    """返回每个交易日的 (date, start_i, end_i)。"""
    out = []
    start = 0
    for i in range(1, len(D.date)):
        if D.date[i] != D.date[i - 1]:
            out.append((D.date[start], start, i))
            start = i
    out.append((D.date[start], start, len(D.date)))
    return out


def daily_from_5min(D):
    rows = []
    for dt, a, b in day_slices(D):
        rows.append(
            {
                "date": dt,
                "open": D.o[a],
                "high": D.h[a:b].max(),
                "low": D.l[a:b].min(),
                "close": D.c[b - 1],
            }
        )
    return pd.DataFrame(rows).set_index("date")


def build_events(D, sl_pts=SL_PTS, tp_pts=TP_PTS, brk=BRK, or_min=OR_MIN,
                 trade_dow=TRADE_DOW, eod_min=EOD_MIN):
    """抽取每个交易日的基础事件 + 基线交易结果 + 丰富特征。

    返回 per-day DataFrame, 含:
      side, entry_i(全局idx), entry_px, anchor(OR边界), sl, tp, pnl, exit_reason,
      以及 gap / prev_level / OR 等特征。
    """
    daily = daily_from_5min(D)
    daily["prev_close"] = daily["close"].shift(1)
    daily["prev_high"] = daily["high"].shift(1)
    daily["prev_low"] = daily["low"].shift(1)
    daily["tr"] = np.maximum(daily["high"] - daily["low"],
                             np.maximum((daily["high"] - daily["prev_close"]).abs(),
                                        (daily["low"] - daily["prev_close"]).abs()))
    daily["atr14"] = daily["tr"].rolling(14).mean()
    daily["dow"] = pd.DatetimeIndex(daily.index).dayofweek
    prev = daily.shift(1)

    recs = []
    for dt, a, b in day_slices(D):
        dow = D.dow[a]
        if dow not in trade_dow:
            continue
        or_end = OR_START + or_min
        sess = [i for i in range(a, b) if OR_START <= D.mod[i] < or_end]
        if not sess:
            continue
        orhi = D.h[sess].max()
        orlo = D.l[sess].min()
        orw = orhi - orlo
        after = [i for i in range(sess[-1] + 1, b) if D.mod[i] < eod_min]
        if not after:
            continue
        pc = prev["close"].get(dt, np.nan)
        opening = D.o[a]
        gap = opening - pc if not np.isnan(pc) else np.nan
        atr = prev["atr14"].get(dt, np.nan)
        ph = prev["high"].get(dt, np.nan)
        pl = prev["low"].get(dt, np.nan)

        rec = {
            "date": dt, "dow": dow,
            "orhi": orhi, "orlo": orlo, "orw": orw,
            "open": opening, "prev_close": pc,
            "prev_high": ph, "prev_low": pl,
            "gap": gap, "gap_atr": gap / atr if atr and atr > 0 else np.nan,
            "atr": atr,
        }
        rec.update(simulate(D, after, orhi, orlo, sl_pts, tp_pts, brk, eod_min, a))
        recs.append(rec)
    return pd.DataFrame(recs)


def _fill_stoplimit(D, i, pos, sl_px, tp_px):
    """返回 (fill, reason) 或 (None,None)。语义同 ztp.orb。"""
    if pos > 0:
        if D.o[i] <= sl_px:
            return D.o[i], "SL"
        if D.o[i] >= tp_px:
            return D.o[i], "TP"
        if D.l[i] <= sl_px and D.h[i] >= tp_px:
            return (sl_px, "SL") if D.c[i] >= D.o[i] else (tp_px, "TP")
        if D.l[i] <= sl_px:
            return sl_px, "SL"
        if D.h[i] >= tp_px:
            return tp_px, "TP"
    else:
        if D.o[i] >= sl_px:
            return D.o[i], "SL"
        if D.o[i] <= tp_px:
            return D.o[i], "TP"
        if D.h[i] >= sl_px and D.l[i] <= tp_px:
            return (tp_px, "TP") if D.c[i] >= D.o[i] else (sl_px, "SL")
        if D.h[i] >= sl_px:
            return sl_px, "SL"
        if D.l[i] <= tp_px:
            return tp_px, "TP"
    return None, None


def simulate(D, after, orhi, orlo, sl_pts, tp_pts, brk, eod_min, day_start):
    """基线: brk 根确认后于收盘入场, 锚定 OR 边界, 固定点数 SL/TP, 过 eod 收盘平。"""
    bull = bear = 0
    out = {"side": "", "entry_i": -1, "entry_px": np.nan, "anchor": np.nan,
           "sl": np.nan, "tp": np.nan, "exit_i": -1, "exit_px": np.nan,
           "pnl": 0.0, "exit_reason": "NO_TRADE", "bars": 0,
           "mfe": np.nan, "mae": np.nan, "fib_lo": np.nan, "fib_hi": np.nan}
    for i in after:
        if D.c[i] > orhi:
            bull += 1; bear = 0
        elif D.c[i] < orlo:
            bear += 1; bull = 0
        else:
            bull = bear = 0
        if bull >= brk:
            side, anchor, pos = 1, orhi, 1
        elif bear >= brk:
            side, anchor, pos = -1, orlo, -1
        else:
            continue
        # 入场: 收盘价市价
        entry = rnd(D.c[i])
        if pos > 0:
            sl, tp = rnd(anchor - sl_pts), rnd(anchor + tp_pts)
        else:
            sl, tp = rnd(anchor + sl_pts), rnd(anchor - tp_pts)
        out.update(side="LONG" if pos > 0 else "SHORT", entry_i=i, entry_px=entry,
                   anchor=anchor, sl=sl, tp=tp)
        # 入场当根: 收盘越过 TP 即刻成交
        if (pos > 0 and D.c[i] >= tp) or (pos < 0 and D.c[i] <= tp):
            out.update(exit_i=i, exit_px=rnd(D.c[i]), pnl=pos * QTY * (rnd(D.c[i]) - entry),
                       exit_reason="TP", bars=0)
            return out
        # 后续 bar: 只到 eod
        ex = None
        for j in range(i + 1, after[-1] + 1):
            if D.mod[j] >= eod_min:
                break
            f, r = _fill_stoplimit(D, j, pos, sl, tp)
            if f is not None:
                ex = (j, rnd(f), r)
                break
        if ex is None:
            # eod 收盘平
            j = max(k for k in after if D.mod[k] < eod_min) if any(D.mod[k] < eod_min for k in after) else i
            ex = (j, rnd(D.c[j]), "EOD")
        j, px, r = ex
        out.update(exit_i=j, exit_px=px, pnl=pos * QTY * (px - entry), exit_reason=r, bars=j - i)
        return out
    return out


def stats(df, pnl_col="pnl", date_col="date"):
    p = df[pnl_col]
    n = len(df)
    if n == 0:
        return dict(n=0)
    win = (p > 0).mean()
    gw = p[p > 0].sum()
    gl = p[p < 0].sum()
    eq = p.cumsum()
    mdd = (eq.cummax() - eq).max()
    return dict(n=n, win=round(win * 100, 1),
                exp=round(p.mean(), 2),
                payoff=round(p[p > 0].mean() / abs(p[p < 0].mean()), 2) if (p < 0).any() else np.inf,
                pf=round(gw / abs(gl), 2) if gl != 0 else np.inf,
                tot=round(p.sum(), 0), mdd=round(mdd, 0))


if __name__ == "__main__":
    D = prep(load_5min())
    ev = build_events(D)
    print("events:", len(ev))
    print(stats(ev[ev.exit_reason != "NO_TRADE"]))
    ev.to_parquet("results/orb_events_base.parquet")
    print("saved results/orb_events_base.parquet")
