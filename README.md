# ZTP — Zeiierman Trend Pressure 策略复刻与 0DTE 期权实盘

将 TradingView 上的 [Zeiierman Trend Pressure] 日内策略逐字节复刻为 Python：

1. **回测引擎**：精确复刻 TradingView 策略测试器的撮合语义（`process_orders_on_close`、tick 取整、盘中 stop/limit 路径假设、回调入场超时等）
2. **实盘执行**：通过长桥 OpenAPI（websocket 推送驱动）交易 SPY 0DTE 期权

> ⚠️ 免责声明：本项目仅供学习研究，不构成投资建议。期权交易风险极高，0DTE 尤甚。
> 原始指标 © [Zeiierman](https://www.zeiierman.com/)（CC BY-NC-SA 4.0），本项目为其个人用途复刻。

---

## 复刻验证结论

| 验证项 | 结果 |
|---|---|
| 指标数学（pz/tz/EMA/ATR） | 与 TradingView **浮点机器精度一致**（~1e-14） |
| 交叉信号（同一数据源） | 907/907 全部一致 |
| 交易结构（真实数据回测） | 91% 交易方向+入场时间一致 |
| 剩余分歧 | 全部归因于 BATS 与长桥数据源的分级价差（数据源差异，非逻辑差异） |
| 实盘决策 vs 回测引擎 | 31/31 逐笔一致（`tests/test_live_consistency.py`） |

详见 [docs/validation.md](docs/validation.md)。

## 目录结构

```
├── src布局见 ztp/            # 核心包
│   ├── config.py             # 策略参数（与 Pine 脚本一一对应）
│   ├── tvfuncs.py            # TradingView 内置函数精确复刻
│   ├── signals.py            # Z-Pulse / Z-Trend / 压力衰竭引擎
│   ├── engine.py             # TV 撮合语义回测引擎
│   ├── live.py               # 实时决策状态机（与回测同逻辑）
│   ├── options.py            # 0DTE 期权链/delta/选档/预算规则
│   ├── runner.py             # 实盘主循环（推送驱动）
│   ├── longport_data.py      # 长桥行情下载与缓存
│   ├── logging.py            # ET 时区日志
│   └── paths.py              # 数据/结果目录（环境变量可覆盖）
├── scripts/                  # 回测/比对/实盘入口
├── tests/                    # 一致性测试
├── tradingview/              # Pine 脚本（原策略 + 调试指标）
├── data/reference/           # TradingView 导出的参考数据
├── data_cache/               # K线缓存（parquet，运行时生成）
└── results/                  # 日志/成交/状态（运行时生成）
```

## 快速开始

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
cp .env.example .env   # 填入长桥 API 凭证
```

**下载数据 + 回测：**

```bash
.venv/bin/python scripts/download_data.py --start 2025-07-01 --end 2026-09-18 --adjust none
.venv/bin/python scripts/run_backtest.py
.venv/bin/python scripts/compare_tv.py        # 与 TradingView 交易明细逐笔比对
```

**实盘（模拟盘 / 真实盘取决于 API 凭证所属账户）：**

```bash
.venv/bin/python scripts/live_run.py --dry-run   # 只打印信号
.venv/bin/python scripts/live_run.py             # 真实下单
```

**Docker 部署（日本/任意服务器）：**

```bash
cp .env.example .env   # 填入凭证
docker compose up -d --build
docker logs -f ztp-live
```

## 策略参数（与 Pine 默认一致）

信号模式 Cross；时段 09:30–12:30 ET；每日最多 2 笔；回调 EMA12、最长等待 9 根；
ATR(14) 止盈 4.2× / 止损 1.5×（开仓后首根 bar 固定）；15:45–16:00 ET 强制平仓。
参数集中在 `ztp/config.py`。

## 期权执行规则

- 0DTE（当日到期），多头买 call / 空头买 put
- 选档：预算内选择 delta 最接近 0.50 的档（`--delta` 可调）
- 数量：`2×ask×100 ≤ 200` 买 2 张；否则 `ask×100 ≤ 200` 买 1 张；否则放弃（`--budget` 可调）
- TP/SL 按 SPY 现价触发（与回测同一套水平），限价卖出，5 秒未成交自动追价
- 15:47 ET 强平；重复/乱序推送有去重防护

## 时区说明

- 所有日志时间戳为**美东时间 (ET)**，与交易时段一致；部署服务器的本地时区无关紧要
- K 线时间戳在运行时自动探测进程本地时区并转换（长桥 SDK 行为），容器内固定 `TZ=UTC`
- 状态机/时段判断全部使用 ET；`data_cache`、`live_state.json` 中的日期均为 ET 日期

## 日志

- `results/ztp.log`（自动轮转 20MB×10）+ stdout，均为 ET 时间戳
- 成交记录：`results/live_trades.csv`
- 运行状态：`results/live_state.json`（断电/重启自动恢复）
