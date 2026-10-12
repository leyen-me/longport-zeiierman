"""研究1/2 精修: 反向单按年稳健性; 斐波那契回调在"风险归一"下的真实表现。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
import pandas as pd

from orb_research import (QTY, SL_PTS, TP_PTS, BRK, OR_MIN, OR_START, EOD_MIN,
                          rnd, load_5min, prep, day_slices)
from orb_variants import sim_base, sim_reversal, fill, close_trade, or_for_day, stat


def sim_fib2(D, a, b, f, stop_mode="fixed", tp_mode="fixed",
             sl_pts=SL_PTS, tp_pts=TP_PTS, eod=EOD_MIN):
    """突破确认后, 挂 fib 回调限价; 成交后按 stop_mode/tp_mode 出场。

    stop_mode: fixed(entry∓sl_pts) | edge(OR另一边界 orlo/orhi) | hilo(入场前的最低/最高)
    tp_mode:   fixed(entry±tp_pts) | mm(measured move: orhi+orw / orlo-orw)
    """
    orr = or_for_day(D, a, b)
    if not orr:
        return None
    orhi, orlo, orlast = orr
    orw = orhi - orlo
    after = [i for i in range(orlast + 1, b) if D.mod[i] < EOD_MIN]
    bull = bear = 0
    di = -1
    for i in after:
        if D.c[i] > orhi:
            bull += 1; bear = 0
        elif D.c[i] < orlo:
            bear += 1; bull = 0
        else:
            bull = bear = 0
        if bull >= BRK or bear >= BRK:
            pos = 1 if bull >= BRK else -1
            di = i
            break
    if di < 0:
        return {"pnl": 0.0, "reason": "NO_BREAK", "filled": 0, "pos": 0}
    limit = rnd(orhi - f * orw) if pos > 0 else rnd(orlo + f * orw)
    fi = -1
    for j in range(di + 1, b):
        if D.mod[j] >= eod:
            break
        if (pos > 0 and D.l[j] <= limit) or (pos < 0 and D.h[j] >= limit):
            fi = j
            break
    if fi < 0:
        return {"pnl": 0.0, "reason": "NO_FILL", "filled": 0, "pos": pos}
    entry = limit
    # 入场前极值 (仅在突破后、成交前观察, 无前视)
    seg_h = D.h[di:fi + 1].max(); seg_l = D.l[di:fi + 1].min()
    if pos > 0:
        sl = {"fixed": entry - sl_pts, "edge": orlo, "hilo": min(seg_l, entry) - sl_pts}[stop_mode]
        tp = {"fixed": entry + tp_pts, "mm": orhi + orw}[tp_mode]
    else:
        sl = {"fixed": entry + sl_pts, "edge": orhi, "hilo": max(seg_h, entry) + sl_pts}[stop_mode]
        tp = {"fixed": entry - tp_pts, "mm": orlo - orw}[tp_mode]
    sl, tp = rnd(sl), rnd(tp)
    for j in range(fi, b):
        if D.mod[j] >= eod:
            break
        if (pos > 0 and D.c[j] >= tp) or (pos < 0 and D.c[j] <= tp):
            return {"pnl": close_trade(pos, entry, rnd(D.c[j])), "reason": "TP", "filled": 1, "pos": pos,
                    "risk": abs(entry - sl), "entry": entry}
        fpx, r = fill(D, j, pos, sl, tp)
        if fpx is not None:
            return {"pnl": close_trade(pos, entry, rnd(fpx)), "reason": r, "filled": 1, "pos": pos,
                    "risk": abs(entry - sl), "entry": entry}
    j = max(k for k in range(fi, b) if D.mod[k] < eod)
    return {"pnl": close_trade(pos, entry, rnd(D.c[j])), "reason": "EOD", "filled": 1, "pos": pos,
            "risk": abs(entry - sl), "entry": entry}


def report(name, df, pnl="pnl"):
    p = df[pnl]
    gw = p[p > 0].sum(); gl = p[p < 0].sum()
    wr = (p > 0).mean() * 100 if len(p) else np.nan
    payoff = p[p > 0].mean() / abs(p[p < 0].mean()) if (p < 0).any() and (p > 0).any() else np.nan
    print(f"  {name:34s} n={len(df):4d} 胜率={wr:5.1f}% 盈亏比={payoff:.2f} "
          f"PF={gw/abs(gl) if gl else float('inf'):.2f} 期望={p.mean():6.2f} 总={p.sum():8.0f}")


def main():
    D = prep(load_5min())
    days = [(a, b) for _, a, b in day_slices(D) if D.dow[a] in (0, 1, 2)]

    print("=== 基线 ===")
    bdf = pd.DataFrame([sim_base(D, a, b) for a, b in days])
    bt = bdf[bdf.reason != "NO_TRADE"]
    report("baseline", bt)

    # 研究1 按年
    print("\n=== 研究1: 止损后反向 (按年稳健性) ===")
    for rev in ["fixed", "keep"]:
        r = pd.DataFrame([sim_reversal(D, a, b, rev=rev) for a, b in days])
        r["yr"] = [D.ts[a].year for a, b in days]
        tr = r[r.base_reason != "NO_TRADE"]
        print(f"\n  rev={rev}: 全样本")
        report("  总(含反向)", tr)
        g = tr.copy()
        g["base_only"] = g.base_pnl; g["rev_only"] = g.rev_pnl
        print("    逐年净贡献(反向):")
        for y, x in tr.groupby("yr"):
            sl = x[x.base_reason == "SL"]
            print(f"      {y}: 基线{x.base_pnl.sum():7.0f}  反向{sl.rev_pnl.sum():7.0f}  合计{x.pnl.sum():7.0f}  止损{len(sl)}")

    # 研究2 风险归一
    print("\n=== 研究2: 斐波那契回调入场 (风险归一) ===")
    print("[A] 固定风险/固定目标 (SL=1.1, TP=2.1 相对入场)")
    for f in [0.236, 0.382, 0.5, 0.618, 0.786]:
        r = pd.DataFrame([sim_fib2(D, a, b, f, "fixed", "fixed") for a, b in days])
        tr = r[r.filled == 1]
        report(f"fib={f}", tr)
    print("[B] 区间止损(OR另一边界) + measured-move 目标")
    for f in [0.236, 0.382, 0.5, 0.618, 0.786]:
        r = pd.DataFrame([sim_fib2(D, a, b, f, "edge", "mm") for a, b in days])
        tr = r[r.filled == 1]
        if len(tr):
            rr = (np.where(tr.pos > 0, tr.pnl, tr.pnl) / (QTY * tr.risk)).mean()
            report(f"fib={f} (R={rr:.2f})", tr)
        else:
            print(f"  fib={f} 无成交")
    print("[C] 区间止损 + 固定目标")
    for f in [0.382, 0.5, 0.618]:
        r = pd.DataFrame([sim_fib2(D, a, b, f, "edge", "fixed") for a, b in days])
        tr = r[r.filled == 1]
        report(f"fib={f}", tr)
    # 基线成交占比参考
    print(f"\n  参考: 突破入场(基线) n={len(bt)}; fib成交率随 f 递减")


if __name__ == "__main__":
    main()
