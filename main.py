import os
import io
import time
import math
import logging
import threading
from datetime import datetime
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
import numpy as np
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

import telebot


# ============================================================
# QUANTUM SCALPER V4
# ============================================================
# Strategy engine:
# 1. Horizontal Level Breakout
# 2. Trendline Compression Breakout
# 3. Momentum Breakout
#
# Timeframes:
# 1H  -> market structure / direction
# 15M -> setup / level / compression
# 5M  -> entry confirmation
#
# IMPORTANT:
# This bot sends signals only when the setup is formed BEFORE
# the expected breakout, so the user has time to prepare an order.
# ============================================================


# ============================================================
# CONFIG
# ============================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()

OKX_BASE_URL = "https://openapi.okx.com"

MIN_VOLUME_24H = 60_000_000
MIN_SCORE = 80

SCAN_INTERVAL = 30

# We analyse the highest-volume liquid markets.
# The volume filter is applied to the whole ticker list first.
MAX_CANDIDATES = 20

# Signal frequency protection
GLOBAL_SIGNAL_COOLDOWN = 12 * 60          # 12 min
SYMBOL_SIGNAL_COOLDOWN = 60 * 60          # 60 min
MAX_SIGNALS_PER_HOUR = 5

# A signal should be close enough to the trigger
# to remain actionable, but not already extended.
MAX_ENTRY_DISTANCE = 0.0040               # 0.40%

# Avoid extremely tight / noisy setups.
MIN_ATR_PERCENT = 0.0015                  # 0.15%
MAX_ATR_PERCENT = 0.0350                   # 3.50%

KYIV = ZoneInfo("Europe/Kyiv")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

log = logging.getLogger("quantum")

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is missing")

if not CHANNEL_ID:
    raise RuntimeError("CHANNEL_ID is missing")

bot = telebot.TeleBot(TELEGRAM_TOKEN)

session = requests.Session()
session.headers.update({
    "User-Agent": "QuantumScalper/4.0"
})


# ============================================================
# STATE
# ============================================================

last_signal_by_symbol = {}
signal_times = []

oi_history = {}

last_morning_message_date = None
scan_number = 0


# ============================================================
# BASIC HELPERS
# ============================================================

def now_kyiv():
    return datetime.now(KYIV)


def fmt_price(value):
    try:
        p = float(value)

        if p >= 1000:
            return f"{p:,.2f}".replace(",", " ")

        if p >= 100:
            return f"{p:.2f}"

        if p >= 1:
            return f"{p:.4f}".rstrip("0").rstrip(".")

        if p >= 0.01:
            return f"{p:.6f}".rstrip("0").rstrip(".")

        return f"{p:.8f}".rstrip("0").rstrip(".")

    except Exception:
        return str(value)


def fmt_money(value):
    try:
        v = float(value)

        if v >= 1_000_000_000:
            return f"${v / 1_000_000_000:.2f}B"

        if v >= 1_000_000:
            return f"${v / 1_000_000:.1f}M"

        if v >= 1_000:
            return f"${v / 1_000:.1f}K"

        return f"${v:.0f}"

    except Exception:
        return "$0"


def safe_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def clamp(value, low, high):
    return max(low, min(high, value))


def percent(a, b):
    if b == 0:
        return 0.0
    return (a - b) / b * 100.0


def score_label(score):
    if score >= 95:
        return "ELITE"
    if score >= 90:
        return "PREMIUM"
    if score >= 85:
        return "STRONG"
    return "STANDARD"


# ============================================================
# OKX API
# ============================================================

def okx_get(path, params=None, timeout=8):
    """
    Public OKX request with retries.

    We deliberately do not let a failed request kill the scanner.
    """

    last_error = None

    for attempt in range(3):
        try:
            response = session.get(
                OKX_BASE_URL + path,
                params=params,
                timeout=timeout
            )

            response.raise_for_status()

            data = response.json()

            if str(data.get("code")) != "0":
                raise RuntimeError(
                    f"OKX error {data.get('code')}: {data.get('msg')}"
                )

            return data.get("data", [])

        except Exception as exc:
            last_error = exc

            if attempt < 2:
                time.sleep(0.8 * (attempt + 1))

    log.warning("OKX request failed: %s %s", path, last_error)
    return None


def get_tickers():
    return okx_get(
        "/api/v5/market/tickers",
        {"instType": "SWAP"}
    )


def get_candles(inst_id, bar, limit=100):
    data = okx_get(
        "/api/v5/market/candles",
        {
            "instId": inst_id,
            "bar": bar,
            "limit": str(limit)
        }
    )

    if not data:
        return []

    candles = []

    for row in data:
        if len(row) < 9:
            continue

        candles.append({
            "ts": int(row[0]),
            "o": safe_float(row[1]),
            "h": safe_float(row[2]),
            "l": safe_float(row[3]),
            "c": safe_float(row[4]),
            "vol": safe_float(row[5]),
            "vol_ccy": safe_float(row[6]),
            "confirm": row[8]
        })

    # OKX normally returns newest first.
    candles.sort(key=lambda x: x["ts"])

    # Ignore the currently forming candle.
    if candles and candles[-1]["confirm"] != "1":
        candles = candles[:-1]

    return candles


def get_open_interest(inst_id):
    data = okx_get(
        "/api/v5/public/open-interest",
        {
            "instType": "SWAP",
            "instId": inst_id
        }
    )

    if not data:
        return 0.0

    return safe_float(data[0].get("oi"), 0.0)


# ============================================================
# MARKET SELECTION
# ============================================================

def get_liquid_markets():
    """
    IMPORTANT:
    The $60M filter is applied BEFORE technical analysis.

    Therefore low-volume coins never reach the expensive
    1H / 15M / 5M analysis.
    """

    tickers = get_tickers()

    if not tickers:
        return []

    markets = []

    for ticker in tickers:

        inst_id = ticker.get("instId", "")

        if not inst_id.endswith("-USDT-SWAP"):
            continue

        last = safe_float(ticker.get("last"))
        volume = safe_float(ticker.get("volCcy24h"))

        if last <= 0:
            continue

        if volume < MIN_VOLUME_24H:
            continue

        markets.append({
            "inst_id": inst_id,
            "coin": inst_id.split("-")[0],
            "price": last,
            "volume": volume,
            "high24h": safe_float(ticker.get("high24h")),
            "low24h": safe_float(ticker.get("low24h"))
        })

    markets.sort(
        key=lambda x: x["volume"],
        reverse=True
    )

    return markets[:MAX_CANDIDATES]


# ============================================================
# INDICATORS
# ============================================================

def ema(values, period):
    values = np.asarray(values, dtype=float)

    if len(values) < period:
        return None

    alpha = 2.0 / (period + 1.0)

    result = np.zeros_like(values)
    result[0] = values[0]

    for i in range(1, len(values)):
        result[i] = alpha * values[i] + (1 - alpha) * result[i - 1]

    return result


def atr(candles, period=14):
    if len(candles) < period + 1:
        return 0.0

    highs = np.array([x["h"] for x in candles], dtype=float)
    lows = np.array([x["l"] for x in candles], dtype=float)
    closes = np.array([x["c"] for x in candles], dtype=float)

    tr = []

    for i in range(1, len(candles)):
        tr.append(
            max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1])
            )
        )

    return float(np.mean(tr[-period:]))


def volume_ratio(candles, lookback=20):
    if len(candles) < lookback + 1:
        return 1.0

    current = candles[-1]["vol_ccy"]

    previous = [
        x["vol_ccy"]
        for x in candles[-lookback - 1:-1]
        if x["vol_ccy"] > 0
    ]

    if not previous:
        return 1.0

    avg = float(np.mean(previous))

    if avg == 0:
        return 1.0

    return current / avg


def linreg_slope(values):
    if len(values) < 2:
        return 0.0

    y = np.asarray(values, dtype=float)
    x = np.arange(len(y), dtype=float)

    slope = np.polyfit(x, y, 1)[0]

    return float(slope)


# ============================================================
# 1H MARKET STRUCTURE
# ============================================================

def analyse_1h(candles):
    if len(candles) < 60:
        return {
            "direction": "NEUTRAL",
            "score": 0,
            "text": "INSUFFICIENT DATA"
        }

    closes = np.array([x["c"] for x in candles], dtype=float)

    e20 = ema(closes, 20)
    e50 = ema(closes, 50)

    if e20 is None or e50 is None:
        return {
            "direction": "NEUTRAL",
            "score": 0,
            "text": "INSUFFICIENT DATA"
        }

    price = closes[-1]

    slope20 = linreg_slope(e20[-12:])

    bullish = (
        price > e20[-1]
        and e20[-1] > e50[-1]
        and slope20 > 0
    )

    bearish = (
        price < e20[-1]
        and e20[-1] < e50[-1]
        and slope20 < 0
    )

    if bullish:
        return {
            "direction": "LONG",
            "score": 20,
            "text": "BULLISH STRUCTURE"
        }

    if bearish:
        return {
            "direction": "SHORT",
            "score": 20,
            "text": "BEARISH STRUCTURE"
        }

    # weaker structure
    if price > e20[-1]:
        return {
            "direction": "LONG",
            "score": 11,
            "text": "WEAK BULLISH"
        }

    if price < e20[-1]:
        return {
            "direction": "SHORT",
            "score": 11,
            "text": "WEAK BEARISH"
        }

    return {
        "direction": "NEUTRAL",
        "score": 0,
        "text": "NEUTRAL"
    }


# ============================================================
# LEVEL DETECTION
# ============================================================

def detect_horizontal_level(candles, direction):
    """
    Finds a recent swing level.

    For LONG:
        resistance = recent swing highs

    For SHORT:
        support = recent swing lows
    """

    if len(candles) < 30:
        return None

    recent = candles[-45:-2]

    if direction == "LONG":

        highs = np.array(
            [x["h"] for x in recent],
            dtype=float
        )

        level = float(np.percentile(highs, 85))

        # closest meaningful high cluster
        candidates = [
            x["h"]
            for x in recent
            if x["h"] >= level
        ]

        if not candidates:
            return None

        return float(np.median(candidates))

    else:

        lows = np.array(
            [x["l"] for x in recent],
            dtype=float
        )

        level = float(np.percentile(lows, 15))

        candidates = [
            x["l"]
            for x in recent
            if x["l"] <= level
        ]

        if not candidates:
            return None

        return float(np.median(candidates))


# ============================================================
# STRATEGY 1
# HORIZONTAL LEVEL BREAKOUT
# ============================================================

def horizontal_strategy(c15, c5, direction, current_price):
    level = detect_horizontal_level(c15, direction)

    if not level or level <= 0:
        return None

    atr5 = atr(c5)

    if atr5 <= 0:
        return None

    distance = abs(current_price - level) / current_price

    if distance > MAX_ENTRY_DISTANCE:
        return None

    last5 = c5[-1]
    prev5 = c5[-2]

    vr = volume_ratio(c5)

    if direction == "LONG":

        approaching = current_price <= level

        candle_confirm = (
            last5["c"] > prev5["c"]
            and last5["c"] > last5["o"]
        )

        # We want price close to resistance,
        # but NOT already too far above it.
        if not approaching and current_price > level * 1.003:
            return None

        confirmation = candle_confirm

        strength = 0

        if approaching:
            strength += 20

        if confirmation:
            strength += 15

        if vr >= 1.15:
            strength += 5

        return {
            "name": "Horizontal Level Breakout",
            "level": level,
            "score": strength,
            "ready": True,
            "reason": "Цена поджата к сопротивлению; 5M показывает бычье давление."
        }

    else:

        approaching = current_price >= level

        candle_confirm = (
            last5["c"] < prev5["c"]
            and last5["c"] < last5["o"]
        )

        if not approaching and current_price < level * 0.997:
            return None

        confirmation = candle_confirm

        strength = 0

        if approaching:
            strength += 20

        if confirmation:
            strength += 15

        if vr >= 1.15:
            strength += 5

        return {
            "name": "Horizontal Level Breakout",
            "level": level,
            "score": strength,
            "ready": True,
            "reason": "Цена поджата к поддержке; 5M показывает медвежье давление."
        }


# ============================================================
# STRATEGY 2
# TRENDLINE COMPRESSION BREAKOUT
# ============================================================

def trendline_strategy(c15, c5, direction, current_price):
    if len(c15) < 35:
        return None

    closes = np.array(
        [x["c"] for x in c15[-30:]],
        dtype=float
    )

    highs = np.array(
        [x["h"] for x in c15[-30:]],
        dtype=float
    )

    lows = np.array(
        [x["l"] for x in c15[-30:]],
        dtype=float
    )

    upper_slope = linreg_slope(highs)
    lower_slope = linreg_slope(lows)

    width_now = highs[-1] - lows[-1]
    width_old = highs[:8].mean() - lows[:8].mean()

    if width_old <= 0:
        return None

    compression = width_now / width_old

    # Need visible compression.
    if compression > 0.72:
        return None

    if direction == "LONG":

        # descending resistance + rising support
        if upper_slope >= 0 or lower_slope <= 0:
            return None

        resistance = float(
            np.mean(highs[-5:])
        )

        distance = abs(current_price - resistance) / current_price

        if distance > MAX_ENTRY_DISTANCE:
            return None

        return {
            "name": "Trendline Compression Breakout",
            "level": resistance,
            "score": 25,
            "ready": True,
            "reason": "15M сжимается между нисходящей верхней и восходящей нижней линиями."
        }

    else:

        if upper_slope <= 0 or lower_slope >= 0:
            return None

        support = float(
            np.mean(lows[-5:])
        )

        distance = abs(current_price - support) / current_price

        if distance > MAX_ENTRY_DISTANCE:
            return None

        return {
            "name": "Trendline Compression Breakout",
            "level": support,
            "score": 25,
            "ready": True,
            "reason": "15M сжимается между восходящей верхней и нисходящей нижней линиями."
        }


# ============================================================
# STRATEGY 3
# MOMENTUM BREAKOUT
# ============================================================

def momentum_strategy(c5, direction, current_price):
    if len(c5) < 30:
        return None

    closes = np.array(
        [x["c"] for x in c5],
        dtype=float
    )

    atr5 = atr(c5)

    if atr5 <= 0:
        return None

    e9 = ema(closes, 9)
    e20 = ema(closes, 20)

    if e9 is None or e20 is None:
        return None

    recent_high = max(
        x["h"] for x in c5[-12:-1]
    )

    recent_low = min(
        x["l"] for x in c5[-12:-1]
    )

    vr = volume_ratio(c5)

    if direction == "LONG":

        if e9[-1] <= e20[-1]:
            return None

        if current_price < recent_high * 0.996:
            return None

        if vr < 1.10:
            return None

        return {
            "name": "Momentum Breakout",
            "level": recent_high,
            "score": 24,
            "ready": True,
            "reason": "5M EMA9 выше EMA20, цена у локального импульсного максимума, объём повышен."
        }

    else:

        if e9[-1] >= e20[-1]:
            return None

        if current_price > recent_low * 1.004:
            return None

        if vr < 1.10:
            return None

        return {
            "name": "Momentum Breakout",
            "level": recent_low,
            "score": 24,
            "ready": True,
            "reason": "5M EMA9 ниже EMA20, цена у локального импульсного минимума, объём повышен."
        }


# ============================================================
# OI ANALYSIS
# ============================================================

def update_oi(inst_id, oi):
    now = time.time()

    if inst_id not in oi_history:
        oi_history[inst_id] = []

    oi_history[inst_id].append((now, oi))

    # Keep last 10 minutes.
    cutoff = now - 600

    oi_history[inst_id] = [
        item
        for item in oi_history[inst_id]
        if item[0] >= cutoff
    ]


def get_oi_state(inst_id, current_oi, direction):
    if current_oi <= 0:
        return "UNAVAILABLE", 0

    history = oi_history.get(inst_id, [])

    if len(history) < 2:
        return "WAITING", 0

    # Compare against approximately 2 minutes ago.
    target_time = time.time() - 120

    old = min(
        history,
        key=lambda x: abs(x[0] - target_time)
    )

    old_oi = old[1]

    if old_oi <= 0:
        return "WAITING", 0

    change = (current_oi - old_oi) / old_oi * 100

    if abs(change) < 0.35:
        return "FLAT", 0

    if change > 0:

        # Rising OI is useful when direction agrees with structure.
        return "RISING", 8

    return "FALLING", 2


# ============================================================
# SCORE
# ============================================================

def calculate_score(
    strategy,
    structure,
    c15,
    c5,
    volume24h,
    oi_state,
    oi_points,
    direction
):
    score = 0

    # Strategy quality: max 25
    score += min(strategy["score"], 25)

    # 1H structure: max 20
    score += min(structure["score"], 20)

    # Volume quality: max 15
    if volume24h >= 500_000_000:
        score += 15
    elif volume24h >= 250_000_000:
        score += 13
    elif volume24h >= 100_000_000:
        score += 10
    else:
        score += 7

    # 5M volume confirmation: max 10
    vr5 = volume_ratio(c5)

    if vr5 >= 1.50:
        score += 10
    elif vr5 >= 1.25:
        score += 8
    elif vr5 >= 1.10:
        score += 5

    # 15M structure / compression: max 10
    vr15 = volume_ratio(c15)

    if vr15 >= 1.25:
        score += 10
    elif vr15 >= 1.10:
        score += 7
    else:
        score += 4

    # OI: max 8
    score += min(oi_points, 8)

    # Price action candle quality: max 7
    last = c5[-1]

    body = abs(last["c"] - last["o"])
    candle_range = max(last["h"] - last["l"], 1e-12)

    body_ratio = body / candle_range

    if body_ratio >= 0.65:
        score += 7
    elif body_ratio >= 0.45:
        score += 5
    elif body_ratio >= 0.30:
        score += 3

    # Do not allow a fake direction.
    if direction == "LONG" and last["c"] < last["o"]:
        score -= 4

    if direction == "SHORT" and last["c"] > last["o"]:
        score -= 4

    return int(clamp(score, 0, 100))


# ============================================================
# RISK / TRADE PLAN
# ============================================================

def build_trade_plan(direction, level, current_price, atr5):
    """
    Entry is built around the breakout level.

    We intentionally don't wait for a huge breakout candle.
    The level is the trigger area.

    Stop is based on volatility rather than an arbitrary fixed 1%.
    """

    if direction == "LONG":

        entry_low = level * 0.9990
        entry_high = level * 1.0015

        entry = level

        sl_distance = max(
            atr5 * 1.25,
            entry * 0.006
        )

        sl = entry - sl_distance

        risk = (entry - sl) / entry

        tp1 = entry + risk * entry * 1.20
        tp2 = entry + risk * entry * 2.00
        tp3 = entry + risk * entry * 3.00

    else:

        entry_low = level * 0.9985
        entry_high = level * 1.0010

        entry = level

        sl_distance = max(
            atr5 * 1.25,
            entry * 0.006
        )

        sl = entry + sl_distance

        risk = (sl - entry) / entry

        tp1 = entry - risk * entry * 1.20
        tp2 = entry - risk * entry * 2.00
        tp3 = entry - risk * entry * 3.00

    # Avoid nonsensical plans.
    if direction == "LONG":
        if sl >= entry or tp1 <= entry:
            return None
    else:
        if sl <= entry or tp1 >= entry:
            return None

    return {
        "entry": entry,
        "entry_low": entry_low,
        "entry_high": entry_high,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "risk_pct": risk * 100
    }


# ============================================================
# SIGNAL COOLDOWN
# ============================================================

def clean_signal_times():
    global signal_times

    cutoff = time.time() - 3600

    signal_times = [
        t for t in signal_times
        if t >= cutoff
    ]


def can_send_signal(inst_id):
    clean_signal_times()

    if len(signal_times) >= MAX_SIGNALS_PER_HOUR:
        return False

    now = time.time()

    last_global = signal_times[-1] if signal_times else 0

    if now - last_global < GLOBAL_SIGNAL_COOLDOWN:
        return False

    last_symbol = last_signal_by_symbol.get(inst_id, 0)

    if now - last_symbol < SYMBOL_SIGNAL_COOLDOWN:
        return False

    return True


# ============================================================
# CHART
# ============================================================

def create_chart(
    coin,
    candles,
    direction,
    level,
    plan,
    strategy_name
):
    """
    Creates a clean 5M chart.

    The image is sent BEFORE the text message,
    so Telegram shows the chart above the signal.
    """

    data = candles[-60:]

    x = np.arange(len(data))

    opens = np.array([x["o"] for x in data])
    highs = np.array([x["h"] for x in data])
    lows = np.array([x["l"] for x in data])
    closes = np.array([x["c"] for x in data])

    fig, ax = plt.subplots(
        figsize=(12, 6),
        dpi=130
    )

    fig.patch.set_facecolor("#0b1020")
    ax.set_facecolor("#0b1020")

    for i in range(len(data)):

        color = "#22c55e" if closes[i] >= opens[i] else "#ef4444"

        ax.plot(
            [i, i],
            [lows[i], highs[i]],
            color=color,
            linewidth=1
        )

        bottom = min(opens[i], closes[i])
        height = abs(closes[i] - opens[i])

        if height == 0:
            height = max(
                (highs[i] - lows[i]) * 0.02,
                1e-12
            )

        ax.add_patch(
            plt.Rectangle(
                (i - 0.32, bottom),
                0.64,
                height,
                facecolor=color,
                edgecolor=color
            )
        )

    ax.axhline(
        level,
        color="#facc15",
        linewidth=2,
        linestyle="--",
        label="BREAKOUT LEVEL"
    )

    ax.axhline(
        plan["sl"],
        color="#ef4444",
        linewidth=1.5,
        linestyle=":",
        label="STOP"
    )

    ax.axhline(
        plan["tp1"],
        color="#38bdf8",
        linewidth=1,
        linestyle=":",
        label="TP1"
    )

    ax.axhline(
        plan["tp2"],
        color="#38bdf8",
        linewidth=1,
        linestyle=":",
        label="TP2"
    )

    ax.axhline(
        plan["tp3"],
        color="#a78bfa",
        linewidth=1,
        linestyle=":",
        label="TP3"
    )

    ax.set_title(
        f"{coin}/USDT • 5M • {direction} • {strategy_name}",
        color="white",
        fontsize=14,
        fontweight="bold"
    )

    ax.tick_params(
        colors="#94a3b8"
    )

    for spine in ax.spines.values():
        spine.set_color("#334155")

    ax.grid(
        alpha=0.12,
        color="white"
    )

    ax.legend(
        facecolor="#111827",
        edgecolor="#334155",
        labelcolor="white"
    )

    plt.tight_layout()

    buffer = io.BytesIO()

    plt.savefig(
        buffer,
        format="png",
        bbox_inches="tight",
        facecolor=fig.get_facecolor()
    )

    plt.close(fig)

    buffer.seek(0)

    return buffer


# ============================================================
# TELEGRAM SIGNAL
# ============================================================

def build_signal_text(
    market,
    direction,
    strategy,
    structure,
    plan,
    score,
    oi_state,
    c15,
    c5
):
    coin = market["coin"]
    volume = market["volume"]

    quality = score_label(score)

    vr5 = volume_ratio(c5)

    if oi_state == "RISING":
        oi_text = "RISING — подтверждает интерес"
    elif oi_state == "FALLING":
        oi_text = "FALLING — подтверждение слабое"
    elif oi_state == "FLAT":
        oi_text = "FLAT"
    else:
        oi_text = oi_state

    arrow = "🟢 LONG" if direction == "LONG" else "🔴 SHORT"

    risk = plan["risk_pct"]

    return f"""
🔥 <b>{coin}USDT — {direction}</b>

💰 <b>Текущая цена:</b> {fmt_price(market["price"])}
💵 <b>24H объём:</b> {fmt_money(volume)}

⭐ <b>SIGNAL SCORE: {score}/100 — {quality}</b>

🧠 <b>ЛОГИКА СДЕЛКИ</b>

Стратегия: <b>{strategy["name"]}</b>

{strategy["reason"]}

📊 <b>1H:</b> {structure["text"]}
📊 <b>15M:</b> SETUP
⚡ <b>5M:</b> ENTRY CONFIRMATION

💧 <b>ЛИКВИДНОСТЬ:</b> HIGH
📦 <b>ОБЪЁМ:</b> HIGH
⚡ <b>OI:</b> {oi_text}

🎯 <b>ТОЧКА ВХОДА</b>

{fmt_price(plan["entry_low"])} — {fmt_price(plan["entry_high"])}

🛑 <b>STOP LOSS</b>

{fmt_price(plan["sl"])}
Риск: −{risk:.2f}%

🪜 <b>ЗАКРЫТИЕ ЛЕСЕНКОЙ</b>

TP1 — 30%
{fmt_price(plan["tp1"])}

TP2 — 30%
{fmt_price(plan["tp2"])}

TP3 — 40%
{fmt_price(plan["tp3"])}

🔒 После TP1 → SL в BE

📍 <b>Основной уровень:</b> {fmt_price(strategy["level"])}

📈 <b>Рабочий таймфрейм входа:</b> 5M
🧭 <b>Фильтр направления:</b> 1H
🔎 <b>Формирование сетапа:</b> 15M

⚠️ <b>ВАЖНО</b>

Это PRE-ENTRY сигнал около уровня.
Не догоняем цену после сильной свечи.

Если рынок уже ушёл далеко от указанной зоны —
сделку пропускаем.

<b>Качество важнее количества.</b>
""".strip()


# ============================================================
# SEND SIGNAL
# ============================================================

def send_signal(
    market,
    direction,
    strategy,
    structure,
    plan,
    score,
    oi_state,
    c15,
    c5
):
    inst_id = market["inst_id"]

    if not can_send_signal(inst_id):
        log.info(
            "SIGNAL BLOCKED BY COOLDOWN: %s",
            inst_id
        )
        return False

    chart = create_chart(
        market["coin"],
        c5,
        direction,
        strategy["level"],
        plan,
        strategy["name"]
    )

    text = build_signal_text(
        market,
        direction,
        strategy,
        structure,
        plan,
        score,
        oi_state,
        c15,
        c5
    )

    try:
        # Photo first.
        bot.send_photo(
            CHANNEL_ID,
            chart
        )

        # Text second.
        bot.send_message(
            CHANNEL_ID,
            text,
            parse_mode="HTML",
            disable_web_page_preview=True
        )

        last_signal_by_symbol[inst_id] = time.time()
        signal_times.append(time.time())

        log.info(
            "SIGNAL SENT | %s | %s | %s | SCORE=%d",
            inst_id,
            direction,
            strategy["name"],
            score
        )

        return True

    except Exception:
        log.exception(
            "TELEGRAM SIGNAL ERROR: %s",
            inst_id
        )
        return False


# ============================================================
# MORNING MESSAGE
# ============================================================

def send_morning_message_if_needed():
    global last_morning_message_date

    current = now_kyiv()
    current_date = current.date()

    if last_morning_message_date == current_date:
        return

    # Send after 08:00 Kyiv.
    if current.hour < 8:
        return

    message = """
🌅 <b>Доброе утро, ребята!</b>

Начинаем новый торговый день вместе с QUANTUM.

Сегодня работаем спокойно и дисциплинированно:

🧠 ждём только качественные сетапы;
🎯 не догоняем рынок;
🛑 соблюдаем Stop Loss;
💰 не рискуем депозитом одной сделкой;
⏳ если хорошей сделки нет — просто ждём.

Наш принцип:

<b>Качество важнее количества.</b>

Удачного торгового дня! 🚀
""".strip()

    try:
        bot.send_message(
            CHANNEL_ID,
            message,
            parse_mode="HTML"
        )

        last_morning_message_date = current_date

        log.info(
            "MORNING MESSAGE SENT"
        )

    except Exception:
        log.exception(
            "MORNING MESSAGE ERROR"
        )


# ============================================================
# ANALYSE ONE MARKET
# ============================================================

def analyse_market(market):
    inst_id = market["inst_id"]

    try:

        c1h = get_candles(
            inst_id,
            "1H",
            100
        )

        c15 = get_candles(
            inst_id,
            "15m",
            100
        )

        c5 = get_candles(
            inst_id,
            "5m",
            100
        )

        if (
            len(c1h) < 60
            or len(c15) < 50
            or len(c5) < 50
        ):
            log.info(
                "SKIP %s | insufficient candles",
                inst_id
            )
            return

        structure = analyse_1h(c1h)

        if structure["direction"] == "NEUTRAL":
            return

        direction = structure["direction"]

        atr5 = atr(c5)

        if atr5 <= 0:
            return

        atr_pct = atr5 / market["price"]

        if not (
            MIN_ATR_PERCENT
            <= atr_pct
            <= MAX_ATR_PERCENT
        ):
            return

        current_price = market["price"]

        # Current OI
        oi = get_open_interest(inst_id)

        update_oi(
            inst_id,
            oi
        )

        oi_state, oi_points = get_oi_state(
            inst_id,
            oi,
            direction
        )

        strategies = []

        s1 = horizontal_strategy(
            c15,
            c5,
            direction,
            current_price
        )

        if s1:
            strategies.append(s1)

        s2 = trendline_strategy(
            c15,
            c5,
            direction,
            current_price
        )

        if s2:
            strategies.append(s2)

        s3 = momentum_strategy(
            c5,
            direction,
            current_price
        )

        if s3:
            strategies.append(s3)

        if not strategies:
            return

        # Pick the strongest strategy.
        strategies.sort(
            key=lambda x: x["score"],
            reverse=True
        )

        strategy = strategies[0]

        # Price must be reasonably close to trigger level.
        distance = abs(
            current_price - strategy["level"]
        ) / current_price

        if distance > MAX_ENTRY_DISTANCE:
            return

        score = calculate_score(
            strategy,
            structure,
            c15,
            c5,
            market["volume"],
            oi_state,
            oi_points,
            direction
        )

        log.info(
            "CANDIDATE | %s | %s | %s | SCORE=%d | OI=%s | VOL=%s",
            inst_id,
            direction,
            strategy["name"],
            score,
            oi_state,
            fmt_money(market["volume"])
        )

        if score < MIN_SCORE:
            return

        plan = build_trade_plan(
            direction,
            strategy["level"],
            current_price,
            atr5
        )

        if not plan:
            return

        # Avoid absurdly wide stops.
        if plan["risk_pct"] > 3.5:
            return

        send_signal(
            market,
            direction,
            strategy,
            structure,
            plan,
            score,
            oi_state,
            c15,
            c5
        )

    except Exception:
        # One bad coin can NEVER kill the scanner.
        log.exception(
            "MARKET ANALYSIS ERROR: %s",
            inst_id
        )


# ============================================================
# SCANNER
# ============================================================

def scanner_loop():
    global scan_number

    log.info("=" * 50)
    log.info("QUANTUM SCALPER V4 STARTING")
    log.info("MIN 24H VOLUME: $%s", f"{MIN_VOLUME_24H:,}")
    log.info("MIN SCORE: %d", MIN_SCORE)
    log.info("MAX CANDIDATES: %d", MAX_CANDIDATES)
    log.info("SCAN INTERVAL: %ds", SCAN_INTERVAL)
    log.info("TIMEZONE: Europe/Kyiv")
    log.info("=" * 50)

    try:
        bot.send_message(
            CHANNEL_ID,
            """
🚀 <b>QUANTUM SCALPER V4 ONLINE</b>

🟢 OKX Market Data
🟢 24H Volume Filter ≥ $60M
🟢 Open Interest
🟢 1H Structure
🟢 15M Setup
🟢 5M Confirmation
🟢 Telegram
🟢 Chart Generation
🟢 Render Web Service

🧠 <b>3 стратегии:</b>

1️⃣ Horizontal Level Breakout
2️⃣ Trendline Compression Breakout
3️⃣ Momentum Breakout

⭐ <b>Минимальный Score:</b> 80/100

🟡 READY → 🟢 ACTIVE

<b>Качество важнее количества.</b>
""".strip(),
            parse_mode="HTML"
        )

        log.info("STARTUP MESSAGE SENT")

    except Exception:
        log.exception(
            "STARTUP MESSAGE ERROR"
        )

    while True:

        scan_number += 1

        started = time.time()

        log.info("=" * 30)
        log.info("SCAN #%d", scan_number)

        try:

            send_morning_message_if_needed()

            markets = get_liquid_markets()

            if not markets:

                log.warning(
                    "NO MARKETS ABOVE $60M"
                )

            else:

                log.info(
                    "Markets >= $60M: %d",
                    len(markets)
                )

                for index, market in enumerate(markets, start=1):

                    log.info(
                        "ANALYSE %d/%d | %s | VOL=%s",
                        index,
                        len(markets),
                        market["inst_id"],
                        fmt_money(market["volume"])
                    )

                    analyse_market(market)

                    # Small delay prevents unnecessary API bursts.
                    time.sleep(0.15)

        except Exception:
            log.exception(
                "SCAN LOOP ERROR"
            )

        elapsed = time.time() - started

        log.info(
            "SCAN #%d COMPLETE | %.1fs",
            scan_number,
            elapsed
        )

        sleep_for = max(
            2,
            SCAN_INTERVAL - elapsed
        )

        time.sleep(sleep_for)


# ============================================================
# RENDER WEB SERVER
# ============================================================

class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):

        if self.path in ("/", "/health"):

            self.send_response(200)

            self.send_header(
                "Content-Type",
                "text/plain; charset=utf-8"
            )

            self.end_headers()

            self.wfile.write(
                b"QUANTUM SCALPER V4 ACTIVE"
            )

            return

        self.send_response(200)
        self.end_headers()

        self.wfile.write(
            b"QUANTUM SCALPER V4"
        )

    def do_HEAD(self):

        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        # Keep Render logs clean.
        return


def run_web_server():

    port = int(
        os.environ.get(
            "PORT",
            "10000"
        )
    )

    server = HTTPServer(
        ("0.0.0.0", port),
        HealthHandler
    )

    log.info(
        "WEB SERVER: 0.0.0.0:%d",
        port
    )

    server.serve_forever()


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    web_thread = threading.Thread(
        target=run_web_server,
        daemon=True
    )

    web_thread.start()

    scanner_loop()
