"""v2.1 质变过滤 (顺势发力 / 跳空未回补) 的引擎测试 + 引擎/实盘辅助量一致性。

覆盖用户关心的"看不出来的小细节":
  - 跳空在 OR 内被回补 -> 被过滤; 未回补 -> 放行
  - 顺势发力不足 -> 被过滤; 达标 -> 放行
  - prior_daily_atr 与实时状态机 _daily_atr 逐日完全一致 (含窗口边界 NaN)
  - prime() 重建的 OR 状态与逐 bar 驱动完全一致
"""

import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from ztp.orb import ORBConfig, run, prior_daily_atr
from ztp.orb_live import ORBLiveStrategy

ET = "America/New_York"


def _day_df(date, bars: dict, fill, eod="12:30"):
    times = pd.date_range(f"{date} 09:30", f"{date} {eod}", freq="5min", tz=ET)
    d = {pd.Timestamp(f"{date} {t}", tz=ET): v for t, v in bars.items()}
    idx, rows = [], []
    for ts in times:
        idx.append(ts)
        rows.append(d.get(ts, (fill, fill, fill, fill)))
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"],
                        index=pd.DatetimeIndex(idx))


# --------------------------------------------------------------- 跳空未回补
def _gapfill_df(touch_prev_close: bool):
    prev = _day_df("2024-03-01", {"09:30": (100.0, 100.0, 100.0, 100.0)}, 100.0)
    low0 = 99.99 if touch_prev_close else 101.7
    seq = {
        "09:30": (102.0, 102.2, low0, 102.0),
        "09:35": (102.0, 102.1, 101.9, 102.0),
        "09:40": (102.0, 102.0, 101.7, 101.9),   # OR H=102.2 L=min
        "09:45": (101.9, 102.4, 101.9, 102.4),   # 突破
        "09:50": (102.4, 102.5, 102.3, 102.5),
        "09:55": (102.5, 102.6, 102.4, 102.6),   # 第3根 -> 做多
    }
    return pd.concat([prev, _day_df("2024-03-04", seq, 102.6)]).sort_index()


def test_gapfill_filter_blocks_when_filled():
    df = _gapfill_df(touch_prev_close=True)     # OR 下探 99.99 <= 昨收 100 -> 回补
    assert len(run(df, replace(ORBConfig(), use_gapfill_filter=True))) == 0
    assert len(run(df, replace(ORBConfig(), use_gapfill_filter=False))) == 1


def test_gapfill_filter_passes_when_not_filled():
    df = _gapfill_df(touch_prev_close=False)    # OR 全在昨收上方 -> 未回补
    assert len(run(df, replace(ORBConfig(), use_gapfill_filter=True))) == 1


# --------------------------------------------------------------- 顺势发力
def _strength_df(breakout_move: float, n_prev: int = 15):
    """前 n_prev 天平盘 (日振幅=2 -> ATR14≈2), 末日突破幅度 = breakout_move。"""
    frames = []
    for i in range(n_prev):
        bars = {
            "09:30": (100.0, 101.0, 99.0, 100.0),
            "09:35": (100.0, 100.5, 99.5, 100.0),
            "09:40": (100.0, 100.3, 99.7, 100.0),
            "09:45": (100.0, 100.2, 99.9, 100.0),
            "09:50": (100.0, 100.1, 99.95, 100.0),
            "09:55": (100.0, 100.0, 99.9, 100.0),
        }
        frames.append(_day_df(f"2024-01-{i+2:02d}", bars, 100.0))
    # 末日: OR 高点 100.1, 收盘 100.1+move
    p = 100.1 + breakout_move
    seq = {
        "09:30": (100.0, 100.1, 99.9, 100.0),
        "09:35": (100.0, 100.1, 99.9, 100.0),
        "09:40": (100.0, 100.05, 99.95, 100.0),  # OR H=100.1
        "09:45": (100.1, p, 100.05, p),
        "09:50": (p, p, p - 0.01, p),
        "09:55": (p, p, p - 0.01, p),            # 3 根 > OR H -> 信号
    }
    frames.append(_day_df("2024-01-17", seq, p))
    return pd.concat(frames).sort_index()


def test_strength_filter_blocks_weak_move():
    df = _strength_df(0.20)   # close-open=0.30 < 0.15*ATR(≈2)=0.30? 边界; 用 0.20 明确不足
    on = run(df, replace(ORBConfig(), use_gapfill_filter=False, use_strength_filter=True))
    off = run(df, replace(ORBConfig(), use_gapfill_filter=False, use_strength_filter=False))
    assert len(on) == 0
    assert len(off) == 1


def test_strength_filter_passes_strong_move():
    df = _strength_df(0.80)   # 明显超过 0.15*ATR
    t = run(df, replace(ORBConfig(), use_gapfill_filter=False, use_strength_filter=True))
    assert len(t) == 1


# --------------------------------------------------------------- ATR 一致性(含边界)
def _synthetic(n_days=40, seed=3):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2023-01-02", periods=n_days)
    frames = []
    for d in dates:
        times = pd.date_range(f"{d.date()} 09:30", f"{d.date()} 12:30", freq="5min", tz=ET)
        price = 100 + rng.uniform(-3, 3)
        rows = []
        for _ in times:
            o = price
            c = o + rng.normal(0, 0.2)
            hi = max(o, c) + abs(rng.normal(0, 0.08))
            lo = min(o, c) - abs(rng.normal(0, 0.08))
            rows.append((o, hi, lo, c)); price = c
        frames.append(pd.DataFrame(rows, columns=["open", "high", "low", "close"],
                                    index=pd.DatetimeIndex(times)))
    return pd.concat(frames).sort_index()


def test_prior_daily_atr_parity_with_live():
    df = _synthetic(40)
    cfg = ORBConfig()
    s = prior_daily_atr(df, cfg.tz)
    strat = ORBLiveStrategy(cfg, df)
    days = sorted({ts.date() for ts in df.index})
    checked_finite = 0
    for d in days:
        v_live = strat._daily_atr(d)
        key = pd.Timestamp(d).tz_localize(cfg.tz)
        v_eng = float(s.get(key, np.nan))
        if v_eng == v_eng or v_live == v_live:
            assert abs(v_eng - v_live) < 1e-9, (d, v_eng, v_live)
            if v_eng == v_eng:
                checked_finite += 1
    assert checked_finite > 0
    # 首个有限值 (ATR 生效日) 起, 引擎与 live 必须一致; 且窗口边界处为 NaN (双方一致)
    first_finite = next(d for d in days
                        if np.isfinite(strat._daily_atr(d)))
    assert np.isfinite(strat._daily_atr(first_finite))
    assert not np.isfinite(strat._daily_atr(days[0]))   # 首日必然 NaN


# --------------------------------------------------------------- prime 重建一致性
def _drive_state(df, upto):
    strat = ORBLiveStrategy(ORBConfig(), df.iloc[:0])
    for i in range(upto + 1):
        strat.append_bar(df.index[i], *(float(df[c].iloc[i]) for c in
                                        ("open", "high", "low", "close")))
        strat.on_bar()
    return strat.st


def test_prime_matches_bar_driven_state():
    df = _synthetic(30)
    # 选某日 09:55 bar (OR 结束、尚无 3 根突破)
    target_day = sorted({ts.date() for ts in df.index})[20]
    k = [i for i, ts in enumerate(df.index)
         if ts.date() == target_day and ts.hour == 9 and ts.minute == 55][0]
    strat_p = ORBLiveStrategy(ORBConfig(), df.iloc[:k + 1])
    strat_p.prime()
    st_p = strat_p.st
    st_d = _drive_state(df, k)
    fields = ("session_high", "session_low", "session_done", "traded_today",
              "bull", "bear", "day_open", "prev_close", "first_dir",
              "atr_daily", "or_gap_filled")
    for f in fields:
        a, b = getattr(st_p, f), getattr(st_d, f)
        if isinstance(a, float) and a != a and isinstance(b, float) and b != b:
            continue
        assert a == b, f"{f}: prime={a} driven={b}"


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
