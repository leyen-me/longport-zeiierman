"""超级大乐透每期对奖 + 中奖邮件通知。

- 固定 5 组号码 (每期买的), 与官方开奖号比对, 判定奖级, 计算实得奖金。
- 开奖号来源: 中国体彩官方接口 webapi.sporttery.cn (gameNo=85); 该接口对海外/机房 IP
  地域封锁(HTTP 567), 故内置备用源 500.com (海外可访问) 自动回退。可用 LOTTERY_HTTP_PROXY
  或标准 https_proxy 走代理直连官方源。

规则 (2026-02 起新规, 9 奖级合并为 7 奖级):
  一等奖 5+2      浮动奖 (追加单注 = 基本 ×80%)
  二等奖 5+1      浮动奖 (追加单注 = 基本 ×80%)
  三等奖 5+0 / 4+2  固定奖 5000 (奖池≥8亿时 6666)
  四等奖 4+1      固定奖 300  (≥8亿 380)
  五等奖 4+0 / 3+2  固定奖 150  (≥8亿 200)
  六等奖 3+1 / 2+2  固定奖 15   (≥8亿 18)
  七等奖 3+0 / 2+1 / 1+2 / 0+2  固定奖 5 (≥8亿 7)
注: 追加投注仅参与浮动奖 (一、二等奖) 分配, 固定奖追加不加钱。

环境变量 (沿用项目已有的 QQ 邮箱配置, 与交易信号同一套):
  MSG_SMTP_USERNAME  发件邮箱 (如 123456@qq.com), 别名 QQ_SMTP_USER
  MSG_SMTP_PASSWORD  SMTP 授权码,             别名 QQ_SMTP_SECRET
  QQ_NOTIFY_TO       收件邮箱 (默认发件邮箱自身)
  ENABLE_EMAIL_MSG   true/false (默认 true)
"""

from __future__ import annotations

import json
import os
import re
import smtplib
import ssl
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

SMTP_HOST = "smtp.qq.com"
SMTP_PORT = 465

# 官方源: 中国体彩 webapi (字段最全, 但对海外/机房 IP 地域封锁返回 567)
SPORTTERY_URL = (
    "https://webapi.sporttery.cn/gateway/lottery/getHistoryPageListV1.qry"
    "?gameNo=85&provinceId=0&pageSize={page_size}&isVerify=1&pageNo={page_no}"
)
# 备用源: 500.com (海外可访问; 一等奖/二等奖金额取自页面, 固定奖按规则推算)
WUBAI_URL = "https://datachart.500.com/dlt/history/newinc/history.php?{q}"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# 每期固定购买的 5 组号码: (前区 5 个, 后区 2 个)
TICKETS: list[tuple[list[int], list[int]]] = [
    ([7, 13, 22, 32, 35], [8, 11]),
    ([3, 11, 19, 28, 33], [4, 9]),
    ([6, 14, 20, 27, 34], [2, 12]),
    ([5, 16, 21, 29, 31], [5, 10]),
    ([8, 15, 17, 24, 30], [3, 7]),
]

# 是否每组都买了追加 (追加仅对一、二等奖加成)
APPEND_BET = True

TIER_NAMES = ["一等奖", "二等奖", "三等奖", "四等奖", "五等奖", "六等奖", "七等奖"]


@dataclass
class Draw:
    draw_num: str          # 期号
    draw_time: str         # 开奖日期
    front: list[int]       # 开奖前区 (5)
    back: list[int]        # 开奖后区 (2)
    pool_after: str = ""   # 开奖后奖池余额
    levels: list[dict] = field(default_factory=list)  # prizeLevelList 原样


@dataclass
class TicketResult:
    index: int             # 第几组 (1-based)
    front: list[int]
    back: list[int]
    front_hits: int
    back_hits: int
    tier: int              # 0 = 未中奖, 1..7 = 奖级
    amount: int            # 实得金额 (基本+追加)


def _http_get(url: str, referer: str, timeout: int) -> str:
    """GET 文本。代理: 显式读 LOTTERY_HTTP_PROXY, 否则用标准 https_proxy 环境变量。"""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Referer": referer})
    proxy = (
        os.environ.get("LOTTERY_HTTP_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
    )
    handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else None)
    opener = urllib.request.build_opener(
        handler, urllib.request.HTTPSHandler(context=ssl.create_default_context())
    )
    with opener.open(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "ignore")


def _num(s) -> int | None:
    try:
        return int(str(s).replace(",", "").strip())
    except Exception:
        return None


def _fetch_sporttery(page_size: int, page_no: int, timeout: int) -> list[Draw]:
    """官方接口 (字段最全, 含全部奖级金额与追加)。海外可能被 567 拦截。"""
    txt = _http_get(
        SPORTTERY_URL.format(page_size=page_size, page_no=page_no),
        "https://www.lottery.gov.cn/",
        timeout,
    )
    data = json.loads(txt)
    if not data.get("success"):
        raise RuntimeError(f"官方接口失败: {data.get('errorMessage')}")
    draws: list[Draw] = []
    for item in data["value"]["list"]:
        nums = [int(x) for x in item["lotteryDrawResult"].split()]
        draws.append(
            Draw(
                draw_num=item["lotteryDrawNum"],
                draw_time=item.get("lotteryDrawTime", ""),
                front=nums[:5],
                back=nums[5:7],
                pool_after=item.get("poolBalanceAfterdraw", ""),
                levels=item.get("prizeLevelList", []),
            )
        )
    return draws


def _levels_from_500(amt1, amt2, pool_before: int | None) -> list[dict]:
    """用 500.com 的一/二等奖金 + 规则推算的固定奖, 合成与官方同构的 levels 表。"""
    hi = pool_before is not None and pool_before >= 800_000_000  # 奖池≥8亿, 固定奖上浮
    f3, f4, f5, f6, f7 = (6666, 380, 200, 18, 7) if hi else (5000, 300, 150, 15, 5)
    def fmt(v):
        return str(v) if v is not None else "-1"
    return [
        {"prizeLevel": "一等奖", "stakeAmountFormat": fmt(_num(amt1))},
        {"prizeLevel": "二等奖", "stakeAmountFormat": fmt(_num(amt2))},
        {"prizeLevel": "三等奖", "stakeAmountFormat": str(f3)},
        {"prizeLevel": "四等奖", "stakeAmountFormat": str(f4)},
        {"prizeLevel": "五等奖", "stakeAmountFormat": str(f5)},
        {"prizeLevel": "六等奖", "stakeAmountFormat": str(f6)},
        {"prizeLevel": "七等奖", "stakeAmountFormat": str(f7)},
    ]


def _fetch_500(limit: int = 30, timeout: int = 20, start=None, end=None) -> list[Draw]:
    """备用源 500.com。start/end 按期号区间查, 否则取最近 limit 期。"""
    if start is not None and end is not None:
        q = f"start={start}&end={end}"
    else:
        q = f"limit={max(limit + 1, 2)}&sort=0"  # 多取一行, 供算上一期奖池
    html = _http_get(WUBAI_URL.format(q=q), "https://datachart.500.com/", timeout)
    html = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    block = re.search(r'<tbody id="tdata">(.*?)</tbody>', html, flags=re.S)
    body = block.group(1) if block else html
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", body, flags=re.S):
        tds = [re.sub(r"<[^>]+>", "", t).strip()
               for t in re.findall(r"<td[^>]*>(.*?)</td>", tr, flags=re.S)]
        if len(tds) >= 15:
            rows.append(tds)

    draws: list[Draw] = []
    for i, tds in enumerate(rows):
        prev_pool = _num(rows[i + 1][8]) if i + 1 < len(rows) else None
        draws.append(
            Draw(
                draw_num=tds[0],
                draw_time=tds[14],
                front=[int(tds[j]) for j in range(1, 6)],
                back=[int(tds[j]) for j in range(6, 8)],
                pool_after=str(_num(tds[8]) or ""),
                levels=_levels_from_500(tds[10], tds[12], prev_pool),
            )
        )
    if start is not None and end is not None:
        return draws
    return draws[:limit]


def fetch_draws(page_size: int = 30, page_no: int = 1, timeout: int = 20) -> list[Draw]:
    """拉取最近的若干期开奖数据 (最近的在最前)。官方源优先, 被地域封锁时回退 500.com。"""
    try:
        return _fetch_sporttery(page_size, page_no, timeout)
    except Exception as e:
        if page_no == 1:
            try:
                return _fetch_500(limit=page_size, timeout=timeout)
            except Exception:
                pass
        raise RuntimeError(f"开奖数据源均不可用: {e}")


def fetch_latest(timeout: int = 20) -> Draw:
    """取最近一期开奖。"""
    draws = fetch_draws(page_size=1, timeout=timeout)
    if not draws:
        raise RuntimeError("未取到任何开奖数据")
    return draws[0]


def fetch_by_draw_num(draw_num: str, max_pages: int = 5, timeout: int = 20) -> Draw | None:
    """按期号精确查找 (找不到返回 None)。"""
    target = str(draw_num)
    try:
        for page in range(1, max_pages + 1):
            for d in _fetch_sporttery(30, page, timeout):
                if d.draw_num == target:
                    return d
        return None
    except Exception:
        try:
            n = int(target)
            draws = _fetch_500(timeout=timeout, start=n - 1, end=n)
        except Exception:
            return None
        for d in draws:
            if d.draw_num == target:
                return d
        return None


def judge_tier(front_hits: int, back_hits: int) -> int:
    """按新规 7 奖级判定, 返回 0(未中) 或 1..7。"""
    key = (front_hits, back_hits)
    if key == (5, 2):
        return 1
    if key == (5, 1):
        return 2
    if key in ((5, 0), (4, 2)):
        return 3
    if key == (4, 1):
        return 4
    if key in ((4, 0), (3, 2)):
        return 5
    if key in ((3, 1), (2, 2)):
        return 6
    if key in ((3, 0), (2, 1), (1, 2), (0, 2)):
        return 7
    return 0


def _level_amount(levels: list[dict], name: str) -> int | None:
    for lv in levels:
        if lv.get("prizeLevel") == name:
            fmt = lv.get("stakeAmountFormat")
            if fmt in (None, "", "-1"):
                return None
            return int(fmt)
    return None


def ticket_amount(tier: int, levels: list[dict], append: bool = APPEND_BET) -> int:
    """计算单注实得奖金。

    固定奖(三～七等)按官方当期金额; 浮动奖(一、二等) 追加单注 = 基本单注 ×80%
    (规则固定, 不依赖当期是否有追加中奖者)。
    """
    if tier == 0:
        return 0
    name = TIER_NAMES[tier - 1]
    basic = _level_amount(levels, name) or 0
    if append and tier in (1, 2):
        return basic + round(basic * 0.8)
    return basic


def check_draw(draw: Draw, append: bool = APPEND_BET) -> list[TicketResult]:
    """把 5 组固定号码对照某期开奖, 返回每组结果。"""
    front_set = set(draw.front)
    back_set = set(draw.back)
    results: list[TicketResult] = []
    for i, (front, back) in enumerate(TICKETS, start=1):
        fh = len(set(front) & front_set)
        bh = len(set(back) & back_set)
        tier = judge_tier(fh, bh)
        results.append(
            TicketResult(
                index=i,
                front=list(front),
                back=list(back),
                front_hits=fh,
                back_hits=bh,
                tier=tier,
                amount=ticket_amount(tier, draw.levels, append),
            )
        )
    return results


def winning(results: list[TicketResult]) -> list[TicketResult]:
    return [r for r in results if r.tier > 0]


def format_report(draw: Draw, results: list[TicketResult]) -> tuple[str, str]:
    """生成 (邮件主题, 邮件正文)。"""
    wins = winning(results)
    nums = " ".join(f"{n:02d}" for n in draw.front) + "  +  " + " ".join(f"{n:02d}" for n in draw.back)
    total = sum(r.amount for r in wins)

    if wins:
        subject = f"【大乐透中奖】第{draw.draw_num}期 中{len(wins)}注 合计{total}元"
    else:
        subject = f"大乐透第{draw.draw_num}期 未中奖"

    lines = [
        f"期号: {draw.draw_num}    开奖日期: {draw.draw_time}",
        f"开奖号码: {nums}",
        "",
    ]
    for r in results:
        f = " ".join(f"{n:02d}" for n in r.front)
        b = " ".join(f"{n:02d}" for n in r.back)
        if r.tier > 0:
            tag = f"✅ 中 {TIER_NAMES[r.tier - 1]}  实得 {r.amount} 元"
        else:
            tag = "—"
        lines.append(f"第{r.index}组  {f}  +  {b}   (中{r.front_hits}+{r.back_hits})  {tag}")
    lines.append("")
    if wins:
        lines.append(f"合计中奖 {len(wins)} 注, 奖金合计 {total} 元" + ("（含追加）" if APPEND_BET else ""))
    else:
        lines.append("本期未中奖，下期继续 💪")
    return subject, "\n".join(lines)


# ---------------------------------------------------------------------------
# 独立邮件发送 (自带配置与实现, 不依赖 ztp/mailer.py, 与交易链路完全隔离)
# ---------------------------------------------------------------------------

def mail_enabled() -> bool:
    flag = os.environ.get("ENABLE_EMAIL_MSG", "true").strip().lower()
    return bool(_smtp_user()) and bool(_smtp_secret()) and flag in ("1", "true", "yes", "on")


def _smtp_user() -> str:
    return os.environ.get("MSG_SMTP_USERNAME") or os.environ.get("QQ_SMTP_USER", "")


def _smtp_secret() -> str:
    return os.environ.get("MSG_SMTP_PASSWORD") or os.environ.get("QQ_SMTP_SECRET", "")


def send_mail(subject: str, body: str = "", timeout: int = 20) -> bool:
    """同步发送邮件。返回是否成功。配置缺失或失败返回 False, 不抛异常。"""
    user = _smtp_user()
    secret = _smtp_secret()
    to_addr = os.environ.get("QQ_NOTIFY_TO") or user
    if not (user and secret):
        return False

    msg = EmailMessage()
    msg["Subject"] = subject[:120]
    msg["From"] = user
    msg["To"] = to_addr
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(
        domain="qq.com", idstring=datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    )
    msg.set_content(body or subject, charset="utf-8")
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=timeout) as s:
            s.login(user, secret)
            s.send_message(msg)
        return True
    except Exception:
        return False
