"""Zeiierman Trend Pressure 信号引擎, 与 Pine 脚本逐行对应。

输出:
  pz        Z-Pulse (平滑压力)
  tz        Z-Trend (自适应平滑趋势)
  xup/xdn   pz 上穿/下穿 tz
  us/ls     上/下压力衰竭开始
  ubk/lbk   上/下压力衰竭解除
  long_signal / short_signal 按 signal_mode 输出
"""

import numpy as np

from .config import ZTPConfig
from . import tvfuncs as tv


def _williams_range(high, low, close, k, mintick):
    hh = tv.highest(high, k)
    ll = tv.lowest(low, k)
    return 100.0 * (close - hh) / np.maximum(hh - ll, mintick)


def _stoch(x, k):
    hi = tv.highest(x, k)
    lo = tv.lowest(x, k)
    return -100.0 + 100.0 * (x - lo) / np.maximum(hi - lo, 1e-10)


def _efficiency(close, k, mintick):
    d = np.full(len(close), np.nan)
    d[1:] = np.abs(np.diff(close))
    p = tv.sma(d, k) * k
    prev = np.full(len(close), np.nan)
    prev[k:] = close[:-k]
    n = np.abs(close - prev)
    with np.errstate(invalid="ignore"):
        return tv.clip(n / np.maximum(p, mintick), 0.0, 1.0)


def compute(close, high, low, cfg: ZTPConfig):
    n = len(close)
    mintick = cfg.mintick

    # --- Z-Pulse (Pine L125-132) ---
    rz_f = _williams_range(high, low, close, cfg.pulse_range, mintick)
    rz_s = _williams_range(high, low, close, cfg.trend_range, mintick)
    rz_m = _williams_range(high, low, close, cfg.macro_trend, mintick)

    rp = _stoch(rz_f, cfg.pulse_stoch)
    pr = tv.clip(rz_f * 0.72 + rp * 0.28, -100.0, 0.0)
    pz = tv.ema(pr, cfg.pulse_smooth)

    # --- Z-Trend (Pine L134-151) ---
    fw = max(0.0, 1.0 - cfg.sw - cfg.mw)
    ws0 = fw + cfg.sw + cfg.mw
    cn = tv.clip((rz_f * fw + rz_s * cfg.sw + rz_m * cfg.mw) / ws0, -100.0, 0.0)
    ag = 1.0 - tv.clip(
        np.abs(rz_f - rz_s) + np.abs(rz_s - rz_m) + np.abs(rz_f - rz_m), 0.0, 300.0
    ) / 300.0
    ef = np.nan_to_num(_efficiency(close, cfg.trend_range, mintick), nan=0.0)

    ud = np.zeros(n)
    dd = np.zeros(n)
    for i in range(n):
        up_prev = ud[i - 1] if i > 0 else 0.0
        dn_prev = dd[i - 1] if i > 0 else 0.0
        with np.errstate(invalid="ignore"):
            ud[i] = min(1.0, up_prev * 0.93 + 0.07) if cn[i] > -30.0 else up_prev * 0.94
            dd[i] = min(1.0, dn_prev * 0.93 + 0.07) if cn[i] < -70.0 else dn_prev * 0.94

    tg0 = tv.clip(cn + cfg.trend_persistence * (ud - dd), -100.0, 0.0)
    tg = tv.ema(tg0, 2)

    ba = 2.0 / (cfg.trend_smooth + 1.0)
    tz = np.full(n, np.nan)
    for i in range(n):
        if np.isnan(tg[i]):
            continue
        prev = tz[i - 1] if i > 0 else np.nan
        if np.isnan(prev):
            tz[i] = tg[i]
            continue
        mac = rz_m[i]
        side = 1 if mac > -50.0 else (-1 if mac < -50.0 else 0)
        pull = (side == 1 and tg[i] < prev) or (side == -1 and tg[i] > prev)
        lk = min(0.97, max(0.0, 0.15 + ag[i] * ef[i] * cfg.th))
        pa = max(0.015, ba * (1.0 - lk)) if pull else max(ba * 0.75, 0.03)
        tz[i] = tv.clip_scalar(prev + pa * (tg[i] - prev), -100.0, 0.0)

    # --- 压力衰竭 (Pine L153-189) ---
    d = float(cfg.sensitivity - 5)
    ez = min(42.0, max(8.0, float(cfg.exhaustion_zone) - d * 4.0))
    rb = min(18.0, max(1.0, 8.0 + d * 1.75))
    cf = 1 if cfg.sensitivity <= 5 else (2 if cfg.sensitivity <= 7 else (3 if cfg.sensitivity <= 9 else 4))
    fast_exit = cfg.sensitivity <= 2

    up = -ez
    dn = -100.0 + ez
    ur = up - rb
    lr = dn + rb

    with np.errstate(invalid="ignore"):
        uc = (pz >= up) & (tz >= up)
        lc = (pz <= dn) & (tz <= dn)
        ux = ((pz < ur) | (tz < ur)) if fast_exit else ((pz < ur) & (tz < ur))
        lx = ((pz > lr) | (tz > lr)) if fast_exit else ((pz > lr) & (tz > lr))
        uc &= ~np.isnan(pz) & ~np.isnan(tz)
        lc &= ~np.isnan(pz) & ~np.isnan(tz)
        ux &= ~np.isnan(pz) & ~np.isnan(tz)
        lx &= ~np.isnan(pz) & ~np.isnan(tz)

    est = 0
    ucn = 0
    lcn = 0
    us = np.zeros(n, dtype=bool)
    ls = np.zeros(n, dtype=bool)
    ubk = np.zeros(n, dtype=bool)
    lbk = np.zeros(n, dtype=bool)
    for i in range(n):
        ucn = ucn + 1 if uc[i] else 0
        lcn = lcn + 1 if lc[i] else 0
        prev = est
        if est == 0:
            if ucn >= cf:
                est = 1
            elif lcn >= cf:
                est = -1
        elif est == 1 and ux[i]:
            est = 0
        elif est == -1 and lx[i]:
            est = 0
        us[i] = est == 1 and prev != 1
        ls[i] = est == -1 and prev != -1
        ubk[i] = est == 0 and prev == 1
        lbk[i] = est == 0 and prev == -1

    xup = tv.crossover(pz, tz)
    xdn = tv.crossunder(pz, tz)

    if cfg.signal_mode == "Exhaustion":
        long_signal, short_signal = ls, us
    elif cfg.signal_mode == "Release":
        long_signal, short_signal = lbk, ubk
    else:
        long_signal, short_signal = xup, xdn

    return {
        "pz": pz,
        "tz": tz,
        "xup": xup,
        "xdn": xdn,
        "us": us,
        "ls": ls,
        "ubk": ubk,
        "lbk": lbk,
        "long_signal": long_signal,
        "short_signal": short_signal,
    }
