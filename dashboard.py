import csv, json, os, subprocess, sys
from pathlib import Path
import streamlit as st

ROOT = Path(__file__).resolve().parent
STATE, TRADES = ROOT/"paper_state.json", ROOT/"paper_trades_v2.csv"
PIDFILE, LOGFILE = ROOT/"bot.pid", ROOT/"bot.log"
BOT = ROOT/"kalshi_btc15_paper_bot.py"
st.set_page_config(page_title="BTC 15m Paper Lab", page_icon="₿", layout="centered")
st.title("₿ Kalshi BTC 15-minute Paper Lab")
st.caption("Public market data • paper simulation only • no Kalshi orders")

def read_state():
    if STATE.exists():
        try: return json.loads(STATE.read_text())
        except Exception: pass
    return {"starting_bankroll":100.0,"cash":100.0,"bankroll":100.0,
            "daily_pnl":0.0,"position":None,"day":"Not started"}

def is_running():
    if not PIDFILE.exists(): return False
    try:
        pid=int(PIDFILE.read_text().strip()); os.kill(pid,0); return True
    except Exception: return False

def start_bot():
    if is_running(): return
    log=open(LOGFILE,"a",buffering=1)
    p=subprocess.Popen([sys.executable,str(BOT)],cwd=str(ROOT),
        stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
    PIDFILE.write_text(str(p.pid))

def stop_bot():
    if not PIDFILE.exists(): return
    try: os.kill(int(PIDFILE.read_text().strip()),15)
    except Exception: pass
    PIDFILE.unlink(missing_ok=True)

s=read_state(); running=is_running(); pos=s.get("position")
cash=float(s.get("cash",s.get("bankroll",100)))
reserved=(float(pos.get("cost",0))+float(pos.get("fee",0))) if pos else 0.0
equity=round(cash+reserved,2)
start=float(s.get("starting_bankroll",100))
net=round(equity-start,2)
st.metric("Bot status","RUNNING" if running else "STOPPED")
c1,c2=st.columns(2)
c1.metric("Available cash",f"${cash:.2f}")
c2.metric("Book equity",f"${equity:.2f}",delta=f"${net:+.2f} vs start")
c3,c4=st.columns(2)
c3.metric("Realized P/L",f"${float(s.get('realized_pnl',net)):+.2f}")
c4.metric("Today's settled P/L",f"${float(s.get('daily_pnl',0)):+.2f}")
st.info("Open-position value is carried at entry cost plus estimated fee until settlement; this is NOT a live resale valuation.")
st.warning("Paper-only. This app does not submit Kalshi Demo or Production orders.")
a,b=st.columns(2)
with a:
    if st.button("▶ Start paper bot",use_container_width=True,type="primary",disabled=running):
        start_bot(); st.rerun()
with b:
    if st.button("■ Stop bot",use_container_width=True,disabled=not running):
        stop_bot(); st.rerun()
st.subheader("Risk controls")
st.write("- Starting bankroll: $100")
st.write("- Maximum contract cost per trade: $2 (fees additional)")
st.write("- Daily realized-loss pause: $10")
st.write("- One open position maximum")
st.write("- One entry per 15-minute ticker")
if pos:
    st.subheader("Open paper position")
    st.write(f"**{pos.get('side','').upper()}** · {pos.get('contracts')} contracts · "
             f"entry ${float(pos.get('price',0)):.2f}")
    st.write(f"Contract cost: ${float(pos.get('cost',0)):.2f}")
    st.write(f"Estimated fee: ${float(pos.get('fee',0)):.2f}")
    st.write(f"Cash committed: ${reserved:.2f}")
    st.caption(f"Ticker: {pos.get('ticker','')} · heuristic score {pos.get('confidence_heuristic','n/a')} (not a probability)")
st.subheader("Recent settled trades")
if TRADES.exists():
    try:
        with TRADES.open(newline="") as f: rows=list(csv.DictReader(f))
        if rows: st.dataframe(rows[-25:][::-1],use_container_width=True,hide_index=True)
        else: st.caption("No v2 settled trades yet.")
    except Exception as e: st.error(f"Could not read v2 ledger: {e}")
else: st.caption("New v2 settlements will appear here. Previous trade CSV is preserved.")
st.subheader("Bot log")
if LOGFILE.exists():
    lines=LOGFILE.read_text(errors="replace").splitlines()
    st.code("\n".join(lines[-40:]) or "Waiting for output…")
else: st.caption("Log appears after the bot starts.")
if st.button("Refresh dashboard"): st.rerun()
