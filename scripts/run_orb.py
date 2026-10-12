"""ORB30 回测: 长桥原生 5 分钟 K 线 / TV 导出 5min CSV -> 复刻引擎 -> results/orb_trades.csv。"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from ztp.orb import ORBConfig, load_bars_csv, run
from ztp.longport_data import load_cache


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="SPY.US")
    p.add_argument("--adjust", default="none")
    p.add_argument("--start", default=None)
    p.add_argument("--end", default=None)
    p.add_argument("--bars", default=None, help="直接用 TV 导出的 5min K线 CSV(路径/glob), 而非长桥缓存")
    p.add_argument("--out", default="results/orb_trades.csv")
    a = p.parse_args()

    if a.bars:
        df5 = load_bars_csv(a.bars)
    else:
        df5 = load_cache(a.symbol, a.adjust, period="min5")
        if a.start:
            df5 = df5[df5.index >= pd.Timestamp(a.start, tz=df5.index.tz)]
        if a.end:
            df5 = df5[df5.index <= pd.Timestamp(a.end, tz=df5.index.tz) + pd.Timedelta(days=1)]

    cfg = ORBConfig(symbol=a.symbol)
    trades = run(df5, cfg)

    root = Path(__file__).resolve().parent.parent
    out = root / a.out
    out.parent.mkdir(exist_ok=True)
    t = trades.copy()
    for col in ("entry_time", "exit_time"):
        t[col] = t[col].dt.strftime("%Y-%m-%d %H:%M")
    t.to_csv(out, index=False)

    wins = (trades["pnl"] > 0).sum()
    print(f"bars({len(df5)} @5min)  trades={len(trades)}  wins={wins}  losses={len(trades)-wins}")
    if len(trades):
        print(f"pnl_total={trades['pnl'].sum():.2f}  final_equity={trades['equity_after'].iloc[-1]:.2f}")
        print(f"exit reasons: {trades['exit_reason'].value_counts().to_dict()}")
    print(f"saved: {out}")


if __name__ == "__main__":
    main()
