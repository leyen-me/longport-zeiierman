"""与 TradingView Data Window 导出 (BATS_SPY, 2.csv) 逐 bar 比对指标值。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from ztp.config import ZTPConfig
from ztp.longport_data import load_cache
from ztp.paths import reference_dir
from ztp import signals as zt, tvfuncs as tvf


def main():
    root = Path(__file__).resolve().parent.parent
    exp = pd.read_csv(reference_dir() / "tv_debug_bars_SPY_2026-09-14_18.csv")
    exp["dt"] = pd.to_datetime(exp["time"], unit="s", utc=True).dt.tz_convert(
        "America/New_York"
    )
    exp["hm"] = exp["dt"].dt.strftime("%Y-%m-%d %H:%M")
    print("TV导出范围:", exp["dt"].min(), "->", exp["dt"].max(), "bars:", len(exp))

    df = load_cache("SPY.US", "none")
    df["hm"] = df.index.strftime("%Y-%m-%d %H:%M")

    cfg = ZTPConfig()
    c = df["close"].to_numpy(float)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    sig = zt.compute(c, h, l, cfg)
    ema12 = tvf.ema(c, cfg.pullback_ema_len)
    atr14 = tvf.atr(h, l, c, cfg.atr_len)

    pos = {hm: i for i, hm in enumerate(df["hm"])}
    rows = []
    for _, r in exp.iterrows():
        i = pos.get(r["hm"])
        if i is None:
            continue
        rows.append(
            {
                "hm": r["hm"],
                "pz_our": sig["pz"][i], "pz_tv": r["pz"],
                "tz_our": sig["tz"][i], "tz_tv": r["tz"],
                "xup_our": int(sig["xup"][i]), "xup_tv": int(r["xup"]),
                "xdn_our": int(sig["xdn"][i]), "xdn_tv": int(r["xdn"]),
                "ema_our": ema12[i], "ema_tv": r["ema12"],
                "atr_our": atr14[i], "atr_tv": r["atr14"],
            }
        )
    m = pd.DataFrame(rows)
    print("比对bars:", len(m))
    for name in ["pz", "tz", "ema", "atr"]:
        d = (m[f"{name}_our"] - m[f"{name}_tv"]).abs()
        print(
            f"  {name:4s} max|diff|={d.max():.2e}  mean|diff|={d.mean():.2e}"
            f"  <1e-6: {(d < 1e-6).sum()}/{len(d)}"
        )
    xup_ok = (m["xup_our"] == m["xup_tv"]).sum()
    xdn_ok = (m["xdn_our"] == m["xdn_tv"]).sum()
    print(f"  xup 一致: {xup_ok}/{len(m)}   xdn 一致: {xdn_ok}/{len(m)}")
    print(f"  TV交叉次数: xup={m['xup_tv'].sum()} xdn={m['xdn_tv'].sum()}")
    bad = m[(m["xup_our"] != m["xup_tv"]) | (m["xdn_our"] != m["xdn_tv"])]
    if len(bad):
        print("不一致的bar:")
        print(bad[["hm", "pz_our", "pz_tv", "tz_our", "tz_tv"]].to_string(index=False))


if __name__ == "__main__":
    main()
