"""长桥行情数据下载: SPY 2 分钟线 -> parquet 缓存 (美东时区索引)。

环境变量: LONGPORT_APP_KEY / LONGPORT_APP_SECRET / LONGPORT_ACCESS_TOKEN

时区说明: 长桥 SDK 返回的 K 线时间是"本地时区"的 naive datetime (跟随进程 TZ),
本模块统一转换为美东时间; 并在下载后做 RTH 覆盖率校验, 时区假设错误会立即报错。
"""

import os
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from .paths import data_dir

CACHE_DIR = data_dir()

ET = "America/New_York"
from zoneinfo import ZoneInfo as _ZI

ET_TZ = _ZI("America/New_York")


def local_tz() -> ZoneInfo:
    """进程本地时区 (SDK naive 时间戳的实际时区)。"""
    return datetime.now().astimezone().tzinfo


def candle_ts_to_et(naive_dt) -> pd.Timestamp:
    return (
        pd.Timestamp(naive_dt)
        .tz_localize(local_tz())
        .tz_convert(ET_TZ)
    )


def _config():
    from longport import openapi

    os.environ.setdefault("LONGPORT_HTTP_URL", "https://openapi.longportapp.com")
    os.environ.setdefault(
        "LONGPORT_QUOTE_WS_URL", "wss://openapi-quote.longportapp.com"
    )
    os.environ.setdefault(
        "LONGPORT_TRADE_WS_URL", "wss://openapi-trade.longportapp.com"
    )
    return openapi.Config.from_apikey_env()


def fetch_range(
    symbol: str,
    start: date,
    end: date,
    adjust: str = "forward",
    period: str = "min2",
) -> pd.DataFrame:
    from longport import openapi

    ctx = openapi.QuoteContext(_config())
    period_map = {
        "min1": openapi.Period.Min_1,
        "min2": openapi.Period.Min_2,
    }
    p = period_map[period]
    suffix = {"min1": "min1", "min2": "min2"}[period]
    adj = (
        openapi.AdjustType.ForwardAdjust
        if adjust == "forward"
        else openapi.AdjustType.NoAdjust
    )
    sessions = openapi.TradeSessions.Intraday

    rows = []
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=4), end)
        for attempt in range(5):
            try:
                candles = ctx.history_candlesticks_by_date(
                    symbol, p, adj, start=cur, end=chunk_end, trade_sessions=sessions
                )
                break
            except Exception as e:
                if attempt == 4:
                    raise
                wait = min(2**attempt * 2, 30)
                print(f"  retry {cur}~{chunk_end}: {e} -> sleep {wait}s")
                import time

                time.sleep(wait)
        for c in candles:
            rows.append(
                (
                    c.timestamp,
                    float(c.open),
                    float(c.high),
                    float(c.low),
                    float(c.close),
                    float(c.volume),
                )
            )
        import time

        time.sleep(1.0)
        cur = chunk_end + timedelta(days=1)

    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates(subset="ts").sort_values("ts")
    idx = pd.to_datetime(df["ts"]).dt.tz_localize(local_tz()).dt.tz_convert(ET_TZ)
    df.index = idx
    df = df.drop(columns="ts")

    # 只保留常规时段 09:30 <= t < 16:00; 若大量 bar 被过滤说明时区假设错了
    mins = df.index.hour * 60 + df.index.minute
    in_rth = (mins >= 570) & (mins < 960)
    if len(df) and in_rth.mean() < 0.9:
        raise RuntimeError(
            f"K线时区校验失败: 仅 {in_rth.mean()*100:.0f}% 的 bar 落在美东常规时段, "
            f"进程本地时区={local_tz()}, 请检查容器 TZ 设置"
        )
    df = df[in_rth]
    return df[["open", "high", "low", "close", "volume"]].astype(float)


def update_cache(symbol: str, start: date, end: date, adjust: str = "forward") -> pd.DataFrame:
    CACHE_DIR.mkdir(exist_ok=True)
    tag = adjust[:3]
    path = CACHE_DIR / f"{symbol.replace('.', '_')}_{tag}_min2.parquet"

    old = None
    if path.exists():
        old = pd.read_parquet(path)
        start = min(start, old.index.min().date())

    df = fetch_range(symbol, start, end, adjust)
    if old is not None and len(old):
        df = (
            pd.concat([old, df])
            .loc[lambda d: ~d.index.duplicated()]
            .sort_index()
        )
    df.to_parquet(path)
    print(f"cache: {path} bars={len(df)} range={df.index.min()} ~ {df.index.max()}")
    return df


def load_cache(symbol: str = "SPY.US", adjust: str = "forward") -> pd.DataFrame:
    tag = adjust[:3]
    path = CACHE_DIR / f"{symbol.replace('.', '_')}_{tag}_min2.parquet"
    if not path.exists():
        raise FileNotFoundError(path)
    return pd.read_parquet(path)


def cache_path(symbol: str = "SPY.US", adjust: str = "forward") -> Path:
    tag = adjust[:3]
    return CACHE_DIR / f"{symbol.replace('.', '_')}_{tag}_min2.parquet"


def bootstrap_history(symbol: str = "SPY.US", days: int = 120,
                      adjust: str = "none") -> pd.DataFrame:
    """本地无缓存时, 首次启动从长桥拉取近 N 天历史并写入缓存。"""
    end = datetime.now(ET_TZ).date()
    start = end - timedelta(days=days)
    print(f"[bootstrap] 本地无缓存, 拉取 {start} ~ {end} 的2分钟线 (约1分钟)...")
    df = fetch_range(symbol, start, end, adjust)
    if df.empty:
        raise RuntimeError("bootstrap 拉取到 0 根 bar, 请检查行情权限/网络")
    df.to_parquet(cache_path(symbol, adjust))
    print(f"[bootstrap] 完成: {len(df)} 根 bar -> {df.index.min()} ~ {df.index.max()}")
    return df


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="SPY.US")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--adjust", default="forward", choices=["forward", "none"])
    a = p.parse_args()
    update_cache(a.symbol, date.fromisoformat(a.start), date.fromisoformat(a.end), a.adjust)
