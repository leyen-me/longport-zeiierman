"""与 TradingView 策略测试器导出的交易明细 CSV 逐笔比对 (序列对齐版)。"""

import argparse
import difflib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from ztp.paths import reference_dir

DEFAULT_TV = reference_dir() / "tv_trades_SPY_2025-09_2026-09.csv"


def load_tv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    rows = []
    for no, g in df.groupby("交易编号"):
        e = g[g["类型"].str.contains("进场")].iloc[0]
        x = g[g["类型"].str.contains("出场")].iloc[0]
        rows.append(
            {
                "trade_no": no,
                "direction": e["信号"],
                "entry_time": e["日期和时间"],
                "entry_price": round(float(e["价格 USD"]), 2),
                "qty": int(e["大小（数量）"]),
                "exit_time": x["日期和时间"],
                "exit_price": round(float(x["价格 USD"]), 2),
                "pnl": round(float(x["净损益 USD"]), 2),
                "bars": int(x["持续时间（K线）"]),
            }
        )
    return pd.DataFrame(rows).sort_values("trade_no").reset_index(drop=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--tv", default=str(DEFAULT_TV))
    p.add_argument("--ours", default="results/trades.csv")
    p.add_argument("--show", type=int, default=15)
    a = p.parse_args()

    root = Path(__file__).resolve().parent.parent
    tv = load_tv(root / a.tv)
    ours = pd.read_csv(root / a.ours)
    ours = ours.rename(columns={"bars_held": "bars"})
    ours["entry_time"] = ours["entry_time"].str.slice(0, 16)
    ours["exit_time"] = ours["exit_time"].str.slice(0, 16)
    for c in ["entry_price", "exit_price", "pnl"]:
        ours[c] = ours[c].astype(float).round(2)
    ours = ours[pd.to_datetime(ours["entry_time"]) >= tv["entry_time"].min()].reset_index(drop=True)

    print(f"TV trades: {len(tv)}   ours: {len(ours)} (>= {tv['entry_time'].min()})")

    keys_tv = [f"{r.direction}|{r.entry_time}|{r.entry_price:.2f}" for r in tv.itertuples()]
    keys_our = [f"{r.direction}|{r.entry_time}|{r.entry_price:.2f}" for r in ours.itertuples()]
    sm = difflib.SequenceMatcher(None, keys_tv, keys_our, autojunk=False)
    matched_pairs = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            matched_pairs.extend(zip(range(i1, i2), range(j1, j2)))

    fields = ["direction", "entry_time", "entry_price", "qty", "exit_time", "exit_price", "pnl", "bars"]
    n_pair = len(matched_pairs)
    field_ok = {f: 0 for f in fields}
    full_ok = 0
    mismatch_rows = []
    for i, j in matched_pairs:
        rt, ro = tv.iloc[i], ours.iloc[j]
        row_ok = True
        for f in fields:
            vt, vo = rt[f], ro[f]
            if isinstance(vt, (int, float)) and isinstance(vo, (int, float)):
                same = abs(float(vt) - float(vo)) < 0.005
            else:
                same = str(vt) == str(vo)
            field_ok[f] += same
            row_ok &= same
        full_ok += row_ok
        if not row_ok:
            mismatch_rows.append((rt, ro))

    print(f"\n入场(方向|时间|价格)对齐一致: {n_pair}/{len(tv)} = {n_pair/len(tv)*100:.0f}%")
    print("\n== 对齐交易的字段匹配 ==")
    for f in fields:
        print(f"  {f:12s} {field_ok[f]}/{n_pair}")
    print(f"\n整笔全字段一致: {full_ok}/{n_pair}")

    print("\n== 对齐但仍不一致的明细 ==")
    for rt, ro in mismatch_rows[: a.show]:
        diffs = [f for f in fields if str(rt[f]) != str(ro[f])]
        print(f"  TV#{rt['trade_no']} {rt['entry_time']} vs OURS {ro['entry_time']}  差异: {diffs}")
        for f in diffs:
            print(f"      {f}: TV={rt[f]}  OURS={ro[f]}")

    print("\n== 未对齐(结构性分歧) ==")
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "equal":
            print(f"  {tag}: TV={keys_tv[i1:i2]}  OURS={keys_our[j1:j2]}")


if __name__ == "__main__":
    main()
