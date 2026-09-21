"""一致性测试: LiveStrategy 逐 bar 驱动的决策必须与回测引擎完全一致。

方法: 同一份数据, 引擎一次性跑完 vs LiveStrategy 逐根喂入(含盘中 TP/SL 模拟成交),
比对全部交易字段。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from ztp.config import ZTPConfig
from ztp.engine import run_full
from ztp.live import LiveStrategy
from ztp.longport_data import load_cache


def test_consistency(days: int = 40):
    cfg = ZTPConfig()
    df = load_cache("SPY.US", "none")
    df = df[df.index >= df.index.max() - pd.Timedelta(days=days)]

    engine_trades, _ = run_full(df, cfg)

    strat = LiveStrategy(cfg, df.iloc[:1])
    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)

    for i in range(1, len(df)):
        strat.append_bar(df.index[i], o[i], h[i], l[i], c[i])
        st = strat.st
        if st.pos_dir != 0 and st.exit_armed and i >= st.exit_from:
            fill, reason = None, None
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

    live = strat.closed_trades
    print(f"引擎交易: {len(engine_trades)}  LiveStrategy 交易: {len(live)}")
    n = min(len(engine_trades), len(live))
    bad = 0
    for j in range(n):
        e = engine_trades.iloc[j]
        v = live[j]
        for f_ev, f_lv in [
            ("direction", "direction"),
            ("entry_time", "entry_time"),
            ("entry_price", "entry_price"),
            ("exit_time", "exit_time"),
            ("exit_price", "exit_price"),
            ("exit_reason", "exit_reason"),
            ("bars_held", "bars_held"),
        ]:
            ve, vv = e[f_ev], v[f_lv]
            if isinstance(ve, float) or isinstance(vv, float):
                if abs(float(ve) - float(vv)) > 1e-9:
                    bad += 1
                    print(f"  差异 trade{j} {f_ev}: 引擎={ve} live={vv}")
                    break
            elif str(ve)[:16] != str(vv)[:16] and ve != vv:
                bad += 1
                print(f"  差异 trade{j} {f_ev}: 引擎={ve} live={vv}")
                break
    assert len(engine_trades) == len(live), "交易数量不一致"
    assert bad == 0, f"{bad} 处字段不一致"
    print("PASS: 逐笔完全一致")


import pandas as pd  # noqa: E402

if __name__ == "__main__":
    test_consistency(int(sys.argv[1]) if len(sys.argv) > 1 else 40)
