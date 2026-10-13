"""ORB 因子探索 (重点: 成交量), 目标找"质变"因子。

思路: 用 v2 入场 + 基线出场, 逐笔记录入场时刻所有可得特征(含成交量/VWAP/OR结构),
      用 MFE 归因区分好坏单; 对每个因子看三段稳定性; 再用"P1+P2 选因子, P3 盲测"
      的嵌套验证防过拟合。

数据: data_cache/SPY_US_non_min2.parquet -> 5min (美东)。
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
OR_START = 9 * 60 + 30


# ---------------------------------------------------------------- helpers
def safe(a, b):
    return a / b if b else np.nan


def build(D, dl):
    # 全局滚动量能均值 (用前一根, 避免前视)
    vol = np.asarray(D["v"], float)
    vol_ma20 = pd.Series(vol).rolling(20).mean().shift(1).to_numpy()

    # 每日 OR 量能 (供跨日相对量)
    or_vol_by_day, or_range_by_day = {}, {}
    for dt, a, b in D["_days"]:
        sess = [i for i in range(a, b) if OR_START <= D["mod"][i] < OR_START + OR_MIN]
        if sess:
            or_vol_by_day[dt] = vol[sess].sum()
            or_range_by_day[dt] = D["h"][sess].max() - D["l"][sess].min()
    ovs = pd.Series(or_vol_by_day).sort_index()
    or_vol_med = ovs.rolling(20).median().shift(1)  # 前 20 交易日均值

    rows = []
    for dt, a, b in D["_days"]:
        if D["dow"][a] not in TRADE_DOW:
            continue
        e = day_entry(D, a, b, OR_MIN, BRK, "close")
        if e is None:
            continue
        if e["first_dir"] != e["pos"]:
            continue
        if dt not in dl.index:
            continue
        d = dl.loc[dt]
        i = e["entry_i"]
        orhi, orlo = e["orhi"], e["orlo"]
        sess = [k for k in range(a, b) if OR_START <= D["mod"][k] < OR_START + OR_MIN]
        pc = d["pc"]
        atr = d["atr"]
        gap = D["o"][a] - pc if np.isfinite(pc) else np.nan
        # OR 结构
        or_open = D["o"][sess[0]]; or_close = D["c"][sess[-1]]
        or_vol = vol[sess].sum()
        or_vol_bar_avg = vol[sess].mean()
        touches = sum(1 for k in sess if D["h"][k] >= orhi - 0.02 or D["l"][k] <= orlo + 0.02)
        # 跳空是否在 OR 内被回补
        gap_fill = int(any((D["l"][k] <= pc <= D["h"][k]) for k in sess)) if np.isfinite(pc) else 0
        # 突破 bar
        vbrk = vol[i]
        brk_body = safe(abs(D["c"][i] - D["o"][i]), D["h"][i] - D["l"][i])
        brk_clv = safe((D["c"][i] - D["l"][i]), D["h"][i] - D["l"][i])
        # session VWAP 到 entry (含)
        seg = slice(a, i + 1)
        tp = (D["h"][seg] + D["l"][seg] + D["c"][seg]) / 3.0
        vsum = vol[seg].sum()
        vwap = (tp * vol[seg]).sum() / vsum if vsum else np.nan
        side = e["pos"]
        entry = e["entry"]

        row = dict(
            date=dt, side=side, entry=entry, entry_i=i, entry_min=D["mod"][i],
            brk_delay=i - (sess[-1] + 1),
            # regime
            atr=atr, atr_pct=safe(atr, D["o"][a]), rv20=d["vol20"],
            # OR
            orw=e["orw"], orw_atr=safe(e["orw"], atr), or_cl=e["or_cl"],
            or_body=safe(or_close - or_open, e["orw"]),
            or_touches=touches,
            or_rvol=safe(or_vol_bar_avg, vol_ma20[i]),          # OR 均量 / 常态量
            or_vol_rel=safe(or_vol_by_day.get(dt, np.nan), or_vol_med.get(dt, np.nan)),
            orw_prev=safe(e["orw"], or_range_by_day.get(dt, np.nan)),
            # 突破 bar
            brk_rvol=safe(vbrk, vol_ma20[i]),                    # 突破量 / 常态量
            brk_body=brk_body, brk_clv=brk_clv,
            brk_ext=safe((D["c"][i] - orhi) if side > 0 else (orlo - D["c"][i]), atr),
            # gap
            gap_atr=safe(gap, atr), gap_fill=gap_fill,
            # 趋势/位置
            dist_prev_atr=safe(((entry - d["ph"]) if side > 0 else (d["pl"] - entry)), atr),
            vwap_dist_atr=safe((entry - vwap) * side, atr),
            sma50_s=safe(entry - d["sma50"], atr) if np.isfinite(d["sma50"]) else np.nan,
            open_dist_atr=safe((entry - D["o"][a]) * side, atr),
            trend_align=int(((side > 0 and entry > d["sma20"]) or
                             (side < 0 and entry < d["sma20"])) if np.isfinite(d["sma20"]) else 0),
            # 昨日
            prev_range_atr=safe(d["ph"] - d["pl"], atr) if np.isfinite(d["ph"]) else np.nan,
            dow=D["dow"][a],
        )
        row.update(sim_record(D, a, b, e))
        rows.append(row)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- eval
def pf(x):
    p = x.pnl
    if len(p) == 0:
        return np.nan
    gw, gl = p[p > 0].sum(), p[p < 0].sum()
    return gw / abs(gl) if gl else 9


def line(x, tag=""):
    p = x.pnl
    if len(p) == 0:
        return f"{tag} n=0"
    return (f"{tag} n={len(p):3d} 胜={(p>0).mean()*100:4.1f}% PF={pf(x):4.2f} "
            f"总={p.sum():6.0f}")


def periods(tr):
    d = pd.to_datetime(tr.date)
    return {"P1": d < "2023-01-01",
            "P2": (d >= "2023-01-01") & (d < "2025-01-01"),
            "P3": d >= "2025-01-01"}


def monotonic_buckets(tr, col, q=5):
    try:
        b = pd.qcut(tr[col], q, duplicates="drop")
    except (ValueError, TypeError):
        return
    g = tr.groupby(b, observed=True).apply(lambda x: pd.Series({
        "n": len(x), "胜%": round((x.pnl > 0).mean() * 100, 1), "PF": round(pf(x), 2),
        "总": round(x.pnl.sum(), 0)}), include_groups=False)
    print(f"  -- {col} --")
    print(g.to_string())


# ---------------------------------------------------------------- tiny regression tree
def best_split(x, y, feats, min_leaf=40):
    best = None
    n = len(y); tot = y.mean()
    for f in feats:
        v = x[f].to_numpy(float)
        if not np.isfinite(v).any():
            continue
        qs = np.nanquantile(v, [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8])
        for t in np.unique(qs):
            m = v <= t
            if m.sum() < min_leaf or (~m).sum() < min_leaf:
                continue
            yl, yr = y[m], y[~m]
            sse = ((yl - yl.mean()) ** 2).sum() + ((yr - yr.mean()) ** 2).sum()
            if best is None or sse < best[0]:
                best = (sse, f, t)
    return best


def tree_report(tr, feats, depth=2, min_leaf=40):
    sel = tr["_sel"].to_numpy()
    # 节点用 (在tr上的布尔mask, 条件列表) 表示, 便于把同一条件应用到盲测集
    nodes = [(sel, [])]
    for _ in range(depth):
        new = []
        for mask, conds in nodes:
            idx = np.where(mask)[0]
            if len(idx) < 2 * min_leaf:
                new.append((mask, conds)); continue
            sub = tr.iloc[idx]
            bs = best_split(sub, sub.pnl.to_numpy(float), feats, min_leaf)
            if bs is None:
                new.append((mask, conds)); continue
            _, f, t = bs
            v = tr[f].to_numpy(float)
            new.append((mask & (v <= t), conds + [(f, "<=", t)]))
            new.append((mask & (v > t), conds + [(f, ">", t)]))
        nodes = new
    print(f"  [回归树 depth={depth}, 训练=P1+P2, 叶在 P3 盲测]")
    for j, (mask, conds) in enumerate(nodes):
        rule = " & ".join(f"{f}{op}{t:.3g}" for f, op, t in conds) or "(root)"
        n_sel = int(mask.sum())
        p_sel = tr.pnl[mask]
        pf_sel = pf(tr[mask])
        blind = np.ones(len(tr), bool)
        for f, op, t in conds:
            v = tr[f].to_numpy(float)
            blind &= (v <= t) if op == "<=" else (v > t)
        blind &= ~sel
        oo = tr[blind]
        print(f"    叶{j}: [{rule[:60]:60s}] 训练 n={n_sel:3d} PF={pf_sel:4.2f} | "
              f"P3 n={len(oo):3d} PF={pf(oo):4.2f} 总={oo.pnl.sum():6.0f}")


# ---------------------------------------------------------------- main
def main():
    df = load()
    D = prep(df)
    dl = daily(D)
    D["_days"] = slices(D)

    tr = build(D, dl)
    tr["yr"] = pd.to_datetime(tr.date).dt.year
    per = periods(tr)
    tr["_sel"] = per["P1"] | per["P2"]           # 选因子集
    d = pd.to_datetime(tr.date)
    print(f"交易数 {len(tr)}  {tr.date.min()} ~ {tr.date.max()}")
    print(f"基线: {line(tr)}  | P1 {line(tr[per['P1']])} P2 {line(tr[per['P2']])} P3 {line(tr[per['P3']])}")

    # ---- 1. 坏单 vs 赢单 ----
    print("\n=== 1. 坏单(MFE<0.25R) vs 赢单 特征分离 (含成交量) ===")
    bad = tr[tr.mfe_R < 0.25]; good = tr[tr.pnl > 0]
    cols = ["atr_pct", "rv20", "orw_atr", "or_cl", "or_rvol", "or_vol_rel", "orw_prev",
            "brk_rvol", "brk_body", "brk_clv", "brk_ext", "gap_atr", "gap_fill",
            "vwap_dist_atr", "dist_prev_atr", "open_dist_atr", "entry_min", "brk_delay",
            "or_touches", "prev_range_atr"]
    print(f"  {'特征':16s} {'坏单':>9s} {'赢单':>9s} {'分离':>6s}")
    for c in cols:
        if c not in tr:
            continue
        mb, mg = bad[c].mean(), good[c].mean(); sd = tr[c].std() or 1
        print(f"  {c:16s} {mb:9.3f} {mg:9.3f} {(mg-mb)/sd:6.2f}")

    # ---- 2. 分档 ----
    print("\n=== 2. 关键因子分档 (全样本) ===")
    for c in ["brk_rvol", "or_rvol", "or_vol_rel", "or_touches", "brk_clv", "brk_body",
              "vwap_dist_atr", "atr_pct", "orw_prev", "gap_fill", "brk_ext", "dist_prev_atr"]:
        if c in tr and tr[c].nunique() >= 4:
            monotonic_buckets(tr, c)

    # ---- 3. 三段稳定性 ----
    print("\n=== 3. 因子三段稳定性 (满足条件时各段 PF vs 该段基准) ===")
    base = {k: pf(tr[m]) for k, m in per.items()}
    print(f"  基准: " + " ".join(f"{k}={v:.2f}" for k, v in base.items()))
    cands = {
        "brk_rvol>=0.8": tr.brk_rvol >= 0.8, "brk_rvol<=2.5": tr.brk_rvol <= 2.5,
        "brk_rvol 0.8-2.5": tr.brk_rvol.between(0.8, 2.5),
        "or_rvol>=0.8": tr.or_rvol >= 0.8, "or_vol_rel>=1": tr.or_vol_rel >= 1.0,
        "or_vol_rel>=0.9": tr.or_vol_rel >= 0.9,
        "brk_clv>=0.5": tr.brk_clv >= 0.5, "brk_clv>=0.7": tr.brk_clv >= 0.7,
        "brk_body>=0.4": tr.brk_body >= 0.4,
        "vwap_dist>=0.1ATR": tr.vwap_dist_atr >= 0.1,
        "vwap_dist>=0.2ATR": tr.vwap_dist_atr >= 0.2,
        "gap_fill=0": tr.gap_fill == 0,
        "orw_prev>=0.5": tr.orw_prev >= 0.5,
        "early<=10:30": tr.entry_min <= 630,
        "gap|.|>=0.1ATR": tr.gap_atr.abs() >= 0.1,
        "强势>=0.15ATR": tr.open_dist_atr >= 0.15,
        "brk_ext>=0.07ATR": tr.brk_ext >= 0.07,
        "低波atr%<=0.011": tr.atr_pct <= 0.011,
    }
    print(f"  {'因子':22s} {'P1':>6s} {'P2':>6s} {'P3':>6s} {'一致':>4s}")
    stable = []
    for name, m in cands.items():
        vals = [pf(tr[m & per[k]]) for k in ("P1", "P2", "P3")]
        ok = all(np.isfinite(v) and v > base[k] for v, k in zip(vals, ("P1", "P2", "P3")))
        vs = " ".join(f"{v:6.2f}" if np.isfinite(v) else "   n/a" for v in vals)
        print(f"  {name:22s} {vs} {'✅' if ok else '  '}")
        if ok:
            stable.append(name)

    # ---- 4. 嵌套验证: P1+P2 选, P3 盲测 ----
    print("\n=== 4. 嵌套验证 (在 P1+P2 上 beam search, P3 盲测) ===")
    sel = tr["_sel"]
    from itertools import combinations
    keys = list(cands)
    res = []
    for r in (1, 2, 3):
        for ks in combinations(keys, r):
            m = cands[ks[0]]
            for k in ks[1:]:
                m = m & cands[k]
            if (m & sel).sum() < 150:
                continue
            s = pf(tr[m & sel])
            os_ = pf(tr[m & ~sel])
            res.append((" & ".join(ks), s, os_, (m & sel).sum(), (m & ~sel).sum()))
    res.sort(key=lambda x: -(x[1] if np.isfinite(x[1]) else 0))
    print(f"  {'组合(选于P1+P2)':44s} {'P1P2_PF':>8s} {'P3_PF':>7s} {'P3_n':>5s}")
    for name, s, os_, ns, no in res[:15]:
        print(f"  {name:44s} {s:8.2f} {os_ if np.isfinite(os_) else 0:7.2f} {no:5d}")

    print("\n  -- Top5 组合逐年 (全样本) --")
    for name, s, os_, ns, no in res[:5]:
        m = cands[name.split(" & ")[0]]
        for k in name.split(" & ")[1:]:
            m = m & cands[k]
        x = tr[m]; p = x.pnl; eq = p.cumsum()
        yrs = " ".join(f"{y}:{pf(x[x.yr == y]):.2f}" for y in sorted(x.yr.unique()) if len(x[x.yr == y]) >= 6)
        print(f"  {name}")
        print(f"     n={len(x)} 胜={(p>0).mean()*100:.1f}% PF={pf(x):.2f} 总={p.sum():.0f} "
              f"MDD={(eq.cummax()-eq).max():.0f} | {yrs}")

    # ---- 5. 回归树 ----
    print("\n=== 5. 回归树 (训练 P1+P2, P3 盲测) ===")
    tfeats = ["brk_rvol", "brk_clv", "vwap_dist_atr", "entry_min", "or_vol_rel",
              "orw_atr", "gap_atr", "brk_ext", "or_touches", "open_dist_atr"]
    tree_report(tr, tfeats, depth=2)

    tr.to_csv("results/orb_factors_trades.csv", index=False)
    print("\n已保存 results/orb_factors_trades.csv")


if __name__ == "__main__":
    main()
