import os
import time
import logging
import threading
from datetime import datetime
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
import telebot
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


# ============================================================
# QUANTUM SCALPER V5
# ============================================================

OKX_BASE = "https://www.okx.com"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()

MIN_VOLUME_USDT = 60_000_000
MIN_SCORE = 80

SCAN_INTERVAL = 30
MAX_CANDIDATES = 30

GLOBAL_COOLDOWN = 10 * 60
SYMBOL_COOLDOWN = 45 * 60

MAX_SIGNALS_PER_DAY = 8

KYIV = ZoneInfo("Europe/Kyiv")

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is not set")

if not CHANNEL_ID:
    raise RuntimeError("CHANNEL_ID is not set")


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

log = logging.getLogger("QUANTUM")


# ============================================================
# CLIENTS
# ============================================================

session = requests.Session()
session.headers.update(
    {
        "User-Agent": "QuantumScalperV5/1.0",
        "Accept": "application/json",
    }
)

bot = telebot.TeleBot(TELEGRAM_TOKEN)


# ============================================================
# STATE
# ============================================================

symbol_cooldowns = {}
last_global_signal = 0

daily_signals = 0
daily_date = None

# OI history:
# symbol -> [(timestamp, oi), ...]
oi_history = {}

# Used so first scan only builds OI history.
warmup_done = False

last_morning_message_date = None


# ============================================================
# HELPERS
# ============================================================

def now_kyiv():
    return datetime.now(KYIV)


def fmt_price(value):
    try:
        p = float(value)

        if p >= 1000:
            return f"{p:,.2f}".replace(",", " ")

        if p >= 1:
            return f"{p:.4f}".rstrip("0").rstrip(".")

        if p >= 0.01:
            return f"{p:.6f}".rstrip("0").rstrip(".")

        if p >= 0.0001:
            return f"{p:.8f}".rstrip("0").rstrip(".")

        return f"{p:.10f}".rstrip("0").rstrip(".")

    except Exception:
        return str(value)


def fmt_money(value):
    value = float(value)

    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.2f}B"

    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"

    if value >= 1_000:
        return f"${value / 1_000:.1f}K"

    return f"${value:.0f}"


def clamp(value, low, high):
    return max(low, min(high, value))


def safe_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


# ============================================================
# OKX HTTP
# ============================================================

def okx_get(path, params=None, timeout=8):
    try:
        r = session.get(
            OKX_BASE + path,
            params=params,
            timeout=timeout,
        )

        r.raise_for_status()

        data = r.json()

        if data.get("code") != "0":
            log.warning(
                "OKX ERROR | %s | %s",
                path,
                data.get("msg", ""),
            )
            return None

        return data

    except Exception as e:
        log.warning("OKX REQUEST ERROR | %s | %s", path, e)
        return None


# ============================================================
# MARKET UNIVERSE
# ============================================================

def get_liquid_markets():
    """
    Только USDT perpetual swaps.
    24h turnover должен быть >= $60M.

    OKX ticker:
    volCcy24h используется как 24h turnover field.
    Для нашего USDT-SWAP universe фильтруем по quote/turnover.
    """

    data = okx_get(
        "/api/v5/market/tickers",
        {"instType": "SWAP"},
    )

    if not data:
        return []

    markets = []

    for m in data.get("data", []):
        inst_id = m.get("instId", "")

        if not inst_id.endswith("-USDT-SWAP"):
            continue

        if m.get("last", "") == "":
            continue

        price = safe_float(m.get("last"))
        turnover = safe_float(m.get("volCcy24h"))

        if price <= 0:
            continue

        if turnover < MIN_VOLUME_USDT:
            continue

        markets.append(
            {
                "instId": inst_id,
                "price": price,
                "volume": turnover,
                "high24": safe_float(m.get("high24h")),
                "low24": safe_float(m.get("low24h")),
            }
        )

    markets.sort(
        key=lambda x: x["volume"],
        reverse=True,
    )

    return markets[:MAX_CANDIDATES]


# ============================================================
# CANDLES
# ============================================================

def get_candles(inst_id, bar, limit=100):
    data = okx_get(
        "/api/v5/market/candles",
        {
            "instId": inst_id,
            "bar": bar,
            "limit": str(min(limit, 300)),
        },
    )

    if not data:
        return []

    candles = []

    for row in data.get("data", []):
        if len(row) < 9:
            continue

        ts = int(row[0])

        o = safe_float(row[1])
        h = safe_float(row[2])
        l = safe_float(row[3])
        c = safe_float(row[4])
        vol = safe_float(row[5])
        vol_ccy = safe_float(row[6])
        confirm = row[8]

        if min(o, h, l, c) <= 0:
            continue

        candles.append(
            {
                "ts": ts,
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": vol,
                "volume_quote": vol_ccy,
                "confirm": confirm,
            }
        )

    # OKX отдаёт newest first.
    candles.sort(key=lambda x: x["ts"])

    # Только закрытые свечи.
    candles = [
        c for c in candles
        if str(c["confirm"]) == "1"
    ]

    return candles


# ============================================================
# INDICATORS
# ============================================================

def ema(values, period):
    if len(values) < period:
        return []

    alpha = 2 / (period + 1)

    result = [sum(values[:period]) / period]

    for value in values[period:]:
        result.append(
            alpha * value
            + (1 - alpha) * result[-1]
        )

    return result


def atr(candles, period=14):
    if len(candles) < period + 1:
        return 0

    trs = []

    for i in range(1, len(candles)):
        h = candles[i]["high"]
        l = candles[i]["low"]
        pc = candles[i - 1]["close"]

        tr = max(
            h - l,
            abs(h - pc),
            abs(l - pc),
        )

        trs.append(tr)

    return sum(trs[-period:]) / period


def average_volume(candles, count=20):
    values = [
        x["volume_quote"]
        for x in candles[-count:]
        if x["volume_quote"] > 0
    ]

    if not values:
        return 0

    return sum(values) / len(values)


def candle_body_pct(c):
    if c["open"] == 0:
        return 0

    return abs(c["close"] - c["open"]) / c["open"]


# ============================================================
# OI
# ============================================================

def get_open_interest(inst_id):
    data = okx_get(
        "/api/v5/public/open-interest",
        {
            "instType": "SWAP",
            "instId": inst_id,
        },
    )

    if not data or not data.get("data"):
        return None

    item = data["data"][0]

    oi = safe_float(item.get("oi"))

    if oi <= 0:
        return None

    return oi


def update_oi(inst_id, current_oi):
    history = oi_history.setdefault(inst_id, [])

    history.append(
        (
            time.time(),
            current_oi,
        )
    )

    # Keep ~10 minutes.
    cutoff = time.time() - 10 * 60

    oi_history[inst_id] = [
        x for x in history
        if x[0] >= cutoff
    ]


def get_oi_confirmation(inst_id, direction, price_change):
    """
    OI confirmation:
    LONG:
        price rising + OI rising

    SHORT:
        price falling + OI rising

    We compare against the oldest recent OI observation
    instead of the immediately previous 30s observation.
    """

    history = oi_history.get(inst_id, [])

    if len(history) < 2:
        return False, 0.0

    old_oi = history[0][1]
    new_oi = history[-1][1]

    if old_oi <= 0:
        return False, 0.0

    oi_change = (new_oi - old_oi) / old_oi

    # Noise filter.
    oi_rising = oi_change >= 0.0005

    if direction == "LONG":
        confirmed = (
            oi_rising
            and price_change > 0
        )
    else:
        confirmed = (
            oi_rising
            and price_change < 0
        )

    return confirmed, oi_change


# ============================================================
# MARKET STRUCTURE
# ============================================================

def structure_1h(candles):
    if len(candles) < 55:
        return "NEUTRAL", 0

    closes = [x["close"] for x in candles]

    e20 = ema(closes, 20)
    e50 = ema(closes, 50)

    if len(e20) < 5 or len(e50) < 5:
        return "NEUTRAL", 0

    e20_now = e20[-1]
    e20_prev = e20[-5]
    e50_now = e50[-1]

    price = closes[-1]

    if (
        price > e20_now
        and e20_now > e50_now
        and e20_now > e20_prev
    ):
        return "BULLISH", 20

    if (
        price < e20_now
        and e20_now < e50_now
        and e20_now < e20_prev
    ):
        return "BEARISH", 20

    # Softer structure.
    if price > e20_now and e20_now > e50_now:
        return "BULLISH", 14

    if price < e20_now and e20_now < e50_now:
        return "BEARISH", 14

    return "NEUTRAL", 0


def structure_15m(candles, direction):
    if len(candles) < 30:
        return False, 0

    closes = [x["close"] for x in candles]

    e9 = ema(closes, 9)
    e20 = ema(closes, 20)

    if not e9 or not e20:
        return False, 0

    if direction == "LONG":
        good = (
            e9[-1] > e20[-1]
            and closes[-1] > e20[-1]
        )
    else:
        good = (
            e9[-1] < e20[-1]
            and closes[-1] < e20[-1]
        )

    return good, 15 if good else 0


# ============================================================
# HORIZONTAL LEVEL
# ============================================================

def horizontal_setup(candles, direction):
    if len(candles) < 25:
        return None

    recent = candles[-21:-1]

    if direction == "LONG":
        level = max(x["high"] for x in recent)
        price = candles[-1]["close"]

        distance = (level - price) / price

        # PRE-ENTRY:
        # price is below resistance but close enough.
        if 0.0005 <= distance <= 0.006:
            return {
                "level": level,
                "distance": distance,
            }

    else:
        level = min(x["low"] for x in recent)
        price = candles[-1]["close"]

        distance = (price - level) / price

        if 0.0005 <= distance <= 0.006:
            return {
                "level": level,
                "distance": distance,
            }

    return None


# ============================================================
# TRENDLINE COMPRESSION
# ============================================================

def linear_slope(values):
    n = len(values)

    if n < 3:
        return 0

    x_mean = (n - 1) / 2
    y_mean = sum(values) / n

    numerator = 0
    denominator = 0

    for i, y in enumerate(values):
        numerator += (i - x_mean) * (y - y_mean)
        denominator += (i - x_mean) ** 2

    if denominator == 0:
        return 0

    return numerator / denominator


def trendline_setup(candles, direction):
    if len(candles) < 30:
        return None

    window = candles[-20:]

    highs = [x["high"] for x in window]
    lows = [x["low"] for x in window]

    high_slope = linear_slope(highs)
    low_slope = linear_slope(lows)

    price = candles[-1]["close"]

    avg_price = sum(x["close"] for x in window) / len(window)

    if avg_price <= 0:
        return None

    # Compression = distance between upper/lower boundaries
    # becomes smaller.
    first_range = highs[:5]
    last_range = highs[-5:]

    first_width = (
        max(x["high"] for x in window[:5])
        - min(x["low"] for x in window[:5])
    )

    last_width = (
        max(x["high"] for x in window[-5:])
        - min(x["low"] for x in window[-5:])
    )

    if first_width <= 0:
        return None

    compression = last_width / first_width

    if compression > 0.75:
        return None

    if direction == "LONG":
        # Rising lows + relatively flat/falling highs.
        if low_slope <= 0:
            return None

        resistance = max(highs[-5:])

        if 0 < (resistance - price) / price <= 0.007:
            return {
                "level": resistance,
                "compression": compression,
            }

    else:
        # Falling highs + relatively flat/rising lows.
        if high_slope >= 0:
            return None

        support = min(lows[-5:])

        if 0 < (price - support) / price <= 0.007:
            return {
                "level": support,
                "compression": compression,
            }

    return None


# ============================================================
# MOMENTUM SETUP
# ============================================================

def momentum_setup(candles, direction):
    if len(candles) < 30:
        return None

    closes = [x["close"] for x in candles]

    e9 = ema(closes, 9)
    e20 = ema(closes, 20)

    if not e9 or not e20:
        return None

    last = candles[-1]

    avg_vol = average_volume(candles[:-1], 20)

    if avg_vol <= 0:
        return None

    current_vol = last["volume_quote"]

    volume_ratio = current_vol / avg_vol

    if volume_ratio < 1.15:
        return None

    if direction == "LONG":
        if e9[-1] <= e20[-1]:
            return None

        level = max(
            x["high"]
            for x in candles[-13:-1]
        )

        distance = (level - last["close"]) / last["close"]

        if -0.002 <= distance <= 0.005:
            return {
                "level": level,
                "volume_ratio": volume_ratio,
            }

    else:
        if e9[-1] >= e20[-1]:
            return None

        level = min(
            x["low"]
            for x in candles[-13:-1]
        )

        distance = (last["close"] - level) / last["close"]

        if -0.002 <= distance <= 0.005:
            return {
                "level": level,
                "volume_ratio": volume_ratio,
            }

    return None


# ============================================================
# 5M CONFIRMATION
# ============================================================

def confirmation_5m(candles, direction):
    if len(candles) < 30:
        return False, 0, 0

    closes = [x["close"] for x in candles]

    e9 = ema(closes, 9)
    e20 = ema(closes, 20)

    if not e9 or not e20:
        return False, 0, 0

    last = candles[-1]
    previous = candles[-2]

    if direction == "LONG":
        ema_ok = e9[-1] > e20[-1]

        candle_ok = (
            last["close"] > last["open"]
            or last["close"] > previous["close"]
        )

    else:
        ema_ok = e9[-1] < e20[-1]

        candle_ok = (
            last["close"] < last["open"]
            or last["close"] < previous["close"]
        )

    price_change = (
        last["close"] - previous["close"]
    ) / previous["close"]

    good = ema_ok and candle_ok

    return good, 20 if good else 0, price_change


# ============================================================
# SIGNAL SCORE
# ============================================================

def score_signal(
    direction,
    structure_score,
    setup_score,
    confirmation_score,
    volume_ratio,
    oi_confirmed,
    strategy_quality,
):
    score = 0

    score += structure_score
    score += setup_score
    score += confirmation_score

    # Volume: 0-10
    if volume_ratio >= 2.0:
        score += 10
    elif volume_ratio >= 1.5:
        score += 8
    elif volume_ratio >= 1.25:
        score += 6
    elif volume_ratio >= 1.10:
        score += 4

    # OI confirmation: 15
    if oi_confirmed:
        score += 15

    # Strategy quality: max 10
    score += strategy_quality

    return int(clamp(score, 0, 100))


def score_label(score):
    if score >= 95:
        return "ELITE"
    if score >= 90:
        return "PREMIUM"
    if score >= 85:
        return "STRONG"
    return "STANDARD"


# ============================================================
# DETERMINE TRADE
# ============================================================

def analyse_market(market):
    inst_id = market["instId"]

    candles_1h = get_candles(
        inst_id,
        "1H",
        80,
    )

    candles_15m = get_candles(
        inst_id,
        "15m",
        80,
    )

    candles_5m = get_candles(
        inst_id,
        "5m",
        100,
    )

    if (
        len(candles_1h) < 55
        or len(candles_15m) < 30
        or len(candles_5m) < 30
    ):
        return None

    h1_direction, h1_score = structure_1h(
        candles_1h
    )

    if h1_direction == "NEUTRAL":
        return None

    direction = (
        "LONG"
        if h1_direction == "BULLISH"
        else "SHORT"
    )

    setup15_ok, setup15_score = structure_15m(
        candles_15m,
        direction,
    )

    if not setup15_ok:
        return None

    confirm_ok, confirm_score, price_change = (
        confirmation_5m(
            candles_5m,
            direction,
        )
    )

    if not confirm_ok:
        return None

    # --------------------------------------------------------
    # Strategy detection
    # --------------------------------------------------------

    horizontal = horizontal_setup(
        candles_5m,
        direction,
    )

    trendline = trendline_setup(
        candles_5m,
        direction,
    )

    momentum = momentum_setup(
        candles_5m,
        direction,
    )

    candidates = []

    if horizontal:
        candidates.append(
            (
                "Horizontal Level Breakout",
                horizontal["level"],
                10,
            )
        )

    if trendline:
        candidates.append(
            (
                "Trendline Compression Breakout",
                trendline["level"],
                10,
            )
        )

    if momentum:
        candidates.append(
            (
                "Momentum Breakout",
                momentum["level"],
                8,
            )
        )

    if not candidates:
        return None

    # Pick strongest strategy.
    candidates.sort(
        key=lambda x: x[2],
        reverse=True,
    )

    strategy, level, strategy_quality = candidates[0]

    # --------------------------------------------------------
    # Volume
    # --------------------------------------------------------

    avg_vol = average_volume(
        candles_5m[:-1],
        20,
    )

    current_vol = candles_5m[-1]["volume_quote"]

    volume_ratio = (
        current_vol / avg_vol
        if avg_vol > 0
        else 0
    )

    # --------------------------------------------------------
    # OI
    # --------------------------------------------------------

    current_oi = get_open_interest(
        inst_id
    )

    if current_oi is None:
        return None

    update_oi(
        inst_id,
        current_oi,
    )

    oi_confirmed, oi_change = (
        get_oi_confirmation(
            inst_id,
            direction,
            price_change,
        )
    )

    # CRITICAL:
    # No OI confirmation = no Telegram signal.
    if not oi_confirmed:
        return None

    # --------------------------------------------------------
    # SCORE
    # --------------------------------------------------------

    score = score_signal(
        direction=direction,
        structure_score=h1_score,
        setup_score=setup15_score,
        confirmation_score=confirm_score,
        volume_ratio=volume_ratio,
        oi_confirmed=oi_confirmed,
        strategy_quality=strategy_quality,
    )

    if score < MIN_SCORE:
        return None

    # --------------------------------------------------------
    # PRE-ENTRY filter
    # --------------------------------------------------------

    price = candles_5m[-1]["close"]

    distance_to_level = (
        abs(level - price) / price
    )

    # If price has already moved too far from level,
    # don't chase.
    if distance_to_level > 0.008:
        return None

    # Avoid giant 5m candles.
    if candle_body_pct(candles_5m[-1]) > 0.012:
        return None

    # --------------------------------------------------------
    # Risk model
    # --------------------------------------------------------

    current_atr = atr(
        candles_5m,
        14,
    )

    if current_atr <= 0:
        return None

    # Entry is slightly around the level.
    entry_buffer = max(
        current_atr * 0.10,
        price * 0.00015,
    )

    if direction == "LONG":
        entry_low = level - entry_buffer
        entry_high = level + entry_buffer

        # Stop below level.
        sl = level - max(
            current_atr * 0.75,
            level * 0.006,
        )

        risk = abs(
            level - sl
        )

        tp1 = level + risk * 1.0
        tp2 = level + risk * 2.0
        tp3 = level + risk * 3.0

    else:
        entry_low = level - entry_buffer
        entry_high = level + entry_buffer

        sl = level + max(
            current_atr * 0.75,
            level * 0.006,
        )

        risk = abs(
            sl - level
        )

        tp1 = level - risk * 1.0
        tp2 = level - risk * 2.0
        tp3 = level - risk * 3.0

    risk_pct = (
        abs(level - sl) / level * 100
    )

    # Don't publish crazy risk.
    if risk_pct < 0.35 or risk_pct > 1.50:
        return None

    coin = inst_id.replace(
        "-USDT-SWAP",
        "",
    )

    return {
        "inst_id": inst_id,
        "coin": coin,
        "direction": direction,
        "strategy": strategy,
        "level": level,
        "entry_low": entry_low,
        "entry_high": entry_high,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "risk_pct": risk_pct,
        "price": price,
        "volume": market["volume"],
        "volume_ratio": volume_ratio,
        "oi_change": oi_change,
        "score": score,
        "label": score_label(score),
        "h1": h1_direction,
        "price_change_5m": price_change,
        "candles_5m": candles_5m,
    }


# ============================================================
# CHART
# ============================================================

def make_chart(signal):
    candles = signal["candles_5m"][-60:]

    prices = [x["close"] for x in candles]

    times = [
        datetime.fromtimestamp(
            x["ts"] / 1000,
            tz=KYIV,
        ).strftime("%H:%M")
        for x in candles
    ]

    fig, ax = plt.subplots(
        figsize=(12, 6),
        dpi=120,
    )

    fig.patch.set_facecolor("#0b1020")
    ax.set_facecolor("#0b1020")

    ax.plot(
        range(len(prices)),
        prices,
        color="#00e5ff",
        linewidth=2,
        label="5M Price",
    )

    level = signal["level"]

    ax.axhline(
        level,
        color="#ffd166",
        linewidth=2,
        linestyle="--",
        label="BREAKOUT LEVEL",
    )

    ax.axhline(
        signal["sl"],
        color="#ff4d6d",
        linewidth=1.5,
        linestyle=":",
        label="STOP",
    )

    ax.axhline(
        signal["tp1"],
        color="#00ff88",
        linewidth=1,
        linestyle=":",
        label="TP1",
    )

    ax.axhline(
        signal["tp2"],
        color="#00ff88",
        linewidth=1,
        linestyle=":",
        label="TP2",
    )

    ax.axhline(
        signal["tp3"],
        color="#00ff88",
        linewidth=1,
        linestyle=":",
        label="TP3",
    )

    title = (
        f"QUANTUM SCALPER V5 | "
        f"{signal['coin']} | "
        f"{signal['direction']} | "
        f"{signal['score']}/100"
    )

    ax.set_title(
        title,
        color="white",
        fontsize=15,
        fontweight="bold",
    )

    ax.tick_params(
        colors="#9aa4bf"
    )

    for spine in ax.spines.values():
        spine.set_color("#26304a")

    ax.grid(
        alpha=0.15,
        color="white",
    )

    ax.legend(
        facecolor="#11182b",
        edgecolor="#26304a",
        labelcolor="white",
        fontsize=8,
    )

    # Show ~10 time labels.
    step = max(1, len(times) // 10)

    ax.set_xticks(
        list(range(0, len(times), step))
    )

    ax.set_xticklabels(
        times[::step],
        rotation=45,
        ha="right",
        fontsize=8,
    )

    plt.tight_layout()

    filename = (
        f"/tmp/"
        f"{signal['coin']}_"
        f"{int(time.time())}.png"
    )

    plt.savefig(
        filename,
        facecolor=fig.get_facecolor(),
        bbox_inches="tight",
    )

    plt.close(fig)

    return filename


# ============================================================
# TELEGRAM
# ============================================================

def build_signal_text(signal):
    direction = signal["direction"]

    arrow = "🔥 LONG" if direction == "LONG" else "🔻 SHORT"

    return (
        f"🔥 **{signal['coin']}USDT — {arrow}**\n\n"

        f"💰 **Текущая цена:** "
        f"`{fmt_price(signal['price'])}`\n"
        f"💵 **24H оборот:** "
        f"`{fmt_money(signal['volume'])}`\n\n"

        f"⭐ **SIGNAL SCORE: "
        f"{signal['score']}/100 — "
        f"{signal['label']}**\n\n"

        f"🧠 **ЛОГИКА СДЕЛКИ**\n\n"

        f"Стратегия: **{signal['strategy']}**\n"
        f"Цена формирует PRE-ENTRY возле "
        f"ключевого уровня. Ждём продолжения "
        f"движения в сторону структуры старшего "
        f"таймфрейма.\n\n"

        f"📊 **1H:** {signal['h1']}\n"
        f"📊 **15M:** SETUP CONFIRMED\n"
        f"⚡ **5M:** ENTRY CONFIRMATION\n\n"

        f"📦 **ОБЪЁМ:** HIGH\n"
        f"⚡ **OI:** CONFIRMED "
        f"({signal['oi_change'] * 100:+.2f}%)\n\n"

        f"🎯 **ТОЧКА ВХОДА**\n\n"
        f"`{fmt_price(signal['entry_low'])}` — "
        f"`{fmt_price(signal['entry_high'])}`\n\n"

        f"🛑 **STOP LOSS**\n\n"
        f"`{fmt_price(signal['sl'])}`\n"
        f"Риск: −{signal['risk_pct']:.2f}%\n\n"

        f"🪜 **ЗАКРЫТИЕ ЛЕСЕНКОЙ**\n\n"

        f"TP1 — 30%\n"
        f"`{fmt_price(signal['tp1'])}`\n\n"

        f"TP2 — 30%\n"
        f"`{fmt_price(signal['tp2'])}`\n\n"

        f"TP3 — 40%\n"
        f"`{fmt_price(signal['tp3'])}`\n\n"

        f"🔒 После TP1 → SL в BE\n\n"

        f"📍 **Основной уровень:** "
        f"`{fmt_price(signal['level'])}`\n\n"

        f"📈 **Рабочий таймфрейм входа:** 5M\n"
        f"🧭 **Фильтр направления:** 1H\n"
        f"🔎 **Формирование сетапа:** 15M\n\n"

        f"⚠️ **ВАЖНО**\n\n"

        f"Это PRE-ENTRY сигнал около уровня.\n"
        f"Не догоняем цену после сильной свечи.\n\n"

        f"Если рынок уже ушёл далеко от "
        f"указанной зоны — сделку пропускаем.\n\n"

        f"**Качество важнее количества.**"
    )


def send_signal(signal):
    global last_global_signal
    global daily_signals

    now = time.time()

    if now - last_global_signal < GLOBAL_COOLDOWN:
        log.info(
            "GLOBAL COOLDOWN | %s",
            signal["coin"],
        )
        return False

    last_symbol = symbol_cooldowns.get(
        signal["inst_id"],
        0,
    )

    if now - last_symbol < SYMBOL_COOLDOWN:
        log.info(
            "SYMBOL COOLDOWN | %s",
            signal["coin"],
        )
        return False

    if daily_signals >= MAX_SIGNALS_PER_DAY:
        log.info(
            "DAILY LIMIT REACHED | %s",
            signal["coin"],
        )
        return False

    chart = None

    try:
        chart = make_chart(signal)

        # Photo FIRST.
        # Therefore the chart is physically above the
        # detailed signal in Telegram.
        with open(chart, "rb") as photo:
            bot.send_photo(
                CHANNEL_ID,
                photo,
                caption=(
                    f"🚨 QUANTUM SCALPER V5\n"
                    f"#{signal['coin']}USDT "
                    f"{signal['direction']}\n"
                    f"⭐ SCORE {signal['score']}/100 "
                    f"{signal['label']}\n"
                    f"📍 {signal['strategy']}"
                ),
            )

        bot.send_message(
            CHANNEL_ID,
            build_signal_text(signal),
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )

        last_global_signal = now
        symbol_cooldowns[
            signal["inst_id"]
        ] = now

        daily_signals += 1

        log.info(
            "SIGNAL SENT | %s | %s | %s | SCORE=%s",
            signal["inst_id"],
            signal["direction"],
            signal["strategy"],
            signal["score"],
        )

        return True

    except Exception as e:
        log.exception(
            "TELEGRAM SIGNAL ERROR | %s",
            e,
        )
        return False

    finally:
        if chart:
            try:
                os.remove(chart)
            except Exception:
                pass


# ============================================================
# MORNING MESSAGE
# ============================================================

def send_morning_message():
    global last_morning_message_date

    today = now_kyiv().date()

    if last_morning_message_date == today:
        return

    hour = now_kyiv().hour

    # Send once between 07:00 and 10:59 Kyiv.
    if hour < 7 or hour >= 11:
        return

    text = (
        "☀️ **Доброе утро, ребята!**\n\n"
        "Начинаем новый торговый день вместе. 🚀\n\n"
        "Сегодня работаем только по системе:\n"
        "📊 1H — направление\n"
        "🔎 15M — сетап\n"
        "⚡ 5M — подтверждение\n"
        "💧 Ликвидность — от $60M/24H\n"
        "⚡ OI — обязательное подтверждение\n\n"
        "Не догоняем рынок.\n"
        "Не увеличиваем риск после убытка.\n"
        "Не входим без подтверждения.\n\n"
        "🎯 **Качество важнее количества.**\n\n"
        "Всем спокойной и дисциплинированной торговли! 💎"
    )

    try:
        bot.send_message(
            CHANNEL_ID,
            text,
            parse_mode="Markdown",
        )

        last_morning_message_date = today

        log.info(
            "MORNING MESSAGE SENT"
        )

    except Exception as e:
        log.exception(
            "MORNING MESSAGE ERROR | %s",
            e,
        )


# ============================================================
# DAILY RESET
# ============================================================

def reset_daily_counter():
    global daily_date
    global daily_signals

    today = now_kyiv().date()

    if daily_date != today:
        daily_date = today
        daily_signals = 0

        log.info(
            "DAILY COUNTER RESET | %s",
            today,
        )


# ============================================================
# SCANNER
# ============================================================

def scanner_loop():
    global warmup_done

    scan_number = 0

    log.info(
        "=================================================="
    )

    log.info(
        "QUANTUM SCALPER V5 STARTING"
    )

    log.info(
        "MIN 24H TURNOVER: $%s",
        f"{MIN_VOLUME_USDT:,}",
    )

    log.info(
        "MIN SCORE: %s",
        MIN_SCORE,
    )

    log.info(
        "MAX CANDIDATES: %s",
        MAX_CANDIDATES,
    )

    log.info(
        "SCAN INTERVAL: %ss",
        SCAN_INTERVAL,
    )

    log.info(
        "TIMEZONE: Europe/Kyiv"
    )

    log.info(
        "=================================================="
    )

    # Startup message.
    try:
        bot.send_message(
            CHANNEL_ID,
            (
                "🚀 **QUANTUM SCALPER V5 ONLINE**\n\n"
                "🟢 OKX Market Data\n"
                "🟢 1H Structure\n"
                "🟢 15M Setup\n"
                "🟢 5M Confirmation\n"
                "🟢 Open Interest\n"
                "🟢 $60M+ 24H Turnover Filter\n"
                "🟢 Telegram\n"
                "🟢 Chart Generator\n\n"
                "🧠 **3 стратегии:**\n"
                "1️⃣ Horizontal Level Breakout\n"
                "2️⃣ Trendline Compression Breakout\n"
                "3️⃣ Momentum Breakout\n\n"
                "⭐ Minimum Score: `80/100`\n"
                "🎯 Quality > Quantity"
            ),
            parse_mode="Markdown",
        )

        log.info(
            "STARTUP MESSAGE SENT"
        )

    except Exception as e:
        log.exception(
            "STARTUP MESSAGE ERROR | %s",
            e,
        )

    while True:
        started = time.time()

        try:
            reset_daily_counter()
            send_morning_message()

            scan_number += 1

            log.info(
                "=============================="
            )

            log.info(
                "SCAN #%s",
                scan_number,
            )

            markets = get_liquid_markets()

            log.info(
                "MARKETS >= $60M: %s",
                len(markets),
            )

            if not markets:
                log.warning(
                    "NO LIQUID MARKETS FOUND"
                )

            for index, market in enumerate(
                markets,
                start=1,
            ):
                inst_id = market["instId"]

                log.info(
                    "ANALYSE %s/%s | %s | VOL=%s",
                    index,
                    len(markets),
                    inst_id,
                    fmt_money(
                        market["volume"]
                    ),
                )

                try:
                    signal = analyse_market(
                        market
                    )

                    if signal:
                        log.info(
                            "QUALIFIED | %s | %s | %s | SCORE=%s",
                            inst_id,
                            signal["direction"],
                            signal["strategy"],
                            signal["score"],
                        )

                        send_signal(signal)

                except Exception as e:
                    log.exception(
                        "SYMBOL ERROR | %s | %s",
                        inst_id,
                        e,
                    )

                # Small delay protects free Render
                # and exchange API.
                time.sleep(0.15)

            # After first full pass OI history exists.
            if not warmup_done:
                warmup_done = True

                log.info(
                    "OI WARMUP COMPLETE"
                )

            elapsed = time.time() - started

            log.info(
                "SCAN #%s COMPLETE | %.1fs",
                scan_number,
                elapsed,
            )

            sleep_for = max(
                5,
                SCAN_INTERVAL - elapsed,
            )

            time.sleep(sleep_for)

        except Exception as e:
            # IMPORTANT:
            # Never allow one bad API response to kill
            # the whole bot.
            log.exception(
                "SCANNER LOOP ERROR | %s",
                e,
            )

            time.sleep(15)


# ============================================================
# RENDER WEB SERVER
# ============================================================

class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        body = b"QUANTUM SCALPER V5 ACTIVE"

        self.send_response(200)
        self.send_header(
            "Content-Type",
            "text/plain; charset=utf-8",
        )
        self.send_header(
            "Content-Length",
            str(len(body)),
        )
        self.end_headers()

        self.wfile.write(body)

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        return


def run_web_server():
    port = int(
        os.environ.get(
            "PORT",
            "10000",
        )
    )

    server = HTTPServer(
        ("0.0.0.0", port),
        HealthHandler,
    )

    log.info(
        "WEB SERVER: 0.0.0.0:%s",
        port,
    )

    server.serve_forever()


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    threading.Thread(
        target=run_web_server,
        daemon=True,
    ).start()

    scanner_loop()
