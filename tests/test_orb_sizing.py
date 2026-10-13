"""下注预算 (账户权益 × size_pct) 与兜底逻辑单元测试。

不连长桥: 用 object.__new__ 绕过 __init__ 注入桩。
兜底顺序: 实时权益 -> 最近一次成功权益(≤max_age) -> 固定 budget。
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ztp.orb_runner import ORBLiveRunner


def _make(equity_fn, *, size_pct=0.25, budget=300.0, min_budget=50.0,
          last_eq=None, last_ts=0.0, max_age=24 * 3600, use_eq=True):
    r = object.__new__(ORBLiveRunner)
    r.use_equity_sizing = use_eq
    r.size_pct = size_pct
    r.budget = budget
    r.min_budget = min_budget
    r._last_equity = last_eq
    r._last_equity_ts = last_ts
    r.equity_max_age_sec = max_age
    r.tctx = object()          # 非 None, 走 API 分支
    r._account_equity = equity_fn
    return r


def test_fresh_equity():
    r = _make(lambda: 1000.0)
    assert r._entry_budget() == 250.0


def test_min_budget_floor():
    r = _make(lambda: 100.0)   # 25% = 25 < floor 50
    assert r._entry_budget() == 50.0


def test_api_fail_uses_recent_cache():
    r = _make(lambda: None, last_eq=800.0, last_ts=time.time())
    assert r._entry_budget() == 200.0


def test_api_fail_uses_stale_cache_within_age():
    r = _make(lambda: None, last_eq=800.0, last_ts=time.time() - 3600,
              max_age=24 * 3600)
    assert r._entry_budget() == 200.0


def test_api_fail_stale_cache_expired():
    r = _make(lambda: None, last_eq=800.0, last_ts=time.time() - 2 * 24 * 3600)
    assert r._entry_budget() == 300.0    # 回退固定预算


def test_api_fail_no_cache():
    r = _make(lambda: None)
    assert r._entry_budget() == 300.0


def test_equity_sizing_disabled():
    r = _make(lambda: 1000.0, use_eq=False)
    assert r._entry_budget() == 300.0


def test_zero_equity_falls_back():
    r = _make(lambda: 0.0)
    assert r._entry_budget() == 300.0


# --------------------------------------------------------------- 权益读取重试/兜底
import types


class _StubTctx:
    def __init__(self, results):
        self.results = list(results)
        self.n = 0

    def account_balance(self):
        r = self.results[self.n] if self.n < len(self.results) else self.results[-1]
        self.n += 1
        if isinstance(r, Exception):
            raise r
        return r


def test_account_equity_retry_recovers_and_caches():
    r = object.__new__(ORBLiveRunner)
    r.tctx = _StubTctx([RuntimeError("boom"),
                        types.SimpleNamespace(net_assets=1200)])
    r._last_equity = None
    r._last_equity_ts = 0.0
    assert r._account_equity(retries=2) == 1200.0
    assert r._last_equity == 1200.0 and r._last_equity_ts > 0


def test_account_equity_all_fail_returns_none_keeps_cache():
    r = object.__new__(ORBLiveRunner)
    r.tctx = _StubTctx([RuntimeError("x")])
    r._last_equity = 888.0
    r._last_equity_ts = 123.0
    assert r._account_equity(retries=2) is None
    assert r._last_equity == 888.0 and r._last_equity_ts == 123.0   # 不清缓存


def test_account_equity_fallback_field_total_cash():
    r = object.__new__(ORBLiveRunner)
    r.tctx = _StubTctx([types.SimpleNamespace(net_assets=None, total_cash=555)])
    r._last_equity = None
    r._last_equity_ts = 0.0
    assert r._account_equity(retries=0) == 555.0


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
