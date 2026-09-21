# 复刻验证报告

目标：Python 复刻 TradingView 策略 "Zeiierman Trend Pressure Strategy" (ZTP-S, 2分钟 SPY)，
达到与策略测试器逐笔一致。

## 验证方法与结果

### 1. 指标数学（决定性验证）

用 TradingView Data Window 导出的逐 bar 指标值（`tradingview/ZTP_debug_plots.pine`）比对。

方法：把 TV 导出的 OHLC 与长桥历史数据拼接成连续序列喂给 Python 引擎，
消除输入数据差异后对比输出。

| 日期 | pz 最大差 | tz 最大差 | ema12 | atr14 |
|---|---|---|---|---|
| 09-14（预热交接日） | 3.8e-01 | 1.5e-01 | 8.4e-04 | 3.9e-03 |
| 09-15 | 1.4e-14 | 2.0e-03 | 3.4e-13 | 3.2e-07 |
| 09-16 起稳态 | ~1e-14 | ~1e-14 | ~2e-13 | ~2e-16 |

09-15 之后达到浮点机器精度：**指标数学与 Pine 实现逐位一致**。
（交接日残差来自预热历史数据不同，2 天内衰减到零。）

同一数据下交叉信号（xup/xdn）**907/907 全部一致**。

### 2. 真实数据回测（长桥数据 vs TV 交易明细）

- 方向+入场时间一致：**187/206 (91%)**
- 对齐交易中整笔全字段一致：21 笔（受数据源价差影响的天花板）
- 全年 19 处结构性分歧，逐一排查均为数据边界翻转，典型证据：
  - `2025-09-23 10:52`：close−EMA12 = **+0.0017 美元**（0.17 美分决定回踩触发）
  - `2025-10-07 09:38`：pz−tz = **+0.09**（±100 刻度上的剃刀交叉）
- 根因：TV 图表数据为 BATS 源，长桥为另一数据源，约半数 bar 收盘价有
  ±0.01~0.13 的微小差异（`scripts/compare_indicators.py` 可复现）

### 3. 实盘决策逻辑一致性

`tests/test_live_consistency.py`：同一份历史数据，
回测引擎（批量）vs 实时状态机（逐根推送式喂入 + 盘中 TP/SL 模拟成交）。

结果：**31/31 交易逐笔完全一致**（方向/入场时间/入场价/出场时间/出场价/原因/持仓 bar 数）。

## 已复刻的 TradingView 撮合语义

- `process_orders_on_close=true`：信号 bar 收盘价成交市价单
- TP/SL 委托价四舍五入到 mintick 后挂出，下一根 bar 生效
- 盘中触发：跳空以开盘价成交；TP/SL 同 bar 可触发时按 bar 方向
  （阳线 O→L→H→C / 阴线 O→H→L→C）决定先后
- `entryAtr` 取开仓后第一根 bar 收盘的 ATR，持仓期间固定不漂移
- 回调入场：信号 bar 即可触发（Pine 中入场判断先于超时取消）
- `strategy.percent_of_equity`：qty = floor(权益×10%/成交价)
- 强制平仓：15:46 ET 那根 bar 的收盘价（7 笔 15:46 出场验证）

## 复刻脚本

- `scripts/compare_tv.py`：与 TV 交易明细 CSV 序列对齐逐笔比对
- `scripts/compare_indicators.py`：与 TV Data Window 导出逐 bar 比对指标
- `tests/test_live_consistency.py`：实盘决策 vs 回测引擎一致性
