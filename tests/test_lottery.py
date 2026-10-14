"""大乐透对奖逻辑单元测试 (离线, 不联网)。用法: python tests/test_lottery.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ztp.lottery import (
    Draw, TICKETS, check_draw, judge_tier, ticket_amount,
)

# 合成奖金表 (奖池 < 8 亿时的固定奖金额)
LEVELS = [
    {"prizeLevel": "一等奖", "stakeAmountFormat": "10000000"},
    {"prizeLevel": "一等奖(追加)", "stakeAmountFormat": "---"},
    {"prizeLevel": "二等奖", "stakeAmountFormat": "213000"},
    {"prizeLevel": "二等奖(追加)", "stakeAmountFormat": "170400"},
    {"prizeLevel": "三等奖", "stakeAmountFormat": "5000"},
    {"prizeLevel": "四等奖", "stakeAmountFormat": "300"},
    {"prizeLevel": "五等奖", "stakeAmountFormat": "150"},
    {"prizeLevel": "六等奖", "stakeAmountFormat": "15"},
    {"prizeLevel": "七等奖", "stakeAmountFormat": "5"},
]

TIER_CASES = {
    (5, 2): 1, (5, 1): 2,
    (5, 0): 3, (4, 2): 3,
    (4, 1): 4,
    (4, 0): 5, (3, 2): 5,
    (3, 1): 6, (2, 2): 6,
    (3, 0): 7, (2, 1): 7, (1, 2): 7, (0, 2): 7,
    (2, 0): 0, (1, 1): 0, (1, 0): 0, (0, 1): 0, (0, 0): 0,
}


def test_tier_mapping():
    for (f, b), exp in TIER_CASES.items():
        got = judge_tier(f, b)
        assert got == exp, f"judge_tier({f},{b})={got}, 期望 {exp}"
    print(f"  奖级映射 {len(TIER_CASES)} 例通过")


def test_amounts():
    # 固定奖: 追加不加钱
    assert ticket_amount(3, LEVELS, append=True) == 5000
    assert ticket_amount(7, LEVELS, append=True) == 5
    # 浮动奖: 追加 = 基本 × 1.8 (规则, 不依赖当期是否有追加中奖者)
    assert ticket_amount(1, LEVELS, append=True) == 18_000_000
    assert ticket_amount(2, LEVELS, append=True) == 383_400
    assert ticket_amount(2, LEVELS, append=False) == 213_000
    assert ticket_amount(0, LEVELS, append=True) == 0
    print("  奖金计算通过")


def test_check_draw_hit():
    # 用第 1 组的号码当开奖号, 应中一等奖
    t = TICKETS[0]
    d = Draw(draw_num="T", draw_time="", front=list(t[0]), back=list(t[1]), levels=LEVELS)
    res = check_draw(d)
    assert res[0].tier == 1 and res[0].amount == 18_000_000
    assert all(r.tier == 0 for r in res[1:])  # 其余组不中
    print("  单期中奖判定通过")


def test_check_draw_none():
    d = Draw(draw_num="T", draw_time="", front=[1, 2, 3, 4, 5], back=[1, 2], levels=LEVELS)
    res = check_draw(d)
    assert all(r.tier == 0 for r in res)
    print("  未中奖判定通过")


if __name__ == "__main__":
    test_tier_mapping()
    test_amounts()
    test_check_draw_hit()
    test_check_draw_none()
    print("lottery 测试全部通过")
