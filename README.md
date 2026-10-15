# Zeiierman 日内策略复刻 + 长桥 0DTE 期权实盘

把 TradingView 日内策略逐字节复刻为 Python，并通过长桥 OpenAPI（websocket 推送驱动）交易 SPY 0DTE 期权。仓库含两套策略：

1. **ZTP**（Zeiierman Trend Pressure）— 见下
2. **ORB30 v2**（开盘区间突破，当前生产版）— 见「ORB30 v2」章节

> ⚠️ 免责声明：本项目仅供学习研究，不构成投资建议。期权交易风险极高，0DTE 尤甚。
> 原始指标 © [Zeiierman](https://www.zeiierman.com/)（CC BY-NC-SA 4.0），本项目为其个人用途复刻。

## 生产状态

- **当前实盘策略：ORB30 v2**（引擎 `ztp/orb.py` / 实盘 `ztp/orb_live.py` / Pine `tradingview/ORB30_v2.pine`，三处一致）
- 已对齐：本地方向引擎 ↔ 实盘状态机（`tests/test_orb_live_consistency.py`），且与 TradingView 导出的成交**逐笔吻合**（入场价一致到分）
- 参数被 `tests/test_orb_config.py` 锁定，误改即测试失败

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
├── ztp/                      # 核心包
│   ├── orb.py                # ★ ORB30 v2 回测引擎 (与 Pine 同语义)
│   ├── orb_live.py           # ★ ORB30 v2 实盘决策状态机
│   ├── orb_runner.py         # ★ ORB 实盘主循环 (推送驱动)
│   ├── options.py            # 0DTE 期权链/delta/选档/预算规则
│   ├── longport_data.py      # 长桥行情下载与缓存
│   ├── config.py / engine.py / live.py / runner.py / signals.py / tvfuncs.py
│   │                         # ZTP 策略 (另一套)
│   ├── logging.py / mailer.py / paths.py
│   ├── lottery.py            # 大乐透对奖 + 中奖判定 (复用 mailer 发 QQ 邮件)
├── scripts/                  # 回测/比对/实盘入口
│   ├── run_orb.py            # ORB v2 回测
│   ├── orb_live.py           # ORB v2 实盘入口 (dry-run 支持)
│   ├── compare_orb.py        # 与 TV 成交明细逐笔比对
│   ├── live_run.py / run_backtest.py / compare_tv.py   # ZTP 入口
│   ├── check_lottery.py      # 大乐透对奖入口 (中奖发邮件)
├── research/                 # 研究脚本 (非生产, 见 research/README.md)
├── tests/                    # 单元测试 (见下)
├── tradingview/              # Pine 脚本 (ORB30_v2.pine 为生产版本)
├── data/reference/           # TradingView 导出的参考数据
├── data_cache/               # K线缓存 (parquet, 运行时生成, gitignore)
└── results/                  # 日志/成交/状态 (运行时生成, gitignore)
```

## ORB30 v2（当前生产策略）

**逻辑**：开盘区间 15 分钟 → 连续 3 根 5min 收盘突破 → 顺势入场（收盘价成交）
→ 固定止损/止盈（锚 OR 边界 ∓1.1 / ±2.1）→ 美东 12:00 强平；每日最多 1 笔。
**过滤**：跳空方向过滤（关）、首根方向过滤（开）、首个突破锁定当日（开）、移动止损（关）。
交易日周一/二/三/五（周四无 edge）。

参数集中在 `ztp/orb.py:ORBConfig`，并由 `tests/test_orb_config.py` 锁定。

```bash
# 回测 (本地缓存 5min)
.venv/bin/python scripts/run_orb.py --symbol SPY.US --out results/orb_trades.csv
# 与 TradingView 成交明细逐笔比对
.venv/bin/python scripts/compare_orb.py --tv data/reference/ORB30_AMEX_SPY_2026-10-12.csv --ours results/orb_trades.csv

# 实盘 (模拟/真实取决于长桥凭证)
.venv/bin/python scripts/orb_live.py --dry-run --budget 300   # 只打印信号
.venv/bin/python scripts/orb_live.py --budget 300             # 真实下单
# 或: python -m ztp.orb_runner --dry-run
```

## 大乐透对奖

每期固定买 5 组号（见 `ztp/lottery.py` 的 `TICKETS`），脚本拉体彩官方开奖号比对，
中奖就通过 QQ 邮箱发通知。**独立实现**：不 import、不改动任何交易代码，但复用项目已有的
`MSG_SMTP_*` 邮箱配置。

```bash
.venv/bin/python scripts/check_lottery.py                    # 查最近一期, 中奖发邮件
.venv/bin/python scripts/check_lottery.py --draw 26116       # 查指定期号
.venv/bin/python scripts/check_lottery.py --fake "09 18 22 25 35 01 10"  # 合成号码测判定
.venv/bin/python scripts/check_lottery.py --wait 40          # 等当天新开奖(定时任务用)
.venv/bin/python scripts/check_lottery.py --test-email       # 验证邮箱配置
```

- **规则**：2026-02 起新规 7 奖级；追加投注仅对一、二等奖加成（追加单注 = 基本 ×80%），
  三～七等奖为固定奖，追加不加钱。
- **数据源**：官方 `webapi.sporttery.cn` 优先；该接口对**海外/机房 IP 地域封锁**（腾讯 WAF，HTTP 567），
  命中时自动回退 **500.com**（海外可访问，固定奖金额按奖池是否≥8亿推算）。若想强制走官方源，
  可设代理：`LOTTERY_HTTP_PROXY=http://user:pass@host:port`（或标准 `https_proxy`）。
- **邮箱配置**（`.env`）：`MSG_SMTP_USERNAME`（QQ 号@qq.com）、`MSG_SMTP_PASSWORD`（SMTP
  授权码，非登录密码）、`QQ_NOTIFY_TO`（收件箱，默认发件箱自身）。与交易信号通知同一套配置。
- **每期自动跑（Docker 单容器）**：大乐透每周一/三/六 21:25（北京时间）开奖，脚本带
  `--wait 40`（拉不到当天新开奖就每 5 分钟重试，最多 40 分钟），并按期号去重（同期只发一次）。
  容器只跑交易，彩票对奖由**宿主机 cron** 定期 `docker exec` 进容器触发，两者互不影响：
  ```cron
  # crontab -e  (CRON_TZ 让计划按北京时间走, 服务器在日本也不用换算)
  CRON_TZ=Asia/Shanghai
  # 22:00 北京时间 = 开奖后约 35 分钟; docker exec 会继承容器的 MSG_SMTP_* 环境变量
  0 22 * * 1,3,6 docker exec ztp-live python /app/scripts/check_lottery.py --wait 40 >> /var/log/dlt-lottery.log 2>&1
  ```
  前提：启动容器时把邮箱配置也传进去（见下方 `docker run` 示例里的 `MSG_SMTP_*`）。
  手动跑一次：`docker exec ztp-live python /app/scripts/check_lottery.py`。
  去重状态写在 `results/lottery_state.json`（已随 `results` 卷持久化）。

## 测试

无需 pytest，直接用内置 runner（每个测试文件也可单独运行）：

```bash
.venv/bin/python run_tests.py            # 全部
.venv/bin/python run_tests.py orb        # 只跑文件名含 orb 的
```

| 测试 | 内容 |
|---|---|
| `test_orb_engine.py` | 引擎语义：OR/突破确认/锚边界/过滤/锁当日/移动止损/EOD/取整 |
| `test_orb_config.py` | 锁定生产参数（默认可变更即失败） |
| `test_orb_live_consistency.py` | 状态机逐笔 == 引擎（合成 + 参考数据，含多组参数） |
| `test_option_sizing.py` | 期权定量规则 |
| `test_live_consistency.py` | ZTP 状态机 == ZTP 引擎 |
| `test_lottery.py` | 大乐透奖级判定 + 奖金计算（离线） |

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

**Docker / PaaS 部署（日本/任意服务器）：**

凭证通过环境变量注入（无需 .env 文件），容器首次启动会自动拉取近 120 天 K 线预热：

```bash
docker run -d --name ztp-live \
  -e LONGPORT_APP_KEY=xxx \
  -e LONGPORT_APP_SECRET=xxx \
  -e LONGPORT_ACCESS_TOKEN=xxx \
  -e MSG_SMTP_USERNAME=xxx@qq.com \
  -e MSG_SMTP_PASSWORD=xxx \
  -v $(pwd)/data_cache:/app/data_cache \
  -v $(pwd)/results:/app/results \
  --restart unless-stopped \
  $(docker build -q .)
docker logs -f ztp-live
```

> 大乐透对奖用的 `MSG_SMTP_*`（QQ 邮箱账号/授权码）也在上面一起 `-e` 注入；即使只跑交易，
> 加上它们也不会影响交易——只在大乐透脚本里被读取。

Zeabur / Railway 等 PaaS：直接导入 GitHub 仓库，在环境变量里配置长桥三项即可。

**启动策略由 `STRATEGY` 环境变量决定**（Dockerfile 已内置，默认 ORB30 v2）：
- `STRATEGY=ztp.orb_runner`（默认）→ ORB30 v2（5min）
- `STRATEGY=ztp.runner` → ZTP（2min）

> ⚠️ 若在 Zeabur 控制台里手填了 "Start Command"，它会**覆盖** Dockerfile 的 CMD——
> 上线 ORB 请把 Start Command 设为 `python -m ztp.orb_runner`（或留空用默认）。
> 启动日志出现 `[ORB] 服务启动` 且订阅 "5分钟K线" 才是对的。

> 系统层面持久化：写入 `/etc/environment`、systemd unit 的 `Environment=`，
> 或 `~/.zshrc` 均可——程序只认环境变量本身。

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
