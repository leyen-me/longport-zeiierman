"""实盘状态持久化 round-trip 测试: 重启后 OR 状态/新过滤字段/权益缓存都要能恢复。"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from ztp.orb import ORBConfig
from ztp.orb_live import ORBLiveStrategy
from ztp.orb_runner import ORBLiveRunner

_EMPTY = pd.DataFrame(columns=["open", "high", "low", "close"])


def _blank_runner(state_path):
    r = object.__new__(ORBLiveRunner)
    r.strategy = ORBLiveStrategy(ORBConfig(), _EMPTY)
    r.option_pos = None
    r.state_path = state_path
    r._last_equity = None
    r._last_equity_ts = 0.0
    return r


def test_state_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        sp = Path(td) / "state.json"
        r = _blank_runner(sp)
        st = r.strategy.st
        st.session_high, st.session_low = 105.5, 99.5
        st.session_done, st.traded_today = True, False
        st.bull, st.bear = 2, 0
        st.pos_dir, st.pos_qty = 0, 0
        st.entry_px, st.entry_i = 0.0, -1
        st.sl_px, st.tp_px = 99.1, 107.6
        st.init_sl_px, st.R = 99.1, 6.4
        st.run_ext = 105.5
        st.day_open, st.prev_close = 100.0, 98.0
        st.first_dir = 1
        st.atr_daily = 3.75
        st.or_gap_filled = True
        st.exit_armed, st.exit_from = False, 1 << 60
        r._last_equity, r._last_equity_ts = 1234.5, 999.0
        r.save_state()

        r2 = _blank_runner(sp)
        r2._restore_state()
        s = r2.strategy.st
        assert s.session_high == 105.5 and s.session_low == 99.5
        assert s.session_done is True and s.traded_today is False
        assert s.bull == 2 and s.bear == 0
        assert s.sl_px == 99.1 and s.tp_px == 107.6
        assert s.day_open == 100.0 and s.prev_close == 98.0
        assert s.first_dir == 1
        assert s.atr_daily == 3.75
        assert s.or_gap_filled is True
        assert r2._last_equity == 1234.5 and r2._last_equity_ts == 999.0


def test_state_equity_restored_even_when_stale_date():
    """旧日期状态: OR 字段不恢复, 但权益缓存应恢复 (供 API 异常兜底)。"""
    import json
    with tempfile.TemporaryDirectory() as td:
        sp = Path(td) / "state.json"
        sp.write_text(json.dumps({
            "date": "2000-01-01", "last_equity": 777.0, "last_equity_ts": 42.0,
        }))
        r = _blank_runner(sp)
        r._restore_state()
        assert r._last_equity == 777.0 and r._last_equity_ts == 42.0


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
