"""期权定量规则单元测试: size_contracts。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ztp.options import size_contracts


def test_sizing():
    # ZTP 模式 (最多 2 张)
    assert size_contracts(0.5, 200) == 2
    assert size_contracts(1.0, 200) == 2
    assert size_contracts(1.5, 200) == 1
    assert size_contracts(2.0, 200) == 1
    assert size_contracts(2.5, 200) == 0

    # ORB 模式 (用满预算, 风险=权利金<=budget, >=1 张)
    assert size_contracts(0.5, 300, True) == 6
    assert size_contracts(1.0, 300, True) == 3
    assert size_contracts(2.0, 300, True) == 1   # >2 也至少一张
    assert size_contracts(2.5, 300, True) == 1
    assert size_contracts(3.0, 300, True) == 1   # 刚好 300
    assert size_contracts(3.05, 300, True) == 0  # 超过 300 上限, 放弃
    assert size_contracts(0.0, 300, True) == 0
    assert size_contracts(1.0, 0, True) == 0

    # 单笔最大风险 <= budget
    for ask in (0.3, 0.77, 1.23, 2.9):
        q = size_contracts(ask, 300, True)
        assert q * ask * 100 <= 300 + 1e-9, (ask, q)

    # max_qty 硬上限 (小资金防超配): 无论如何最多 N 张
    assert size_contracts(0.5, 300, True, max_qty=1) == 1
    assert size_contracts(0.28, 1000, True, max_qty=2) == 2
    assert size_contracts(2.0, 500, True, max_qty=3) == 2      # 预算只够 2
    assert size_contracts(0.5, 300, False, max_qty=1) == 1
    assert size_contracts(3.05, 300, True, max_qty=1) == 0     # 超预算仍旧 0

    # min_one: 信号必开 >=1 张, 第 1 张不限价; 多张时总额 <= budget
    assert size_contracts(5.0, 300, True, min_one=True) == 1   # 第1张$500 也照买
    assert size_contracts(3.0, 300, True, min_one=True) == 1
    assert size_contracts(1.5, 300, True, min_one=True) == 2   # 2*150=300
    assert size_contracts(0.5, 300, True, min_one=True) == 6   # 6*50=300
    assert size_contracts(0.0, 300, True, min_one=True) == 0   # 价格无效
    assert size_contracts(0.9, 0, True, min_one=True) == 1     # 预算0也必开1张
    assert size_contracts(0.9, 300, True, min_one=True, max_qty=1) == 1
    # 多张一定不超预算
    for ask in (0.4, 0.9, 1.4, 2.9):
        q = size_contracts(ask, 300, True, min_one=True)
        if q > 1:
            assert q * ask * 100 <= 300 + 1e-9, (ask, q)
    print("PASS option sizing")


if __name__ == "__main__":
    test_sizing()
