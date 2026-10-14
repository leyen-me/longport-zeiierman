"""检查大乐透是否中奖, 中奖则发 QQ 邮件通知。

用法:
  python3 scripts/check_lottery.py                 # 查最近一期
  python3 scripts/check_lottery.py --draw 26116    # 查指定期号
  python3 scripts/check_lottery.py --fake "09 18 22 25 35 01 10"  # 用合成号码测试判定
  python3 scripts/check_lottery.py --wait 40       # 等当天新开奖(每5分钟重试, 最多40分钟), 供定时任务用
  python3 scripts/check_lottery.py --no-email      # 只打印不发邮件
  python3 scripts/check_lottery.py --force         # 忽略"该期已处理"去重, 强制重新判断/发信
  python3 scripts/check_lottery.py --test-email    # 只发一封测试邮件, 验证配置

环境变量(QQ 邮箱, 见 .env): MSG_SMTP_USERNAME / MSG_SMTP_PASSWORD / QQ_NOTIFY_TO
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CST = timezone(timedelta(hours=8))
STATE_PATH = ROOT / "results" / "lottery_state.json"


def load_env(path: Path):
    """极简 .env 解析: KEY=VALUE, 支持 # 注释与引号, 不覆盖已有环境变量。"""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def today_cst() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d")


def fetch_until_today(fetch_latest, wait_minutes: int):
    """取最近一期; 若还没出当天新开奖, 每 5 分钟重试, 最多 wait_minutes 分钟。"""
    today = today_cst()
    deadline = datetime.now(CST) + timedelta(minutes=wait_minutes)
    attempt = 0
    while True:
        attempt += 1
        draw = fetch_latest()
        if draw.draw_time == today:
            return draw
        remaining = (deadline - datetime.now(CST)).total_seconds()
        if wait_minutes <= 0 or remaining <= 0:
            return draw
        print(f"[{attempt}] 最新一期还是 {draw.draw_num}({draw.draw_time}), 等当天开奖…", flush=True)
        time.sleep(min(300, remaining))


def main():
    p = argparse.ArgumentParser(description="超级大乐透对奖 + 中奖邮件通知")
    p.add_argument("--draw", help="指定期号 (默认最近一期)")
    p.add_argument("--fake", help="用合成开奖号测试判定, 格式: 前区5个 后区2个")
    p.add_argument("--wait", type=int, default=0, metavar="MIN",
                   help="等当天新开奖, 每5分钟重试, 最多等待这么多分钟 (定时任务用, 默认不等)")
    p.add_argument("--no-email", action="store_true", help="不发送邮件")
    p.add_argument("--force", action="store_true", help="忽略去重, 强制重新判断/发信")
    p.add_argument("--test-email", action="store_true", help="只发一封测试邮件")
    a = p.parse_args()

    load_env(ROOT / ".env")

    from ztp.lottery import (
        Draw, check_draw, fetch_by_draw_num, fetch_latest, format_report, send_mail,
        winning,
    )

    if a.test_email:
        ok = send_mail("【大乐透】邮件通知测试", "如果你收到这封邮件，说明 QQ 邮箱配置成功。")
        print("测试邮件已发送 ✅" if ok else "测试邮件发送失败 ❌ (检查 .env 的 MSG_SMTP_* 配置)")
        return

    if a.fake:
        nums = [int(x) for x in a.fake.replace("+", " ").split()]
        if len(nums) != 7:
            sys.exit("--fake 需要 7 个号码: 前区5个 + 后区2个")
        draw = Draw(draw_num="TEST", draw_time="(合成)", front=nums[:5], back=nums[5:7])
        try:  # 借用最近一期的奖金表, 方便预览金额
            draw.levels = fetch_latest().levels
        except Exception:
            pass
    elif a.draw:
        draw = fetch_by_draw_num(a.draw)
        if draw is None:
            sys.exit(f"未找到期号 {a.draw} 的开奖数据")
    else:
        draw = fetch_until_today(fetch_latest, a.wait)

    results = check_draw(draw)
    subject, body = format_report(draw, results)
    print(body)
    print()

    wins = winning(results)
    if a.no_email:
        print("[--no-email] 跳过邮件")
        return

    # 仅合成/指定期号测试时也发; 正常按期号去重, 同期只发一次。
    is_test = bool(a.fake)
    state = load_state()
    already = state.get("last_draw") == draw.draw_num

    if already and not a.force and not is_test:
        print(f"第{draw.draw_num}期已处理过, 跳过邮件 (用 --force 可重发)")
        return

    if wins or is_test:
        ok = send_mail(subject, body)
        print("邮件已发送 ✅" if ok else "邮件发送失败/未配置 ❌")
        if ok:
            state["last_draw"] = draw.draw_num
            state["last_time"] = datetime.now(CST).isoformat(timespec="seconds")
            save_state(state)
    else:
        print("未中奖, 不发邮件。")
        state["last_draw"] = draw.draw_num
        state["last_time"] = datetime.now(CST).isoformat(timespec="seconds")
        save_state(state)


if __name__ == "__main__":
    main()
