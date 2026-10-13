"""亏损归因研究: 逐笔诊断亏损单, 区分"入场就错" vs "管理搞砸", 再挖能质变的因子。

方法:
  1. 用 v2 入场 (or15/brk3, 收盘市价) + 基线出场 (锚 OR 边界固定 SL/TP, 过 12:00 平)。
  2. 逐笔记录 MFE/MAE (以初始风险 R 为单位)、出场原因、入场时刻、当日 regime 特征。
  3. 把亏损分成:
       · 从没工作  : MFE < 0.5R 就亏  -> 入场质量问题 (该加过滤)
       · 先赚后吐  : MFE >= 1R 仍亏   -> 出场/管理问题 (该改出场)
       · 中间      : 0.5R <= MFE < 1R
  4. 单因子分档: 每个特征按分位切成桶, 看哪一桶亏损集中 (win/PF/期望)。
  5. 候选过滤因子做 IS/OOS + 贪心组合, 找能显著提升的开关。

数据: data_cache/SPY_US_non_min2.parquet -> 5min (美东)。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
import pandas as pd

from orb_lab import load, prep, slices, daily, day_entry, metrics, QTY, rnd

OR_MIN = 15
BRK = 3
TRADE_DOW = (0, 1, 2, 4)
SL_PTS = 1.1
TP_PTS = 2.1
EOD = 12 * 60


# --------------------------------------------------------------- simulate + MFE/MAE
def sim_record(D, a, b, e, sl_pts=SL_PTS, tp_pts=TP_PTS, eod=EOD):
    pos, entry, i = e["pos"], e["entry"], e["entry_i"]
    anchor = e["anchor"]
    sl = rnd(anchor - sl_pts) if pos > 0 else rnd(anchor + sl_pts)
    tp = rnd(anchor + tp_pts) if pos > 0 else rnd(anchor - tp_pts)
    R = abs(entry - sl)
    mfe = mae = 0.0
    exit_px = reason = None
    exit_i = i

    def pack(px, rsn, j):
        return dict(pnl=pos * QTY * (rnd(px) - entry), reason=rsn,
                    exit_i=j, exit_px=rnd(px),
                    mfe_R=mfe / R if R else np.nan, mae_R=mae / R if R else np.nan,
                    bars=j - i, sl=sl, tp=tp, R=R)

    # 入场当根: 收盘越 TP
    if (pos > 0 and D["c"][i] >= tp) or (pos < 0 and D["c"][i] <= tp):
        fav = (D["c"][i] - entry) if pos > 0 else (entry - D["c"][i])
        mfe = max(mfe, fav)
        return pack(D["c"][i], "TP", i)
    for j in range(i + 1, b):
        if D["mod"][j] >= eod:
            break
        fav = (D["h"][j] - entry) if pos > 0 else (entry - D["l"][j])
        adv = (entry - D["l"][j]) if pos > 0 else (D["h"][j] - entry)
        mfe = max(mfe, fav)
        mae = max(mae, adv)
        if pos > 0:
            if D["o"][j] <= sl:  return pack(D["o"][j], "SL", j)
            if D["o"][j] >= tp:  return pack(D["o"][j], "TP", j)
            if D["l"][j] <= sl and D["h"][j] >= tp:
                return pack(sl if D["c"][j] >= D["o"][j] else tp,
                            "SL" if D["c"][j] >= D["o"][j] else "TP", j)
            if D["l"][j] <= sl: return pack(sl, "SL", j)
            if D["h"][j] >= tp: return pack(tp, "TP", j)
        else:
            if D["o"][j] >= sl:  return pack(D["o"][j], "SL", j)
            if D["o"][j] <= tp:  return pack(D["o"][j], "TP", j)
            if D["h"][j] >= sl and D["l"][j] <= tp:
                return pack(tp if D["c"][j] >= D["o"][j] else sl,
                            "TP" if D["c"][j] >= D["o"][j] else "SL", j)
            if D["h"][j] >= sl: return pack(sl, "SL", j)
            if D["l"][j] <= tp: return pack(tp, "TP", j)
    cand = [k for k in range(i, b) if D["mod"][k] < eod]
    j = max(cand) if cand else i
    return pack(D["c"][j], "EOD", j)


# --------------------------------------------------------------- build trades table
def build(D, dl, or_min=OR_MIN, brk=BRK, trade_dow=TRADE_DOW, use_gap=False,
          use_fd=True, first_only=True):
    rows = []
    for dt, a, b in D["_days"]:
        if D["dow"][a] not in trade_dow:
            continue
        e = day_entry(D, a, b, or_min, brk, "close")
        if e is None:
            continue
        # fd 过滤
        if use_fd and e["first_dir"] != e["pos"]:
            continue
        # gap 过滤
        row0 = dict(date=dt)
        if dt in dl.index:
            d = dl.loc[dt]
            pc = d["pc"]
            gap = D["o"][a] - pc if np.isfinite(pc) else np.nan
            if use_gap and np.isfinite(pc):
                if (e["pos"] > 0 and gap < 0) or (e["pos"] < 0 and gap > 0):
                    continue
            atr = d["atr"]
            ph, pl = d["ph"], d["pl"]
            row0.update(gap=gap, gap_atr=gap / atr if atr and atr > 0 else np.nan,
                        atr=atr, ph=ph, pl=pl, pc=pc,
                        sma20=d["sma20"], sma50=d["sma50"], vol20=d["vol20"])
        else:
            continue

        r = sim_record(D, a, b, e)
        entry = e["entry"]
        side = e["pos"]
        # 入场前特征
        ph, pl, atr = row0["ph"], row0["pl"], row0["atr"]
        row0.update(
            side=side, entry=entry, entry_i=e["entry_i"],
            entry_min=D["mod"][e["entry_i"]],
            brk_delay=e["entry_i"] - (a + int(or_min / 5) - 1),
            orw=e["orw"], orw_atr=e["orw"] / atr if atr and atr > 0 else np.nan,
            or_cl=e["or_cl"], first_dir=e["first_dir"],
            # 破昨关 / 昨关距离
            broke_prev=(entry - ph) if side > 0 else (pl - entry),
            broke_prev_ok=int(((side > 0 and entry > ph) or (side < 0 and entry < pl))
                              if np.isfinite(ph) and np.isfinite(pl) else 0),
            dist_prev_atr=((entry - ph) if side > 0 else (pl - entry)) / atr
            if atr and atr > 0 and np.isfinite(ph) and np.isfinite(pl) else np.nan,
            # 趋势/位置
            trend_align=int(((side > 0 and entry > row0["sma20"]) or
                             (side < 0 and entry < row0["sma20"]))
                            if np.isfinite(row0["sma20"]) else 0),
            open_dist_atr=(entry - D["o"][a]) / atr * side
            if atr and atr > 0 else np.nan,
            rv20=row0["vol20"],
        )
        row0.update(r)
        rows.append(row0)
    return pd.DataFrame(rows)


# --------------------------------------------------------------- reports
def summ(p, label, n_all=None):
    p = pd.Series(p)
    if len(p) == 0:
        print(f"  {label:34s} n=0"); return
    gw, gl = p[p > 0].sum(), p[p < 0].sum()
    exp = p.mean(); se = p.std() / np.sqrt(len(p)) if len(p) > 1 else np.nan
    pf = gw / abs(gl) if gl else float("inf")
    print(f"  {label:34s} n={len(p):4d} 胜={(p>0).mean()*100:4.1f}% PF={pf:4.2f} "
          f"期望={exp:6.2f} t={exp/se if se and se>0 else 0:5.2f} 总={p.sum():7.0f}")


def bucket(df, col, q=5, labels=None):
    if df[col].nunique() < 2:
        return
    try:
        b = pd.qcut(df[col], q, labels=labels, duplicates="drop")
    except ValueError:
        return
    g = df.groupby(b, observed=True).apply(lambda x: pd.Series({
        "n": len(x), "胜率": round((x.pnl > 0).mean() * 100, 1),
        "PF": round(x[x.pnl > 0].pnl.sum() / abs(x[x.pnl < 0].pnl.sum()), 2)
        if (x.pnl < 0).any() else 9, "期望": round(x.pnl.mean(), 1),
        "总": round(x.pnl.sum(), 0)}, 
    ), include_groups=False)
    print(g.to_string())


def main():
    df = load()
    D = prep(df)
    dl = daily(D)
    D["_days"] = slices(D)

    tr = build(D, dl)
    print(f"交易数: {len(tr)}  {tr.date.min()} ~ {tr.date.max()}")
    tr["yr"] = pd.to_datetime(tr.date).dt.year
    tr["is_pre"] = pd.to_datetime(tr.date) < "2024-01-01"

    base = metrics(tr)
    print(f"基线: {base}")

    # ---------------- 1. 亏损分类 ----------------
    print("\n=== 1. 亏损分类 (MFE 视角) ===")
    los = tr[tr.pnl < 0]
    win = tr[tr.pnl > 0]
    print(f"  亏损 {len(los)} 笔 / 全部 {len(tr)}  (胜率 {(tr.pnl>0).mean()*100:.1f}%)")
    print(f"  亏损总亏 {los.pnl.sum():.0f} / 盈利总赚 {win.pnl.sum():.0f}")
    print("  亏损按出场原因:")
    for r, x in los.groupby("reason"):
        print(f"    {r:4s} n={len(x):4d} 总亏={x.pnl.sum():8.0f} 均MFE={x.mfe_R.mean():.2f}R "
              f"均MAE={x.mae_R.mean():.2f}R 均持有={x.bars.mean():.1f}bar")
    print("  亏损按 MFE 分层 (浮盈最高到多少 R):")
    bins = [(-1, 0.25, "从没工作 <0.25R"), (0.25, 0.5, "0.25~0.5R"),
            (0.5, 1.0, "0.5~1R"), (1.0, 99, "≥1R 后回吐")]
    for lo_, hi_, lab in bins:
        x = los[(los.mfe_R >= lo_) & (los.mfe_R < hi_)]
        print(f"    {lab:18s} n={len(x):4d} ({len(x)/len(los)*100:4.0f}%) "
              f"总亏={x.pnl.sum():8.0f} 均出场原因={dict(x.reason.value_counts())}")
    print(f"  盈利单均MFE={win.mfe_R.mean():.2f}R  均MAE={win.mae_R.mean():.2f}R")

    # ---------------- 2. 单因子分档 ----------------
    print("\n=== 2. 单因子分档 (看亏损集中在哪) ===")
    for col, lab in [("atr", "日ATR"), ("orw_atr", "OR宽/ATR"), ("orw", "OR宽"),
                     ("gap_atr", "跳空/ATR(signed)"), ("entry_min", "入场时刻(分钟)"),
                     ("brk_delay", "突破延迟(bar)"), ("or_cl", "OR收盘位置"),
                     ("open_dist_atr", "开盘起涨(ATR,顺势)"), ("rv20", "20日波动率"),
                     ("dist_prev_atr", "到昨关距离/ATR(顺势)")]:
        print(f"  -- {lab} --")
        bucket(tr, col)

    # ---------------- 3. 候选二元因子 ----------------
    print("\n=== 3. 候选二元因子 (全样本 / IS / OOS) ===")
    feats = {
        "破昨关": tr.broke_prev_ok == 1,
        "趋势对齐(sma20)": tr.trend_align == 1,
        "入场早(<=10:30)": tr.entry_min <= 10 * 60 + 30,
        "突破快(<=2bar)": tr.brk_delay <= 2,
        "窄OR(orw<=0.4)": tr.orw <= 0.4,
        "OR宽/ATR<=0.5": tr.orw_atr <= 0.5,
        "ATR<=1.0(低波)": tr.atr <= 1.0,
        "顺势走强(open_dist>=0.5ATR)": tr.open_dist_atr >= 0.5,
        "远离昨关(<=0.5ATR)": tr.dist_prev_atr <= 0.5,
    }
    for name, m in feats.items():
        ins, oos = m & tr["is_pre"], m & ~tr["is_pre"]
        def line(x, tag):
            p = x.pnl
            if len(p) == 0:
                return f"{tag}:n=0"
            gw, gl = p[p > 0].sum(), p[p < 0].sum()
            return (f"{tag}: n={len(p):4d} 胜={(p>0).mean()*100:4.1f}% "
                    f"PF={gw/abs(gl) if gl else 9:4.2f} 总={p.sum():7.0f}")
        keep = line(tr[m], "ALL")
        ki = line(tr[ins], "IS ")
        ko = line(tr[oos], "OOS")
        print(f"  {name:26s} {keep} | {ki} | {ko}")

    # ---------------- 4. 坏单(从没工作) vs 赢单 的特征分离 ----------------
    print("\n=== 4. 坏单(MFE<0.25R) vs 赢单(pnl>0) 特征均值对比 ===")
    bad = tr[(tr.mfe_R < 0.25)]
    good = tr[tr.pnl > 0]
    cols = ["gap_atr", "orw_atr", "orw", "atr", "entry_min", "brk_delay", "or_cl",
            "open_dist_atr", "dist_prev_atr", "rv20", "trend_align", "broke_prev_ok"]
    print(f"  {'特征':16s} {'坏单均':>10s} {'赢单均':>10s} {'分离度':>8s}")
    for c in cols:
        mb, mg = bad[c].mean(), good[c].mean()
        sd = tr[c].std() or 1
        print(f"  {c:16s} {mb:10.3f} {mg:10.3f} {(mg-mb)/sd:8.2f}")

    # ---------------- 5. 过滤组合搜索 ----------------
    print("\n=== 5. 过滤组合搜索 (候选池 -> 组合, 按 min(IS, OOS) PF 排序) ===")
    pool = {
        "早<=10:00":      tr.entry_min <= 10 * 60,
        "早<=10:30":      tr.entry_min <= 10 * 60 + 30,
        "早<=11:00":      tr.entry_min <= 11 * 60,
        "破昨关":          tr.broke_prev_ok == 1,
        "有跳空(|gap|>=0.1ATR)": tr.gap_atr.abs() >= 0.1,
        "OR收盘不在极低(or_cl>0.2)": tr.or_cl > 0.2,
        "OR宽/ATR>=0.15":  tr.orw_atr >= 0.15,
        "避开中波ATR(5.1~8.1)": ~tr.atr.between(5.1, 8.1),
        "顺势走强(>=0.15ATR)": tr.open_dist_atr >= 0.15,
    }

    def m_of(x):
        p = x.pnl
        if len(p) < 100:
            return None
        gw, gl = p[p > 0].sum(), p[p < 0].sum()
        pf = gw / abs(gl) if gl else 9
        exp = p.mean(); se = p.std() / np.sqrt(len(p)) if len(p) > 1 else np.nan
        return dict(n=len(p), win=(p > 0).mean() * 100, pf=pf, exp=exp,
                    t=exp / se if se and se > 0 else 0, tot=p.sum())

    results = []
    names = list(pool)
    from itertools import combinations
    for r in (1, 2, 3):
        for keys in combinations(names, r):
            m_ = pool[keys[0]]
            for key in keys[1:]:
                m_ = m_ & pool[key]
            all_ = m_of(tr[m_])
            if all_ is None:
                continue
            is_ = m_of(tr[m_ & tr["is_pre"]])
            oo_ = m_of(tr[m_ & ~tr["is_pre"]])
            if is_ is None or oo_ is None:
                continue
            results.append((" & ".join(keys), all_, is_, oo_))
    results.sort(key=lambda x: -min(x[2]["pf"], x[3]["pf"]))
    print(f"  {'组合':46s} {'n':>4s} {'胜':>5s} {'PF':>5s} | {'IS_PF':>5s} {'OOS_PF':>6s} {'总':>7s}")
    for name, all_, is_, oo_ in results[:15]:
        print(f"  {name:46s} {all_['n']:4d} {all_['win']:5.1f} {all_['pf']:5.2f} | "
              f"{is_['pf']:5.2f} {oo_['pf']:6.2f} {all_['tot']:7.0f}")

    # ---------------- 6. 最优组合逐年 ----------------
    print("\n=== 6. Top 组合逐年 ===")
    for name, all_, is_, oo_ in results[:4]:
        m_ = pd.Series(True, index=tr.index)
        for key in name.split(" & "):
            m_ = m_ & pool[key]
        parts = []
        for y, x in tr[m_].groupby("yr"):
            p = x.pnl; gw, gl = p[p > 0].sum(), p[p < 0].sum()
            parts.append(f"{y}:{gw/abs(gl) if gl else 9:.2f}({p.sum():.0f})")
        print(f"  {name}")
        print("    " + "  ".join(parts))

    # ---------------- 7. 与原 v2 出场叠加 (trail0.75) 看是否可加 ----------------
    print("\n=== 7. 最强过滤 × 出场配置 对照 (需另跑 exits; 此处给入场过滤后的胜率/盈亏比) ===")
    for name in ["早<=10:30", "破昨关 & 早<=10:30"]:
        m_ = pd.Series(True, index=tr.index)
        for key in name.split(" & "):
            m_ = m_ & pool[key]
        x = tr[m_]
        p = x.pnl
        print(f"  {name:26s} 胜={ (p>0).mean()*100:.1f}% 盈亏比="
              f"{p[p>0].mean()/abs(p[p<0].mean()):.2f} 均MFE={x.mfe_R.mean():.2f}R")

    # ---------------- 8. 因子三段稳定性 (P1/P2/P3) ----------------
    print("\n=== 8. 因子三段稳定性 (每段内 满足条件 的 PF) ===")
    d = pd.to_datetime(tr.date)
    pers = {"P1 20-07~22-12": d < "2023-01-01",
            "P2 23~24": (d >= "2023-01-01") & (d < "2025-01-01"),
            "P3 25~26": d >= "2025-01-01"}

    def pf(x):
        p = x.pnl
        if len(p) < 20:
            return np.nan
        gw, gl = p[p > 0].sum(), p[p < 0].sum()
        return gw / abs(gl) if gl else 9

    print(f"  {'因子':30s} {'P1':>6s} {'P2':>6s} {'P3':>6s}  {'符号一致':>6s}")
    for name, m in pool.items():
        vals = [pf(tr[m & per]) for per in pers.values()]
        base_pf = [pf(tr[per]) for per in pers.values()]
        ok = all(v > b for v, b in zip(vals, base_pf) if np.isfinite(v))
        vs = " ".join(f"{v:6.2f}" if np.isfinite(v) else "   n/a" for v in vals)
        print(f"  {name:30s} {vs}     {'是' if ok else '否'}")
    base_all = " ".join(f"{pf(tr[per]):6.2f}" for per in pers.values())
    print(f"  {'(基准全样本)':30s} {base_all}")

    tr.to_csv("results/orb_loss_attrib_trades.csv", index=False)
    print("\n已保存 results/orb_loss_attrib_trades.csv")


if __name__ == "__main__":
    main()
