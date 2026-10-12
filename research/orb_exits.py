"""出场研究: 固定点数 vs ATR/OR宽 缩放, 保本止损, 移动止损, 时间出场。

入场统一为基线 (OR30 + 2根确认 + 收盘市价, 锚定 OR 边界)。
只改出场管理, 便于归因。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
import pandas as pd

from orb_research import (QTY, SL_PTS, TP_PTS, BRK, OR_MIN, OR_START, EOD_MIN,
                          rnd, load_5min, prep, day_slices, daily_from_5min)
from orb_variants import or_for_day, close_trade


def entry_of(D, a, b):
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
            return dict(pos=pos, i=i, entry=rnd(D.c[i]), orhi=orhi, orlo=orlo,
                        orw=orhi - orlo, anchor=orhi if pos > 0 else orlo)
    return None


def sim_managed(D, a, b, sl0, tp0,
                be_R=None, be_buf=0.0, trail_R=None, trail_pts=None,
                use_tp=True, eod=EOD_MIN):
    """通用出场管理。sl0/tp0 为绝对价格 (由调用方决定锚点)。

    be_R: 达到该 R 倍数后把止损移到保本(+be_buf), None=不用
    trail_R: 移动止损 = 极值 - trail_R*初始风险(点); None=不用
    trail_pts: 移动止损 = 极值 - trail_pts(固定点); None=不用
    use_tp: 是否保留止盈
    """
    e = entry_of(D, a, b)
    if e is None:
        return None
    pos, i, entry = e["pos"], e["i"], e["entry"]
    sl = rnd(sl0)
    tp = rnd(tp0) if np.isfinite(tp0) else np.inf
    R = abs(entry - sl)
    be_done = False
    for j in range(i, b):
        if D.mod[j] >= eod:
            break
        # 1) 本 bar 触发判断 (entry bar 先看收盘止盈)
        if j == i and use_tp and ((pos > 0 and D.c[j] >= tp) or (pos < 0 and D.c[j] <= tp)):
            return _res(pos, entry, rnd(D.c[j]), "TP", R)
        if j > i:
            f, r = fill_managed(D, j, pos, sl, tp if use_tp else None)
            if f is not None:
                return _res(pos, entry, rnd(f), r, R)
        # 2) 更新管理 (本 bar 极值)
        fav = (D.h[j] - entry) if pos > 0 else (entry - D.l[j])
        ext = D.h[j] if pos > 0 else D.l[j]
        if be_R is not None and not be_done and fav >= be_R * R:
            newsl = entry + be_buf if pos > 0 else entry - be_buf
            sl = max(sl, rnd(newsl)) if pos > 0 else min(sl, rnd(newsl))
            be_done = True
        if trail_R is not None:
            cand = ext - trail_R * R if pos > 0 else ext + trail_R * R
            sl = max(sl, rnd(cand)) if pos > 0 else min(sl, rnd(cand))
        if trail_pts is not None:
            cand = ext - trail_pts if pos > 0 else ext + trail_pts
            sl = max(sl, rnd(cand)) if pos > 0 else min(sl, rnd(cand))
    cand = [k for k in range(i, b) if D.mod[k] < eod]
    j = max(cand) if cand else i
    return _res(pos, entry, rnd(D.c[j]), "EOD", R)


def fill_managed(D, i, pos, sl, tp):
    tp = np.inf if tp is None else tp
    if pos > 0:
        if D.o[i] <= sl: return D.o[i], "SL"
        if D.o[i] >= tp: return D.o[i], "TP"
        if D.l[i] <= sl and D.h[i] >= tp:
            return (sl, "SL") if D.c[i] >= D.o[i] else (tp, "TP")
        if D.l[i] <= sl: return sl, "SL"
        if D.h[i] >= tp: return tp, "TP"
    else:
        if D.o[i] >= sl: return D.o[i], "SL"
        if D.o[i] <= tp: return D.o[i], "TP"
        if D.h[i] >= sl and D.l[i] <= tp:
            return (tp, "TP") if D.c[i] >= D.o[i] else (sl, "SL")
        if D.h[i] >= sl: return sl, "SL"
        if D.l[i] <= tp: return tp, "TP"
    return None, None


def _res(pos, entry, px, reason, R):
    return dict(pnl=close_trade(pos, entry, px), reason=reason, entry=entry,
                exit=px, R=pos * (px - entry) / R)


def report(name, rows):
    df = pd.DataFrame([r for r in rows if r])
    tr = df.copy()
    p = tr.pnl
    if len(p) == 0:
        print(f"  {name:40s} 无交易")
        return
    gw = p[p > 0].sum(); gl = p[p < 0].sum()
    exp, se = p.mean(), p.std() / np.sqrt(len(p))
    eq = p.cumsum(); mdd = (eq.cummax() - eq).max()
    # R 倍数
    Rr = tr.R.mean()
    print(f"  {name:38s} n={len(p):4d} 胜={ (p>0).mean()*100:4.1f}% PF={gw/abs(gl) if gl else 9:4.2f} "
          f"期望={exp:6.2f} t={exp/se if se else 0:5.2f} 均R={Rr:5.2f} 总={p.sum():7.0f} MDD={mdd:5.0f} "
          f"| TP={ (tr.reason=='TP').mean()*100:4.0f}% SL={ (tr.reason=='SL').mean()*100:4.0f}% EOD={ (tr.reason=='EOD').mean()*100:4.0f}%")


def main():
    D = prep(load_5min())
    daily = daily_from_5min(D)
    daily["prev_close"] = daily["close"].shift(1)
    daily["tr"] = np.maximum(daily["high"] - daily["low"],
                             np.maximum((daily["high"] - daily["prev_close"]).abs(),
                                        (daily["low"] - daily["prev_close"]).abs()))
    daily["atr14"] = daily["tr"].rolling(14).mean().shift(1)
    days = [(dt, a, b) for dt, a, b in day_slices(D) if D.dow[a] in (0, 1, 2)]
    atrs = {dt: daily["atr14"].get(dt, np.nan) for dt, a, b in days}

    def stops(e, sl_u, tp_u, anchor="edge"):
        ref = e["anchor"] if anchor == "edge" else e["entry"]
        if e["pos"] > 0:
            return ref - sl_u, ref + tp_u
        return ref + sl_u, ref - tp_u

    def run(sl_u_fn, tp_u_fn, anchor="edge", **mgmt):
        rows = []
        for dt, a, b in days:
            e = entry_of(D, a, b)
            if e is None:
                rows.append(None); continue
            if callable(sl_u_fn):
                sl_u, tp_u = sl_u_fn(e, dt), tp_u_fn(e, dt)
            else:
                sl_u, tp_u = sl_u_fn, tp_u_fn
            sl0, tp0 = stops(e, sl_u, tp_u, anchor)
            rows.append(sim_managed(D, a, b, sl0, tp0, **mgmt))
        return rows

    # 1) 基线: 固定点数 (锚 OR 边界, 与实盘一致)
    print("=== 1) 固定点数 ===")
    report("fixed 1.1/2.1 @edge (实盘基线)", run(SL_PTS, TP_PTS, "edge"))
    report("fixed 1.1/2.1 @entry", run(SL_PTS, TP_PTS, "entry"))

    # 2) ATR 缩放
    print("\n=== 2) ATR 缩放 (锚 OR 边界) ===")
    for k, m in [(0.3, 0.6), (0.5, 1.0), (0.75, 1.5), (1.0, 2.0), (0.5, 0.75)]:
        rows = []
        for dt, a, b in days:
            atr = atrs[dt]
            if not np.isfinite(atr):
                rows.append(None); continue
            e = entry_of(D, a, b)
            if e is None:
                rows.append(None); continue
            sl0, tp0 = stops(e, k * atr, m * atr, "edge")
            rows.append(sim_managed(D, a, b, sl0, tp0))
        report(f"ATR {k}x/{m}x @edge", rows)

    # 3) OR宽缩放
    print("\n=== 3) OR宽缩放 (锚 OR 边界) ===")
    for k, m in [(0.5, 1.0), (0.75, 1.5), (1.0, 2.0), (0.5, 0.75)]:
        rows = []
        for dt, a, b in days:
            e = entry_of(D, a, b)
            if e is None:
                rows.append(None); continue
            sl0, tp0 = stops(e, k * e["orw"], m * e["orw"], "edge")
            rows.append(sim_managed(D, a, b, sl0, tp0))
        report(f"ORw {k}x/{m}x @edge", rows)

    # 4) 保本止损
    print("\n=== 4) 保本止损 (基线1.1/2.1 + 到 X R 移保本) ===")
    for be in [0.4, 0.5, 0.7, 1.0]:
        report(f"BE at {be}R", run(SL_PTS, TP_PTS, "edge", be_R=be))
    print("   取消止盈 + 保本:")
    for be in [0.5, 0.7, 1.0]:
        report(f"noTP + BE {be}R", run(SL_PTS, TP_PTS, "edge", be_R=be, use_tp=False))

    # 5) 移动止损
    print("\n=== 5) 移动止损 ===")
    print("  [a] 纯移动 (无TP)")
    for tr in [0.5, 0.75, 1.0, 1.5]:
        report(f"trail {tr}R, noTP", run(SL_PTS, TP_PTS, "edge", trail_R=tr, use_tp=False))
    print("  [b] 移动 + TP2.1")
    for tr in [0.5, 0.75, 1.0, 1.5]:
        report(f"trail {tr}R + TP", run(SL_PTS, TP_PTS, "edge", trail_R=tr, use_tp=True))
    print("  [c] 固定点移动 (无TP)")
    for tp_ in [0.5, 0.8, 1.1]:
        report(f"trail {tp_}pt, noTP", run(SL_PTS, TP_PTS, "edge", trail_pts=tp_, use_tp=False))
    print("  [d] 保本0.5R + 移动1R, 无TP")
    report("BE0.5 + trail1.0R noTP", run(SL_PTS, TP_PTS, "edge", be_R=0.5, trail_R=1.0, use_tp=False))

    # 6) 时间出场
    print("\n=== 6) 时间出场 (固定1.1/2.1 @edge) ===")
    for eod in [600, 630, 660, 690, 720, 780, 900, 930, 959]:
        report(f"EOD {eod//60:02d}:{eod%60:02d}", run(SL_PTS, TP_PTS, "edge", eod=eod))

    # 7) 固定点数何时失效: 按当日波动(ATR/OR宽)分档
    print("\n=== 7) 固定点数 1.1/2.1 的失效条件 (按波动分档) ===")
    base_rows = run(SL_PTS, TP_PTS, "edge")
    for dt, a, b in days:
        pass
    dfb = pd.DataFrame([r for r in base_rows if r])
    dfb["date"] = [dt for dt, _, _ in days if True][:len(dfb)] if len(dfb) == len(days) else None
    # 重新对齐日期
    metas = []
    for dt, a, b in days:
        e = entry_of(D, a, b)
        if e is not None:
            metas.append((dt, atrs[dt], e["orw"]))
    dfb = dfb.reset_index(drop=True)
    md = pd.DataFrame(metas, columns=["date", "atr", "orw"])
    dfb = pd.concat([dfb, md], axis=1)
    for col, label in [("atr", "日ATR"), ("orw", "OR宽度")]:
        dfb["_b"] = pd.qcut(dfb[col], 4, labels=["Q1最低", "Q2", "Q3", "Q4最高"])
        print(f"  按 {label} 分档:")
        g = dfb.groupby("_b", observed=True).apply(lambda x: pd.Series({
            "n": len(x), "胜率": round((x.pnl > 0).mean() * 100, 1),
            "PF": round(x[x.pnl > 0].pnl.sum() / abs(x[x.pnl < 0].pnl.sum()), 2),
            "期望": round(x.pnl.mean(), 1), "总": round(x.pnl.sum(), 0)}), include_groups=False)
        print(g.to_string())

    # 8) 逐年稳健性
    print("\n=== 8) 逐年稳健性 (关键出场方案) ===")
    configs = {
        "baseline 1.1/2.1 @edge": (SL_PTS, TP_PTS, "edge", {}),
        "@entry": (SL_PTS, TP_PTS, "entry", {}),
        "BE0.4R": (SL_PTS, TP_PTS, "edge", dict(be_R=0.4)),
        "trail0.8pt noTP": (SL_PTS, TP_PTS, "edge", dict(trail_pts=0.8, use_tp=False)),
        "trail1.0R+TP": (SL_PTS, TP_PTS, "edge", dict(trail_R=1.0, use_tp=True)),
    }
    for name, (slu, tpu, anc, mg) in configs.items():
        rows = run(slu, tpu, anc, **mg)
        df = pd.DataFrame([r for r in rows if r])
        df["yr"] = [dt.year for dt, a, b in days if entry_of(D, a, b)]
        parts = []
        for y, x in df.groupby("yr"):
            gw = x[x.pnl > 0].pnl.sum(); gl = x[x.pnl < 0].pnl.sum()
            parts.append(f"{y}:PF{gw/abs(gl) if gl else 9:.2f}/总{x.pnl.sum():6.0f}")
        print(f"  {name:24s} " + "  ".join(parts))


if __name__ == "__main__":
    main()
