"""ORB30 实盘/模拟盘 runner (websocket 推送驱动)。

- K 线: 订阅 SPY 5分钟线 confirmed 推送, bar 收盘即触发决策 (ztp.orb_live.ORBLiveStrategy)
- 报价: 订阅 SPY 逐笔报价推送, 持仓期间实时监控 TP/SL (价位锚定开盘区间边界)
- 兜底: RTH 期间 30 秒无推送自动轮询对账; 到达强平时间强平
- 标的: SPY 0DTE 期权 (多头买 call / 空头买 put), 复用 ztp/options.py 选档/追价
- 时区: 所有日志时间戳为美东时间 (ET)

用法:
  python -m ztp.orb_runner             # 模拟盘/真实盘下单
  python -m ztp.orb_runner --dry-run   # 只打印不下单
"""

import csv
import json
import os
import queue
import time as time_mod
from datetime import date, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

import pandas as pd

from .logging import get_logger, setup_logging
from .longport_data import (
    ET_TZ,
    _config,
    bootstrap_history,
    candle_ts_to_et,
    load_cache,
)
from .mailer import Mailer
from .options import pick_option, top_bid
from .orb import ORBConfig
from .orb_live import ORBLiveStrategy
from .paths import data_dir, results_dir

SYMBOL = "SPY.US"

log = get_logger()


def _now() -> datetime:
    return datetime.now(ET_TZ)


def _et_minutes(now: datetime) -> int:
    return now.hour * 60 + now.minute


class ORBLiveRunner:
    def __init__(self, dry_run: bool = False, budget: float = 300.0,
                 target_delta: float = 0.50, max_contracts: int | None = None,
                 min_one: bool = True):
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
        self.cfg = ORBConfig(symbol=SYMBOL)
        self.dry_run = dry_run
        self.budget = budget
        self.target_delta = target_delta
        self.max_contracts = max_contracts
        self.min_one = min_one
        self.openapi = __import__("longport").openapi
        self.qctx = self.openapi.QuoteContext(_config())
        self.tctx = None if dry_run else self.openapi.TradeContext(_config())
        self.strategy: ORBLiveStrategy | None = None
        self.events: queue.Queue = queue.Queue()
        self.last_push_ts = 0.0
        self.last_reconcile = 0.0
        self.tail_path = data_dir() / "orb_tail_5m.parquet"
        self.state_path = results_dir() / "orb_live_state.json"
        self.trades_path = results_dir() / "orb_live_trades.csv"
        self.option_pos: dict | None = None
        self.entering = False
        self.exiting = False
        self._last_bar_ts: pd.Timestamp | None = None
        self.mailer = Mailer()
        self._summary_sent_date: str | None = None
        self._today_trades: list[dict] = []

    # ---------- 数据同步 ----------

    def _closed_bars_from(self, candles) -> pd.DataFrame:
        rows = []
        for c in candles:
            ts = candle_ts_to_et(c.timestamp)
            if ts + timedelta(minutes=5) <= _now():
                rows.append((ts, float(c.open), float(c.high), float(c.low), float(c.close)))
        if not rows:
            return pd.DataFrame(columns=["open", "high", "low", "close"])
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close"]).set_index("ts")
        df.index.name = None
        return df[~df.index.duplicated()].sort_index()

    def sync_history(self):
        try:
            base = load_cache(SYMBOL, "none", period="min5")
        except FileNotFoundError:
            log.warning("本地无5min缓存, 首次启动自动拉取近120天历史预热")
            base = bootstrap_history(SYMBOL, days=120, adjust="none", period="min5")
        if self.tail_path.exists():
            tail = pd.read_parquet(self.tail_path)
            base = pd.concat([base, tail]).loc[lambda d: ~d.index.duplicated()].sort_index()
        last = base.index.max()
        if last + timedelta(minutes=5) < _now():
            candles = self.qctx.history_candlesticks_by_date(
                SYMBOL, self.openapi.Period.Min_5, self.openapi.AdjustType.NoAdjust,
                start=last.date(), end=_now().date(),
                trade_sessions=self.openapi.TradeSessions.Intraday,
            )
            extra = self._closed_bars_from(candles)
            if len(extra):
                base = pd.concat([base, extra]).loc[lambda d: ~d.index.duplicated()].sort_index()
                log.info(f"补齐历史 {len(extra)} 根 bar -> {base.index.max()}")
        self.strategy = ORBLiveStrategy(self.cfg, base)
        self._restore_state()
        if self.strategy.st.pos_dir == 0:
            self.strategy.prime()
        self._save_tail()
        log.info(f"数据就绪: {len(base)} 根 5min bar, 最新收盘 {base.index.max()}")

    def _save_tail(self):
        self.strategy.df.to_parquet(self.tail_path)

    # ---------- 推送订阅 ----------

    def subscribe(self):
        self.qctx.set_on_quote(self._on_quote)
        self.qctx.set_on_candlestick(self._on_candlestick)
        self.qctx.subscribe([SYMBOL], [self.openapi.SubType.Quote])
        self.qctx.subscribe_candlesticks(
            SYMBOL, self.openapi.Period.Min_5,
            trade_sessions=self.openapi.TradeSessions.Intraday,
        )
        log.info("已订阅: SPY 报价推送 + 5分钟K线(confirmed)推送")

    def _on_quote(self, symbol, quote):
        self.last_push_ts = time_mod.time()
        self.events.put(("quote", quote))

    def _on_candlestick(self, symbol, candle):
        self.last_push_ts = time_mod.time()
        if candle.is_confirmed and candle.period == self.openapi.Period.Min_5:
            self.events.put(("bar", candle.candlestick))

    # ---------- 状态持久化 ----------

    def save_state(self):
        st = self.strategy.st
        data = {
            "date": str(_now().date()),
            "session_high": st.session_high if st.session_high == st.session_high else None,
            "session_low": st.session_low if st.session_low == st.session_low else None,
            "session_done": st.session_done,
            "traded_today": st.traded_today,
            "bull": st.bull, "bear": st.bear,
            "pos_dir": st.pos_dir, "pos_qty": st.pos_qty,
            "entry_px": st.entry_px, "entry_i": st.entry_i,
            "sl_px": st.sl_px if st.sl_px == st.sl_px else None,
            "tp_px": st.tp_px if st.tp_px == st.tp_px else None,
            "init_sl_px": st.init_sl_px if st.init_sl_px == st.init_sl_px else None,
            "R": st.R if st.R == st.R else None,
            "run_ext": st.run_ext if st.run_ext == st.run_ext else None,
            "day_open": st.day_open if st.day_open == st.day_open else None,
            "prev_close": st.prev_close if st.prev_close == st.prev_close else None,
            "first_dir": st.first_dir,
            "exit_armed": st.exit_armed, "exit_from": st.exit_from,
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
        st.session_high = data["session_high"] if data["session_high"] is not None else float("nan")
        st.session_low = data["session_low"] if data["session_low"] is not None else float("nan")
        st.session_done = data["session_done"]
        st.traded_today = data["traded_today"]
        st.bull, st.bear = data["bull"], data["bear"]
        st.pos_dir, st.pos_qty = data["pos_dir"], data["pos_qty"]
        st.entry_px, st.entry_i = data["entry_px"], data["entry_i"]
        st.sl_px = data["sl_px"] if data["sl_px"] is not None else float("nan")
        st.tp_px = data["tp_px"] if data["tp_px"] is not None else float("nan")
        st.init_sl_px = data.get("init_sl_px") if data.get("init_sl_px") is not None else float("nan")
        st.R = data.get("R") if data.get("R") is not None else float("nan")
        st.run_ext = data.get("run_ext") if data.get("run_ext") is not None else float("nan")
        st.day_open = data.get("day_open") if data.get("day_open") is not None else float("nan")
        st.prev_close = data.get("prev_close") if data.get("prev_close") is not None else float("nan")
        st.first_dir = data.get("first_dir", 0)
        st.exit_armed, st.exit_from = data["exit_armed"], data["exit_from"]
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
                use_full_budget=True, max_qty=self.max_contracts, min_one=self.min_one,
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
                self.mailer.send(
                    f"[ORB] 买入失败, 信号回滚 ({'LONG' if is_call else 'SHORT'})",
                    f"目标: {pick.qty}x {pick.symbol} @ {pick.ask}\nSPY={spot}\n"
                    f"时间={_now():%H:%M:%S} ET",
                )
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
            self.mailer.send(
                f"[ORB] 买入 {filled}x {pick.symbol} @ ~{pick.ask}",
                f"方向: {'LONG(call)' if is_call else 'SHORT(put)'}\n"
                f"行权价: {pick.strike}  delta={pick.delta:.2f}\n"
                f"SPY={spot}  信号价={ref_price}\n时间={_now():%H:%M:%S} ET",
            )
        except Exception as e:
            log.error(f"期权入场异常, 回滚信号: {e!r}", exc_info=True)
            self.strategy.st.pos_dir = 0
            self.strategy.st.entry_i = -1
        finally:
            self.entering = False

    def _order_status(self, order_id):
        od = self.tctx.order_detail(order_id)
        return od.status, int(od.executed_quantity)

    @staticmethod
    def _tick_align(px: Decimal, side: str) -> Decimal:
        tick = Decimal("0.05") if px >= Decimal("3") else Decimal("0.01")
        rounding = ROUND_CEILING if side == "Buy" else ROUND_FLOOR
        return ((px / tick).to_integral_value(rounding=rounding) * tick).quantize(Decimal("0.01"))

    def _submit_and_wait(self, osym: str, side: str, qty: int, ref_px: float,
                         aggressive: bool = False, poll_secs: int = 4,
                         wait_secs: int = 90) -> int:
        px = Decimal(str(ref_px))
        if aggressive:
            px = px - Decimal("0.05") if side == "Sell" else px + Decimal("0.05")
        px = self._tick_align(px, side)
        OS = self.openapi.OrderStatus
        order = self.tctx.submit_order(
            symbol=osym,
            order_type=self.openapi.OrderType.LO,
            side=self.openapi.OrderSide.Buy if side == "Buy" else self.openapi.OrderSide.Sell,
            submitted_quantity=Decimal(qty),
            time_in_force=self.openapi.TimeInForceType.Day,
            submitted_price=px,
            remark="ORB",
        )
        log.info(f"下单 {side} {qty}x {osym} @ {px} (order_id={order.order_id})")
        done = 0
        deadline = time_mod.time() + wait_secs
        while time_mod.time() < deadline:
            time_mod.sleep(poll_secs)
            try:
                status, done = self._order_status(order.order_id)
            except Exception as e:
                log.warning(f"查询订单失败: {e}")
                continue
            if status == OS.Filled:
                log.info(f"成交: {done} 张 @ ~{px}")
                return done
            if status in (OS.Rejected, OS.Expired):
                log.warning(f"订单终结: {status}")
                return done
            if time_mod.time() > deadline - 30 and status not in (
                OS.PartialFilled, OS.PartialWithdrawal
            ):
                try:
                    self.tctx.cancel_order(order.order_id)
                except Exception:
                    pass
        try:
            status, done = self._order_status(order.order_id)
            if status not in (OS.Canceled, OS.Filled):
                self.tctx.cancel_order(order.order_id)
                status, done = self._order_status(order.order_id)
        except Exception:
            pass
        return done

    def _exit_chase(self, osym: str, qty: int) -> tuple[int, float | None]:
        remaining = qty
        cycle = 0
        total_filled = 0
        last_px: float | None = None
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
                done = self._submit_and_wait(osym, "Sell", remaining, px,
                                             poll_secs=3, wait_secs=12)
            except Exception as e:
                log.error(f"追价下单异常: {e!r}")
                time_mod.sleep(3)
                cycle += 1
                continue
            if done > 0:
                remaining -= done
                total_filled += done
                last_px = px
                cycle = 0
            else:
                cycle += 1
            time_mod.sleep(1)
        return total_filled, last_px

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
                self.notify_exit(reason, est_exit_px_ref=bid)
                return
            filled, last_px = self._exit_chase(op["symbol"], op["qty"])
            if filled < op["qty"]:
                log.error(f"!! 平仓未完成: {filled}/{op['qty']} 张已成交, 剩余请人工处理 !!")
                self.mailer.send(
                    f"[ORB][紧急] 平仓未完成 {filled}/{op['qty']} 张!",
                    f"{op['symbol']} 原因={reason}\n请立即人工处理!\n时间={_now():%H:%M:%S} ET",
                )
            if filled > 0:
                self.strategy.st.pos_qty = filled
                self.notify_exit(reason, est_exit_px_ref=last_px)
        except Exception as e:
            log.error(f"平仓异常: {e!r}", exc_info=True)
        finally:
            self.exiting = False

    def notify_exit(self, reason: str, est_exit_px_ref: float | None = None):
        spot = self._underlying_last()
        op = self.option_pos
        exit_px = op.get("signal_price", spot) if op else spot
        self.strategy.notify_exit_fill(spot, reason)
        if op:
            t = {
                "direction": "LONG" if op["is_call"] else "SHORT",
                "entry_time": op["time"], "exit_time": _now().isoformat(),
                "symbol": op["symbol"], "strike": op["strike"],
                "qty": op["qty"], "ask_ref": op["ask_ref"],
                "exit_ref": est_exit_px_ref,
                "spot_entry": op["spot_at_entry"], "spot_exit": spot,
                "exit_reason": reason,
            }
            self.record_trade(t)
            self._today_trades.append(t)
            spot_in = float(op["spot_at_entry"])
            spot_chg = (spot / spot_in - 1) * 100 if spot_in else 0.0
            est_pnl = ""
            if est_exit_px_ref:
                est = (est_exit_px_ref - float(op["ask_ref"])) * 100 * op["qty"]
                est_pnl = f"期权盈亏(估): {est:+.0f}$ (卖~{est_exit_px_ref} - 买~{op['ask_ref']})\n"
            self.mailer.send(
                f"[ORB] 平仓({reason}) SPY={spot}",
                f"{op['qty']}x {op['symbol']} (strike={op['strike']})\n"
                f"SPY: {spot_in} -> {spot} ({spot_chg:+.2f}%)\n{est_pnl}"
                f"时间={_now():%H:%M:%S} ET",
            )
        self.option_pos = None
        self.save_state()
        log.info(f"平仓完成 ({reason}) SPY={spot}")

    def record_trade(self, row: dict):
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
            f"目标delta={self.target_delta} "
            f"至少1张(不限价){'/最多%s张' % self.max_contracts if self.max_contracts else ''} "
            f"OR={self.cfg.or_minutes}min "
            f"SL/TP={self.cfg.sl_points}/{self.cfg.tp_points}点 强平={self.cfg.eod_hour}:{self.cfg.eod_minute:02d}"
        )
        self.mailer.send(
            f"[ORB] 服务启动 {_now():%m-%d %H:%M} ET",
            f"dry_run={self.dry_run}\n预算=${self.budget:.0f} 目标delta={self.target_delta}\n"
            f"缓存bar数={len(self.strategy.df)}\n最新收盘={self.strategy.df.index.max()}",
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
        if ts + timedelta(minutes=5) > _now():
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
                log.info(
                    f"信号 {act.kind} @ SPY={act.price} 区间HH/LL="
                    f"{self.strategy.st.session_high}/{self.strategy.st.session_low} "
                    f"bull={self.strategy.st.bull} bear={self.strategy.st.bear}"
                )
                self._option_entry(direction, act.price, act.bar_index)
                if self.strategy.st.pos_dir != 0 and self.option_pos is None:
                    log.warning("入场未建立期权持仓, 回滚信号")
                    self.strategy.st.pos_dir = 0
                    self.strategy.st.entry_i = -1
                self.save_state()
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
        m = _et_minutes(now_et)
        if m < 9 * 60 + 25:
            if self._summary_sent_date and self._summary_sent_date != now_et.date().isoformat():
                self._today_trades = []
                self._summary_sent_date = None
            return
        self._maybe_daily_summary(now_et)
        if not (9 * 60 + 25 <= m < 16 * 60):
            return
        # 过了强平时间仍持仓 -> 兜底强平
        if self.strategy.st.pos_dir != 0 and not self.exiting and m >= (
            self.cfg.eod_hour * 60 + self.cfg.eod_minute
        ):
            log.warning("强平窗口兜底: 平掉期权持仓")
            self._option_exit("EOD")
            return
        if now - self.last_push_ts > 30:
            log.warning("30秒无推送, 主动轮询对账")
            candles = self.qctx.candlesticks(
                SYMBOL, self.openapi.Period.Min_5, 3,
                self.openapi.AdjustType.NoAdjust,
                trade_sessions=self.openapi.TradeSessions.Intraday,
            )
            closed = self._closed_bars_from(candles)
            for ts, row in closed.iterrows():
                self.process_closed_bar(ts, row)
            if self.strategy.st.pos_dir != 0:
                q = self.qctx.quote([SYMBOL])[0]
                self.handle_quote(q)

    def _maybe_daily_summary(self, now_et: datetime):
        today = now_et.date().isoformat()
        if self._summary_sent_date == today:
            return
        m = _et_minutes(now_et)
        eod_m = self.cfg.eod_hour * 60 + self.cfg.eod_minute
        if m < eod_m + 5 or self.strategy.st.pos_dir != 0 or self.exiting:
            return
        self._summary_sent_date = today
        if not self._today_trades:
            self.mailer.send(f"[ORB] 当日总结 {now_et:%Y-%m-%d}", "今日无交易。\n(服务正常运行)")
            return
        lines = []
        for t in self._today_trades:
            chg = ""
            if t.get("spot_entry"):
                chg = f" (SPY {t['spot_entry']}->{t['spot_exit']})"
            lines.append(
                f"{t['direction']} {t['qty']}x {t['symbol']} [{t['exit_reason']}]{chg}"
            )
        self.mailer.send(
            f"[ORB] 当日总结 {now_et:%Y-%m-%d}: {len(self._today_trades)} 笔",
            "\n".join(lines) + f"\n\n时间={_now():%H:%M:%S} ET",
        )


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="ORB30 策略 SPY 0DTE 期权 实盘/模拟盘")
    p.add_argument("--dry-run", action="store_true", help="只打印信号, 不真实下单")
    p.add_argument("--budget", type=float, default=300.0, help="单笔期权预算/最大风险(美元), 默认 300")
    p.add_argument("--delta", type=float, default=0.50, help="目标 delta")
    p.add_argument("--max-contracts", type=int, default=None,
                   help="单笔最大张数上限 (小资金防超配, 如 1)")
    a = p.parse_args()
    ORBLiveRunner(dry_run=a.dry_run, budget=a.budget, target_delta=a.delta,
                  max_contracts=a.max_contracts).run()
