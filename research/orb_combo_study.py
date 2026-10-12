"""组合研究: 把 跳空/昨日关口/动量 有效因子组合, 做 IS/OOS + bootstrap 显著性。

基事件来自 results/orb_events_base.parquet (由 scripts/orb_research.py 生成)。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent


def load():
    ev = pd.read_parquet(ROOT / "results/orb_events_base.parquet")
    ev = ev[ev.exit_reason != "NO_TRADE"].copy()
    ev["date"] = pd.to_datetime(ev["date"])
    ev["yr"] = ev["date"].dt.year
    ev["side_num"] = np.where(ev.side == "LONG", 1, -1)
    ev["gap_bps"] = ev.gap / ev.prev_close * 1e4
    ev["broke_prev"] = np.where(ev.side_num > 0, ev.entry_px - ev.prev_high,
                                ev.prev_low - ev.entry_px)
    ev["broke_prev_atr"] = ev.broke_prev / ev.atr
    ev["or_out"] = (ev.orlo >= ev.prev_high) | (ev.orhi <= ev.prev_low)
    ev["gap_align"] = np.where(ev.side_num > 0, ev.gap > 0, ev.gap < 0)
    ev["broke_ok"] = ev.broke_prev > 0
    ev["near_prev"] = ev.broke_prev_atr > -0.5   # 不小于"最远离"档
    return ev


def summ(p, label, n_all=None):
    p = pd.Series(p)
    if len(p) == 0:
        print(f"  {label:40s} n=0")
        return
    gw = p[p > 0].sum(); gl = p[p < 0].sum()
    exp = p.mean()
    se = p.std() / np.sqrt(len(p))
    t = exp / se if se > 0 else np.nan
    pf = gw / abs(gl) if gl else float("inf")
    eq = p.cumsum(); mdd = (eq.cummax() - eq).max()
    print(f"  {label:40s} n={len(p):4d} 胜={ (p>0).mean()*100:4.1f}% "
          f"PF={pf:4.2f} 期望={exp:6.2f} t={t:5.2f} 总={p.sum():7.0f} MDD={mdd:6.0f}")


def bootstrap_p(p, n=5000, seed=0):
    """单边检验: H0 期望<=0 的 bootstrap p 值 (符号翻转)。"""
    rng = np.random.default_rng(seed)
    p = np.asarray(p)
    obs = p.mean()
    cnt = 0
    for _ in range(n):
        s = rng.choice([-1, 1], size=len(p)) * p
        if s.mean() >= obs:
            cnt += 1
    return cnt / n


def main():
    ev = load()
    print(f"总事件 {len(ev)}  区间 {ev.date.min().date()} ~ {ev.date.max().date()}")
    tr_mask = ev.date < "2024-01-01"
    print("\n=== 单因子 (全样本) ===")
    summ(ev.pnl, "baseline 全部")
    summ(ev[ev.gap_align].pnl, "F1 顺跳空")
    summ(ev[~ev.gap_align].pnl, "F1' 逆跳空")
    summ(ev[ev.broke_ok].pnl, "F2 已破昨日关口")
    summ(ev[~ev.near_prev].pnl, "F2' 最远离昨关(差)")
    summ(ev[ev.or_out].pnl, "F3 OR在昨日区间外(缺口日)")

    combos = {
        "C1 顺跳空 & 破昨关": ev.gap_align & ev.broke_ok,
        "C2 顺跳空 & OR外": ev.gap_align & ev.or_out,
        "C3 破昨关 & OR外": ev.broke_ok & ev.or_out,
        "C4 顺跳空&破昨关&OR外": ev.gap_align & ev.broke_ok & ev.or_out,
        "C5 顺跳空 & 非最远离": ev.gap_align & ev.near_prev,
    }
    print("\n=== 组合 (全样本) ===")
    for name, m in combos.items():
        summ(ev[m].pnl, name)

    print("\n=== IS/OOS 拆分 (IS=2020-07~2023, OOS=2024~2026) ===")
    for name, m in {"baseline": pd.Series(True, index=ev.index), **combos}.items():
        is_ = ev[m & tr_mask]; oo = ev[m & ~tr_mask]
        print(f"\n  [{name}]")
        summ(is_.pnl, "    IS")
        summ(oo.pnl, "    OOS")
        if len(oo):
            print(f"    OOS bootstrap p(期望>0)= {bootstrap_p(oo.pnl):.3f}")

    # 组合 + 反向止损层 (keep 模式), 用事件近似: 对止损笔加反向净贡献
    print("\n=== 组合 + 反向单(研究1) ===")
    rev = pd.read_parquet(ROOT / "results/orb_events_base.parquet")  # placeholder


if __name__ == "__main__":
    main()
