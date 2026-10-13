"""从 $1000 起步的 0DTE 一年推演 (基于新默认配置: 顺势+未补缺)。

- 期权收益用 research/orb_0dte.py 的模型: SPY 0DTE ATM, BS 定价,
  IV = 20日已实现波动*1.2, 买卖各 $0.03 点差, 入场买/出场卖。
- 蒙特卡洛: 从实际逐笔期权收益分布 bootstrap, 每年 N 笔, 逐笔复利。
- 注意: 半定量, 未含佣金; 出场在标的止损/止盈时, 期权实际可能更差点/不可成交,
  故结果为乐观上界。
"""

import sys, math
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
import pandas as pd

from orb_lab import load, prep, slices, daily
from orb_factors import build
from orb_0dte import bs, expiry_dt


def option_rets(D, tr, iv_mult=1.2, half_spread=0.03):
    rets = []
    for r in tr.itertuples():
        ts_i = D["ts"][r.entry_i]; ts_x = D["ts"][r.exit_i]
        S0, S1 = r.entry, r.exit_px
        K = float(round(S0)); is_call = r.side > 0
        iv = max(0.08, (r.rv20 if np.isfinite(r.rv20) else 0.012) * math.sqrt(252) * iv_mult)
        Te = (expiry_dt(ts_i) - ts_i).total_seconds() / (365 * 24 * 3600)
        Tx = (expiry_dt(ts_x) - ts_x).total_seconds() / (365 * 24 * 3600)
        mid0 = bs(S0, K, max(Te, 1e-6), iv, is_call)
        mid1 = bs(S1, K, max(Tx, 1e-6), iv, is_call)
        ask0 = mid0 + half_spread; bid1 = mid1 - half_spread
        if ask0 <= 0.05:
            continue
        rets.append(max(bid1 / ask0 - 1.0, -1.0))  # 最多亏 100% 权利金
    return np.array(rets)


def project(rets, n_trades, f, cap0=1000.0, n_paths=50000, seed=7):
    rng = np.random.default_rng(seed)
    out = np.empty(n_paths)
    for k in range(n_paths):
        draw = rng.choice(rets, size=n_trades, replace=True)
        c = cap0
        for x in draw:
            c *= (1.0 + f * x)
            if c < 1.0:            # 近乎爆仓即停
                c = max(c, 0.0); break
        out[k] = c
    return out


def main():
    D = prep(load()); dl = daily(D); D["_days"] = slices(D)
    tr = build(D, dl).reset_index(drop=True)
    S = tr.open_dist_atr >= 0.15; F = tr.gap_fill == 0
    nd = tr[S & F].reset_index(drop=True)
    print(f"新默认 交易数={len(nd)}  (2020-07 ~ 2026-09, ~{len(nd)/6.2:.0f} 笔/年)")

    rets = option_rets(D, nd)
    n = len(rets)
    wins = rets[rets > 0]; loss = rets[rets <= 0]
    print(f"\n0DTE 逐笔收益 (n={n}):")
    print(f"  胜率={len(wins)/n*100:.1f}%  均收益={rets.mean()*100:+.1f}%  "
          f"均盈={wins.mean()*100:+.1f}%  均亏={loss.mean()*100:+.1f}%")
    r = np.clip(rets, -0.999, None)
    print(f"  平均对数增长(满仓)={np.log1p(r).mean()*100:+.1f}%/笔  近全损(<−95%)占比={np.mean(rets<=-0.95)*100:.1f}%")
    print(f"  中位收益={np.median(rets)*100:+.1f}%  最好={rets.max()*100:+.0f}%  最差={rets.min()*100:+.0f}%")
    # Kelly 最优下注比例: 最大化 E[log(1+f*r)]
    fs = np.linspace(0.02, 1.0, 99)
    g = [np.log1p(fs[i] * r).mean() for i in range(len(fs))]
    kelly = fs[int(np.argmax(g))]
    print(f"  Kelly 最优下注比例 ≈ {kelly*100:.0f}%  (每笔平均对数增长 {max(g)*100:+.2f}%)")

    NT = 71  # 每年交易笔数 (439/6.2)
    print(f"\n=== $1000 起步, {NT} 笔/年, 逐笔复利 (bootstrap) ===")
    print(f"  {'下注比例':>8s} {'中位':>9s} {'均值':>10s} {'5%分位':>9s} {'95%分位':>10s} {'亏损概率':>8s}")
    for f in (0.1, 0.25, 0.5, 1.0):
        res = project(rets, NT, f)
        print(f"  {f*100:6.0f}%  "
              f"${np.median(res):8.0f} ${res.mean():9.0f} ${np.percentile(res,5):8.0f} "
              f"${np.percentile(res,95):9.0f} {np.mean(res<1000)*100:6.1f}%")

    # 参考: 只做标的(不用期权)的复利
    print("\n=== 对照: 固定80股标的(非复利, 只加总) ===")
    print(f"  期望/笔 ${nd.pnl.mean():.1f} × {NT} 笔 ≈ ${nd.pnl.mean()*NT:.0f}/年 (相对1000本金的粗暴线性)")

    # 最近一年 (近似最近12个月)
    end = pd.to_datetime(nd.date).max()
    last = nd[pd.to_datetime(nd.date) > end - pd.Timedelta(days=365)]
    rl = option_rets(D, last.reset_index(drop=True))
    if len(rl):
        print(f"\n最近一年(约{len(last)}笔标的): 期权均收益={rl.mean()*100:+.1f}% "
              f"胜率={np.mean(rl>0)*100:.0f}%  逐笔几何={((1+rl).prod()**(1/len(rl))-1)*100:+.1f}%")
        res = project(rl, NT, 0.5)
        print(f"  若按最近一年分布, f=50%: 中位 ${np.median(res):.0f}, 5%~95% ${np.percentile(res,5):.0f}~${np.percentile(res,95):.0f}")


if __name__ == "__main__":
    main()
