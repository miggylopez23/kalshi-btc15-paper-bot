#!/usr/bin/env python3
"""
Kalshi BTC 15-minute PAPER bot.
- Reads public Kalshi market data only.
- Simulates trades locally; it NEVER submits orders or accesses account credentials.
- Intended for strategy testing, not a claim of profitability.

Python 3.10+
Install: pip install requests
Run:    python kalshi_btc15_paper_bot.py
"""

from __future__ import annotations
import csv
import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Any

import requests

BASE = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = "KXBTC15M"

# Conservative test defaults; edit only after reviewing paper results.
PAPER_BANKROLL = 100.00
RISK_PER_TRADE = 2.00
MAX_DAILY_LOSS = 10.00
MAX_OPEN_POSITIONS = 1
POLL_SECONDS = 5
LOOKBACK_SECONDS = 45
MIN_MOMENTUM = 0.025          # 2.5 cents of YES-mid movement
MAX_SPREAD = 0.04             # do not trade wide books
MIN_ENTRY_PRICE = 0.15
MAX_ENTRY_PRICE = 0.85
NO_NEW_ENTRY_LAST_SECONDS = 240
MIN_SECONDS_AFTER_OPEN = 60
MIN_CONFIDENCE = 0.60         # heuristic filter, NOT a calibrated probability

OUT = Path("paper_trades.csv")
STATE = Path("paper_state.json")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "BTC15M-PaperBot/1.0"})


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
    # Kalshi market responses commonly use ISO-8601 timestamps.
    raw = market.get(key)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def discover_current_market() -> dict | None:
    data = get_json("/markets", {"series_ticker": SERIES, "status": "open", "limit": 100})
    now = time.time()
    markets = data.get("markets", [])
    active = []
    for m in markets:
        close_ts = market_time(m, "close_time")
        if close_ts and close_ts > now:
            active.append((close_ts, m))
    return min(active, key=lambda x: x[0])[1] if active else None


def orderbook_quotes(ticker: str) -> tuple[float, float] | None:
    data = get_json(f"/markets/{ticker}/orderbook")
    book = data.get("orderbook_fp") or data.get("orderbook") or {}
    yes = book.get("yes_dollars")
    no = book.get("no_dollars")
    if yes is not None and no is not None:
        yes_levels = [(as_float(x[0]), as_float(x[1])) for x in yes]
        no_levels = [(as_float(x[0]), as_float(x[1])) for x in no]
        yes_levels = [x for x in yes_levels if x[0] is not None and x[1] is not None and x[1] > 0]
        no_levels = [x for x in no_levels if x[0] is not None and x[1] is not None and x[1] > 0]
        if not yes_levels or not no_levels:
            return None
        best_yes_bid = max(p for p, _ in yes_levels)
        best_no_bid = max(p for p, _ in no_levels)
    else:
        # Legacy integer-cent orderbook format.
        yes = book.get("yes", [])
        no = book.get("no", [])
        if not yes or not no:
            return None
        best_yes_bid = max(float(x[0]) for x in yes) / 100
        best_no_bid = max(float(x[0]) for x in no) / 100

    yes_ask = 1.0 - best_no_bid
    if not (0 < best_yes_bid < 1 and 0 < yes_ask < 1 and yes_ask >= best_yes_bid):
        return None
    return best_yes_bid, yes_ask


def get_result(ticker: str) -> str | None:
    try:
        m = get_json(f"/markets/{ticker}").get("market", {})
        result = (m.get("result") or "").lower()
        if result in ("yes", "no"):
            return result
        status = (m.get("status") or "").lower()
        return None
    except requests.RequestException:
        return None


def fee_dollars(price: float, contracts: int) -> float:
    # Approximate standard taker fee model, rounded UP to the next cent per order.
    raw = 0.07 * contracts * price * (1.0 - price)
    return math.ceil(raw * 100 - 1e-9) / 100


@dataclass
class Quote:
    ts: float
    mid: float
    bid: float
    ask: float


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except Exception:
            pass
    return {"bankroll": PAPER_BANKROLL, "daily_pnl": 0.0, "day": str(date.today()),
            "history": [], "position": None, "traded_tickers": []}


def save_state(state: dict) -> None:
    STATE.write_text(json.dumps(state, indent=2))


def log_trade(row: dict) -> None:
    exists = OUT.exists()
    with OUT.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def seconds_left(market: dict) -> float:
    close_ts = market_time(market, "close_time")
    return max(0.0, close_ts - time.time()) if close_ts else 0.0


def signal(history: list[Quote], market: dict) -> tuple[str, float, float] | None:
    if len(history) < 2:
        return None
    now = time.time()
    old = next((q for q in history if now - q.ts >= LOOKBACK_SECONDS), None)
    if old is None:
        return None
    current = history[-1]
    delta = current.mid - old.mid
    spread = current.ask - current.bid
    if spread > MAX_SPREAD or seconds_left(market) <= NO_NEW_ENTRY_LAST_SECONDS:
        return None
    opened = market_time(market, "open_time")
    if opened and now - opened < MIN_SECONDS_AFTER_OPEN:
        return None
    if delta >= MIN_MOMENTUM and MIN_ENTRY_PRICE <= current.ask <= MAX_ENTRY_PRICE:
        strength = min(0.90, 0.50 + abs(delta) * 4.0)
        if strength >= MIN_CONFIDENCE:
            return "yes", strength, current.ask
    if delta <= -MIN_MOMENTUM:
        no_ask = 1.0 - current.bid
        if MIN_ENTRY_PRICE <= no_ask <= MAX_ENTRY_PRICE:
            strength = min(0.90, 0.50 + abs(delta) * 4.0)
            if strength >= MIN_CONFIDENCE:
                return "no", strength, no_ask
    return None


def main() -> None:
    state = load_state()
    history: list[Quote] = []
    print("Kalshi BTC 15m PAPER bot — no real orders will be placed.")
    print(f"Paper bankroll ${state['bankroll']:.2f}; risk/trade ${RISK_PER_TRADE:.2f}; "
          f"daily stop -${MAX_DAILY_LOSS:.2f}. Ctrl+C to stop.")
    while True:
        try:
            today = str(date.today())
            if state["day"] != today:
                state["day"] = today
                state["daily_pnl"] = 0.0
                state["traded_tickers"] = []
            if state["daily_pnl"] <= -MAX_DAILY_LOSS:
                print("Daily loss limit reached. Pausing until the next calendar day.")
                time.sleep(60)
                continue

            # If a paper position exists, wait for settlement and grade it.
            pos = state.get("position")
            if pos:
                result = get_result(pos["ticker"])
                if result:
                    won = result == pos["side"]
                    payout = pos["contracts"] * (1.0 if won else 0.0)
                    pnl = payout - pos["cost"] - pos["fee"]
                    state["bankroll"] += payout
                    state["daily_pnl"] += pnl
                    log_trade({
                        "settled_utc": datetime.now(timezone.utc).isoformat(),
                        "ticker": pos["ticker"], "side": pos["side"],
                        "contracts": pos["contracts"], "entry_price": pos["price"],
                        "entry_fee": pos["fee"], "cost": pos["cost"],
                        "result": result, "pnl": round(pnl, 4),
                        "bankroll_after": round(state["bankroll"], 4)
                    })
                    print(f"SETTLED {pos['ticker']} result={result.upper()} "
                          f"P/L=${pnl:+.2f} bankroll=${state['bankroll']:.2f}")
                    state["position"] = None
                    save_state(state)
                time.sleep(POLL_SECONDS)
                continue

            market = discover_current_market()
            if not market:
                print("No open KXBTC15M market found; retrying.")
                time.sleep(POLL_SECONDS)
                continue

            ticker = market["ticker"]
            quotes = orderbook_quotes(ticker)
            if not quotes:
                print(f"{ticker}: order book incomplete; waiting.")
                time.sleep(POLL_SECONDS)
                continue
            bid, ask = quotes
            mid = (bid + ask) / 2
            now = time.time()
            history.append(Quote(now, mid, bid, ask))
            history = [q for q in history if now - q.ts <= LOOKBACK_SECONDS * 3]
            left = seconds_left(market)

            if ticker in state["traded_tickers"]:
                print(f"{ticker} already traded; waiting for next window.")
            else:
                sig = signal(history, market)
                print(f"{ticker} mid={mid:.3f} spread={ask-bid:.3f} "
                      f"left={left:.0f}s signal={sig[0] if sig else 'NONE'}")
                if sig:
                    side, confidence, price = sig
                    contracts = max(1, int(RISK_PER_TRADE // price))
                    cost = contracts * price
                    fee = fee_dollars(price, contracts)
                    if cost + fee <= state["bankroll"] and cost <= RISK_PER_TRADE + 0.01:
                        state["position"] = {
                            "ticker": ticker, "side": side, "price": round(price, 4),
                            "contracts": contracts, "cost": round(cost, 4),
                            "fee": fee, "confidence_heuristic": round(confidence, 3)
                        }
                        state["bankroll"] -= cost + fee
                        state["traded_tickers"].append(ticker)
                        print(f"PAPER BUY {side.upper()} x{contracts} @ ${price:.3f}; "
                              f"cost=${cost:.2f} fee~${fee:.2f}. Holding to settlement.")
                        save_state(state)
            time.sleep(POLL_SECONDS)

        except KeyboardInterrupt:
            print("\nStopped. Paper state saved.")
            save_state(state)
            break
        except requests.RequestException as e:
            print(f"Network/API error: {e}; retrying in 10 seconds.")
            time.sleep(10)
        except (KeyError, ValueError, TypeError) as e:
            print(f"Unexpected market-data format: {e}; retrying in 10 seconds.")
            time.sleep(10)


if __name__ == "__main__":
    main()
