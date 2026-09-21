"""SPY 0DTE 期权选择: 到期日/期权链/BS delta/选档/预算数量规则。"""

import math
from dataclasses import dataclass
from datetime import date, datetime, time as dtime

from .longport_data import ET_TZ as ET


def norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def bs_delta(spot: float, strike: float, years: float, iv: float, is_call: bool,
             r: float = 0.04, q: float = 0.012) -> float:
    if years <= 0 or iv <= 0:
        if is_call:
            return 1.0 if spot > strike else 0.0
        return -1.0 if spot < strike else 0.0
    sqrt_t = math.sqrt(years)
    d1 = (
        math.log(spot / strike) + (r - q + 0.5 * iv * iv) * years
    ) / (iv * sqrt_t)
    if is_call:
        return math.exp(-q * years) * norm_cdf(d1)
    return math.exp(-q * years) * (norm_cdf(d1) - 1.0)


def expiry_deadline(expiry: date) -> datetime:
    return datetime.combine(expiry, dtime(16, 0), tzinfo=ET)


def pick_expiry(qctx, symbol: str, now: datetime) -> date:
    exps = [d for d in qctx.option_chain_expiry_date_list(symbol) if d >= now.date()]
    if not exps:
        raise RuntimeError("无可用的期权到期日")
    if exps[0] == now.date() and now >= expiry_deadline(exps[0]):
        return exps[1]
    return exps[0]


def years_to_expiry(expiry: date, now: datetime) -> float:
    secs = max((expiry_deadline(expiry) - now).total_seconds(), 60.0)
    return secs / (365.0 * 24 * 3600)


def _top_of_book(qctx, sym: str) -> tuple[float, float]:
    try:
        d = qctx.depth(sym)
        bid = float(d.bid_depth[0].price) if d.bid_depth else 0.0
        ask = float(d.ask_depth[0].price) if d.ask_depth else 0.0
        return bid, ask
    except Exception:
        return 0.0, 0.0


@dataclass
class OptionPick:
    symbol: str
    strike: float
    is_call: bool
    delta: float
    ask: float
    qty: int
    expiry: date
    spot: float


def pick_option(qctx, underlying: str, is_call: bool, spot: float,
                budget: float = 200.0, target_delta: float = 0.50,
                now: datetime | None = None) -> OptionPick | None:
    now = now or datetime.now(ET)
    expiry = pick_expiry(qctx, underlying, now)
    chain = qctx.option_chain_info_by_date(underlying, expiry)
    symbols = [c.call_symbol if is_call else c.put_symbol for c in chain]
    quotes = {}
    for i in range(0, len(symbols), 50):
        for q in qctx.option_quote(symbols[i : i + 50]):
            quotes[q.symbol] = q

    y = years_to_expiry(expiry, now)
    best = None
    best_gap = 1e9
    for c in chain:
        sym = c.call_symbol if is_call else c.put_symbol
        q = quotes.get(sym)
        if q is None or not q.implied_volatility or q.implied_volatility <= 0:
            continue
        bid, ask = _top_of_book(qctx, sym)
        if ask <= 0.05:
            ask = float(q.last_done or 0)
        if ask <= 0.05:
            continue
        delta = bs_delta(spot, float(c.price), y, float(q.implied_volatility), is_call)
        if not is_call:
            delta = -delta
        if not (0.25 <= abs(delta) <= 0.75):
            continue
        qty = 2 if 2 * ask * 100 <= budget else (1 if ask * 100 <= budget else 0)
        if qty == 0:
            continue
        gap = abs(abs(delta) - target_delta)
        if gap < best_gap:
            best_gap = gap
            best = OptionPick(
                symbol=sym, strike=float(c.price), is_call=is_call, delta=delta,
                ask=ask, qty=qty, expiry=expiry, spot=spot,
            )
    return best


def top_bid(qctx, sym: str, fallback: float = 0.0) -> float:
    bid, _ = _top_of_book(qctx, sym)
    if bid > 0:
        return bid
    q = qctx.option_quote([sym])[0]
    return float(q.last_done or fallback)
