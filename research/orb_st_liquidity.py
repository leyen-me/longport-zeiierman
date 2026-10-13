"""出场研究 2: Supertrend 出场 × 昨日高/低关口(流动性) 止盈。

入口统一为 v2 入场 (OR=15min, 连续 3 根收盘突破, Mon/Tue/Wed/Fri, 可选 gap/fd 过滤),
只改出场管理, 便于归因。所有结果按 IS(2020-07~2023) / OOS(2024~2026) 拆分。

研究方向:
  A. 基线 (锚 OR 边界固定 1.1/2.1) ± 移动止损
  B. Supertrend 翻转出场 (替代固定 TP/SL)
  C. Supertrend 线当移动止损 (保留固定 TP)
  D. 昨高/昨低 当止盈目标 (流动性)
  E. 组合: 昨关止盈 + Supertrend 移动/翻转
  F. 分段多目标: 昨关先平一半 + 余额 Supertrend

数据: data_cache/SPY_US_non_min2.parquet -> 5min (美东)。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
import pandas as pd

from orb_research import (QTY, MINTICK, OR_START, EOD_MIN, TZ, rnd, load_5min,
                          prep, day_slices, daily_from_5min)

# ------------------------------------------------------------------ v2 入场参数
OR_MIN = 15
BRK = 3
TRADE_DOW = (0, 1, 2, 4)
USE_GAP = False
USE_FD = True
FIRST_ONLY = True
SL_PTS = 1.1
TP_PTS = 2.1


# ------------------------------------------------------------------ Supertrend
def supertrend(h, l, c, period=10, factor=3.0):
    """经典 Supertrend (逐 bar)。返回 (st_line, direction)。

    direction: -1 = 上升趋势 (价格在 ST 线上方), +1 = 下降趋势。
    与 Pine 内置 ta.supertrend 语义一致 (RTH 连续序列上计算)。
    """
    n = len(c)
    hl2 = (h + l) / 2.0
    pc = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    atr = pd.Series(tr).rolling(period).mean().to_numpy(float)

    up = hl2 + factor * atr
    lo = hl2 - factor * atr
    fu = np.full(n, np.nan)
    fl = np.full(n, np.nan)
    st = np.full(n, np.nan)
    d = np.ones(n, dtype=int)

    start = period
    fu[start] = up[start]
    fl[start] = lo[start]
    st[start] = up[start]
    d[start] = 1
    for i in range(start + 1, n):
        fu[i] = up[i] if (up[i] < fu[i - 1] or c[i - 1] > fu[i - 1]) else fu[i - 1]
        fl[i] = lo[i] if (lo[i] > fl[i - 1] or c[i - 1] < fl[i - 1]) else fl[i - 1]
        if st[i - 1] == fu[i - 1]:
            d[i] = 1 if c[i] > fu[i] else -1
        else:
            d[i] = -1 if c[i] < fl[i] else 1
        st[i] = fl[i] if d[i] == -1 else fu[i]
    return st, d


# ------------------------------------------------------------------ 入场
def day_entry(D, a, b, daily, or_min=OR_MIN, brk=BRK, trade_dow=TRADE_DOW,
              use_gap=USE_GAP, use_fd=USE_FD, first_only=FIRST_ONLY, eod=EOD_MIN):
    dt = D.date[a]
    if D.dow[a] not in trade_dow:
        return None
    or_end = OR_START + or_min
    sess = [i for i in range(a, b) if OR_START <= D.mod[i] < or_end]
    if not sess:
        return None
    orhi, orlo = D.h[sess].max(), D.l[sess].min()
    first_dir = 1 if D.c[sess[0]] >= D.o[sess[0]] else -1
    prev_close = daily["prev_close"].get(dt, np.nan)
    gap_up = np.isnan(prev_close) or D.o[a] > prev_close
    gap_dn = np.isnan(prev_close) or D.o[a] < prev_close

    after = [i for i in range(sess[-1] + 1, b) if D.mod[i] < eod]
    bull = bear = 0
    for i in after:
        if D.c[i] > orhi:
            bull += 1; bear = 0
        elif D.c[i] < orlo:
            bear += 1; bull = 0
        else:
            bull = bear = 0
        long_sig, short_sig = bull >= brk, bear >= brk
        if not (long_sig or short_sig):
            continue
        if first_only:
            pass  # 首个突破即锁定当日 (过滤不通过也不再等反向)
        gap_ok_l = (not use_gap) or gap_up
        gap_ok_s = (not use_gap) or gap_dn
        fd_ok_l = (not use_fd) or first_dir > 0
        fd_ok_s = (not use_fd) or first_dir < 0
        if long_sig and gap_ok_l and fd_ok_l:
            pos, anchor = 1, orhi
        elif short_sig and gap_ok_s and fd_ok_s:
            pos, anchor = -1, orlo
        else:
            if first_only:
                return None
            continue
        return dict(pos=pos, i=i, entry=rnd(D.c[i]), anchor=anchor,
                    orhi=orhi, orlo=orlo, first_dir=first_dir)
    return None


def _fill(D, i, pos, sl, tp):
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


# ------------------------------------------------------------------ 出场模拟
def sim_exit(D, a, b, e, st, stdir, prev_high, prev_low, *,
             sl_pts=SL_PTS, tp_pts=TP_PTS, anchor="edge",
             use_tp=True, trail_R=None,
             st_mode=None, st_entry=True, st_flip_after_R=None,
             liq_tp=False, liq_tp_frac=None, liq_mode=None,
             eod=EOD_MIN):
    """统一出场管理。

    anchor: "edge"=锚 OR 边界 | "entry"=锚入场价
    trail_R: 极值 ∓ trail_R*R
    st_mode: None | "flip" (ST 翻转平) | "trail" (ST 线当移动止损) | "both"
    st_flip_after_R: ST 翻转仅在浮盈达该 R 后生效 (保护利润), None=一直生效
    liq_tp: True 时用昨高/昨低当止盈 (保留旧接口)
    liq_mode: None | "only"(昨关) | "nearer"(固定/昨关取近) | "farther"(取远)
    liq_tp_frac: 昨关先平该比例, 余额继续
    """
    pos, entry, i = e["pos"], e["entry"], e["i"]
    ref = e["anchor"] if anchor == "edge" else entry
    sl = rnd(ref - sl_pts) if pos > 0 else rnd(ref + sl_pts)
    mode = liq_mode or ("only" if liq_tp else None)
    base_tp = rnd(ref + tp_pts) if pos > 0 else rnd(ref - tp_pts)
    lvl = prev_high if pos > 0 else prev_low
    lvl_ok = np.isfinite(lvl) and ((pos > 0 and lvl > entry) or (pos < 0 and lvl < entry))
    if (use_tp or mode) and mode and lvl_ok:
        lt = rnd(lvl)
        if mode == "only":
            tp = lt
        elif mode == "nearer":
            tp = min(base_tp, lt) if pos > 0 else max(base_tp, lt)
        else:  # farther
            tp = max(base_tp, lt) if pos > 0 else min(base_tp, lt)
    elif use_tp:
        tp = base_tp
    else:
        tp = np.inf
    R = abs(entry - sl)
    done_frac = 0.0
    realized = 0.0
    start = i if st_entry else i + 1
    tp_live = tp  # 分批模式下: 首批成交后置为 inf

    def result(px, reason):
        bal = QTY * (1 - done_frac) * pos * (rnd(px) - entry) + realized
        return dict(pnl=bal, reason=reason, entry=entry, exit=rnd(px),
                    R=pos * (rnd(px) - entry) / R if R else np.nan,
                    frac=done_frac)

    for j in range(i, b):
        if D.mod[j] >= eod:
            break
        # 入场当根: 收盘越过 TP 即刻成交
        if j == i and use_tp and np.isfinite(tp_live) and \
                ((pos > 0 and D.c[j] >= tp_live) or (pos < 0 and D.c[j] <= tp_live)):
            return result(D.c[j], "TP")
        # 昨关分批: 先落袋 frac (本根最高/最低触及)
        if liq_tp_frac is not None and done_frac == 0.0 and np.isfinite(tp_live) and \
                ((pos > 0 and D.h[j] >= tp_live) or (pos < 0 and D.l[j] <= tp_live)):
            realized += QTY * liq_tp_frac * pos * (tp_live - entry)
            done_frac = liq_tp_frac
            tp_live = np.inf
        # 盘中 stop/limit (入场当根不触发, 与 TV 一致)
        if j > i:
            f, r = _fill(D, j, pos, sl, tp_live)
            if f is not None:
                return result(f, r)
        # ST 线当移动止损: 用上一根的 ST 值判断本根是否击穿
        if st_mode in ("trail", "both") and j > start:
            stv = st[j - 1] if np.isfinite(st[j - 1]) else np.nan
            if np.isfinite(stv):
                if pos > 0 and D.l[j] <= stv:
                    return result(min(stv, D.o[j]) if D.o[j] < stv else stv, "ST_TR")
                if pos < 0 and D.h[j] >= stv:
                    return result(max(stv, D.o[j]) if D.o[j] > stv else stv, "ST_TR")
        # R 倍数移动止损
        if trail_R is not None and j > i:
            ext = D.h[j] if pos > 0 else D.l[j]
            cand = ext - trail_R * R if pos > 0 else ext + trail_R * R
            sl = max(sl, rnd(cand)) if pos > 0 else min(sl, rnd(cand))
        # ST 翻转: 本根方向转为不利 -> 收盘平 (可选: 浮盈达 X R 后才生效)
        if st_mode in ("flip", "both") and j > i:
            fav = (D.h[j] - entry) if pos > 0 else (entry - D.l[j])
            fav_ok = st_flip_after_R is None or fav >= st_flip_after_R * R
            if fav_ok:
                if pos > 0 and stdir[j] == 1:
                    return result(D.c[j], "ST_FLIP")
                if pos < 0 and stdir[j] == -1:
                    return result(D.c[j], "ST_FLIP")

        # 更新移动止损用于下一根 (trail_R 用本根极值, 已在上方)
    cand = [k for k in range(i, b) if D.mod[k] < eod]
    j = max(cand) if cand else i
    return result(D.c[j], "EOD")


# ------------------------------------------------------------------ 评估
def report(name, rows):
    df = pd.DataFrame([r for r in rows if r])
    p = df.pnl
    if len(p) == 0:
        print(f"  {name:34s} 无交易"); return
    gw, gl = p[p > 0].sum(), p[p < 0].sum()
    exp, se = p.mean(), p.std(ddof=1) / np.sqrt(len(p)) if len(p) > 1 else np.nan
    eq = p.cumsum(); mdd = (eq.cummax() - eq).max()
    r = df.reason.value_counts(normalize=True) * 100
    def pc(k): return r.get(k, 0)
    print(f"  {name:34s} n={len(p):4d} 胜={ (p>0).mean()*100:4.1f}% PF={gw/abs(gl) if gl else 9:4.2f} "
          f"期望={exp:6.2f} t={exp/se if se and se>0 else 0:5.2f} 均R={df.R.mean():5.2f} "
          f"总={p.sum():7.0f} MDD={mdd:5.0f} | "
          f"TP={pc('TP'):3.0f}% SL={pc('SL'):3.0f}% "
          f"STf={pc('ST_FLIP'):3.0f}% STr={pc('ST_TR'):3.0f}% EOD={pc('EOD'):3.0f}%")


def is_oos_report(name, rows, dates):
    df = pd.DataFrame([r for r in rows if r])
    d = pd.Series(dates).reset_index(drop=True)
    df = df.reset_index(drop=True).assign(date=d)
    for lab, m in [("IS", pd.to_datetime(df.date) < "2024-01-01"),
                   ("OOS", pd.to_datetime(df.date) >= "2024-01-01")]:
        sub = df[m]
        if len(sub) == 0:
            print(f"    {lab}: n=0"); continue
        p = sub.pnl
        gw, gl = p[p > 0].sum(), p[p < 0].sum()
        print(f"    {lab:3s} n={len(p):4d} 胜={ (p>0).mean()*100:4.1f}% "
              f"PF={gw/abs(gl) if gl else 9:4.2f} 期望={p.mean():6.2f} 总={p.sum():7.0f}")


def main():
    df = load_5min()
    D = prep(df)
    daily = daily_from_5min(D)
    daily["prev_close"] = daily["close"].shift(1)
    daily["prev_high"] = daily["high"].shift(1)
    daily["prev_low"] = daily["low"].shift(1)

    st, stdir = supertrend(D.h, D.l, D.c, period=10, factor=3.0)
    days = day_slices(D)

    def atr_st(period, factor):
        return supertrend(D.h, D.l, D.c, period=period, factor=factor)

    # 预计算入场 (两组: gap关 / gap开, 均 fd开; 用于对照 v2 报告口径)
    def build_entries(use_gap):
        out, ds = {}, []
        for dt, a, b in days:
            e = day_entry(D, a, b, daily, use_gap=use_gap)
            if e is not None:
                out[dt] = (a, b, e)
                ds.append(dt)
        return out, ds

    entries, dates_ok = build_entries(USE_GAP)
    entries_g, dates_g = build_entries(True)
    print(f"可交易事件(gap关): {len(entries)}  区间 {min(dates_ok)} ~ {max(dates_ok)}")
    print(f"可交易事件(gap开): {len(entries_g)}")

    def run(exit_kw, st_line=None, st_dir=None, ent=None):
        ent = entries if ent is None else ent
        rows = []
        for dt, a, b in days:
            if dt not in ent:
                continue
            _, _, e = ent[dt]
            ph = daily["prev_high"].get(dt, np.nan)
            pl = daily["prev_low"].get(dt, np.nan)
            rows.append(sim_exit(D, a, b, e,
                                 st_line if st_line is not None else st,
                                 st_dir if st_dir is not None else stdir,
                                 ph, pl, **exit_kw))
        return rows

    # ---------------- A. 基线 ----------------
    print("\n=== A. 基线 (锚 OR 边界) ===")
    report("fixed 1.1/2.1 @edge (gap关)", run(dict()))
    report("fixed 1.1/2.1 @edge (gap开,v2口径)", run(dict(), ent=entries_g))
    report("v2口径 + trail0.75R", run(dict(trail_R=0.75), ent=entries_g))
    report("fixed + trail 0.75R", run(dict(trail_R=0.75)))
    report("noTP + trail 0.75R", run(dict(trail_R=0.75, use_tp=False)))

    # ---------------- B. Supertrend 翻转 ----------------
    print("\n=== B. Supertrend 翻转出场 (固定 SL 仍在, 翻转平) ===")
    for per, fac in [(7, 2.0), (10, 3.0), (14, 3.0), (10, 2.0), (20, 4.0)]:
        s, sd = atr_st(per, fac)
        report(f"ST{per}/{fac} flip + fixed TP", run(dict(st_mode="flip"), s, sd))
        report(f"ST{per}/{fac} flip noTP", run(dict(st_mode="flip", use_tp=False), s, sd))

    # ---------------- C. Supertrend 移动止损 ----------------
    print("\n=== C. Supertrend 线当移动止损 ===")
    for per, fac in [(10, 3.0), (10, 2.0), (14, 3.0), (7, 2.0)]:
        s, sd = atr_st(per, fac)
        report(f"ST{per}/{fac} trail + fixed TP", run(dict(st_mode="trail"), s, sd))
        report(f"ST{per}/{fac} trail noTP", run(dict(st_mode="trail", use_tp=False), s, sd))

    # ---------------- D. 昨高/昨低 当止盈 ----------------
    print("\n=== D. 昨日高/低关口(流动性) 当止盈 ===")
    report("liq only (昨关替代固定TP)", run(dict(liq_mode="only")))
    report("liq nearer (取近目标)", run(dict(liq_mode="nearer")))
    report("liq farther (让盈利跑到昨关)", run(dict(liq_mode="farther")))
    report("liq farther + trail0.75R", run(dict(liq_mode="farther", trail_R=0.75)))
    report("liq only + trail0.75R", run(dict(liq_mode="only", trail_R=0.75)))

    # ---------------- E. 组合: 昨关 + Supertrend ----------------
    print("\n=== E. 组合: 昨关止盈 + Supertrend ===")
    s, sd = atr_st(10, 3.0)
    report("liqTP only + ST10/3 flip", run(dict(liq_mode="only", st_mode="flip"), s, sd))
    report("liqTP only + ST10/3 trail", run(dict(liq_mode="only", st_mode="trail"), s, sd))
    report("liqTP farther + ST10/3 trail", run(dict(liq_mode="farther", st_mode="trail"), s, sd))
    report("ST10/3 flip after +1R (保护)", run(dict(st_mode="flip", st_flip_after_R=1.0), s, sd))
    report("ST10/3 flip after +0.5R", run(dict(st_mode="flip", st_flip_after_R=0.5), s, sd))

    # ---------------- F. 昨关分批 ----------------
    print("\n=== F. 昨关先平一半 + 余额继续 ===")
    report("liq half + fixed TP rest", run(dict(liq_tp=True, liq_tp_frac=0.5)))
    report("liq half + trail0.75R rest", run(dict(liq_tp=True, liq_tp_frac=0.5, trail_R=0.75)))
    report("liq half + ST10/3 trail rest", run(dict(liq_tp=True, liq_tp_frac=0.5, st_mode="trail"), s, sd))
    report("liq half + ST10/3 flip rest", run(dict(liq_tp=True, liq_tp_frac=0.5, st_mode="flip"), s, sd))
    report("liq half + ST flip(only after1R)", run(dict(liq_tp=True, liq_tp_frac=0.5, st_mode="flip", st_flip_after_R=1.0), s, sd))

    # ---------------- IS/OOS 对照 ----------------
    print("\n=== IS/OOS 对照 (关键方案) ===")
    cfgs = {
        "baseline(gap关)": (dict(), entries),
        "baseline(gap开) v2口径": (dict(), entries_g),
        "trail0.75R": (dict(trail_R=0.75), None),
        "ST10/3 trail + TP": (dict(st_mode="trail"), None),
        "ST10/3 flip + TP": (dict(st_mode="flip"), None),
        "ST10/3 flip after1R": (dict(st_mode="flip", st_flip_after_R=1.0), None),
        "liq only": (dict(liq_mode="only"), None),
        "liq farther": (dict(liq_mode="farther"), None),
        "liq only + ST trail": (dict(liq_mode="only", st_mode="trail"), None),
        "liq half + ST trail": (dict(liq_tp=True, liq_tp_frac=0.5, st_mode="trail"), None),
    }
    for name, (kw, ent) in cfgs.items():
        rows = run(kw, ent=ent)
        print(f"  [{name}]")
        is_oos_report(name, rows, dates_g if ent is entries_g else dates_ok)


if __name__ == "__main__":
    main()
