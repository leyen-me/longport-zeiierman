"""期权盘口缺失兜底单元测试。

覆盖: depth 重试 → last_done 兜底; runner._best_bid 的本地缓存。
"""

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ztp.options import top_bid, _top_of_book
from ztp.orb_runner import ORBLiveRunner


def _depth(bid, ask):
    lv = lambda p: types.SimpleNamespace(price=p)
    return types.SimpleNamespace(
        bids=[lv(bid)] if bid else [], asks=[lv(ask)] if ask else []
    )


class FakeQ:
    """按序列返回 depth 结果; "ERR" 表示抛异常。"""

    def __init__(self, seq, last=0.0):
        self.seq = list(seq)
        self.n = 0
        self.last = last

    def depth(self, sym):
        r = self.seq[self.n] if self.n < len(self.seq) else self.seq[-1]
        self.n += 1
        if r == "ERR":
            raise RuntimeError("depth unavailable")
        return r

    def option_quote(self, syms):
        return [types.SimpleNamespace(last_done=self.last)]


def test_top_bid_from_depth():
    q = FakeQ([_depth(1.20, 1.25)])
    assert _top_of_book(q, "X") == (1.20, 1.25)
    assert top_bid(q, "X") == 1.20


def test_top_bid_lastdone_fallback():
    q = FakeQ(["ERR", "ERR", "ERR"], last=0.90)   # depth 全失败 -> last_done
    assert top_bid(q, "X") == 0.90


def test_depth_retry_recovers():
    q = FakeQ(["ERR", _depth(1.10, 1.15)])         # 第2次成功
    assert top_bid(q, "X") == 1.10


def test_top_bid_no_quote_returns_fallback():
    q = FakeQ(["ERR", "ERR", "ERR"], last=0.0)
    assert top_bid(q, "X", fallback=0.0) == 0.0


def test_best_bid_cache():
    r = object.__new__(ORBLiveRunner)
    r._last_bid = {}
    # 1) 完全无报价 -> 0, 无缓存
    q = FakeQ([_depth(0, 0), "ERR", "ERR", _depth(1.50, 1.55),
               "ERR", "ERR", "ERR"])
    r.qctx = q
    assert r._best_bid("X") == 0.0
    # 2) 盘口恢复 -> 1.50 并缓存
    assert r._best_bid("X") == 1.50
    # 3) 盘口再次缺失 -> 用缓存 1.50
    assert r._best_bid("X") == 1.50


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
