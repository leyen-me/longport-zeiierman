"""ORB30: 与 TradingView 导出的交易明细逐笔比对 (序列对齐版)。"""

import argparse
import difflib
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd


def _rhu(x: float) -> float:
    """四舍五入到 2 位 (对齐 TV 显示, 而非 Python 银行家进位)。"""
    return math.floor(x * 100 + 0.5) / 100

from ztp.paths import reference_dir

DEFAULT_TV = reference_dir() / "ORB30_AMEX_SPY_2026-10-12.csv"


def load_tv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    rows = []
    for no, g in df.groupby("交易编号"):
        e = g[g["类型"].str.contains("进场")].iloc[0]
        x = g[g["类型"].str.contains("出场")].iloc[0]
        rows.append(
            {
                "trade_no": no,
                "direction": str(e["信号"]).upper(),
                "entry_time": e["日期和时间"],
                "entry_price": _rhu(float(e["价格 USD"])),
                "qty": int(e["大小（数量）"]),
                "exit_time": x["日期和时间"],
                "exit_price": _rhu(float(x["价格 USD"])),
                "pnl": _rhu(float(x["净损益 USD"])),
                "bars": int(x["持续时间（K线）"]),
            }
        )
    return pd.DataFrame(rows).sort_values("trade_no").reset_index(drop=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--tv", default=str(DEFAULT_TV))
    p.add_argument("--ours", default="results/orb_trades.csv")
    p.add_argument("--show", type=int, default=20)
    a = p.parse_args()

    root = Path(__file__).resolve().parent.parent
    tv = load_tv(root / a.tv)
    ours = pd.read_csv(root / a.ours)
    ours["entry_time"] = ours["entry_time"].str.slice(0, 16)
    ours["exit_time"] = ours["exit_time"].str.slice(0, 16)
    for col in ("entry_price", "exit_price", "pnl"):
        ours[col] = ours[col].astype(float).map(_rhu)
    ours["bars"] = ours["bars_held"].astype(int)
    ours = (
        ours[pd.to_datetime(ours["entry_time"]) >= pd.Timestamp(tv["entry_time"].min())]
        .reset_index(drop=True)
    )

    print(f"TV trades: {len(tv)}   ours: {len(ours)} (>= {tv['entry_time'].min()})")

    # 1) 宽松对齐: 方向 + 入场时间 (忽略价格)
    kt_time = [f"{r.direction}|{r.entry_time}" for r in tv.itertuples()]
    ko_time = [f"{r.direction}|{r.entry_time}" for r in ours.itertuples()]
    smt = difflib.SequenceMatcher(None, kt_time, ko_time, autojunk=False)
    time_pairs = []
    for tag, i1, i2, j1, j2 in smt.get_opcodes():
        if tag == "equal":
            time_pairs.extend(zip(range(i1, i2), range(j1, j2)))
    n_time = len(time_pairs)
    print(f"\n[宽松] 方向+入场时间 对齐: {n_time}/{len(tv)} = {n_time/len(tv)*100:.0f}%")
    if time_pairs:
        dd = np.array([abs(tv.iloc[i].entry_price - ours.iloc[j].entry_price) for i, j in time_pairs])
        print(
            f"        入场价 |Δ|: 中位 {np.median(dd):.3f}  p90 {np.percentile(dd,90):.3f}  "
            f"max {dd.max():.3f}   <0.02点 {(dd<0.02).mean()*100:.0f}%   <0.1点 {(dd<0.1).mean()*100:.0f}%"
        )

    # 2) 严格对齐: 方向 + 入场时间 + 入场价
    keys_tv = [f"{r.direction}|{r.entry_time}|{r.entry_price:.2f}" for r in tv.itertuples()]
    keys_our = [f"{r.direction}|{r.entry_time}|{r.entry_price:.2f}" for r in ours.itertuples()]
    sm = difflib.SequenceMatcher(None, keys_tv, keys_our, autojunk=False)
    matched = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            matched.extend(zip(range(i1, i2), range(j1, j2)))

    fields = ["direction", "entry_time", "entry_price", "qty", "exit_time", "exit_price", "pnl", "bars"]
    n_pair = len(matched)
    field_ok = {f: 0 for f in fields}
    full_ok = 0
    mismatches = []
    for i, j in matched:
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
            mismatches.append((rt, ro))

    pct = n_pair / len(tv) * 100 if len(tv) else 0
    print(f"\n入场(方向|时间|价格)对齐一致: {n_pair}/{len(tv)} = {pct:.0f}%")
    print("\n== 对齐交易的字段匹配 ==")
    for f in fields:
        print(f"  {f:12s} {field_ok[f]}/{n_pair}")
    print(f"\n整笔全字段一致: {full_ok}/{n_pair}")

    print("\n== 对齐但仍不一致的明细 ==")
    for rt, ro in mismatches[: a.show]:
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
