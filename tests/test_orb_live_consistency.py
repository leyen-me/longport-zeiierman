"""一致性测试: ORBLiveStrategy 逐 bar 驱动必须与回测引擎 (ztp/orb.py) 完全一致。

两种数据源:
  - 合成长数据 (确定性, 无需外部文件)
  - 参考 5min CSV (data/reference, 若存在)
比对全部交易字段。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from ztp.longport_data import load_cache
from ztp.orb import ORBConfig, load_bars_csv, run as bt_run, _round_tick
from ztp.orb_live import ORBLiveStrategy

ET = "America/New_York"


def _drive(df, cfg):
    """逐根喂入状态机, 并复刻引擎的盘中 TP/SL 成交判定, 返回 live 交易列表。"""
    strat = ORBLiveStrategy(cfg, df.iloc[:0])
    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    for i in range(len(df)):
        strat.append_bar(df.index[i], o[i], h[i], l[i], c[i])
        st = strat.st
        if st.pos_dir != 0 and st.exit_armed and i >= st.exit_from:
            fill = reason = None
            if st.pos_dir > 0:
                if o[i] <= st.sl_px:
                    fill, reason = o[i], "SL"
                elif o[i] >= st.tp_px:
                    fill, reason = o[i], "TP"
                elif l[i] <= st.sl_px and h[i] >= st.tp_px:
                    fill, reason = (st.sl_px, "SL") if c[i] >= o[i] else (st.tp_px, "TP")
                elif l[i] <= st.sl_px:
                    fill, reason = st.sl_px, "SL"
                elif h[i] >= st.tp_px:
                    fill, reason = st.tp_px, "TP"
            else:
                if o[i] >= st.sl_px:
                    fill, reason = o[i], "SL"
                elif o[i] <= st.tp_px:
                    fill, reason = o[i], "TP"
                elif h[i] >= st.sl_px and l[i] <= st.tp_px:
                    fill, reason = (st.tp_px, "TP") if c[i] >= o[i] else (st.sl_px, "SL")
                elif h[i] >= st.sl_px:
                    fill, reason = st.sl_px, "SL"
                elif l[i] <= st.tp_px:
                    fill, reason = st.tp_px, "TP"
            if fill is not None:
                strat.notify_exit_fill(fill, reason)
        strat.on_bar()
        if st.pos_dir != 0 and st.exit_armed and (
            (st.pos_dir > 0 and c[i] >= st.tp_px) or (st.pos_dir < 0 and c[i] <= st.tp_px)
        ):
            strat.notify_exit_fill(_round_tick(c[i], cfg.mintick), "TP")
    return strat.closed_trades


def _compare(engine, live):
    assert len(engine) == len(live), f"交易数量不一致: 引擎={len(engine)} live={len(live)}"
    bad = 0
    fields = ("direction", "entry_time", "entry_price", "exit_time",
              "exit_price", "exit_reason", "bars_held")
    for j in range(len(engine)):
        e, v = engine.iloc[j], live[j]
        for f in fields:
            ve, vv = e[f], v[f]
            if isinstance(ve, float) or isinstance(vv, float):
                same = abs(float(ve) - float(vv)) <= 1e-9
            else:
                same = str(ve)[:16] == str(vv)[:16] or ve == vv
            if not same:
                bad += 1
                print(f"  差异 trade{j} {f}: 引擎={ve} live={vv}")
                break
    assert bad == 0, f"{bad} 处字段不一致"
    return len(engine)


def _synthetic(days=120, seed=7):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2023-01-02", periods=days)
    frames = []
    for d in dates:
        times = pd.date_range(f"{d.date()} 09:30", f"{d.date()} 12:30", freq="5min", tz=ET)
        price = 100 + rng.uniform(-3, 3)
        rows = []
        for _ in times:
            o = price
            c = o + rng.normal(0, 0.18)
            hi = max(o, c) + abs(rng.normal(0, 0.06))
            lo = min(o, c) - abs(rng.normal(0, 0.06))
            rows.append((o, hi, lo, c))
            price = c
        frames.append(pd.DataFrame(rows, columns=["open", "high", "low", "close"],
                                    index=pd.DatetimeIndex(times)))
    return pd.concat(frames).sort_index()


def test_consistency_synthetic():
    df = _synthetic()
    cfg = ORBConfig()
    n = _compare(bt_run(df, cfg), _drive(df, cfg))
    print(f"合成数据一致性 PASS ({n} 笔)")
    # 再验证几组非默认参数
    from dataclasses import replace
    for kw in [dict(use_gap_filter=True), dict(use_firstbar_filter=False),
               dict(first_signal_only=False), dict(use_trail=True),
               dict(or_minutes=30, brk_candles=2)]:
        c = replace(cfg, **kw)
        _compare(bt_run(df, c), _drive(df, c))
    print("非默认参数一致性 PASS")


def _five_min_data() -> pd.DataFrame:
    try:
        return load_cache("SPY.US", "none", period="min5")
    except FileNotFoundError:
        return load_bars_csv("data/reference/BATS_SPY_5m_2025-09-29_2026-10-09.csv")


def test_consistency_reference(days: int = 120):
    try:
        df = _five_min_data()
    except FileNotFoundError:
        print("跳过: 无参考 5min 数据")
        return
    df = df[df.index >= df.index.max() - pd.Timedelta(days=days)]
    cfg = ORBConfig()
    n = _compare(bt_run(df, cfg), _drive(df, cfg))
    print(f"参考数据一致性 PASS ({n} 笔)")


if __name__ == "__main__":
    ok = 0
    fns = [test_consistency_synthetic, test_consistency_reference]
    for fn in fns:
        try:
            fn()
            ok += 1
        except Exception as e:
            import traceback
            print(f"FAIL {fn.__name__}: {e!r}")
            traceback.print_exc()
    print(f"{ok}/{len(fns)} passed")
    sys.exit(0 if ok == len(fns) else 1)
