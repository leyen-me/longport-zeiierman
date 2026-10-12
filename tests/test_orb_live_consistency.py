"""一致性测试: ORBLiveStrategy 逐 bar 驱动必须与回测引擎 (ztp/orb.py) 完全一致。

方法: 同一份 5min 数据, 引擎一次性跑完 vs 状态机逐根喂入(含盘中 TP/SL 模拟成交), 比对全部字段。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from ztp.longport_data import load_cache
from ztp.orb import ORBConfig, load_bars_csv, run as bt_run
from ztp.orb import _round_tick
from ztp.orb_live import ORBLiveStrategy


def _five_min_data() -> pd.DataFrame:
    """优先长桥原生 5min 缓存; 无则用 TV 导出的原生 5min 参考数据。"""
    try:
        return load_cache("SPY.US", "none", period="min5")
    except FileNotFoundError:
        return load_bars_csv("data/reference/BATS_SPY_5m_2025-09-29_2026-10-09.csv")


def test_consistency(days: int = 60):
    cfg = ORBConfig()
    df = _five_min_data()
    df = df[df.index >= df.index.max() - pd.Timedelta(days=days)]

    engine = bt_run(df, cfg)

    strat = ORBLiveStrategy(cfg, df.iloc[:0])
    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)

    for i in range(len(df)):
        strat.append_bar(df.index[i], o[i], h[i], l[i], c[i])
        st = strat.st
        # A) 盘中 stop/limit (先于脚本逻辑, 与引擎一致)
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

        # 入场当根: 收盘已越过止盈 -> 同根成交 (引擎行为)
        if st.pos_dir != 0 and st.exit_armed and (
            (st.pos_dir > 0 and c[i] >= st.tp_px) or (st.pos_dir < 0 and c[i] <= st.tp_px)
        ):
            strat.notify_exit_fill(_round_tick(c[i], cfg.mintick), "TP")

    live = strat.closed_trades
    print(f"引擎交易: {len(engine)}  状态机交易: {len(live)}")
    n = min(len(engine), len(live))
    bad = 0
    for j in range(n):
        e = engine.iloc[j]
        v = live[j]
        for f in ("direction", "entry_time", "entry_price", "exit_time",
                  "exit_price", "exit_reason", "bars_held"):
            ve, vv = e[f], v[f]
            if isinstance(ve, float) or isinstance(vv, float):
                if abs(float(ve) - float(vv)) > 1e-9:
                    bad += 1
                    print(f"  差异 trade{j} {f}: 引擎={ve} live={vv}")
                    break
            elif str(ve)[:16] != str(vv)[:16] and ve != vv:
                bad += 1
                print(f"  差异 trade{j} {f}: 引擎={ve} live={vv}")
                break
    assert len(engine) == len(live), "交易数量不一致"
    assert bad == 0, f"{bad} 处字段不一致"
    print("PASS: 逐笔完全一致")


if __name__ == "__main__":
    test_consistency(int(sys.argv[1]) if len(sys.argv) > 1 else 60)
