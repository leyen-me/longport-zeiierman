"""实盘/模拟盘 runner (websocket 推送驱动)。

- K 线: 订阅 SPY 2分钟线 confirmed 推送, bar 收盘即触发决策 (ztp.live.LiveStrategy)
- 报价: 订阅 SPY 逐笔报价推送, 持仓期间实时监控 TP/SL
- 兜底: RTH 期间 30 秒无推送自动轮询对账, 防止断推漏 bar
- 去重: 重复/乱序推送不会导致重复决策或重复下单
- 时区: 所有日志时间戳为美东时间 (ET), 与交易时段一致, 与部署服务器时区无关

用法:
  python -m ztp.runner             # 模拟盘真实下单
  python -m ztp.runner --dry-run   # 只打印不下单
"""

import json
import os
import queue
import time as time_mod
from datetime import datetime, timedelta
from decimal import Decimal

import pandas as pd

from .config import ZTPConfig
from .live import LiveStrategy, StrategyAction
from .logging import get_logger, setup_logging
from .longport_data import (
    ET_TZ,
    _config,
    bootstrap_history,
    candle_ts_to_et,
    load_cache,
)
from .options import pick_option, top_bid
from .paths import data_dir, results_dir

SYMBOL = "SPY.US"

log = get_logger()


def _now() -> datetime:
    return datetime.now(ET_TZ)


def _et_minutes(now: datetime) -> int:
    return now.hour * 60 + now.minute


class LiveRunner:
    def __init__(self, dry_run: bool = False, budget: float = 200.0,
                 target_delta: float = 0.50):
        os.environ.setdefault("LONGPORT_PUSH_CANDLESTICK_MODE", "confirmed")
        missing = [
            k for k in ("LONGPORT_APP_KEY", "LONGPORT_APP_SECRET", "LONGPORT_ACCESS_TOKEN")
            if not os.environ.get(k)
        ]
        if missing:
            raise SystemExit(
                f"缺少环境变量: {', '.join(missing)} "
                f"(Zeabur: 服务设置 -> 环境变量; docker: docker run -e ...)"
            )
        setup_logging(results_dir())
        self.cfg = ZTPConfig(symbol=SYMBOL)
        self.dry_run = dry_run
        self.budget = budget
        self.target_delta = target_delta
        self.openapi = __import__("longport").openapi
        self.qctx = self.openapi.QuoteContext(_config())
        self.tctx = None if dry_run else self.openapi.TradeContext(_config())
        self.strategy: LiveStrategy | None = None
        self.events: queue.Queue = queue.Queue()
        self.last_push_ts = 0.0
        self.last_reconcile = 0.0
        self.tail_path = data_dir() / "live_tail.parquet"
        self.state_path = results_dir() / "live_state.json"
        self.trades_path = results_dir() / "live_trades.csv"
        self.option_pos: dict | None = None
        self.entering = False
        self.exiting = False
        self._last_bar_ts: pd.Timestamp | None = None

    # ---------- 数据同步 ----------

    def _closed_bars_from(self, candles) -> pd.DataFrame:
        rows = []
        for c in candles:
            ts = candle_ts_to_et(c.timestamp)
            if ts + timedelta(minutes=2) <= _now():
                rows.append((ts, float(c.open), float(c.high), float(c.low), float(c.close)))
        if not rows:
            return pd.DataFrame(columns=["open", "high", "low", "close"])
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close"]).set_index("ts")
        df.index.name = None
        return df[~df.index.duplicated()].sort_index()

    def sync_history(self):
        try:
            base = load_cache(SYMBOL, "none")
        except FileNotFoundError:
            log.warning("本地无K线缓存, 首次启动自动拉取近120天历史预热")
            base = bootstrap_history(SYMBOL, days=120, adjust="none")
        if self.tail_path.exists():
            tail = pd.read_parquet(self.tail_path)
            base = pd.concat([base, tail]).loc[lambda d: ~d.index.duplicated()].sort_index()
        last = base.index.max()
        if last + timedelta(minutes=2) < _now():
            candles = self.qctx.history_candlesticks_by_date(
                SYMBOL, self.openapi.Period.Min_2, self.openapi.AdjustType.NoAdjust,
                start=last.date(), end=_now().date(),
                trade_sessions=self.openapi.TradeSessions.Intraday,
            )
            extra = self._closed_bars_from(candles)
            if len(extra):
                base = pd.concat([base, extra]).loc[lambda d: ~d.index.duplicated()].sort_index()
                log.info(f"补齐历史 {len(extra)} 根 bar -> {base.index.max()}")
        self.strategy = LiveStrategy(self.cfg, base)
        self._restore_state()
        self._save_tail()
        log.info(f"数据就绪: {len(base)} 根 bar, 最新收盘 {base.index.max()}")

    def _save_tail(self):
        self.strategy.df.to_parquet(self.tail_path)

    # ---------- 推送订阅 ----------

    def subscribe(self):
        self.qctx.set_on_quote(self._on_quote)
        self.qctx.set_on_candlestick(self._on_candlestick)
        self.qctx.subscribe([SYMBOL], [self.openapi.SubType.Quote])
        self.qctx.subscribe_candlesticks(
            SYMBOL, self.openapi.Period.Min_2,
            trade_sessions=self.openapi.TradeSessions.Intraday,
        )
        log.info("已订阅: SPY 报价推送 + 2分钟K线(confirmed)推送")

    def _on_quote(self, quote):
        self.last_push_ts = time_mod.time()
        self.events.put(("quote", quote))

    def _on_candlestick(self, candle):
        self.last_push_ts = time_mod.time()
        if candle.is_confirmed and candle.period == self.openapi.Period.Min_2:
            self.events.put(("bar", candle.candlestick))

    # ---------- 状态持久化 ----------

    def save_state(self):
        st = self.strategy.st
        data = {
            "date": str(st.last_date),
            "trades_today": st.trades_today,
            "pos_dir": st.pos_dir,
            "pos_qty": st.pos_qty,
            "entry_px": st.entry_px,
            "entry_i": st.entry_i,
            "entry_atr": st.entry_atr,
            "sl_px": st.sl_px if st.sl_px == st.sl_px else None,
            "tp_px": st.tp_px if st.tp_px == st.tp_px else None,
            "exit_armed": st.exit_armed,
            "exit_from": st.exit_from,
            "pend_long": st.pend_long,
            "pend_short": st.pend_short,
            "pl_bar": st.pl_bar,
            "ps_bar": st.ps_bar,
            "option_pos": self.option_pos,
        }
        self.state_path.write_text(json.dumps(data, default=str))

    def _restore_state(self):
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text())
        except Exception as e:
            log.warning(f"状态文件损坏, 忽略: {e}")
            return
        if data.get("date") != str(_now().date()):
            return
        st = self.strategy.st
        st.trades_today = data["trades_today"]
        st.pos_dir = data["pos_dir"]
        st.pos_qty = data["pos_qty"]
        st.entry_px = data["entry_px"]
        st.entry_i = data["entry_i"]
        st.entry_atr = data["entry_atr"]
        st.sl_px = data["sl_px"] if data["sl_px"] is not None else float("nan")
        st.tp_px = data["tp_px"] if data["tp_px"] is not None else float("nan")
        st.exit_armed = data["exit_armed"]
        st.exit_from = data["exit_from"]
        st.pend_long = data["pend_long"]
        st.pend_short = data["pend_short"]
        st.pl_bar = data["pl_bar"]
        st.ps_bar = data["ps_bar"]
        self.option_pos = data.get("option_pos")
        if st.pos_dir != 0:
            log.warning(
                f"恢复状态: 持仓 dir={st.pos_dir} qty={st.pos_qty} "
                f"TP={st.tp_px} SL={st.sl_px} 期权={self.option_pos.get('symbol') if self.option_pos else None}"
            )

    # ---------- 期权执行 ----------

    def _underlying_last(self) -> float:
        return float(self.qctx.quote([SYMBOL])[0].last_done)

    def _option_entry(self, direction: int, ref_price: float, bar_i: int):
        if self.entering or self.strategy.st.pos_dir == 0:
            return
        self.entering = True
        try:
            spot = self._underlying_last()
            is_call = direction > 0
            pick = pick_option(
                self.qctx, SYMBOL, is_call, spot,
                budget=self.budget, target_delta=self.target_delta,
            )
            if pick is None:
                log.warning(f"无满足预算({self.budget})/delta({self.target_delta})的期权, 放弃信号")
                self.strategy.st.pos_dir = 0
                self.strategy.st.entry_i = -1
                return
            log.info(
                f"信号 {'LONG' if is_call else 'SHORT'} -> 买 {pick.qty} 张 {pick.symbol} "
                f"(strike={pick.strike} delta={pick.delta:.2f} ask={pick.ask}) | SPY={spot}"
            )
            filled = self._submit_and_wait(pick.symbol, "Buy", pick.qty, pick.ask)
            if filled <= 0:
                log.warning("期权买入未成交, 回滚信号")
                self.strategy.st.pos_dir = 0
                self.strategy.st.entry_i = -1
                return
            self.strategy.st.pos_qty = filled
            self.option_pos = {
                "symbol": pick.symbol, "strike": pick.strike, "is_call": is_call,
                "qty": filled, "ask_ref": pick.ask, "spot_at_entry": spot,
                "signal_price": ref_price, "bar_i": bar_i,
                "time": _now().isoformat(),
            }
            self.save_state()
        except Exception as e:
            log.error(f"期权入场异常, 回滚信号: {e!r}", exc_info=True)
            self.strategy.st.pos_dir = 0
            self.strategy.st.entry_i = -1
        finally:
            self.entering = False

    def _order_status(self, order_id) -> tuple[str, int]:
        od = self.tctx.order_detail(order_id)
        return str(od.status), int(od.executed_quantity)

    def _submit_and_wait(self, osym: str, side: str, qty: int, ref_px: float,
                         aggressive: bool = False, poll_secs: int = 4,
                         wait_secs: int = 90) -> int:
        """挂限价单并等待, 超时撤单。返回累计成交张数(可能为部分成交)。"""
        px = Decimal(str(ref_px)).quantize(Decimal("0.01"))
        if aggressive:
            px = px - Decimal("0.05") if side == "Sell" else px + Decimal("0.05")
        order = self.tctx.submit_order(
            symbol=osym,
            order_type=self.openapi.OrderType.LO,
            side=self.openapi.OrderSide.Buy if side == "Buy" else self.openapi.OrderSide.Sell,
            submitted_quantity=Decimal(qty),
            time_in_force=self.openapi.TimeInForceType.Day,
            submitted_price=px,
            remark="ZTP",
        )
        log.info(f"下单 {side} {qty}x {osym} @ {px} (order_id={order.order_id})")
        deadline = time_mod.time() + wait_secs
        while time_mod.time() < deadline:
            time_mod.sleep(poll_secs)
            try:
                status, done = self._order_status(order.order_id)
            except Exception as e:
                log.warning(f"查询订单失败: {e}")
                continue
            if status.endswith("OrderStatus.Filled"):
                log.info(f"成交: {done} 张 @ ~{px}")
                return done
            if "Rejected" in status or "Expired" in status:
                log.warning(f"订单终结: {status}")
                return done
            if time_mod.time() > deadline - 30 and "Partial" not in status:
                try:
                    self.tctx.cancel_order(order.order_id)
                except Exception:
                    pass
        try:
            status, done = self._order_status(order.order_id)
            if "Canceled" not in status and "Filled" not in status:
                self.tctx.cancel_order(order.order_id)
                status, done = self._order_status(order.order_id)
        except Exception:
            pass
        return done

    def _exit_chase(self, osym: str, qty: int) -> int:
        """平仓追价循环: 卖出必须成交。

        每轮: 取最新 bid, 挂限价卖出; 12 秒未全部成交则撤单,
        价格每轮再降 0.05 (最多累计降 0.30, 兜底 0.01) 重新挂出。
        bid=0 (无买盘) 时直接挂 0.01。15:58 ET 后加速降档。
        """
        remaining = qty
        cycle = 0
        total_filled = 0
        while remaining > 0:
            if cycle > 100:
                log.error("追价超过100轮仍未全部成交, 剩余 %d 张, 请人工处理!", remaining)
                break
            m = _et_minutes(_now())
            if m >= 15 * 60 + 59 and cycle > 5:
                log.error("已到 15:59 仍有 %d 张未成交, 停止追价, 请人工处理!", remaining)
                break
            bid = top_bid(self.qctx, osym)
            chase = min(cycle, 6) * 0.05
            if m >= 15 * 60 + 58:
                chase += 0.10
            px = max(round((bid if bid > 0 else 0.05) - chase, 2), 0.01)
            log.info(f"追价平仓 第{cycle+1}轮: 卖 {remaining}x @ {px} (bid={bid})")
            try:
                done = self._submit_and_wait(
                    osym, "Sell", remaining, px, poll_secs=3, wait_secs=12
                )
            except Exception as e:
                log.error(f"追价下单异常: {e!r}")
                time_mod.sleep(3)
                cycle += 1
                continue
            if done > 0:
                remaining -= done
                total_filled += done
                cycle = 0
            else:
                cycle += 1
            time_mod.sleep(1)
        return total_filled

    def _option_exit(self, reason: str):
        op = self.option_pos
        if op is None:
            self.notify_exit(reason)
            return
        self.exiting = True
        try:
            if self.dry_run:
                bid = top_bid(self.qctx, op["symbol"])
                log.info(f"[DRY-RUN] 卖出 {op['qty']}x {op['symbol']} @ ~{bid} ({reason})")
                self.notify_exit(reason)
                return
            filled = self._exit_chase(op["symbol"], op["qty"])
            if filled < op["qty"]:
                log.error(
                    f"!! 平仓未完成: {filled}/{op['qty']} 张已成交, "
                    f"剩余请人工处理 !!"
                )
            if filled > 0:
                self.strategy.st.pos_qty = filled
                self.notify_exit(reason)
        except Exception as e:
            log.error(f"平仓异常: {e!r}", exc_info=True)
        finally:
            self.exiting = False

    def notify_exit(self, reason: str):
        spot = self._underlying_last()
        self.strategy.notify_exit_fill(spot, reason)
        op = self.option_pos
        if op and self.strategy.closed_trades:
            t = self.strategy.closed_trades[-1]
            t.update({k: op[k] for k in ("symbol", "strike", "is_call") if k in op})
            t["qty"] = op["qty"]
            t["time"] = _now().isoformat()
            self.record_trade(t)
        self.option_pos = None
        self.save_state()
        log.info(f"平仓完成 ({reason}) SPY={spot}")

    def record_trade(self, row: dict):
        import csv

        new = not self.trades_path.exists()
        with open(self.trades_path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(row.keys()))
            if new:
                w.writeheader()
            w.writerow(row)

    # ---------- 主循环 ----------

    def run(self):
        self.sync_history()
        self.subscribe()
        log.info(
            f"启动完成: dry_run={self.dry_run} 预算={self.budget} "
            f"目标delta={self.target_delta} 账户={'模拟' if not self.dry_run else 'N/A'}"
        )
        while True:
            try:
                self.pump()
            except KeyboardInterrupt:
                log.info("手动停止")
                break
            except Exception as e:
                log.error(f"主循环异常: {e!r}", exc_info=True)
                time_mod.sleep(3)

    def pump(self):
        while True:
            try:
                kind, payload = self.events.get(timeout=1.0)
            except queue.Empty:
                self.reconcile()
                continue
            if kind == "bar":
                self.handle_closed_bar(payload)
            elif kind == "quote":
                self.handle_quote(payload)

    def handle_closed_bar(self, c) -> None:
        ts = candle_ts_to_et(c.timestamp)
        if ts + timedelta(minutes=2) > _now():
            return
        row = {"open": float(c.open), "high": float(c.high),
               "low": float(c.low), "close": float(c.close)}
        self.process_closed_bar(ts, row)

    def process_closed_bar(self, ts, row) -> None:
        df = self.strategy.df
        if ts in df.index:
            return
        if self._last_bar_ts is not None and ts <= self._last_bar_ts:
            return
        if len(df) and ts < df.index[-1]:
            self.strategy.append_bar(ts, row["open"], row["high"], row["low"], row["close"])
            self._save_tail()
            log.info(f"补插乱序bar {ts} (仅更新数据, 不重复决策)")
            return
        self._last_bar_ts = ts
        self.strategy.append_bar(ts, row["open"], row["high"], row["low"], row["close"])
        self._save_tail()
        log.debug(f"bar 收盘 {ts} close={row['close']}")
        for act in self.strategy.on_bar():
            if act.kind in ("enter_long", "enter_short"):
                direction = 1 if act.kind == "enter_long" else -1
                self.save_state()
                self._option_entry(direction, act.price, act.bar_i)
            elif act.kind == "arm_tpsl":
                log.info(f"TP/SL 已布防: {act.text}")
                self.save_state()
            elif act.kind == "eod":
                if self.option_pos:
                    self._option_exit("EOD")
                else:
                    log.info(f"强平bar收盘 ({act.price})")

    def handle_quote(self, quote) -> None:
        st = self.strategy.st
        if st.pos_dir == 0 or not st.exit_armed or self.exiting:
            return
        if len(self.strategy.df) - 1 < st.exit_from:
            return
        px = float(quote.last_done)
        act = self.strategy.check_tpsl(px)
        if act is not None:
            reason = "TP" if act.kind == "exit_tp" else "SL"
            log.info(f"盘中触发 {reason} @ SPY={px} (TP={st.tp_px} SL={st.sl_px})")
            st.exit_armed = False
            self._option_exit(reason)

    def reconcile(self):
        now = time_mod.time()
        if now - self.last_reconcile < 30:
            return
        self.last_reconcile = now
        now_et = _now()
        if not (570 <= _et_minutes(now_et) < 960):
            st = self.strategy.st
            if st.pos_dir != 0 and not self.exiting and _et_minutes(now_et) >= 15 * 60 + 47:
                log.warning("强平窗口(RTH外兜底): 平掉期权持仓")
                self._option_exit("EOD")
            return
        if now - self.last_push_ts > 30:
            log.warning("30秒无推送, 主动轮询对账")
            candles = self.qctx.candlesticks(
                SYMBOL, self.openapi.Period.Min_2, 3,
                self.openapi.AdjustType.NoAdjust,
                trade_sessions=self.openapi.TradeSessions.Intraday,
            )
            closed = self._closed_bars_from(candles)
            for ts, row in closed.iterrows():
                self.process_closed_bar(ts, row)
            if self.strategy.st.pos_dir != 0:
                q = self.qctx.quote([SYMBOL])[0]
                self.handle_quote(q)


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="ZTP 策略 SPY 0DTE 期权 实盘/模拟盘")
    p.add_argument("--dry-run", action="store_true", help="只打印信号, 不真实下单")
    p.add_argument("--budget", type=float, default=200.0, help="每笔期权预算(美元)")
    p.add_argument("--delta", type=float, default=0.50, help="目标 delta")
    a = p.parse_args()
    LiveRunner(dry_run=a.dry_run, budget=a.budget, target_delta=a.delta).run()
