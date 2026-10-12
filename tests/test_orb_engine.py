"""ORB 引擎语义单元测试 (合成数据, 不依赖外部缓存)。

覆盖: 开盘区间 / 突破确认 / 锚边界 SL-TP / 跳空过滤 / 首根过滤 /
      首个突破锁定 / 移动止损开与关 / EOD 强平 / 交易日 / 取整 / 多空。
"""

import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from ztp.orb import ORBConfig, run, _round_tick

ET = "America/New_York"


def _df(days, eod="12:30"):
    """days: list of (date, {hhmm: (o,h,l,c)}, fill_price)。缺口用 fill 补齐。"""
    frames = []
    for date, supplied, fill in days:
        times = pd.date_range(f"{date} 09:30", f"{date} {eod}", freq="5min", tz=ET)
        d = {pd.Timestamp(f"{date} {t}", tz=ET): v for t, v in supplied.items()}
        idx, rows = [], []
        for ts in times:
            idx.append(ts)
            rows.append(d.get(ts, (fill, fill, fill, fill)))
        frames.append(pd.DataFrame(rows, columns=["open", "high", "low", "close"],
                                    index=pd.DatetimeIndex(idx)))
    return pd.concat(frames).sort_index()


def _cfg(**kw):
    return replace(ORBConfig(), **kw)


LONG_SEQ = {   # 首根向上, OR=100.2/99.6, 3 根站上 -> 09:55 做多 @100.5
    "09:30": (100.0, 100.2, 99.8, 100.0),
    "09:35": (100.0, 100.1, 99.7, 100.0),
    "09:40": (100.0, 100.0, 99.6, 99.9),
    "09:45": (100.0, 100.3, 99.9, 100.3),
    "09:50": (100.3, 100.4, 100.1, 100.4),
    "09:55": (100.4, 100.5, 100.2, 100.5),
}


# --------------------------------------------------------------------------
def test_round_tick():
    assert _round_tick(99.1, 0.01) == 99.1
    assert _round_tick(100.234, 0.01) == 100.23
    assert _round_tick(100.236, 0.01) == 100.24
    assert _round_tick(100.999, 0.01) == 101.0


def test_or_and_long_breakout():
    """OR=09:30-09:45; 3 根收盘站上 OR 高点 -> 做多, 锚 OR 边界。"""
    t = run(_df([("2024-01-02", LONG_SEQ, 100.5)]), _cfg())
    assert len(t) == 1
    r = t.iloc[0]
    assert r.direction == "LONG"
    assert r.entry_price == 100.5
    assert r.anchor == 100.2
    assert r.sl == 99.1 and r.tp == 102.3
    assert str(r.entry_time)[11:16] == "09:55"
    assert r.exit_reason == "EOD"


def test_short_breakout():
    seq = {
        "09:30": (100.2, 100.3, 99.9, 100.0),   # 首根向下 -> fd=-1
        "09:35": (100.0, 100.1, 99.7, 100.0),
        "09:40": (100.0, 100.0, 99.6, 99.9),    # OR H=100.3 L=99.6
        "09:45": (99.9, 100.0, 99.4, 99.5),
        "09:50": (99.5, 99.6, 99.2, 99.3),
        "09:55": (99.3, 99.4, 99.0, 99.1),      # 做空 @99.1
    }
    t = run(_df([("2024-01-03", seq, 99.1)]), _cfg())
    assert len(t) == 1
    r = t.iloc[0]
    assert r.direction == "SHORT"
    assert r.anchor == 99.6
    assert r.sl == 100.7 and r.tp == 97.5
    assert r.exit_reason == "EOD"


def test_firstbar_filter_blocks_long():
    seq = dict(LONG_SEQ)
    seq["09:30"] = (100.1, 100.2, 99.8, 100.0)   # 首根向下
    assert len(run(_df([("2024-01-05", seq, 100.5)]), _cfg(use_firstbar_filter=True))) == 0
    assert len(run(_df([("2024-01-05", seq, 100.5)]), _cfg(use_firstbar_filter=False))) == 1


def test_first_signal_only_locks_day():
    """首个突破(空头)被首根过滤后: 锁当日=不再反向; 不锁=可后反向做多。"""
    seq = {
        "09:30": (100.0, 100.2, 99.8, 100.0),   # 首根向上 -> fd=+1
        "09:35": (100.0, 100.1, 99.7, 100.0),
        "09:40": (100.0, 100.0, 99.6, 99.9),    # OR H=100.2 L=99.6
        "09:45": (99.9, 100.0, 99.3, 99.4),     # 空头突破(被首根过滤)
        "09:50": (99.4, 99.5, 99.1, 99.2),
        "09:55": (99.2, 99.3, 99.0, 99.1),
        "10:00": (99.1, 100.4, 99.1, 100.4),    # 反向多头突破
        "10:05": (100.4, 100.5, 100.3, 100.5),
        "10:10": (100.5, 100.6, 100.4, 100.6),
    }
    assert len(run(_df([("2024-01-08", seq, 99.1)]), _cfg(first_signal_only=True))) == 0
    t = run(_df([("2024-01-08", seq, 99.1)]), _cfg(first_signal_only=False))
    assert len(t) == 1 and t.iloc[0].direction == "LONG"


def test_gap_filter():
    prev = ("2024-01-09", {"09:30": (200.0, 200.0, 200.0, 200.0)}, 200.0)
    seq = {
        "09:30": (198.9, 199.2, 198.8, 199.1),  # 低开, 但首根向上
        "09:35": (199.1, 199.2, 198.9, 199.1),
        "09:40": (199.1, 199.15, 198.85, 199.1),  # OR H=199.2 L=198.85
        "09:45": (199.1, 199.5, 199.0, 199.5),
        "09:50": (199.5, 199.6, 199.4, 199.6),
        "09:55": (199.6, 199.7, 199.5, 199.7),
    }
    day = ("2024-01-10", seq, 199.7)
    df = _df([prev, day])
    assert len(run(df, _cfg(use_gap_filter=True))) == 0    # 逆跳空(低开做多)被挡
    assert len(run(df, _cfg(use_gap_filter=False))) == 1   # 关过滤放行


def test_trailing_toggle_changes_exit():
    seq = dict(LONG_SEQ)
    seq.update({
        "10:00": (100.5, 102.0, 100.5, 102.0),   # 冲高 (未到 TP 102.3)
        "10:05": (102.0, 102.0, 100.6, 100.7),   # 回落
        "10:10": (100.7, 100.8, 100.4, 100.5),
    })
    df = _df([("2024-01-12", seq, 100.5)])
    off = run(df, _cfg(use_trail=False)).iloc[0]
    assert off.exit_reason == "EOD"              # 固定止损 99.1 未触发
    on = run(df, _cfg(use_trail=True)).iloc[0]
    assert on.exit_reason == "SL"                # 移动止损被回落触发
    assert on.exit_price > off.exit_price


def test_eod_close_at_1200():
    t = run(_df([("2024-01-15", LONG_SEQ, 100.5)]), _cfg())
    r = t.iloc[0]
    assert r.exit_reason == "EOD"
    assert str(r.exit_time)[11:16] == "12:00"


def test_trade_days_exclude():
    assert len(run(_df([("2024-01-04", LONG_SEQ, 100.5)]), _cfg(trade_days=(0, 1, 2, 4)))) == 0
    assert len(run(_df([("2024-01-04", LONG_SEQ, 100.5)]), _cfg(trade_days=(0, 1, 2, 3, 4)))) == 1


def test_breakout_confirmation_count():
    seq = {
        "09:30": (100.0, 100.2, 99.8, 100.0),
        "09:35": (100.0, 100.1, 99.7, 100.0),
        "09:40": (100.0, 100.0, 99.6, 99.9),    # OR H=100.2
        "09:45": (100.0, 100.3, 99.9, 100.3),
        "09:50": (100.3, 100.4, 100.1, 100.4),  # 仅 2 根 > OR 高
    }
    df = _df([("2024-01-19", seq, 100.0)])       # fill 100.0 -> 之后不再站上
    assert len(run(df, _cfg(brk_candles=2))) == 1
    assert len(run(df, _cfg(brk_candles=3))) == 0
    assert len(run(df, _cfg(brk_candles=4))) == 0


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    ok = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
            ok += 1
        except Exception as e:
            import traceback
            print(f"  FAIL {fn.__name__}: {e!r}")
            traceback.print_exc()
    print(f"{ok}/{len(fns)} passed")
    sys.exit(0 if ok == len(fns) else 1)
