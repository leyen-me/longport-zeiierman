"""研究1 (止损后反向) 与 研究2 (斐波那契回调入场) 的变体回测。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
import pandas as pd

from orb_research import (QTY, SL_PTS, TP_PTS, BRK, OR_MIN, OR_START, EOD_MIN, TZ,
                          MINTICK, rnd, load_5min, prep, day_slices, daily_from_5min)


def or_for_day(D, a, b, or_min=OR_MIN):
    or_end = OR_START + or_min
    sess = [i for i in range(a, b) if OR_START <= D.mod[i] < or_end]
    if not sess:
        return None
    return D.h[sess].max(), D.l[sess].min(), sess[-1]


def close_trade(pos, entry, px):
    return pos * QTY * (px - entry)


def run_baseline_day(D, a, b, sl_pts=SL_PTS, tp_pts=TP_PTS):
    """返回逐 bar 的基线交易 + 事件序列, 便于叠加反向逻辑。"""
    orr = or_for_day(D, a, b)
    if not orr:
        return None
    orhi, orlo, orlast = orr
    after = [i for i in range(orlast + 1, b) if D.mod[i] < EOD_MIN]
    bull = bear = 0
    for i in after:
        if D.c[i] > orhi:
            bull += 1; bear = 0
        elif D.c[i] < orlo:
            bear += 1; bull = 0
        else:
            bull = bear = 0
        if bull >= BRK or bear >= BRK:
            pos = 1 if bull >= BRK else -1
            anchor = orhi if pos > 0 else orlo
            entry = rnd(D.c[i])
            sl = rnd(anchor - sl_pts) if pos > 0 else rnd(anchor + sl_pts)
            tp = rnd(anchor + tp_pts) if pos > 0 else rnd(anchor - tp_pts)
            return dict(orhi=orhi, orlo=orlo, pos=pos, entry_i=i, entry=entry,
                        anchor=anchor, sl=sl, tp=tp)
    return None


def sim_base(D, a, b, sl_pts=SL_PTS, tp_pts=TP_PTS, eod=EOD_MIN):
    ev = run_baseline_day(D, a, b, sl_pts, tp_pts)
    if ev is None:
        return {"pnl": 0.0, "reason": "NO_TRADE", "entry_i": -1, "sl_i": -1, "sl_px": np.nan, "pos": 0, "orhi": np.nan, "orlo": np.nan}
    i, pos, entry, sl, tp = ev["entry_i"], ev["pos"], ev["entry"], ev["sl"], ev["tp"]
    if (pos > 0 and D.c[i] >= tp) or (pos < 0 and D.c[i] <= tp):
        return dict(pnl=close_trade(pos, entry, rnd(D.c[i])), reason="TP", entry_i=i,
                    sl_i=-1, sl_px=np.nan, pos=pos, entry=entry, tp=tp, sl=sl, exit_i=i, orhi=ev["orhi"], orlo=ev["orlo"])
    for j in range(i + 1, b):
        if D.mod[j] >= eod:
            break
        f, r = fill(D, j, pos, sl, tp)
        if f is not None:
            return dict(pnl=close_trade(pos, entry, rnd(f)), reason=r, entry_i=i,
                        sl_i=(j if r == "SL" else -1), sl_px=(rnd(f) if r == "SL" else np.nan),
                        pos=pos, entry=entry, sl=sl, tp=tp, exit_i=j, orhi=ev["orhi"], orlo=ev["orlo"])
    j = max(k for k in range(i, b) if D.mod[k] < eod)
    return dict(pnl=close_trade(pos, entry, rnd(D.c[j])), reason="EOD", entry_i=i,
                sl_i=-1, sl_px=np.nan, pos=pos, entry=entry, sl=sl, tp=tp, exit_i=j, orhi=ev["orhi"], orlo=ev["orlo"])


def fill(D, i, pos, sl_px, tp_px):
    if pos > 0:
        if D.o[i] <= sl_px: return D.o[i], "SL"
        if D.o[i] >= tp_px: return D.o[i], "TP"
        if D.l[i] <= sl_px and D.h[i] >= tp_px:
            return (sl_px, "SL") if D.c[i] >= D.o[i] else (tp_px, "TP")
        if D.l[i] <= sl_px: return sl_px, "SL"
        if D.h[i] >= tp_px: return tp_px, "TP"
    else:
        if D.o[i] >= sl_px: return D.o[i], "SL"
        if D.o[i] <= tp_px: return D.o[i], "TP"
        if D.h[i] >= sl_px and D.l[i] <= tp_px:
            return (tp_px, "TP") if D.c[i] >= D.o[i] else (sl_px, "SL")
        if D.h[i] >= sl_px: return sl_px, "SL"
        if D.l[i] <= tp_px: return tp_px, "TP"
    return None, None


# ---------------- 研究1: 止损后反向 ----------------
def sim_reversal(D, a, b, rev="fixed", sl_pts=SL_PTS, tp_pts=TP_PTS, eod=EOD_MIN):
    """先跑基线; 若基线止损, 在 SL 成交价反向开单。
    rev: fixed(同样1.1/2.1) | orother(SL相对原OR另一边界, TP=反向同点数) | reenter(收盘重回OR内才反向)
    """
    base = sim_base(D, a, b, sl_pts, tp_pts, eod)
    res = {"pnl": base["pnl"], "base_reason": base["reason"], "rev_taken": 0,
           "rev_pnl": 0.0, "base_pnl": base["pnl"]}
    if base["reason"] != "SL" or base["sl_i"] < 0:
        return res
    i = base["sl_i"]          # SL 成交所在 bar
    sl_px = base["sl_px"]
    pos = -base["pos"]        # 反向
    entry = sl_px
    if rev == "fixed":
        rsl = rnd(entry - sl_pts) if pos > 0 else rnd(entry + sl_pts)
        rtp = rnd(entry + tp_pts) if pos > 0 else rnd(entry - tp_pts)
        start = i + 1
    elif rev == "keep":       # 反向不止损, 只到 EOD 平 (用原日终点)
        rsl = rtp = np.nan
        start = i + 1
    elif rev == "orother":    # TP 打到原 OR 另一边界
        rsl = rnd(entry - sl_pts) if pos > 0 else rnd(entry + sl_pts)
        rtp = base["orlo"] if pos > 0 else base["orhi"]
        start = i + 1
    else:
        start = i + 1
        rsl = rnd(entry - sl_pts) if pos > 0 else rnd(entry + sl_pts)
        rtp = rnd(entry + tp_pts) if pos > 0 else rnd(entry - tp_pts)
    # 从 SL 之后 bar 起监测反向单
    exit_px = None
    for j in range(start, b):
        if D.mod[j] >= eod:
            break
        # 反向单入场当根若已越过 TP
        if np.isfinite(rtp) and ((pos > 0 and D.h[j] >= rtp) or (pos < 0 and D.l[j] <= rtp)):
            if np.isfinite(rsl) and ((pos > 0 and D.l[j] <= rsl) or (pos < 0 and D.h[j] >= rsl)):
                f, _ = fill(D, j, pos, rsl, rtp)
                exit_px = f
            else:
                exit_px = rtp
            break
        if np.isfinite(rsl):
            f, _ = fill(D, j, pos, rsl, rtp)
            if f is not None:
                exit_px = f
                break
    if exit_px is None:
        cand = [k for k in range(start, b) if D.mod[k] < eod]
        if not cand:
            return res
        j = max(cand)
        exit_px = rnd(D.c[j])
    rev_pnl = close_trade(pos, entry, rnd(exit_px))
    res.update(pnl=base["pnl"] + rev_pnl, rev_taken=1, rev_pnl=rev_pnl)
    return res


# ---------------- 研究2: 斐波那契回调入场 ----------------
def sim_fib(D, a, b, f, sl_pts=SL_PTS, tp_pts=TP_PTS, eod=EOD_MIN,
            anchor_at_edge=True, use_range=True):
    """突破后挂回调限价单:
       long 入场 = orhi - f*orw, short = orlo + f*orw
       SL/TP 相对入场价固定点数; 未成交则当日无单。
       anchor_at_edge: True 表示 SL 仍锚定 OR 边界(更宽止损)。
    """
    orr = or_for_day(D, a, b)
    if not orr:
        return None
    orhi, orlo, orlast = orr
    orw = orhi - orlo
    after = [i for i in range(orlast + 1, b) if D.mod[i] < EOD_MIN]
    bull = bear = 0
    broke_i = -1
    pos = 0
    for i in after:
        if D.c[i] > orhi:
            bull += 1; bear = 0
        elif D.c[i] < orlo:
            bear += 1; bull = 0
        else:
            bull = bear = 0
        if broke_i < 0 and (bull >= BRK or bear >= BRK):
            pos = 1 if bull >= BRK else -1
            broke_i = i
            break
    if broke_i < 0:
        return {"pnl": 0.0, "reason": "NO_BREAK", "filled": 0}
    # 挂限价
    if pos > 0:
        limit = rnd(orhi - f * orw)
    else:
        limit = rnd(orlo + f * orw)
    fill_i = -1
    for j in range(broke_i + 1, b):
        if D.mod[j] >= eod:
            break
        if (pos > 0 and D.l[j] <= limit) or (pos < 0 and D.h[j] >= limit):
            fill_i = j
            break
    if fill_i < 0:
        return {"pnl": 0.0, "reason": "NO_FILL", "filled": 0}
    entry = limit
    if anchor_at_edge:
        sl = rnd(orhi - sl_pts) if pos > 0 else rnd(orlo + sl_pts)
        # TP 用入场+固定点, 保证与基线可比
        tp = rnd(entry + tp_pts) if pos > 0 else rnd(entry - tp_pts)
    else:
        sl = rnd(entry - sl_pts) if pos > 0 else rnd(entry + sl_pts)
        tp = rnd(entry + tp_pts) if pos > 0 else rnd(entry - tp_pts)
    for j in range(fill_i, b):
        if D.mod[j] >= eod:
            break
        if (pos > 0 and D.c[j] >= tp) or (pos < 0 and D.c[j] <= tp):
            return {"pnl": close_trade(pos, entry, rnd(D.c[j])), "reason": "TP", "filled": 1}
        fpx, r = fill(D, j, pos, sl, tp)
        if fpx is not None:
            return {"pnl": close_trade(pos, entry, rnd(fpx)), "reason": r, "filled": 1}
    j = max(k for k in range(fill_i, b) if D.mod[k] < eod)
    return {"pnl": close_trade(pos, entry, rnd(D.c[j])), "reason": "EOD", "filled": 1}


def main():
    D = prep(load_5min())
    days = [(a, b) for _, a, b in day_slices(D) if D.dow[a] in (0, 1, 2)]
    print("交易日:", len(days))

    # 基线
    base = [sim_base(D, a, b) for a, b in days]
    bdf = pd.DataFrame(base)
    bt = bdf[bdf.reason != "NO_TRADE"]
    print("\n[基线]", stat(bt.pnl), "  交易:", len(bt))

    print("\n=== 研究1: 止损后反向单 ===")
    for rev in ["fixed", "keep", "orother"]:
        r = pd.DataFrame([sim_reversal(D, a, b, rev=rev) for a, b in days])
        tr = r[r.base_reason != "NO_TRADE"]
        sl = tr[tr.base_reason == "SL"]
        extra = sl.rev_pnl.sum()
        print(f"  rev={rev:8s} 总PnL={r.pnl.sum():8.0f}  盈利笔={stat(tr.pnl)['win']}%  "
              f"PF={stat(tr.pnl)['pf']}  | 止损日数={len(sl)} 反向净贡献={extra:8.0f} "
              f"反向胜率={(sl.rev_pnl>0).mean()*100:.0f}%")

    print("\n=== 研究2: 斐波那契回调入场 (SL锚OR边界) ===")
    for f in [0.236, 0.382, 0.5, 0.618, 0.786]:
        r = pd.DataFrame([sim_fib(D, a, b, f) for a, b in days])
        tr = r[r.reason.isin(["TP", "SL", "EOD"])]
        print(f"  fib={f:<5} 成交={len(tr):4d}  胜率={stat(tr.pnl)['win']}%  "
              f"期望={stat(tr.pnl)['exp']}  PF={stat(tr.pnl)['pf']}  总={tr.pnl.sum():.0f}  "
              f"未成交={len(r)-len(tr)}")
    print("\n=== 研究2b: 斐波那契 (SL相对入场固定点数) ===")
    for f in [0.236, 0.382, 0.5, 0.618]:
        r = pd.DataFrame([sim_fib(D, a, b, f, anchor_at_edge=False) for a, b in days])
        tr = r[r.reason.isin(["TP", "SL", "EOD"])]
        print(f"  fib={f:<5} 成交={len(tr):4d}  胜率={stat(tr.pnl)['win']}%  "
              f"期望={stat(tr.pnl)['exp']}  PF={stat(tr.pnl)['pf']}  总={tr.pnl.sum():.0f}")


def stat(p):
    p = pd.Series(p)
    if len(p) == 0 or p.sum() == 0 and (p == 0).all():
        return {"win": 0, "pf": 0, "exp": 0}
    gw = p[p > 0].sum(); gl = p[p < 0].sum()
    return {"win": round((p > 0).mean() * 100, 1),
            "pf": round(gw / abs(gl), 2) if gl else np.inf,
            "exp": round(p.mean(), 2)}


if __name__ == "__main__":
    main()
