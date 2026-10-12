"""ORB30 v2 信号上的 0DTE 虚实值对比: 实值(ITM) / 平值(ATM) / 虚值(OTM)。

无历史 IV, 用 20 日已实现波动代理; BS 定价 + 买卖点差 + 日内 theta。半定量研究。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import math

import numpy as np
import pandas as pd

import ztp.orb as orb

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


def bs_delta(spot, K, T, iv, is_call, r=0.04, q=0.0):
    if T <= 1e-8 or iv <= 0:
        itm = (spot > K) if is_call else (spot < K)
        return (1.0 if itm else 0.0) * (1 if is_call else -1)
    d1 = (math.log(spot / K) + (r - q + 0.5 * iv * iv) * T) / (iv * math.sqrt(T))
    if is_call:
        return math.exp(-q * T) * N(d1)
    return -math.exp(-q * T) * N(-d1)


def expiry(ts):
    return ts.tz_convert(ET).replace(hour=16, minute=0, second=0, microsecond=0)


def load_5min():
    from ztp.paths import ROOT
    d = pd.read_parquet(ROOT / "data_cache/SPY_US_non_min2.parquet")
    df = (d.resample("5min", label="left", closed="left")
          .agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna())
    mod = df.index.hour * 60 + df.index.minute
    return df[(mod >= 570) & (mod < 960)]


def trades():
    return orb.run(load_5min(), orb.ORBConfig())


def pick_strike(spot, T, iv, is_call, mode, target):
    """mode: 'delta' (target=delta) | 'offset' (target=美元偏移, 正=更虚值)。"""
    base = math.floor(spot)
    grid = np.arange(base - 12, base + 13, 1.0)
    if mode == "offset":
        K = round(spot) + target if is_call else round(spot) - target
        return float(K)
    best, bg = None, 9
    for K in grid:
        d = abs(bs_delta(spot, K, T, iv, is_call))
        if abs(d - target) < bg:
            bg, best = abs(d - target), K
    return float(best)


def run_iv(mult=1.2):
    # 20 日已实现波动 (年化) 作为 IV 代理
    d = load_5min()
    dl = d.resample("1D").agg({"close": "last"}).dropna()
    rv = dl["close"].pct_change().rolling(20).std() * math.sqrt(252)
    t = trades()
    rows = []
    for r in t.itertuples():
        ts0, ts1 = r.entry_time, r.exit_time
        S0, S1 = float(r.entry_price), float(r.exit_price)
        is_call = r.direction == "LONG"
        day = ts0.normalize()
        iv = max(0.08, float(rv.get(day, 0.15)) * mult) if day in rv.index else 0.15
        Te = (expiry(ts0) - ts0).total_seconds() / (365 * 24 * 3600)
        Tx = (expiry(ts1) - ts1).total_seconds() / (365 * 24 * 3600)
        rows.append(dict(ts0=ts0, ts1=ts1, S0=S0, S1=S1, is_call=is_call, iv=iv, Te=Te, Tx=Tx,
                         und_ret=(S1 / S0 - 1) * (1 if is_call else -1)))
    return pd.DataFrame(rows)


def overlay(df, mode, target, half_spread=0.03):
    out = []
    for r in df.itertuples():
        K = pick_strike(r.S0, r.Te, r.iv, r.is_call, mode, target)
        p0 = bs(r.S0, K, max(r.Te, 1e-6), r.iv, r.is_call)
        p1 = bs(r.S1, K, max(r.Tx, 1e-6), r.iv, r.is_call)
        ask, bid = p0 + half_spread, p1 - half_spread
        if ask <= 0.05:
            continue
        out.append(dict(K=K, ask=ask, ret=bid / ask - 1, und_ret=r.und_ret,
                        moneyness=(r.S0 - K) * (1 if r.is_call else -1)))
    return pd.DataFrame(out)


def summ(tag, o):
    p = o.ret
    gw = p[p > 0].sum()
    gl = p[p < 0].sum()
    ruin = (p <= -0.6).mean() * 100
    print(f"  {tag:26s} n={len(o):3d} 胜率={(p>0).mean()*100:4.1f}% 均收益={p.mean()*100:6.1f}% "
          f"均盈={p[p>0].mean()*100:5.1f}% 均亏={p[p<=0].mean()*100:6.1f}% "
          f"盈亏比={p[p>0].mean()/abs(p[p<=0].mean()):4.2f} PF={gw/abs(gl):4.2f} "
          f"亏损>60%占比={ruin:4.1f}% 均权利金=${o.ask.mean():.2f} 均delta档≈{o.moneyness.mean():+.2f}")


def main():
    df = run_iv(1.2)
    print("== 0DTE 虚实值对比 (v2 生产配置, IV=已实现×1.2, 点差$0.03/边, r=4%) ==")
    print("[按 delta 选档]")
    for tag, d in [("实值 ITM  delta≈0.65", 0.65), ("实值偏平 delta≈0.60", 0.60),
                   ("平值 ATM  delta≈0.50", 0.50), ("虚值 OTM  delta≈0.40", 0.40),
                   ("深虚 OTM  delta≈0.30", 0.30), ("极虚 OTM  delta≈0.20", 0.20)]:
        summ(tag, overlay(df, "delta", d))
    print("[按美元偏移选档]")
    for tag, off in [("ITM -$2", -2.0), ("ITM -$1", -1.0), ("ATM  0", 0.0),
                     ("OTM +$1", 1.0), ("OTM +$2", 2.0)]:
        summ(tag, overlay(df, "offset", off))
    print("[点差敏感性 (平值 vs 实值)]")
    for hs in [0.0, 0.02, 0.05, 0.10]:
        print(f"  点差${hs}/边:")
        summ(f"    ATM .50", overlay(df, "delta", 0.50, hs))
        summ(f"    ITM .65", overlay(df, "delta", 0.65, hs))
        summ(f"    OTM .35", overlay(df, "delta", 0.35, hs))


if __name__ == "__main__":
    main()
