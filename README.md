# Kalshi BTC 15-minute Paper Bot

This is a **paper-trading starter**, not a live trading bot. It reads public Kalshi market data for the `KXBTC15M` series and simulates one position at a time. It never logs into your account, reads private keys, or sends orders.

## Requirements
- Python 3.10+
- `pip install requests`

## Run
```bash
python kalshi_btc15_paper_bot.py
```

It creates:
- `paper_state.json` — simulated bankroll and open paper position
- `paper_trades.csv` — settled paper-trade log

Press Ctrl+C to stop. Keep both files in the same folder to preserve your paper results.

## Default rules
- $100 simulated bankroll
- $2 maximum contract spend per trade (plus estimated fee)
- $10 daily loss stop
- One open position at a time
- 45-second YES-mid momentum lookback
- 2.5-cent minimum momentum
- 4-cent maximum spread
- No new entries in the last 4 minutes of a window
- Positions held to settlement

The confidence number is a heuristic, not a calibrated probability. The bot does not prove an edge. Fee calculation is an estimate; verify the current Kalshi fee schedule before relying on any P/L.

## Important
1. Run paper mode for at least 7–14 days and evaluate net P/L, win rate, average win/loss, fees, and number of trades.
2. Do not treat a short winning streak as evidence of profitability.
3. This starter intentionally has no live-order switch. Live trading would require a separate implementation with Kalshi API key authentication, order validation, fill handling, cancel/replace logic, position reconciliation, and explicit risk safeguards.
4. Public market-data endpoint formats can change. If the bot reports an unexpected response format, stop and inspect Kalshi's current API docs rather than disabling safeguards.

Official API overview: https://help.kalshi.com/en/articles/13823854-kalshi-api
