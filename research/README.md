# research/ — 研究脚本（非生产）

这里是从 ORB30 优化过程中沉淀的**探索性**脚本，**不参与实盘**，仅供复现研究。
生产逻辑全部在 `ztp/orb.py`（引擎）与 `ztp/orb_live.py`（实盘状态机）。

| 文件 | 用途 |
|---|---|
| `orb_research.py` | 事件抽取 + 基础模拟器（6 年 SPY 5min） |
| `orb_lab.py` | 批量评估台：入场/出场/过滤变体 + IS/OOS 指标 |
| `orb_exits.py` | 出场研究（固定/ATR/移动/保本/时间） |
| `orb_variants.py` / `orb_variants2.py` | 止损后反向、斐波那契回调入场 |
| `orb_combo_study.py` | 因子组合 + IS/OOS + bootstrap |
| `orb_0dte.py` | 0DTE 期权叠加（BS 定价 + 点差 + theta） |
| `tv_exports/` | TradingView 导出的参考数据/成交明细 |

运行示例（在仓库根目录）：

```bash
.venv/bin/python research/orb_lab.py
```

> 结论摘要见 `results/orb_research_report.md`（若存在）。研究结论可能与
> 当前生产参数不同——生产参数以 `tests/test_orb_config.py` 锁定的为准。
