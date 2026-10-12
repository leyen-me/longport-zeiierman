import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ztp.orb_runner import ORBLiveRunner


def main():
    p = argparse.ArgumentParser(description="ORB30 策略 SPY 0DTE 期权实盘/模拟盘")
    p.add_argument("--dry-run", action="store_true", help="只打印信号和意向订单, 不真实下单")
    p.add_argument("--budget", type=float, default=300.0, help="单笔期权预算/最大风险(美元), 默认 300")
    p.add_argument("--delta", type=float, default=0.50, help="目标 delta, 默认 0.50")
    p.add_argument("--max-contracts", type=int, default=None,
                   help="单笔最大张数上限 (小资金防超配, 如 1)")
    a = p.parse_args()
    ORBLiveRunner(dry_run=a.dry_run, budget=a.budget, target_delta=a.delta,
                  max_contracts=a.max_contracts).run()


if __name__ == "__main__":
    main()
