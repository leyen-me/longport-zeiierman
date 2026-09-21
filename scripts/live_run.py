import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ztp.runner import LiveRunner


def main():
    p = argparse.ArgumentParser(description="ZTP 策略 SPY 0DTE 期权实盘/模拟盘")
    p.add_argument("--dry-run", action="store_true", help="只打印信号和意向订单, 不真实下单")
    p.add_argument("--budget", type=float, default=200.0, help="每笔期权预算(美元), 默认 200")
    p.add_argument("--delta", type=float, default=0.50, help="目标 delta, 默认 0.50")
    p.add_argument("--poll", type=float, default=2.0, help="轮询间隔秒数")
    a = p.parse_args()
    LiveRunner(dry_run=a.dry_run, budget=a.budget, target_delta=a.delta, poll=a.poll).run()


if __name__ == "__main__":
    main()
