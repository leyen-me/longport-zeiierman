"""TradingView Pine 内置函数的精确复刻。

关键语义:
- ta.highest/ta.lowest/ta.sma: 窗口含当前 bar, 数据不足或窗口含 NaN 时返回 NaN
- ta.ema: alpha=2/(n+1), 用窗口 SMA 作为种子 (Pine 参考手册定义), 种子前为 NaN
- ta.rma: alpha=1/n, 同样以 SMA 作种子 (ta.atr 基于此)
- ta.crossover(a,b): a[i]>b[i] 且 a[i-1]<=b[i-1], 首根 bar 恒为 False
"""

import numpy as np
import pandas as pd


def highest(x: np.ndarray, n: int) -> np.ndarray:
    return pd.Series(x).rolling(n, min_periods=n).max().to_numpy()


def lowest(x: np.ndarray, n: int) -> np.ndarray:
    return pd.Series(x).rolling(n, min_periods=n).min().to_numpy()


def sma(x: np.ndarray, n: int) -> np.ndarray:
    return pd.Series(x).rolling(n, min_periods=n).mean().to_numpy()


def _recursive(x: np.ndarray, n: int, alpha: float) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    out = np.full(len(x), np.nan)
    valid = ~np.isnan(x)
    csum = np.cumsum(valid)
    seed = -1
    for i in range(n - 1, len(x)):
        prev = csum[i - n] if i >= n else 0
        if valid[i] and csum[i] - prev == n:
            seed = i
            break
    if seed < 0:
        return out
    out[seed] = x[seed - n + 1 : seed + 1].mean()
    for i in range(seed + 1, len(x)):
        out[i] = alpha * x[i] + (1.0 - alpha) * out[i - 1]
    return out


def ema(x: np.ndarray, n: int) -> np.ndarray:
    return _recursive(x, n, 2.0 / (n + 1.0))


def rma(x: np.ndarray, n: int) -> np.ndarray:
    return _recursive(x, n, 1.0 / n)


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int) -> np.ndarray:
    tr = np.full(len(close), np.nan)
    tr[0] = high[0] - low[0]
    pc = np.roll(close, 1)
    tr[1:] = np.maximum.reduce(
        [
            high[1:] - low[1:],
            np.abs(high[1:] - pc[1:]),
            np.abs(low[1:] - pc[1:]),
        ]
    )
    return rma(tr, n)


def crossover(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    out = np.zeros(len(a), dtype=bool)
    if len(a) < 2:
        return out
    pa, pb = a[:-1], b[:-1]
    out[1:] = (a[1:] > b[1:]) & (pa <= pb)
    out[~np.isfinite(a) | ~np.isfinite(b)] = False
    out[1:][~np.isfinite(pa) | ~np.isfinite(pb)] = False
    return out


def crossunder(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return crossover(-a, -b)


def clip(v: np.ndarray, lo: float, hi: float) -> np.ndarray:
    return np.clip(v, lo, hi)


def clip_scalar(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))
