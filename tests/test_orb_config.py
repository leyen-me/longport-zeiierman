"""锁定 ORB 生产参数 (v2)。任何默认值被误改, 这里会立刻失败。

生产决策记录:
  OR=15min / 确认3根 / 周一~三+周五 / 跳空过滤关 / 首根过滤开 /
  首个突破锁当日 / 无移动止损 / SL-TP=边界∓1.1±2.1 / 12:00 强平
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ztp.orb import ORBConfig

EXPECTED = {
    "symbol": "SPY.US",
    "trade_days": (0, 1, 2, 4),      # 周一/二/三/五 (周四无 edge)
    "or_minutes": 15,
    "brk_candles": 3,
    "qty": 80,
    "sl_points": 1.1,
    "tp_points": 2.1,
    "use_gap_filter": False,          # 关 (更频繁)
    "use_firstbar_filter": True,
    "first_signal_only": True,        # 首个突破锁当日
    "use_strength_filter": True,      # v2.1 质变过滤: 顺势发力
    "strength_atr_mult": 0.15,
    "use_gapfill_filter": True,       # v2.1 质变过滤: 跳空未回补
    "use_trail": False,               # 关移动止损
    "trail_mult": 0.75,
    "eod_hour": 12,
    "eod_minute": 0,
    "tz": "America/New_York",
    "mintick": 0.01,
}


def test_defaults_locked():
    c = ORBConfig()
    for k, v in EXPECTED.items():
        got = getattr(c, k)
        assert got == v, f"{k}: 期望 {v!r}, 实际 {got!r}"


if __name__ == "__main__":
    test_defaults_locked()
    print("PASS orb config defaults locked")
