"""失败突破反手 (fade) 研究: 突破后若"重新跌回 OR 内", 反向做单。

动机: 逐笔归因显示 58% 的突破单从未走出 0.25R (死单)。若这些失败可识别,
      反手做"假突破"或许能独立成策略, 或与顺势单互补。

规则:
  突破事件 (v2 入场) 后, 若某根收盘重新回到 OR 内 (多头: close<orhi), 则:
    - 反手方向 = 原方向取反
    - 入场 = 该收盘价
    - 止损 = 突破期间极值 (多头失败单的 max high)  或 orhi + k*OR宽
    - 目标 = OR 另一边界 (orlo / orhi) 或 ±倍数
  过 12:00 强平。对比: 基线顺势单; 以及"顺势+反手"组合。

数据: data_cache/SPY_US_non_min2.parquet -> 5min。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
import pandas as pd

from orb_lab import load, prep, slices, daily, day_entry, metrics, QTY, rnd
from orb_loss_attrib import sim_record

OR_MIN = 15
BRK = 3
TRADE_DOW = (0, 1, 2, 4)
SL_PTS = 1.1
TP_PTS = 2.1
EOD = 12 * 60


def fade_day(D, a, b, e, stop_mode="extreme", target_mode="opposite",
             sl_pts=SL_PTS, tp_pts=TP_PTS, eod=EOD):
    pos, i = e["pos"], e["entry_i"]
    orhi, orlo, orw = e["orhi"], e["orlo"], e["orw"]
    # 找到失败触发 bar: 收盘重回 OR 内
    trig = -1
    for j in range(i + 1, b):
        if D["mod"][j] >= eod:
            break
        if (pos > 0 and D["c"][j] < orhi) or (pos < 0 and D["c"][j] > orlo):
            trig = j
            break
    if trig < 0:
        return None
    fdir = -pos
    entry = rnd(D["c"][trig])
    if stop_mode == "extreme":
        ext = max(D["h"][i:trig + 1]) if pos > 0 else min(D["l"][i:trig + 1])
        sl = rnd(ext)
    elif stop_mode == "edge":
        sl = rnd(orhi + sl_pts) if fdir > 0 else rnd(orlo - sl_pts)
    else:
        sl = rnd(entry + sl_pts) if fdir > 0 else rnd(entry - sl_pts)
    if fdir > 0 and sl <= entry:
        sl = rnd(entry + sl_pts)
    if fdir < 0 and sl >= entry:
        sl = rnd(entry - sl_pts)
    if target_mode == "opposite":
        tp = rnd(orlo) if fdir > 0 else rnd(orhi)
    else:
        tp = rnd(entry + tp_pts) if fdir > 0 else rnd(entry - tp_pts)
    if fdir > 0 and tp <= entry:
        tp = rnd(entry + tp_pts)
    if fdir < 0 and tp >= entry:
        tp = rnd(entry - tp_pts)
    R = abs(entry - sl)
    mfe = mae = 0.0
    for j in range(trig + 1, b):
        if D["mod"][j] >= eod:
            break
        fav = (D["h"][j] - entry) if fdir > 0 else (entry - D["l"][j])
        adv = (entry - D["l"][j]) if fdir > 0 else (D["h"][j] - entry)
        mfe = max(mfe, fav); mae = max(mae, adv)
        if fdir > 0:
            if D["o"][j] <= sl: return _r(fdir, entry, D["o"][j], "SL", mfe, mae, R, j - trig, sl, tp)
            if D["o"][j] >= tp: return _r(fdir, entry, D["o"][j], "TP", mfe, mae, R, j - trig, sl, tp)
            if D["l"][j] <= sl and D["h"][j] >= tp:
                hit_sl = D["c"][j] >= D["o"][j]
                return _r(fdir, entry, sl if hit_sl else tp, "SL" if hit_sl else "TP", mfe, mae, R, j - trig, sl, tp)
            if D["l"][j] <= sl: return _r(fdir, entry, sl, "SL", mfe, mae, R, j - trig, sl, tp)
            if D["h"][j] >= tp: return _r(fdir, entry, tp, "TP", mfe, mae, R, j - trig, sl, tp)
        else:
            if D["o"][j] >= sl: return _r(fdir, entry, D["o"][j], "SL", mfe, mae, R, j - trig, sl, tp)
            if D["o"][j] <= tp: return _r(fdir, entry, D["o"][j], "TP", mfe, mae, R, j - trig, sl, tp)
            if D["h"][j] >= sl and D["l"][j] <= tp:
                hit_sl = D["c"][j] >= D["o"][j]
                return _r(fdir, entry, tp if hit_sl else sl, "TP" if hit_sl else "SL", mfe, mae, R, j - trig, sl, tp)
            if D["h"][j] >= sl: return _r(fdir, entry, sl, "SL", mfe, mae, R, j - trig, sl, tp)
            if D["l"][j] <= tp: return _r(fdir, entry, tp, "TP", mfe, mae, R, j - trig, sl, tp)
    cand = [k for k in range(trig, b) if D["mod"][k] < eod]
    j = max(cand) if cand else trig
    return _r(fdir, entry, D["c"][j], "EOD", mfe, mae, R, j - trig, sl, tp)


def _r(pos, entry, px, reason, mfe, mae, R, bars, sl, tp):
    return dict(pnl=pos * QTY * (rnd(px) - entry), reason=reason, entry=entry,
                mfe_R=mfe / R if R else np.nan, mae_R=mae / R if R else np.nan,
                bars=bars, sl=sl, tp=tp)


def summ(rows, label):
    p = pd.Series([r["pnl"] for r in rows if r and r["reason"] != "NO_TRADE"])
    if len(p) == 0:
        print(f"  {label:34s} 无交易"); return
    gw, gl = p[p > 0].sum(), p[p < 0].sum()
    exp = p.mean(); se = p.std() / np.sqrt(len(p)) if len(p) > 1 else np.nan
    eq = p.cumsum()
    print(f"  {label:34s} n={len(p):4d} 胜={(p>0).mean()*100:4.1f}% "
          f"PF={gw/abs(gl) if gl else 9:4.2f} 期望={exp:6.2f} t={exp/se if se and se>0 else 0:5.2f} "
          f"总={p.sum():7.0f} MDD={(eq.cummax()-eq).max():6.0f}")


def main():
    df = load()
    D = prep(df)
    dl = daily(D)
    D["_days"] = slices(D)
    d = pd.to_datetime([dt for dt, a, b in D["_days"]])
    per = {"P1": d < "2023-01-01", "P2": (d >= "2023-01-01") & (d < "2025-01-01"),
           "P3": d >= "2025-01-01"}

    base_rows, fade_rows = [], []
    for dt, a, b in D["_days"]:
        e = day_entry(D, a, b, OR_MIN, BRK, "close")
        if e is None or (e["first_dir"] != e["pos"]):
            base_rows.append(None); fade_rows.append(None); continue
        base_rows.append(sim_record(D, a, b, e))
        fade_rows.append(fade_day(D, a, b, e, stop_mode="extreme", target_mode="opposite"))

    print("=== 基线顺势 vs 失败反手 ===")
    summ([r for r in base_rows if r], "顺势(基线)")
    summ([r for r in fade_rows if r], "反手(stop=突破极值, TP=对边界)")

    print("\n=== 反手变体 ===")
    for sm in ["extreme", "edge", "pts"]:
        for tm in ["opposite", "pts"]:
            rows = []
            for dt, a, b in D["_days"]:
                e = day_entry(D, a, b, OR_MIN, BRK, "close")
                if e is None or (e["first_dir"] != e["pos"]):
                    rows.append(None); continue
                rows.append(fade_day(D, a, b, e, stop_mode=sm, target_mode=tm))
            summ([r for r in rows if r], f"stop={sm}, tp={tm}")

    # 组合: 顺势 + 反手 (同一天两笔, 但反手只在突破未到TP时发生)
    print("\n=== 组合 (顺势单 + 反手单相加) ===")
    comb = []
    for br, fr in zip(base_rows, fade_rows):
        if br is None:
            comb.append(None); continue
        p = br["pnl"] + (fr["pnl"] if fr else 0.0)
        comb.append(dict(pnl=p, reason="COMBO"))
    summ([r for r in comb if r], "顺势+反手")
    print("  逐年(组合 vs 纯顺势 vs 纯反手):")
    yrs = sorted(set(d.year))
    for y in yrs:
        ii = [k for k, dt in enumerate(d) if dt.year == y]
        def tot(rows):
            return sum(r["pnl"] for k in ii if rows[k] for r in [rows[k]])
        print(f"    {y}: 顺势={tot(base_rows):7.0f} 反手={tot(fade_rows):7.0f} 组合={tot(comb):7.0f}")

    # 三段
    print("\n=== 三段 (P1/P2/P3) ===")
    for k, m in per.items():
        idx = [j for j, x in enumerate(m) if x]
        def t(rows):
            p = pd.Series([rows[j]["pnl"] for j in idx if rows[j]])
            if len(p) == 0: return "n=0"
            gw, gl = p[p > 0].sum(), p[p < 0].sum()
            return f"n={len(p):3d} PF={gw/abs(gl) if gl else 9:4.2f} 总={p.sum():6.0f}"
        print(f"  {k}: 顺势 {t(base_rows)} | 反手 {t(fade_rows)}")


if __name__ == "__main__":
    main()
