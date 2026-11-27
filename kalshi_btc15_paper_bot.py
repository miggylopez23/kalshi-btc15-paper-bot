"""BTC 15-minute PAPER bot v3.1.
Public market data only. Never submits orders or uses account credentials.

v3.1 keeps the dashboard/state/CSV filenames and existing paper-risk limits.
It adds 45/90/180-second directional confirmation and trade diagnostics.
This is an experimental paper strategy, not a claim of profitability.
"""
from __future__ import annotations

import csv
import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone, date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

import requests

BASE = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = "KXBTC15M"

STARTING_BANKROLL = 100.00
RISK_PER_TRADE = 2.00
MAX_DAILY_LOSS = 10.00
POLL_SECONDS = 5

# Signal controls: all momentum values are changes in the YES-contract midpoint.
WINDOWS = (45, 90, 180)
MIN_MOMENTUM_45 = 0.025
MIN_MOMENTUM_90 = 0.035
MIN_MOMENTUM_180 = 0.045
MAX_SPREAD = 0.025
MIN_ENTRY_PRICE = 0.20
MAX_ENTRY_PRICE = 0.70
NO_NEW_ENTRY_LAST_SECONDS = 240
MIN_SECONDS_AFTER_OPEN = 60
MIN_CONFIRMATIONS = 3
MIN_SECONDS_OF_HISTORY = 180

STATE = Path("paper_state.json")
TRADES = Path("paper_trades_v2.csv")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "BTC15M-PaperBot/3.1-PAPER"})

TRADE_FIELDS = [
    "settled_utc", "entry_utc", "entry_seconds_left", "entry_timing_bucket",
    "ticker", "side", "contracts", "entry_price", "entry_cost",
    "entry_fee_estimate", "result", "pnl", "cash_after", "equity_after",
    "momentum_45", "momentum_90", "momentum_180", "entry_spread",
    "alignment_score", "fee_fraction"
]


def money(x: Any) -> float:
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def get_json(path: str, params: dict | None = None) -> dict:
    response = SESSION.get(f"{BASE}{path}", params=params, timeout=10)
    response.raise_for_status()
    return response.json()


def as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def market_time(market: dict, key: str) -> float | None:
    raw = market.get(key)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def discover_current_market() -> dict | None:
    data = get_json("/markets", {
        "series_ticker": SERIES, "status": "open", "limit": 100
    })
    now = time.time()
    active = [
        (market_time(m, "close_time"), m)
        for m in data.get("markets", [])
    ]
    active = [(close, market) for close, market in active if close and close > now]
    return min(active, key=lambda item: item[0])[1] if active else None


def orderbook_quotes(ticker: str) -> tuple[float, float] | None:
    data = get_json(f"/markets/{ticker}/orderbook")
    book = data.get("orderbook_fp") or data.get("orderbook") or {}
    yes, no = book.get("yes_dollars"), book.get("no_dollars")

    if yes is not None and no is not None:
        yes_levels = [
            (as_float(level[0]), as_float(level[1])) for level in yes
        ]
        no_levels = [
            (as_float(level[0]), as_float(level[1])) for level in no
        ]
        yes_levels = [(p, q) for p, q in yes_levels
                      if p is not None and q is not None and q > 0]
        no_levels = [(p, q) for p, q in no_levels
                     if p is not None and q is not None and q > 0]
        if not yes_levels or not no_levels:
            return None
        bid = max(price for price, _ in yes_levels)
        no_bid = max(price for price, _ in no_levels)
    else:
        yes_levels, no_levels = book.get("yes", []), book.get("no", [])
        if not yes_levels or not no_levels:
            return None
        bid = max(float(level[0]) for level in yes_levels) / 100.0
        no_bid = max(float(level[0]) for level in no_levels) / 100.0

    ask = 1.0 - no_bid
    if not (0 < bid < 1 and 0 < ask < 1 and ask >= bid):
        return None
    return bid, ask


def get_result(ticker: str) -> str | None:
    try:
        market = get_json(f"/markets/{ticker}").get("market", {})
        result = (market.get("result") or "").lower()
        return result if result in ("yes", "no") else None
    except requests.RequestException:
        return None


def fee_dollars(price: float, contracts: int) -> float:
    # Kalshi taker-fee estimate used by the existing paper model.
    raw = 0.07 * contracts * price * (1.0 - price)
    return math.ceil(raw * 100 - 1e-9) / 100


@dataclass
class Quote:
    ts: float
    mid: float
    bid: float
    ask: float


def default_state() -> dict:
    return {
        "starting_bankroll": STARTING_BANKROLL,
        "cash": STARTING_BANKROLL,
        "daily_pnl": 0.0,
        "day": str(date.today()),
        "history": [],
        "position": None,
        "traded_tickers": [],
        "realized_pnl": 0.0,
        "version": 3,
    }


def load_state() -> dict:
    if not STATE.exists():
        return default_state()
    try:
        state = json.loads(STATE.read_text())
    except (OSError, json.JSONDecodeError):
        raise RuntimeError(
            "paper_state.json could not be read. Stop and back it up; "
            "the bot will not reset your balance automatically."
        )

    if "cash" not in state:
        state["cash"] = float(state.get("bankroll", STARTING_BANKROLL))
    state.setdefault("starting_bankroll", STARTING_BANKROLL)
    state.setdefault("daily_pnl", 0.0)
    state.setdefault("day", str(date.today()))
    state.setdefault("traded_tickers", [])
    state.setdefault("position", None)
    state.setdefault("history", [])
    state["version"] = 3

    position = state.get("position")
    reserved = (
        float(position.get("cost", 0)) + float(position.get("fee", 0))
        if position else 0.0
    )
    state["realized_pnl"] = round(
        float(state["cash"]) + reserved - float(state["starting_bankroll"]), 2
    )
    return state


def save_state(state: dict) -> None:
    state["bankroll"] = round(float(state["cash"]), 2)
    temp = STATE.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2))
    temp.replace(STATE)


def append_trade(row: dict) -> None:
    # Preserve all historical rows while upgrading/retaining the expanded header.
    rows = []
    if TRADES.exists():
        with TRADES.open(newline="") as handle:
            rows = list(csv.DictReader(handle))
    temp = TRADES.with_suffix(".tmp")
    with temp.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=TRADE_FIELDS)
        writer.writeheader()
        for old_row in rows:
            writer.writerow({key: old_row.get(key, "") for key in TRADE_FIELDS})
        writer.writerow({key: row.get(key, "") for key in TRADE_FIELDS})
    temp.replace(TRADES)


def timing_bucket(seconds_remaining: float) -> str:
    elapsed = max(0, 900 - seconds_remaining)
    if elapsed < 180:
        return "First 3 min"
    if elapsed < 420:
        return "Minutes 3â7"
    if elapsed < 660:
        return "Minutes 7â11"
    return "Final 4 min"


def seconds_left(market: dict) -> float:
    close = market_time(market, "close_time")
    return max(0.0, close - time.time()) if close else 0.0


def momentum_at(history: list[Quote], now: float, window: int) -> float | None:
    current = history[-1]
    # Use a quote at or just before the target window, with a 10-second tolerance.
    candidates = [q for q in history if q.ts <= now - window]
    if not candidates:
        return None
    old = max(candidates, key=lambda q: q.ts)
    age = now - old.ts
    if age > window + 10:
        return None
    return current.mid - old.mid


def signal(history: list[Quote], market: dict) -> dict | None:
    if len(history) < 2:
        return None

    now = time.time()
    if now - history[0].ts < MIN_SECONDS_OF_HISTORY:
        return None

    current = history[-1]
    spread = current.ask - current.bid
    if spread > MAX_SPREAD:
        return None
    if seconds_left(market) <= NO_NEW_ENTRY_LAST_SECONDS:
        return None

    opened = market_time(market, "open_time")
    if opened and now - opened < MIN_SECONDS_AFTER_OPEN:
        return None

    moves = {window: momentum_at(history, now, window) for window in WINDOWS}
    if any(value is None for value in moves.values()):
        return None

    positive = all(moves[w] >= threshold for w, threshold in (
        (45, MIN_MOMENTUM_45),
        (90, MIN_MOMENTUM_90),
        (180, MIN_MOMENTUM_180),
    ))
    negative = all(moves[w] <= -threshold for w, threshold in (
        (45, MIN_MOMENTUM_45),
        (90, MIN_MOMENTUM_90),
        (180, MIN_MOMENTUM_180),
    ))

    if positive:
        side, price = "yes", current.ask
    elif negative:
        side, price = "no", 1.0 - current.bid
    else:
        return None

    if not (MIN_ENTRY_PRICE <= price <= MAX_ENTRY_PRICE):
        return None

    # This is a descriptive alignment score, not a probability of winning.
    magnitudes = [
        abs(moves[45]) / MIN_MOMENTUM_45,
        abs(moves[90]) / MIN_MOMENTUM_90,
        abs(moves[180]) / MIN_MOMENTUM_180,
    ]
    alignment = min(1.0, sum(min(x, 2.0) for x in magnitudes) / 6.0)
    fee_fraction = fee_dollars(price, 1) / price
    return {
        "side": side,
        "price": price,
        "momentum_45": moves[45],
        "momentum_90": moves[90],
        "momentum_180": moves[180],
        "spread": spread,
        "alignment_score": alignment,
        "fee_fraction": fee_fraction,
    }


def equity(state: dict) -> float:
    position = state.get("position")
    reserved = (
        float(position.get("cost", 0)) + float(position.get("fee", 0))
        if position else 0.0
    )
    return round(float(state["cash"]) + reserved, 2)


def main() -> None:
    state = load_state()
    quotes_history: list[Quote] = []
    history_ticker: str | None = None
    save_state(state)

    print("BTC15M PAPER v3.1 â public data only; no orders.", flush=True)
    print(
        f"Cash ${state['cash']:.2f} | book equity ${equity(state):.2f}",
        flush=True,
    )
    print(
        "Filters: 45/90/180s aligned momentum; spread <= "
        f"{MAX_SPREAD:.3f}; entry ${MIN_ENTRY_PRICE:.2f}-${MAX_ENTRY_PRICE:.2f}.",
        flush=True,
    )

    while True:
        try:
            today = str(date.today())
            if state["day"] != today:
                state["day"] = today
                state["daily_pnl"] = 0.0
                state["traded_tickers"] = []
                save_state(state)

            if float(state["daily_pnl"]) <= -MAX_DAILY_LOSS:
                print("Daily realized-loss limit reached; paused.", flush=True)
                time.sleep(60)
                continue

            position = state.get("position")
            if position:
                result = get_result(position["ticker"])
                if result:
                    payout = (
                        float(position["contracts"])
                        if result == position["side"] else 0.0
                    )
                    pnl = round(
                        payout - float(position["cost"]) - float(position["fee"]), 2
                    )
                    state["cash"] = round(float(state["cash"]) + payout, 2)
                    state["daily_pnl"] = round(float(state["daily_pnl"]) + pnl, 2)
                    state["realized_pnl"] = round(
                        float(state["cash"]) - float(state["starting_bankroll"]), 2
                    )
                    append_trade({
                        "settled_utc": datetime.now(timezone.utc).isoformat(),
                        "entry_utc": position.get("entry_utc", ""),
                        "entry_seconds_left": position.get("entry_seconds_left", ""),
                        "entry_timing_bucket": position.get("entry_timing_bucket", ""),
                        "ticker": position["ticker"],
                        "side": position["side"],
                        "contracts": position["contracts"],
                        "entry_price": position["price"],
                        "entry_cost": position["cost"],
                        "entry_fee_estimate": position["fee"],
                        "result": result,
                        "pnl": pnl,
                        "cash_after": state["cash"],
                        "equity_after": state["cash"],
                        "momentum_45": position.get("momentum_45", ""),
                        "momentum_90": position.get("momentum_90", ""),
                        "momentum_180": position.get("momentum_180", ""),
                        "entry_spread": position.get("entry_spread", ""),
                        "alignment_score": position.get("alignment_score", ""),
                        "fee_fraction": position.get("fee_fraction", ""),
                    })
                    print(
                        f"SETTLED {result.upper()} P/L ${pnl:+.2f}; "
                        f"cash ${state['cash']:.2f}",
                        flush=True,
                    )
                    state["position"] = None
                    save_state(state)
                time.sleep(POLL_SECONDS)
                continue

            market = discover_current_market()
            if not market:
                print("No open KXBTC15M market; retrying.", flush=True)
                time.sleep(POLL_SECONDS)
                continue

            ticker = market["ticker"]
            if ticker != history_ticker:
                # Never compare contract-price momentum across different 15m markets.
                quotes_history.clear()
                history_ticker = ticker
            quotes = orderbook_quotes(ticker)
            if not quotes:
                print(f"{ticker}: incomplete book.", flush=True)
                time.sleep(POLL_SECONDS)
                continue

            bid, ask = quotes
            now = time.time()
            quotes_history.append(Quote(now, (bid + ask) / 2.0, bid, ask))
            quotes_history = [
                quote for quote in quotes_history
                if now - quote.ts <= MIN_SECONDS_OF_HISTORY + 20
            ]

            if ticker not in state["traded_tickers"]:
                candidate = signal(quotes_history, market)
                if candidate:
                    side = candidate["side"]
                    price = candidate["price"]
                    # Keep the original $2 contract-cost ceiling; fees are additional.
                    contracts = max(1, int(RISK_PER_TRADE // price))
                    cost = money(contracts * price)
                    fee = fee_dollars(price, contracts)
                    total = money(cost + fee)

                    if total <= float(state["cash"]) and cost <= RISK_PER_TRADE + 0.001:
                        remaining = seconds_left(market)
                        state["position"] = {
                            "ticker": ticker,
                            "side": side,
                            "price": round(price, 4),
                            "contracts": contracts,
                            "cost": cost,
                            "fee": fee,
                            "confidence_heuristic": round(
                                candidate["alignment_score"], 3
                            ),
                            "momentum_45": round(candidate["momentum_45"], 5),
                            "momentum_90": round(candidate["momentum_90"], 5),
                            "momentum_180": round(candidate["momentum_180"], 5),
                            "entry_spread": round(candidate["spread"], 5),
                            "alignment_score": round(
                                candidate["alignment_score"], 3
                            ),
                            "fee_fraction": round(candidate["fee_fraction"], 5),
                            "entry_utc": datetime.now(timezone.utc).isoformat(),
                            "entry_seconds_left": round(remaining, 1),
                            "entry_timing_bucket": timing_bucket(remaining),
                        }
                        state["cash"] = money(float(state["cash"]) - total)
                        state["traded_tickers"].append(ticker)
                        print(
                            f"PAPER BUY {side.upper()} x{contracts} @ {price:.3f}; "
                            f"cost ${cost:.2f}, estimated fee ${fee:.2f}; "
                            f"45/90/180={candidate['momentum_45']:+.3f}/"
                            f"{candidate['momentum_90']:+.3f}/"
                            f"{candidate['momentum_180']:+.3f}; "
                            f"spread={candidate['spread']:.3f}; "
                            f"cash ${state['cash']:.2f}",
                            flush=True,
                        )
                        save_state(state)
                else:
                    print(
                        f"{ticker} mid={(bid + ask) / 2:.3f} "
                        f"spread={ask - bid:.3f} signal=NONE",
                        flush=True,
                    )

            time.sleep(POLL_SECONDS)

        except KeyboardInterrupt:
            print("Stopped; state saved.", flush=True)
            save_state(state)
            break
        except requests.RequestException as error:
            print(f"API/network error: {error}; retrying in 10s.", flush=True)
            time.sleep(10)
        except (KeyError, ValueError, TypeError, RuntimeError) as error:
            print(f"Data/state error: {error}; retrying in 10s.", flush=True)
            time.sleep(10)


if __name__ == "__main__":
    main()


