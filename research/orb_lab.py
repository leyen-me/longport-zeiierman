"""ORB 研究实验室: 批量评估 入场变体 × 出场变体 × 过滤因子, 输出 IS/OOS 指标。

数据: data_cache/SPY_US_non_min2.parquet -> 5min (美东)。可换 QQQ/IWM 15min。
所有出场只在"入场已完成"后模拟, 无前视。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
TZ = "America/New_York"
QTY = 80
TICK = 0.01
OR_START = 570


def rnd(px, tick=TICK):
    import math
    n = round(1.0 / tick)
    return math.floor(px * n + 0.5) / n


# ---------------------------------------------------------------- data
def load(symbol="SPY", freq="5min"):
    if symbol == "SPY":
        d = pd.read_parquet(ROOT / "data_cache/SPY_US_non_min2.parquet")
    else:
        d = pd.read_parquet(ROOT / f"data_cache/{symbol}_US_non_min15.parquet")
    d = d.astype(float)
    if freq == "5min" and symbol == "SPY":
        d = (d.resample("5min", label="left", closed="left")
             .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
             .dropna())
    mod = d.index.hour * 60 + d.index.minute
    return d[(mod >= 570) & (mod < 960)]


def prep(df):
    loc = df.index.tz_convert(TZ)
    return dict(
        date=np.array(loc.date), o=df["open"].to_numpy(float), h=df["high"].to_numpy(float),
        l=df["low"].to_numpy(float), c=df["close"].to_numpy(float),
        v=df["volume"].to_numpy(float) if "volume" in df else np.ones(len(df)),
        mod=(loc.hour * 60 + loc.minute).to_numpy(), dow=loc.dayofweek.to_numpy(),
        ts=df.index)


def slices(D):
    out, s = [], 0
    for i in range(1, len(D["date"])):
        if D["date"][i] != D["date"][i - 1]:
            out.append((D["date"][s], s, i)); s = i
    out.append((D["date"][s], s, len(D["date"])))
    return out


def daily(D):
    rows = []
    for dt, a, b in slices(D):
        rows.append(dict(date=dt, open=D["o"][a], high=D["h"][a:b].max(),
                         low=D["l"][a:b].min(), close=D["c"][b - 1], vol=D["v"][a:b].sum()))
    df = pd.DataFrame(rows).set_index("date")
    df["pc"] = df["close"].shift(1)
    df["ph"] = df["high"].shift(1)
    df["pl"] = df["low"].shift(1)
    df["tr"] = np.maximum(df["high"] - df["low"],
                          np.maximum((df["high"] - df["pc"]).abs(), (df["low"] - df["pc"]).abs()))
    df["atr"] = df["tr"].rolling(14).mean().shift(1)
    df["sma20"] = df["close"].rolling(20).mean().shift(1)
    df["sma50"] = df["close"].rolling(50).mean().shift(1)
    df["vol20"] = df["close"].pct_change().rolling(20).std().shift(1)
    return df


# ---------------------------------------------------------------- entry
def day_entry(D, a, b, or_min=30, brk=2, entry_type="close"):
    dest = a
    for i in range(a, b):
        if D["mod"][i] >= OR_START:
            dest = i; break
    or_end = OR_START + or_min
    sess = [i for i in range(a, b) if OR_START <= D["mod"][i] < or_end]
    if not sess:
        return None
    orhi = D["h"][sess].max(); orlo = D["l"][sess].min(); orw = orhi - orlo
    orlast = sess[-1]
    after = [i for i in range(orlast + 1, b) if D["mod"][i] < 16 * 60]
    bull = bear = 0; di = -1; pos = 0
    for i in after:
        if D["c"][i] > orhi:
            bull += 1; bear = 0
        elif D["c"][i] < orlo:
            bear += 1; bull = 0
        else:
            bull = bear = 0
        if bull >= brk or bear >= brk:
            pos = 1 if bull >= brk else -1; di = i; break
    if di < 0:
        return None
    # OR 特征
    orbs = D["mod"] == OR_START
    first_i = sess[0]
    first_dir = 1 if D["c"][first_i] >= D["o"][first_i] else -1
    or_cl = (D["c"][orlast] - orlo) / orw if orw > 0 else 0.5
    if orw <= 0:
        return None
    if entry_type == "close":
        ei, epx = di, rnd(D["c"][di])
    elif entry_type == "retest":  # 回踩 OR 边界限价
        lvl = orhi if pos > 0 else orlo
        ei = -1
        for j in range(di + 1, b):
            if D["mod"][j] >= 16 * 60:
                break
            if (pos > 0 and D["l"][j] <= lvl) or (pos < 0 and D["h"][j] >= lvl):
                ei, epx = j, rnd(lvl); break
        if ei < 0:
            return None
    else:
        raise ValueError(entry_type)
    return dict(side="LONG" if pos > 0 else "SHORT", pos=pos, entry_i=ei, entry=epx,
                anchor=orhi if pos > 0 else orlo, orhi=orhi, orlo=orlo, orw=orw,
                or_cl=or_cl, first_dir=first_dir, brk_i=di)


# ---------------------------------------------------------------- exit
def _fill(D, i, pos, sl, tp):
    if pos > 0:
        if D["o"][i] <= sl: return D["o"][i], "SL"
        if D["o"][i] >= tp: return D["o"][i], "TP"
        if D["l"][i] <= sl and D["h"][i] >= tp:
            return (sl, "SL") if D["c"][i] >= D["o"][i] else (tp, "TP")
        if D["l"][i] <= sl: return sl, "SL"
        if D["h"][i] >= tp: return tp, "TP"
    else:
        if D["o"][i] >= sl: return D["o"][i], "SL"
        if D["o"][i] <= tp: return D["o"][i], "TP"
        if D["h"][i] >= sl and D["l"][i] <= tp:
            return (tp, "TP") if D["c"][i] >= D["o"][i] else (sl, "SL")
        if D["h"][i] >= sl: return sl, "SL"
        if D["l"][i] <= tp: return tp, "TP"
    return None, None


def sim_exit(D, a, b, e, sl_pts=1.1, tp_pts=2.1, anchor="entry",
             trail_R=None, trail_pts=None, be_R=None, be_buf=0.0, use_tp=True,
             eod=12 * 60, partial=None):
    """partial: (pt_R, frac) 到 +pt_R*R 时先平 frac 比例, 余额继续。"""
    pos, entry, i = e["pos"], e["entry"], e["entry_i"]
    ref = e["anchor"] if anchor == "edge" else entry
    sl = rnd(ref - sl_pts) if pos > 0 else rnd(ref + sl_pts)
    tp = rnd(ref + tp_pts) if pos > 0 else rnd(ref - tp_pts)
    R = abs(entry - sl)
    be_done = False
    done_frac = 0.0
    realized = 0.0
    for j in range(i, b):
        if D["mod"][j] >= eod:
            break
        if j == i and use_tp and ((pos > 0 and D["c"][j] >= tp) or (pos < 0 and D["c"][j] <= tp)):
            return dict(pnl=QTY * (1 - done_frac) * pos * (rnd(D["c"][j]) - entry) + realized, reason="TP",
                        exit_i=j, exit_px=rnd(D["c"][j]))
        if j > i:
            f, r = _fill(D, j, pos, sl, tp if use_tp else np.inf)
            if f is not None:
                return dict(pnl=QTY * (1 - done_frac) * pos * (rnd(f) - entry) + realized, reason=r,
                            exit_i=j, exit_px=rnd(f))
        if partial is not None and done_frac == 0.0:
            pt_R, frac = partial
            if ((pos > 0 and D["h"][j] >= entry + pt_R * R) or
                    (pos < 0 and D["l"][j] <= entry - pt_R * R)):
                realized += QTY * frac * pos * (pt_R * R)
                done_frac = frac
        fav = (D["h"][j] - entry) if pos > 0 else (entry - D["l"][j])
        ext = D["h"][j] if pos > 0 else D["l"][j]
        if be_R is not None and not be_done and fav >= be_R * R:
            ns = entry + be_buf if pos > 0 else entry - be_buf
            sl = max(sl, rnd(ns)) if pos > 0 else min(sl, rnd(ns)); be_done = True
        if trail_R is not None:
            cand = ext - trail_R * R if pos > 0 else ext + trail_R * R
            sl = max(sl, rnd(cand)) if pos > 0 else min(sl, rnd(cand))
        if trail_pts is not None:
            cand = ext - trail_pts if pos > 0 else ext + trail_pts
            sl = max(sl, rnd(cand)) if pos > 0 else min(sl, rnd(cand))
    cand = [k for k in range(i, b) if D["mod"][k] < eod]
    j = max(cand) if cand else i
    return dict(pnl=QTY * (1 - done_frac) * pos * (rnd(D["c"][j]) - entry) + realized, reason="EOD",
                exit_i=j, exit_px=rnd(D["c"][j]))


# ---------------------------------------------------------------- metrics
def metrics(df, pnl="pnl"):
    p = df[pnl].to_numpy()
    if len(p) == 0:
        return dict(n=0)
    gw = p[p > 0].sum(); gl = p[p < 0].sum()
    exp = p.mean(); se = p.std(ddof=1) / np.sqrt(len(p)) if len(p) > 1 else np.nan
    eq = np.cumsum(p); dd = eq - np.maximum.accumulate(eq)
    return dict(
        n=len(p), win=round((p > 0).mean() * 100, 1),
        payoff=round(p[p > 0].mean() / abs(p[p < 0].mean()), 2) if (p < 0).any() and (p > 0).any() else np.nan,
        pf=round(gw / abs(gl), 2) if gl else float("inf"),
        exp=round(exp, 2), t=round(exp / se, 2) if se and se > 0 else np.nan,
        tot=round(p.sum(), 0), mdd=round(-dd.min(), 0),
    )


def is_oos(df, split="2024-01-01"):
    d = pd.to_datetime(df["date"])
    is_ = df[d < split]
    oo = df[d >= split]
    return metrics(is_), metrics(oo)


# ---------------------------------------------------------------- build trades table
def build(entry_cfg, exit_cfg, days=None, filt=None, D=None, dl=None):
    rows = []
    trade_days = entry_cfg.get("trade_days", (0, 1, 2))
    for idx, (dt, a, b) in enumerate(days):
        if D["dow"][a] not in trade_days:
            continue
        e = day_entry(D, a, b, entry_cfg.get("or_min", 30), entry_cfg.get("brk", 2),
                      entry_cfg.get("entry_type", "close"))
        if e is None:
            continue
        row = dict(date=dt, **{k: e[k] for k in ("side", "entry", "anchor", "orhi", "orlo",
                                                  "orw", "or_cl", "first_dir")})
        row["entry_i"] = e["entry_i"]
        row["entry_ts"] = D["ts"][e["entry_i"]]
        # 日线特征
        if dt in dl.index:
            d = dl.loc[dt]
            row.update(gap=D["o"][a] - d["pc"], gap_bps=(D["o"][a] - d["pc"]) / d["pc"] * 1e4,
                       atr=d["atr"], ph=d["ph"], pl=d["pl"], pc=d["pc"],
                       sma20=d["sma20"], sma50=d["sma50"], vol20=d["vol20"])
        else:
            row.update(gap=np.nan, gap_bps=np.nan, atr=np.nan, ph=np.nan, pl=np.nan,
                       pc=np.nan, sma20=np.nan, sma50=np.nan, vol20=np.nan)
        # 过滤
        if filt is not None and not filt(row):
            continue
        r = sim_exit(D, a, b, e, **exit_cfg)
        row.update(r)
        row["exit_ts"] = D["ts"][r["exit_i"]]
        row["exit_px_u"] = r["exit_px"]
        rows.append(row)
    return pd.DataFrame(rows)


def load_all(symbol="SPY", freq="5min"):
    df = load(symbol, freq)
    D = prep(df)
    dl = daily(D)
    D["_days"] = [s for s in slices(D)]
    return D, dl


if __name__ == "__main__":
    D, dl = load_all()
    days = D["_days"]
    print("交易日:", len(days), "bar:", len(D["c"]))
    for orm in [15, 30, 60]:
        for brk in [1, 2, 3]:
            n = sum(1 for dt, a, b in days if D["dow"][a] in (0, 1, 2)
                    and day_entry(D, a, b, orm, brk) is not None)
            print(f"  or={orm} brk={brk} 可交易日={n}")
