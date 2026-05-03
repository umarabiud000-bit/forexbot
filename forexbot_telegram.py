"""
⚡ ForexBot AI — Telegram Bot
Exact replica of your HTML bot, now with Telegram notifications.

SETUP:
1. pip install python-telegram-bot requests anthropic
2. Fill in your tokens below (BOT_TOKEN, ANTHROPIC_API_KEY, TWELVE_DATA_KEY)
3. python forexbot_telegram.py

COMMANDS:
/start     — Welcome message
/scan      — Scan both XAUUSD & NAS100 for signals NOW
/gold      — Scan GOLD only
/nas       — Scan NAS100 only
/status    — Show session status + last prices
/auto      — Toggle auto-scan every hour during NY session
/help      — Show all commands
"""

import logging
import asyncio
import math
import random
from datetime import datetime, timezone
import pytz
import requests
import json
import os

from telegram import Update, Bot
from telegram.ext import (
    Application, CommandHandler, ContextTypes
)

# ─────────────────────────────────────────────
# ✏️  FILL IN YOUR TOKENS HERE
# ─────────────────────────────────────────────
BOT_TOKEN        = 8563871838:AAEPk7ard1DvKdFVoCmUvcM9wRglEj3TZl8       # From @BotFather
ANTHROPIC_API_KEY = ""        # From console.anthropic.com
TWELVE_DATA_KEY  = ""                               # Optional: twelvedata.com free key (800 calls/day)
# ─────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

NY_TZ = pytz.timezone("America/New_York")

# Asset config — mirrors HTML CFG object
CFG = {
    "XAUUSD": {"name": "Gold",        "sym": "XAUUSD", "tdSym": "XAU/USD", "base": 3345,  "dec": 2, "pre": "$",  "ltf": "15M", "emoji": "🥇"},
    "NAS100": {"name": "Nasdaq 100",  "sym": "NAS100", "tdSym": "NDX",     "base": 21450, "dec": 1, "pre": "",   "ltf": "5M",  "emoji": "📈"},
}

# In-memory state
auto_scan_chats: set = set()   # chat_ids with auto-scan enabled
last_signals: dict = {}        # asset -> last signal dict


# ══════════════════════════════════════════════
#  INDICATORS  (exact port from HTML)
# ══════════════════════════════════════════════

def calc_ema(arr: list, period: int) -> float:
    if len(arr) < period:
        return arr[-1]
    k = 2 / (period + 1)
    e = sum(arr[:period]) / period
    for price in arr[period:]:
        e = price * k + e * (1 - k)
    return e


def calc_rsi(arr: list, period: int = 14) -> float:
    if len(arr) < period + 1:
        return 50.0
    gains, losses = 0.0, 0.0
    for i in range(1, period + 1):
        d = arr[i] - arr[i - 1]
        if d > 0:
            gains += d
        else:
            losses += abs(d)
    ag, al = gains / period, losses / period
    for i in range(period + 1, len(arr)):
        d = arr[i] - arr[i - 1]
        ag = (ag * (period - 1) + max(0, d)) / period
        al = (al * (period - 1) + max(0, -d)) / period
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def calc_vwap(candles: list) -> float:
    tv, vol = 0.0, 0.0
    for c in candles:
        tp = (c["h"] + c["l"] + c["c"]) / 3
        tv += tp * c["v"]
        vol += c["v"]
    return tv / vol if vol > 0 else candles[-1]["c"]


def build_indicators(candles: list) -> dict:
    closes = [c["c"] for c in candles]
    curr   = closes[-1]
    prev   = closes[-2]
    e20    = calc_ema(closes, 20)
    e50    = calc_ema(closes, 50)
    e200   = calc_ema(closes, 200)
    rsi    = calc_rsi(closes, 14)
    vwap   = calc_vwap(candles)

    avg_vol  = sum(c["v"] for c in candles[-20:]) / 20
    curr_vol = candles[-1]["v"]

    # Support / Resistance
    sl = candles[-60:]
    highs = [c["h"] for c in sl]
    lows  = [c["l"] for c in sl]
    l_hi, l_lo = [], []
    for i in range(5, len(sl) - 5):
        if highs[i] == max(highs[i-5:i+6]):
            l_hi.append(highs[i])
        if lows[i] == min(lows[i-5:i+6]):
            l_lo.append(lows[i])

    res = sorted(set(v for v in l_hi if v > curr))[:3]
    sup = sorted(set(v for v in l_lo if v < curr), reverse=True)[:3]
    rng2 = abs(curr - e50)
    if len(res) < 2:
        res += [curr + rng2 * 1.4, curr + rng2 * 2.2]
    if len(sup) < 2:
        sup += [curr - rng2 * 1.4, curr - rng2 * 2.2]

    ts = sum([curr > e200, curr > e50, e20 > e50, e50 > e200])
    trend = "BULLISH" if ts >= 3 else "BEARISH" if ts <= 1 else "NEUTRAL"

    vol_state = ("HIGH" if curr_vol > avg_vol * 1.35
                 else "LOW" if curr_vol < avg_vol * 0.7
                 else "AVERAGE")

    return {
        "curr": curr, "prev": prev,
        "chg": curr - prev, "chg_pct": (curr - prev) / prev * 100,
        "e20": e20, "e50": e50, "e200": e200,
        "rsi": rsi, "vwap": vwap,
        "vol_state": vol_state,
        "res": res[:3], "sup": sup[:3],
        "trend": trend, "ts": ts,
    }


# ══════════════════════════════════════════════
#  SIMULATED DATA  (seeded — matches HTML)
# ══════════════════════════════════════════════

def mk_rng(seed: int):
    state = {"x": seed}
    def rng():
        state["x"] = (state["x"] * 9301 + 49297) % 233280
        return state["x"] / 233280
    return rng


def gen_sim(asset: str) -> dict:
    c = CFG[asset]
    today = datetime.now()
    day_seed = today.year * 10000 + today.month * 100 + today.day
    seed = day_seed + (77777 if asset == "NAS100" else 0)
    rng = mk_rng(seed)

    candles = []
    price = c["base"] * (0.96 + rng() * 0.08)
    drift = (rng() - 0.48) * 0.0003

    for _ in range(210):
        vol = c["base"] * 0.0028 * (0.5 + rng())
        chg = drift * price + (rng() - 0.5) * vol
        o = price
        price = max(price + chg, c["base"] * 0.88)
        h = max(o, price) + rng() * vol * 0.4
        lv = min(o, price) - rng() * vol * 0.4
        v = int(rng() * 1200 + 300)
        candles.append({"o": o, "h": h, "l": lv, "c": price, "v": v})

    return build_indicators(candles)


# ══════════════════════════════════════════════
#  LIVE DATA — Twelve Data API
# ══════════════════════════════════════════════

def fetch_twelve_data(asset: str) -> dict:
    c = CFG[asset]
    url = (
        f"https://api.twelvedata.com/time_series"
        f"?symbol={c['tdSym']}&interval=4h&outputsize=220"
        f"&apikey={TWELVE_DATA_KEY}"
    )
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") == "error":
        raise ValueError(data.get("message", "Twelve Data error"))
    vals = list(reversed(data["values"]))
    candles = [
        {
            "o": float(v["open"]),
            "h": float(v["high"]),
            "l": float(v["low"]),
            "c": float(v["close"]),
            "v": float(v.get("volume", 500)),
        }
        for v in vals
    ]
    return build_indicators(candles)


def get_market_data(asset: str) -> tuple[dict, str]:
    """Returns (market_data, source_label)"""
    if TWELVE_DATA_KEY:
        try:
            return fetch_twelve_data(asset), "LIVE"
        except Exception as e:
            logger.warning(f"Twelve Data failed for {asset}: {e}")
    return gen_sim(asset), "SIMULATED"


# ══════════════════════════════════════════════
#  NY SESSION CHECK
# ══════════════════════════════════════════════

def is_ny_session() -> tuple[bool, str]:
    now_ny = datetime.now(NY_TZ)
    h = now_ny.hour
    time_str = now_ny.strftime("%I:%M %p EST")
    return (8 <= h < 17), time_str


# ══════════════════════════════════════════════
#  CLAUDE AI SIGNAL  (exact prompt from HTML)
# ══════════════════════════════════════════════

def call_claude(asset: str, d: dict, source: str) -> dict:
    c = CFG[asset]
    pre = c["pre"]

    def f(v): return f"{v:.{c['dec']}f}"

    src_note = ("LIVE market data from Twelve Data"
                if source == "LIVE" else "SIMULATED price data")

    prompt = f"""You are a professional forex/commodity signal analyst for {asset} ({c['name']}) during New York session.

DATA SOURCE: {src_note}
ASSET: {asset} | TIMEFRAME: 4H (HTF) + {c['ltf']} (LTF entry)
Current Price: {pre}{f(d['curr'])}
Change: {'+' if d['chg'] >= 0 else ''}{f(d['chg'])} ({'+' if d['chg_pct'] >= 0 else ''}{d['chg_pct']:.2f}%)
HTF Trend: {d['trend']} (Strength: {d['ts']}/4)
EMA 20: {pre}{f(d['e20'])} | EMA 50: {pre}{f(d['e50'])} | EMA 200: {pre}{f(d['e200'])}
RSI(14): {d['rsi']:.1f} | VWAP: {pre}{f(d['vwap'])} (price {'ABOVE' if d['curr'] > d['vwap'] else 'BELOW'})
Volume: {d['vol_state']}
Resistances: {', '.join(pre + f(v) for v in d['res'])}
Supports: {', '.join(pre + f(v) for v in d['sup'])}
Session: NY ACTIVE (8AM–5PM EST)

Rules: Trade with HTF trend only. RSI must not be >70 for BUY or <30 for SELL. VWAP must align. EMA stack must confirm. Only 1:2 or 1:3 RR. Output NO_SIGNAL unless ≥4 conditions met (70%+ quality filter).

Respond ONLY with valid JSON (no markdown):
{{"signal":"BUY"or"SELL"or"NO_SIGNAL","htf_analysis":"2-3 sentence HTF view","confirmations":["list"],"entry":number,"sl":number,"tp1":number,"tp2":number,"rr1":"1:2","rr2":"1:3","confidence":number 65-82,"entry_trigger":"LTF entry description","invalidation":"invalidation rule","trade_notes":"risk management note"}}"""

    headers = {
        "Content-Type": "application/json",
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
    }
    body = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 1000,
        "messages": [{"role": "user", "content": prompt}],
    }
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers=headers, json=body, timeout=30
    )
    resp.raise_for_status()
    data = resp.json()
    txt = "".join(b["text"] for b in data["content"] if b["type"] == "text")
    txt = txt.replace("```json", "").replace("```", "").strip()
    return json.loads(txt)


# ══════════════════════════════════════════════
#  FALLBACK SIGNAL  (exact logic from HTML)
# ══════════════════════════════════════════════

def fallback_sig(d: dict, asset: str) -> dict:
    c = CFG[asset]

    def f(v): return round(v, c["dec"])

    sig = "NO_SIGNAL"
    entry = sl = tp1 = tp2 = d["curr"]

    if (d["trend"] == "BULLISH" and 45 < d["rsi"] < 67
            and d["curr"] > d["vwap"] and d["ts"] >= 3):
        dist = max(abs(d["curr"] - d["e50"]) * 0.75, d["curr"] * 0.004)
        sig = "BUY"
        sl   = d["curr"] - dist
        tp1  = d["curr"] + dist * 2
        tp2  = d["curr"] + dist * 3
    elif (d["trend"] == "BEARISH" and 33 < d["rsi"] < 55
          and d["curr"] < d["vwap"] and d["ts"] <= 1):
        dist = max(abs(d["e50"] - d["curr"]) * 0.75, d["curr"] * 0.004)
        sig = "SELL"
        sl   = d["curr"] + dist
        tp1  = d["curr"] - dist * 2
        tp2  = d["curr"] - dist * 3

    conf = int(68 + random.random() * 12) if sig != "NO_SIGNAL" else 0
    ema_desc = ("bullish" if d["e20"] > d["e50"] > d["e200"]
                else "bearish" if d["e20"] < d["e50"] < d["e200"]
                else "mixed")

    return {
        "signal": sig,
        "htf_analysis": (
            f"{d['trend']} bias on 4H/Daily. EMA stack {ema_desc}. "
            f"Price {'above' if d['curr'] > d['e200'] else 'below'} EMA 200. "
            f"RSI {d['rsi']:.1f}."
        ),
        "confirmations": [
            f"Trend: {d['trend']}",
            f"RSI: {d['rsi']:.1f}",
            f"VWAP: {'Price above' if d['curr'] > d['vwap'] else 'Price below'}",
            f"Volume: {d['vol_state']}",
            f"EMA 200: {'Price above (bullish)' if d['curr'] > d['e200'] else 'Price below (bearish)'}",
        ],
        "entry": f(entry), "sl": f(sl), "tp1": f(tp1), "tp2": f(tp2),
        "rr1": "1:2", "rr2": "1:3", "confidence": conf,
        "entry_trigger": (
            f"On {c['ltf']}: wait for pullback to EMA 20, confirm with "
            f"{'bullish' if sig == 'BUY' else 'bearish'} engulfing or pin-bar candle before entry."
            if sig != "NO_SIGNAL" else "No clean LTF setup."
        ),
        "invalidation": (
            f"Trade off if 4H closes {'below EMA 50' if sig == 'BUY' else 'above EMA 50'} before TP is hit."
            if sig != "NO_SIGNAL" else "N/A"
        ),
        "trade_notes": (
            "Move SL to break-even once TP1 is hit. Let TP2 run."
            if sig != "NO_SIGNAL"
            else "Conditions do not meet 70%+ quality filter. Wait for full confluence."
        ),
    }


# ══════════════════════════════════════════════
#  FORMAT TELEGRAM MESSAGE
# ══════════════════════════════════════════════

def format_signal_message(asset: str, sig: dict, d: dict, source: str, time_str: str) -> str:
    c = CFG[asset]
    pre = c["pre"]

    def f(v): return f"{pre}{v:.{c['dec']}f}"

    signal = sig["signal"]
    emoji  = c["emoji"]

    if signal == "NO_SIGNAL":
        return (
            f"{emoji} *{c['name']} ({asset})* — NY Session Active\n"
            f"🕐 {time_str}  |  📡 {source}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⏳ *NO HIGH-PROBABILITY SETUP*\n\n"
            f"Conditions don't meet the 70%+ quality filter.\n"
            f"Patience — wait for full confluence.\n\n"
            f"💬 *AI View:* {sig.get('htf_analysis', '—')}\n\n"
            f"📊 *Market Snapshot*\n"
            f"  Price: {f(d['curr'])}  ({'+' if d['chg_pct'] >= 0 else ''}{d['chg_pct']:.2f}%)\n"
            f"  Trend: {d['trend']}  |  RSI: {d['rsi']:.1f}\n"
            f"  VWAP: {f(d['vwap'])}  |  Vol: {d['vol_state']}\n\n"
            f"⚠️ _Educational use only. Not financial advice._"
        )

    direction_emoji = "📈" if signal == "BUY" else "📉"
    dir_word = "BUY" if signal == "BUY" else "SELL"

    confs = "\n".join(f"  ✅ {x}" for x in sig.get("confirmations", []))

    return (
        f"{'🟢' if signal == 'BUY' else '🔴'} *SIGNAL ALERT — {c['name']} ({asset})*\n"
        f"🕐 {time_str}  |  📡 {source}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{direction_emoji} *{dir_word}* — {sig['confidence']}% Confidence\n"
        f"🎯 RR: {sig['rr1']} & {sig['rr2']}\n\n"
        f"📌 *Trade Levels*\n"
        f"  Entry:    `{f(sig['entry'])}`\n"
        f"  Stop Loss: `{f(sig['sl'])}`\n"
        f"  TP1 (1:2): `{f(sig['tp1'])}`\n"
        f"  TP2 (1:3): `{f(sig['tp2'])}`\n\n"
        f"📊 *HTF Market View*\n"
        f"  {sig.get('htf_analysis', '—')}\n\n"
        f"✅ *Confluences*\n{confs}\n\n"
        f"⚡ *LTF Entry Trigger ({c['ltf']})*\n"
        f"  {sig.get('entry_trigger', '—')}\n\n"
        f"❌ *Invalidation*\n"
        f"  {sig.get('invalidation', '—')}\n\n"
        f"📝 *Risk Management*\n"
        f"  {sig.get('trade_notes', '—')}\n\n"
        f"📐 *Indicators*\n"
        f"  EMA20: {f(d['e20'])}  |  EMA50: {f(d['e50'])}  |  EMA200: {f(d['e200'])}\n"
        f"  RSI: {d['rsi']:.1f}  |  VWAP: {f(d['vwap'])}  |  Vol: {d['vol_state']}\n\n"
        f"⚠️ _Educational use only. Not financial advice._"
    )


# ══════════════════════════════════════════════
#  CORE SCAN FUNCTION
# ══════════════════════════════════════════════

async def run_scan(asset: str) -> tuple[str, dict]:
    """Returns (formatted_message, signal_dict)"""
    in_session, time_str = is_ny_session()

    if not in_session:
        _, time_str_now = is_ny_session()
        return (
            f"🔒 *NY Session Closed*\n"
            f"Signals only run 8:00 AM – 5:00 PM EST.\n"
            f"Current NY time: {time_str_now}"
        ), {}

    d, source = get_market_data(asset)

    try:
        sig = call_claude(asset, d, source)
    except Exception as e:
        logger.warning(f"Claude API failed: {e} — using fallback")
        sig = fallback_sig(d, asset)

    last_signals[asset] = sig
    msg = format_signal_message(asset, sig, d, source, time_str)
    return msg, sig


# ══════════════════════════════════════════════
#  AUTO-SCAN JOB  (runs every hour during NY)
# ══════════════════════════════════════════════

async def auto_scan_job(context: ContextTypes.DEFAULT_TYPE):
    in_session, _ = is_ny_session()
    if not in_session:
        return

    for chat_id in list(auto_scan_chats):
        for asset in ["XAUUSD", "NAS100"]:
            try:
                msg, _ = await run_scan(asset)
                await context.bot.send_message(
                    chat_id=chat_id, text=msg,
                    parse_mode="Markdown"
                )
                await asyncio.sleep(2)
            except Exception as e:
                logger.error(f"Auto-scan send failed for {chat_id}/{asset}: {e}")


# ══════════════════════════════════════════════
#  TELEGRAM COMMAND HANDLERS
# ══════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚡ *ForexBot AI — Telegram Edition*\n"
        "NAS100 & GOLD · NY Session Only\n\n"
        "I scan for high-probability BUY/SELL signals using:\n"
        "  • Claude AI analysis\n"
        "  • EMA 20/50/200 stack\n"
        "  • RSI (14) · VWAP · Volume\n"
        "  • Support & Resistance\n"
        "  • 1:2 & 1:3 Risk/Reward\n\n"
        "🕐 Signals: *8:00 AM – 5:00 PM EST only*\n\n"
        "📋 *Commands:*\n"
        "/scan — Scan both assets now\n"
        "/gold — Scan GOLD only\n"
        "/nas  — Scan NAS100 only\n"
        "/status — Session status\n"
        "/auto — Toggle hourly auto-alerts\n"
        "/help — Show this menu\n\n"
        "⚠️ _Educational use only. Not financial advice._",
        parse_mode="Markdown"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Scanning GOLD & NAS100... please wait.")
    for asset in ["XAUUSD", "NAS100"]:
        msg, _ = await run_scan(asset)
        await update.message.reply_text(msg, parse_mode="Markdown")
        await asyncio.sleep(1)


async def cmd_gold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Scanning GOLD (XAUUSD)...")
    msg, _ = await run_scan("XAUUSD")
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_nas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Scanning NAS100...")
    msg, _ = await run_scan("NAS100")
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    in_session, time_str = is_ny_session()
    sess_line = "🟢 NY SESSION ACTIVE" if in_session else "🔴 NY SESSION CLOSED"
    data_src = "LIVE (Twelve Data)" if TWELVE_DATA_KEY else "SIMULATED"

    lines = [
        f"📊 *ForexBot Status*\n",
        f"🕐 NY Time: {time_str}",
        f"Session: {sess_line}",
        f"Data: {data_src}",
        f"Auto-alerts this chat: {'✅ ON' if update.effective_chat.id in auto_scan_chats else '❌ OFF'}",
    ]

    for asset in ["XAUUSD", "NAS100"]:
        try:
            d, src = get_market_data(asset)
            c = CFG[asset]
            pre = c["pre"]
            sign = "+" if d["chg_pct"] >= 0 else ""
            lines.append(
                f"\n{c['emoji']} *{c['name']}*: {pre}{d['curr']:.{c['dec']}f} "
                f"({sign}{d['chg_pct']:.2f}%) | {d['trend']} | RSI {d['rsi']:.1f}"
            )
        except Exception:
            lines.append(f"\n{CFG[asset]['emoji']} {asset}: Data unavailable")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_auto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in auto_scan_chats:
        auto_scan_chats.discard(chat_id)
        await update.message.reply_text(
            "🔕 *Auto-alerts OFF*\nHourly scanning disabled for this chat.\n"
            "Use /auto again to re-enable.",
            parse_mode="Markdown"
        )
    else:
        auto_scan_chats.add(chat_id)
        await update.message.reply_text(
            "🔔 *Auto-alerts ON*\n"
            "I'll scan GOLD & NAS100 every hour during the NY session (8AM–5PM EST) "
            "and send signals here automatically.\n\n"
            "Use /auto again to turn off.",
            parse_mode="Markdown"
        )


# ══════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════

def main():
    if BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
        print("❌  Please set BOT_TOKEN at the top of the file.")
        return
    if ANTHROPIC_API_KEY == "YOUR_ANTHROPIC_API_KEY":
        print("❌  Please set ANTHROPIC_API_KEY at the top of the file.")
        return

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("scan",   cmd_scan))
    app.add_handler(CommandHandler("gold",   cmd_gold))
    app.add_handler(CommandHandler("nas",    cmd_nas))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("auto",   cmd_auto))

    # Auto-scan job — runs every 60 minutes
    app.job_queue.run_repeating(auto_scan_job, interval=3600, first=60)

    print("⚡ ForexBot AI is running...")
    print("   Open Telegram and message your bot to start.")
    app.run_polling()


if __name__ == "__main__":
    main()
