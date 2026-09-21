import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from ztp.config import ZTPConfig
from ztp.engine import run_full
from ztp.longport_data import load_cache


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="SPY.US")
    p.add_argument("--adjust", default="none")
    p.add_argument("--start", default=None)
    p.add_argument("--end", default=None)
    p.add_argument("--out", default="results/trades.csv")
    a = p.parse_args()

    df = load_cache(a.symbol, a.adjust)
    if a.start:
        df = df[df.index >= pd.Timestamp(a.start, tz=df.index.tz)]
    if a.end:
        df = df[df.index <= pd.Timestamp(a.end, tz=df.index.tz) + pd.Timedelta(days=1)]

    cfg = ZTPConfig(symbol=a.symbol)
    trades, sig = run_full(df, cfg)

    out = Path(__file__).resolve().parent.parent / a.out
    out.parent.mkdir(exist_ok=True)
    t = trades.copy()
    t["entry_time"] = t["entry_time"].dt.strftime("%Y-%m-%d %H:%M")
    t["exit_time"] = t["exit_time"].dt.strftime("%Y-%m-%d %H:%M")
    t.to_csv(out, index=False)

    wins = (trades["pnl"] > 0).sum()
    print(f"bars={len(df)}  trades={len(trades)}  wins={wins}  losses={len(trades)-wins}")
    print(f"pnl_total={trades['pnl'].sum():.2f}  final_equity={trades['equity_after'].iloc[-1]:.2f}")
    print(f"saved: {out}")


if __name__ == "__main__":
    main()
