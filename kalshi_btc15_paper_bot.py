"""Kalshi BTC 15-minute PAPER bot with performance tracking.
Public market data only. Never submits orders or uses account credentials.
"""
from __future__ import annotations
import csv, json, math, time
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
LOOKBACK_SECONDS = 45
MIN_MOMENTUM = 0.025
MAX_SPREAD = 0.04
MIN_ENTRY_PRICE = 0.15
MAX_ENTRY_PRICE = 0.85
NO_NEW_ENTRY_LAST_SECONDS = 240
MIN_SECONDS_AFTER_OPEN = 60
MIN_CONFIDENCE = 0.60
STATE = Path("paper_state.json")
TRADES = Path("paper_trades_v2.csv")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "BTC15M-PaperBot/2.1"})

TRADE_FIELDS = [
    "settled_utc", "entry_utc", "entry_seconds_left", "entry_timing_bucket",
    "ticker", "side", "contracts", "entry_price", "entry_cost",
    "entry_fee_estimate", "result", "pnl", "cash_after", "equity_after"
]

def money(x: Any) -> float:
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

def get_json(path: str, params: dict | None = None) -> dict:
    r = SESSION.get(f"{BASE}{path}", params=params, timeout=10)
    r.raise_for_status()
    return r.json()

def as_float(v: Any) -> float | None:
    try: return float(v) if v is not None else None
    except (TypeError, ValueError): return None

def market_time(m: dict, key: str) -> float | None:
    raw = m.get(key)
    if not raw: return None
    try: return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except Exception: return None

def discover_current_market() -> dict | None:
    data = get_json("/markets", {"series_ticker": SERIES, "status": "open", "limit": 100})
    now = time.time()
    active = [(market_time(m, "close_time"), m) for m in data.get("markets", [])]
    active = [(t, m) for t, m in active if t and t > now]
    return min(active, key=lambda x: x[0])[1] if active else None

def orderbook_quotes(ticker: str) -> tuple[float, float] | None:
    data = get_json(f"/markets/{ticker}/orderbook")
    book = data.get("orderbook_fp") or data.get("orderbook") or {}
    yes, no = book.get("yes_dollars"), book.get("no_dollars")
    if yes is not None and no is not None:
        y = [(as_float(x[0]), as_float(x[1])) for x in yes]
        n = [(as_float(x[0]), as_float(x[1])) for x in no]
        y = [(p,q) for p,q in y if p is not None and q is not None and q > 0]
        n = [(p,q) for p,q in n if p is not None and q is not None and q > 0]
        if not y or not n: return None
        bid, no_bid = max(p for p,_ in y), max(p for p,_ in n)
    else:
        y, n = book.get("yes", []), book.get("no", [])
        if not y or not n: return None
        bid, no_bid = max(float(x[0]) for x in y)/100, max(float(x[0]) for x in n)/100
    ask = 1.0 - no_bid
    if not (0 < bid < 1 and 0 < ask < 1 and ask >= bid): return None
    return bid, ask

def get_result(ticker: str) -> str | None:
    try:
        m = get_json(f"/markets/{ticker}").get("market", {})
        result = (m.get("result") or "").lower()
        return result if result in ("yes", "no") else None
    except requests.RequestException:
        return None

def fee_dollars(price: float, contracts: int) -> float:
    raw = 0.07 * contracts * price * (1.0 - price)
    return math.ceil(raw * 100 - 1e-9) / 100

@dataclass
class Quote:
    ts: float
    mid: float
    bid: float
    ask: float

def default_state() -> dict:
    return {"starting_bankroll": STARTING_BANKROLL, "cash": STARTING_BANKROLL,
            "daily_pnl": 0.0, "day": str(date.today()), "history": [],
            "position": None, "traded_tickers": [], "realized_pnl": 0.0,
            "version": 2}

def load_state() -> dict:
    if not STATE.exists(): return default_state()
    try: s = json.loads(STATE.read_text())
    except Exception: return default_state()
    if "cash" not in s:
        s["cash"] = float(s.get("bankroll", STARTING_BANKROLL))
    s.setdefault("starting_bankroll", STARTING_BANKROLL)
    s.setdefault("daily_pnl", 0.0)
    s.setdefault("day", str(date.today()))
    s.setdefault("traded_tickers", [])
    s.setdefault("position", None)
    s.setdefault("history", [])
    s["version"] = 2
    p = s.get("position")
    reserved = float(p.get("cost", 0)) + float(p.get("fee", 0)) if p else 0.0
    s["realized_pnl"] = round(float(s["cash"]) + reserved - float(s["starting_bankroll"]), 2)
    return s

def save_state(s: dict) -> None:
    s["bankroll"] = round(float(s["cash"]), 2)
    STATE.write_text(json.dumps(s, indent=2))

def append_trade(row: dict) -> None:
    # Upgrade an older v2 CSV header without discarding any existing rows.
    rows = []
    if TRADES.exists():
        with TRADES.open(newline="") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
    temp = TRADES.with_suffix(".tmp")
    with temp.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TRADE_FIELDS)
        w.writeheader()
        for old in rows:
            w.writerow({k: old.get(k, "") for k in TRADE_FIELDS})
        w.writerow({k: row.get(k, "") for k in TRADE_FIELDS})
    temp.replace(TRADES)

def timing_bucket(seconds_left: float) -> str:
    elapsed = max(0, 900 - seconds_left)
    if elapsed < 180: return "First 3 min"
    if elapsed < 420: return "Minutes 3–7"
    if elapsed < 660: return "Minutes 7–11"
    return "Final 4 min"

def seconds_left(m: dict) -> float:
    t = market_time(m, "close_time")
    return max(0.0, t-time.time()) if t else 0.0

def signal(history: list[Quote], market: dict):
    if len(history) < 2: return None
    now = time.time()
    old = next((q for q in history if now-q.ts >= LOOKBACK_SECONDS), None)
    if old is None: return None
    cur = history[-1]
    delta, spread = cur.mid-old.mid, cur.ask-cur.bid
    if spread > MAX_SPREAD or seconds_left(market) <= NO_NEW_ENTRY_LAST_SECONDS: return None
    opened = market_time(market, "open_time")
    if opened and now-opened < MIN_SECONDS_AFTER_OPEN: return None
    if delta >= MIN_MOMENTUM and MIN_ENTRY_PRICE <= cur.ask <= MAX_ENTRY_PRICE:
        score = min(0.90, 0.50 + abs(delta)*4.0)
        if score >= MIN_CONFIDENCE: return "yes", score, cur.ask
    if delta <= -MIN_MOMENTUM:
        no_ask = 1.0-cur.bid
        if MIN_ENTRY_PRICE <= no_ask <= MAX_ENTRY_PRICE:
            score = min(0.90, 0.50 + abs(delta)*4.0)
            if score >= MIN_CONFIDENCE: return "no", score, no_ask
    return None

def equity(s: dict) -> float:
    p = s.get("position")
    reserved = float(p.get("cost",0)) + float(p.get("fee",0)) if p else 0.0
    return round(float(s["cash"]) + reserved, 2)

def main():
    s = load_state()
    quotes_history: list[Quote] = []
    save_state(s)
    print("BTC15M PAPER v2.1 — public data only; no orders.", flush=True)
    print(f"Cash ${s['cash']:.2f} | book equity ${equity(s):.2f}", flush=True)
    while True:
        try:
            today = str(date.today())
            if s["day"] != today:
                s["day"], s["daily_pnl"], s["traded_tickers"] = today, 0.0, []
                save_state(s)
            if float(s["daily_pnl"]) <= -MAX_DAILY_LOSS:
                print("Daily realized-loss limit reached; paused.", flush=True)
                time.sleep(60); continue
            p = s.get("position")
            if p:
                result = get_result(p["ticker"])
                if result:
                    payout = float(p["contracts"]) if result == p["side"] else 0.0
                    pnl = round(payout-float(p["cost"])-float(p["fee"]), 2)
                    s["cash"] = round(float(s["cash"])+payout, 2)
                    s["daily_pnl"] = round(float(s["daily_pnl"])+pnl, 2)
                    s["realized_pnl"] = round(float(s["cash"])-float(s["starting_bankroll"]), 2)
                    append_trade({"settled_utc":datetime.now(timezone.utc).isoformat(),
                        "entry_utc":p.get("entry_utc",""),
                        "entry_seconds_left":p.get("entry_seconds_left",""),
                        "entry_timing_bucket":p.get("entry_timing_bucket",""),
                        "ticker":p["ticker"],"side":p["side"],"contracts":p["contracts"],
                        "entry_price":p["price"],"entry_cost":p["cost"],
                        "entry_fee_estimate":p["fee"],"result":result,"pnl":pnl,
                        "cash_after":s["cash"],"equity_after":s["cash"]})
                    print(f"SETTLED {result.upper()} P/L ${pnl:+.2f}; cash ${s['cash']:.2f}", flush=True)
                    s["position"] = None; save_state(s)
                time.sleep(POLL_SECONDS); continue
            m = discover_current_market()
            if not m:
                print("No open KXBTC15M market; retrying.", flush=True)
                time.sleep(POLL_SECONDS); continue
            ticker = m["ticker"]
            q = orderbook_quotes(ticker)
            if not q:
                print(f"{ticker}: incomplete book.", flush=True); time.sleep(POLL_SECONDS); continue
            bid, ask = q
            now = time.time()
            quotes_history.append(Quote(now,(bid+ask)/2,bid,ask))
            quotes_history = [x for x in quotes_history if now-x.ts <= LOOKBACK_SECONDS*3]
            if ticker not in s["traded_tickers"]:
                sig = signal(quotes_history,m)
                print(f"{ticker} mid={(bid+ask)/2:.3f} spread={ask-bid:.3f} signal={sig[0] if sig else 'NONE'}", flush=True)
                if sig:
                    side, score, price = sig
                    contracts = max(1, int(RISK_PER_TRADE // price))
                    cost = money(contracts*price)
                    fee = fee_dollars(price,contracts)
                    total = money(cost+fee)
                    if total <= float(s["cash"]) and cost <= RISK_PER_TRADE+0.001:
                        left = seconds_left(m)
                        s["position"] = {"ticker":ticker,"side":side,"price":round(price,4),
                            "contracts":contracts,"cost":cost,"fee":fee,
                            "confidence_heuristic":round(score,3),
                            "entry_utc":datetime.now(timezone.utc).isoformat(),
                            "entry_seconds_left":round(left,1),
                            "entry_timing_bucket":timing_bucket(left)}
                        s["cash"] = money(float(s["cash"])-total)
                        s["traded_tickers"].append(ticker)
                        print(f"PAPER BUY {side.upper()} x{contracts} @ {price:.3f}; "
                              f"cost ${cost:.2f}, estimated fee ${fee:.2f}; cash ${s['cash']:.2f}", flush=True)
                        save_state(s)
            time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            print("Stopped; state saved.", flush=True); save_state(s); break
        except requests.RequestException as e:
            print(f"API/network error: {e}; retrying in 10s.", flush=True); time.sleep(10)
        except (KeyError, ValueError, TypeError) as e:
            print(f"Data/state error: {e}; retrying in 10s.", flush=True); time.sleep(10)

if __name__ == "__main__":
    main()
