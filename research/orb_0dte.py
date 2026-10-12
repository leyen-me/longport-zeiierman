"""在 v2 标的信号上叠加 SPY 0DTE ATM 期权, 评估放大效应 (含 BS 定价+点差+theta)。

注意: 无历史 IV, 用 20日已实现波动*系数 作代理; 这是半定量研究, 非精确复现。
"""

import sys, math
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
import pandas as pd

import orb_lab as L

ET = "America/New_York"


def N(x):
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def bs(spot, K, T, iv, is_call, r=0.04, q=0.0):
    if T <= 1e-8 or iv <= 0:
        return max(spot - K, 0.0) if is_call else max(K - spot, 0.0)
    d1 = (math.log(spot / K) + (r - q + 0.5 * iv * iv) * T) / (iv * math.sqrt(T))
    d2 = d1 - iv * math.sqrt(T)
    if is_call:
        return spot * math.exp(-q * T) * N(d1) - K * math.exp(-r * T) * N(d2)
    return K * math.exp(-r * T) * N(-d2) - spot * math.exp(-q * T) * N(-d1)


def expiry_dt(ts):
    return ts.tz_convert(ET).replace(hour=16, minute=0, second=0, microsecond=0)


def v2_trades(days, td=(0, 1, 2), D=None, dl=None):
    fdefs = {'gap': lambda r: (r['gap'] > 0) if r['side'] == 'LONG' else (r['gap'] < 0),
             'fd': lambda r: r['first_dir'] == (1 if r['side'] == 'LONG' else -1)}
    df = L.build(dict(or_min=15, brk=3, trade_days=td),
                 dict(anchor='edge', trail_R=0.75, tp_pts=2.1, sl_pts=1.1),
                 days=days, D=D, dl=dl)
    for c in ('gap', 'fd'):
        df = df[df.apply(fdefs[c], axis=1)]
    return df.reset_index(drop=True)


def overlay(df, iv_mode="realized", iv_mult=1.2, iv_const=0.15, half_spread=0.03, budget=5000.0):
    rows = []
    for r in df.itertuples():
        ts_i = r.entry_ts; ts_x = r.exit_ts
        S0 = r.entry; S1 = r.exit_px_u
        K = float(round(S0))
        is_call = r.side == "LONG"
        if iv_mode == "const":
            iv = iv_const
        else:
            iv = max(0.08, (r.vol20 if np.isfinite(r.vol20) else 0.012) * math.sqrt(252) * iv_mult)
        Te = (expiry_dt(ts_i) - ts_i).total_seconds() / (365 * 24 * 3600)
        Tx = (expiry_dt(ts_x) - ts_x).total_seconds() / (365 * 24 * 3600)
        mid0 = bs(S0, K, max(Te, 1e-6), iv, is_call)
        mid1 = bs(S1, K, max(Tx, 1e-6), iv, is_call)
        ask0 = mid0 + half_spread
        bid1 = mid1 - half_spread
        if ask0 <= 0.05:
            continue
        qty = int(budget // (ask0 * 100))
        if qty <= 0:
            continue
        opt_pnl = (bid1 - ask0) * 100 * qty
        opt_ret = bid1 / ask0 - 1
        und_ret = (S1 / S0 - 1) * (1 if r.side == "LONG" else -1)
        rows.append(dict(date=r.date, side=r.side, iv=iv, prem=ask0, qty=qty,
                         opt_pnl=opt_pnl, opt_ret=opt_ret, und_ret=und_ret,
                         und_pnl=r.pnl, reason=r.reason, exit_prem=bid1))
    return pd.DataFrame(rows)


def summ(name, o):
    p = o.opt_pnl
    gw = p[p > 0].sum(); gl = p[p < 0].sum()
    ruin = (o.opt_ret <= -0.95).mean() * 100
    print(f"  {name:28s} n={len(o):3d} 期权胜率={ (p>0).mean()*100:4.1f}% "
          f"均收益={o.opt_ret.mean()*100:6.1f}% 均盈={o.opt_ret[o.opt_ret>0].mean()*100:5.1f}% "
          f"均亏={o.opt_ret[o.opt_ret<=0].mean()*100:6.1f}% PF={gw/abs(gl) if gl else 9:4.2f} "
          f"总=${p.sum():8.0f} 近全损占比={ruin:4.1f}% 中位IV={o.iv.median():.3f}")


def main():
    D, dl = L.load_all(); days = D['_days']
    print("== 0DTE 叠加 (v2 Mon-Wed, 预算$5000/笔, 买卖各$0.03点差) ==")
    base = v2_trades(days, (0, 1, 2), D, dl)
    f = L.metrics(base)
    print(f"  标的(v2 MW): 胜={f['win']}% 盈亏比={f['payoff']} PF={f['pf']} 期望=${f['exp']}/80股")
    for mode, mult, cst in [("realized", 1.0, 0), ("realized", 1.2, 0), ("realized", 1.5, 0), ("const", 0, 0.15), ("const", 0, 0.20)]:
        o = overlay(base, "const" if mode == "const" else "realized",
                    iv_mult=mult if mult else 1.2, iv_const=cst or 0.15)
        summ(f"{mode}×{mult or cst}", o)
    print("\n== 点差敏感性 (realized×1.2) ==")
    for hs in [0.0, 0.02, 0.05, 0.10]:
        o = overlay(base, "realized", iv_mult=1.2, half_spread=hs)
        summ(f"half_spread=${hs}", o)
    print("\n== 用 ATM vs 略OTM (delta≈0.4) ==")
    # 简版: 近似 OTM 用 strike=round(S0)+? 略过, 改测更多预算
    for b in [2000, 5000, 10000]:
        o = overlay(base, "realized", iv_mult=1.2, budget=b)
        summ(f"budget=${b}", o)
    print("\n== 年度 (realized×1.2, $5000) ==")
    o = overlay(base, "realized", iv_mult=1.2)
    o['yr'] = pd.to_datetime(o.date).dt.year
    for y, x in o.groupby('yr'):
        print(f"    {y}: n={len(x):2d} 期权胜率={(x.opt_pnl>0).mean()*100:4.0f}% "
              f"均收益={x.opt_ret.mean()*100:6.1f}% 总=${x.opt_pnl.sum():7.0f} PF="
              f"{x[x.opt_pnl>0].opt_pnl.sum()/abs(x[x.opt_pnl<0].opt_pnl.sum()):.2f}")
    o.to_csv("results/orb_0dte_overlay.csv", index=False)


if __name__ == "__main__":
    main()
