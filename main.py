import os
import io
import time
import math
import logging
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
import numpy as np
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

import telebot


# ============================================================
# QUANTUM SCALPER V5
# ============================================================

APP_VERSION = "QUANTUM SCALPER V5"

OKX_BASE = "https://www.okx.com"
OKX_TICKERS = "/api/v5/market/tickers"
OKX_CANDLES = "/api/v5/market/candles"
OKX_OI = "/api/v5/public/open-interest"
OKX_OI_HISTORY = "/api/v5/rubik/stat/contracts/open-interest-history"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHANNEL_ID = os.getenv("CHANNEL_ID", "")

# ============================================================
# ОСНОВНЫЕ НАСТРОЙКИ
# ============================================================

MIN_VOLUME_USD = 60_000_000

MIN_SCORE = 80

# Это НЕ 1 сигнал в день.
# Бот может дать несколько сигналов, но канал не превращается
# в спам.
MAX_SIGNALS_PER_DAY = 6

GLOBAL_SIGNAL_GAP_MIN = 30

# Одна и та же монета не повторяется слишком быстро.
SYMBOL_COOLDOWN_MIN = 180

SCAN_INTERVAL = 30

# Сколько самых ликвидных монет анализируем.
MAX_MARKETS = 25

TIMEZONE = ZoneInfo("Europe/Kyiv")

# PRE-ENTRY:
# Насколько близко цена должна находиться к уровню.
MIN_DISTANCE_TO_LEVEL = 0.0010   # 0.10%
MAX_DISTANCE_TO_LEVEL = 0.0045   # 0.45%

# Минимальный объём последней 5M свечи
VOLUME_CONFIRM_MULTIPLIER = 1.25

# Максимально допустимый риск от входа до SL
MIN_RISK_PCT = 0.35
MAX_RISK_PCT = 1.60

# После TP1 пользователю рекомендуется BE.
TP1_R = 1.0
TP2_R = 1.8
TP3_R = 2.8


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

log = logging.getLogger("QUANTUM")


# ============================================================
# TELEGRAM
# ============================================================

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is not set")

if not CHANNEL_ID:
    raise RuntimeError("CHANNEL_ID is not set")

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML")

session = requests.Session()

cooldowns = {}
signal_times = []

last_greeting_date = None
last_startup_message = False


# ============================================================
# HTTP SERVER FOR RENDER WEB SERVICE
# ============================================================

class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        body = (
            "QUANTUM SCALPER V5 ACTIVE\n"
            "STATUS: ONLINE\n"
        ).encode()

        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        return


def run_web_server():
    port = int(os.environ.get("PORT", "10000"))

    server = HTTPServer(
        ("0.0.0.0", port),
        HealthHandler
    )

    log.info("WEB SERVER: 0.0.0.0:%s", port)

    server.serve_forever()


# ============================================================
# HELPERS
# ============================================================

def now_kyiv():
    return datetime.now(TIMEZONE)


def safe_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def fmt_price(price):
    price = safe_float(price)

    if price <= 0:
        return "0"

    if price >= 1000:
        return f"{price:,.2f}".replace(",", " ")

    if price >= 1:
        return f"{price:.4f}".rstrip("0").rstrip(".")

    if price >= 0.01:
        return f"{price:.6f}".rstrip("0").rstrip(".")

    if price >= 0.0001:
        return f"{price:.8f}".rstrip("0").rstrip(".")

    if price >= 0.000001:
        return f"{price:.10f}".rstrip("0").rstrip(".")

    return f"{price:.12f}".rstrip("0").rstrip(".")


def fmt_volume(volume):
    if volume >= 1_000_000_000:
        return f"${volume / 1_000_000_000:.2f}B"

    if volume >= 1_000_000:
        return f"${volume / 1_000_000:.2f}M"

    return f"${volume / 1_000:.0f}K"


def clamp(value, low, high):
    return max(low, min(high, value))


# ============================================================
# OKX REQUEST
# ============================================================

def okx_get(path, params=None, timeout=8):

    try:
        r = session.get(
            OKX_BASE + path,
            params=params,
            timeout=timeout
        )

        r.raise_for_status()

        data = r.json()

        if data.get("code") != "0":
            log.warning(
                "OKX ERROR | %s | %s",
                path,
                data.get("msg")
            )
            return None

        return data.get("data", [])

    except Exception as e:
        log.warning(
            "OKX REQUEST FAILED | %s | %s",
            path,
            e
        )
        return None


# ============================================================
# MARKET DATA
# ============================================================

def get_markets():

    data = okx_get(
        OKX_TICKERS,
        {
            "instType": "SWAP"
        }
    )

    if not data:
        return []

    markets = []

    for item in data:

        inst_id = item.get("instId", "")

        if not inst_id.endswith("-USDT-SWAP"):
            continue

        last = safe_float(item.get("last"))

        if last <= 0:
            continue

        # Для USDT swap OKX volCcy24h — объём в базовой валюте.
        # Переводим его в USD.
        vol_base = safe_float(item.get("volCcy24h"))

        volume_usd = vol_base * last

        if volume_usd < MIN_VOLUME_USD:
            continue

        markets.append({
            "instId": inst_id,
            "symbol": inst_id.replace("-USDT-SWAP", ""),
            "price": last,
            "volume_usd": volume_usd,
            "high24": safe_float(item.get("high24h")),
            "low24": safe_float(item.get("low24h")),
        })

    markets.sort(
        key=lambda x: x["volume_usd"],
        reverse=True
    )

    return markets[:MAX_MARKETS]


def get_candles(inst_id, bar, limit=120):

    data = okx_get(
        OKX_CANDLES,
        {
            "instId": inst_id,
            "bar": bar,
            "limit": str(limit)
        }
    )

    if not data:
        return []

    candles = []

    for row in reversed(data):

        if len(row) < 9:
            continue

        candles.append({
            "ts": int(row[0]),
            "open": safe_float(row[1]),
            "high": safe_float(row[2]),
            "low": safe_float(row[3]),
            "close": safe_float(row[4]),
            "volume": safe_float(row[5]),
            "confirm": row[8]
        })

    return candles


# ============================================================
# OPEN INTEREST
# ============================================================

def get_open_interest(inst_id):

    data = okx_get(
        OKX_OI,
        {
            "instType": "SWAP",
            "instId": inst_id
        }
    )

    if not data:
        return None

    item = data[0]

    oi_usd = safe_float(item.get("oiUsd"))

    if oi_usd > 0:
        return oi_usd

    return None


def get_oi_change(inst_id):

    data = okx_get(
        OKX_OI_HISTORY,
        {
            "instId": inst_id,
            "period": "5m",
            "limit": "5"
        }
    )

    if not data or len(data) < 2:
        return None

    try:

        # OKX history endpoint returns array-style rows.
        # Берём oiUsd, если он присутствует.
        def extract_oi(row):

            if isinstance(row, dict):
                return safe_float(
                    row.get("oiUsd")
                    or row.get("oi")
                    or row.get("oiCcy")
                )

            # Обычно структура начинается с ts.
            # oiUsd может находиться ближе к концу.
            values = []

            for x in row:
                try:
                    values.append(float(x))
                except Exception:
                    pass

            if len(values) >= 2:
                return values[-1]

            return 0

        newest = extract_oi(data[0])
        oldest = extract_oi(data[-1])

        if oldest <= 0:
            return None

        return (newest - oldest) / oldest * 100

    except Exception:
        return None


# ============================================================
# INDICATORS
# ============================================================

def closes(candles):
    return np.array(
        [x["close"] for x in candles],
        dtype=float
    )


def highs(candles):
    return np.array(
        [x["high"] for x in candles],
        dtype=float
    )


def lows(candles):
    return np.array(
        [x["low"] for x in candles],
        dtype=float
    )


def volumes(candles):
    return np.array(
        [x["volume"] for x in candles],
        dtype=float
    )


def ema(values, period):

    values = np.asarray(values, dtype=float)

    if len(values) < period:
        return np.full(len(values), np.nan)

    alpha = 2 / (period + 1)

    result = np.zeros(len(values))
    result[0] = values[0]

    for i in range(1, len(values)):
        result[i] = (
            alpha * values[i]
            + (1 - alpha) * result[i - 1]
        )

    return result


def atr(candles, period=14):

    if len(candles) < period + 1:
        return 0

    h = highs(candles)
    l = lows(candles)
    c = closes(candles)

    tr = np.zeros(len(c))

    for i in range(1, len(c)):

        tr[i] = max(
            h[i] - l[i],
            abs(h[i] - c[i - 1]),
            abs(l[i] - c[i - 1])
        )

    return float(
        np.mean(tr[-period:])
    )


def rsi(candles, period=14):

    c = closes(candles)

    if len(c) < period + 1:
        return 50

    delta = np.diff(c)

    gains = np.where(delta > 0, delta, 0)
    losses = np.where(delta < 0, -delta, 0)

    avg_gain = np.mean(gains[-period:])
    avg_loss = np.mean(losses[-period:])

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss

    return 100 - (100 / (1 + rs))


def average_volume(candles, period=20):

    if len(candles) < period + 1:
        return 0

    v = volumes(candles)

    return float(
        np.mean(v[-period-1:-1])
    )


# ============================================================
# 1H TREND
# ============================================================

def get_1h_direction(candles):

    c = closes(candles)

    e20 = ema(c, 20)
    e50 = ema(c, 50)

    if len(c) < 60:
        return None

    last = c[-1]

    if (
        e20[-1] > e50[-1]
        and last > e20[-1]
        and e20[-1] > e20[-4]
    ):
        return "LONG"

    if (
        e20[-1] < e50[-1]
        and last < e20[-1]
        and e20[-1] < e20[-4]
    ):
        return "SHORT"

    return None


# ============================================================
# HORIZONTAL LEVEL
# ============================================================

def horizontal_setup(c15, direction):

    if len(c15) < 40:
        return None

    h = highs(c15)
    l = lows(c15)
    c = closes(c15)

    # Не используем последние 2 свечи при построении уровня.
    # Это уменьшает шанс смотреть в будущее.
    recent_high = np.max(h[-27:-2])
    recent_low = np.min(l[-27:-2])

    price = c[-1]

    if direction == "LONG":

        distance = (
            recent_high - price
        ) / price

        if MIN_DISTANCE_TO_LEVEL <= distance <= MAX_DISTANCE_TO_LEVEL:

            touches = np.sum(
                np.abs(
                    h[-40:-2] - recent_high
                ) / recent_high < 0.002
            )

            quality = clamp(
                8 + touches * 3,
                0,
                20
            )

            return {
                "strategy": "Horizontal Level Breakout",
                "direction": "LONG",
                "level": recent_high,
                "quality": quality,
                "distance": distance
            }

    else:

        distance = (
            price - recent_low
        ) / price

        if MIN_DISTANCE_TO_LEVEL <= distance <= MAX_DISTANCE_TO_LEVEL:

            touches = np.sum(
                np.abs(
                    l[-40:-2] - recent_low
                ) / recent_low < 0.002
            )

            quality = clamp(
                8 + touches * 3,
                0,
                20
            )

            return {
                "strategy": "Horizontal Level Breakout",
                "direction": "SHORT",
                "level": recent_low,
                "quality": quality,
                "distance": distance
            }

    return None


# ============================================================
# TRENDLINE COMPRESSION
# ============================================================

def trendline_setup(c15, direction):

    if len(c15) < 40:
        return None

    h = highs(c15)
    l = lows(c15)
    c = closes(c15)

    n = 18

    x = np.arange(n)

    upper = np.polyfit(
        x,
        h[-n:],
        1
    )

    lower = np.polyfit(
        x,
        l[-n:],
        1
    )

    upper_slope = upper[0]
    lower_slope = lower[0]

    current_upper = (
        upper_slope * (n - 1)
        + upper[1]
    )

    current_lower = (
        lower_slope * (n - 1)
        + lower[1]
    )

    price = c[-1]

    width = (
        current_upper - current_lower
    )

    if width <= 0:
        return None

    width_pct = width / price

    # Слишком широкая форма — это не compression.
    if width_pct > 0.018:
        return None

    if direction == "LONG":

        distance = (
            current_upper - price
        ) / price

        # Верхняя граница должна быть выше цены,
        # чтобы сигнал был PRE-ENTRY.
        if MIN_DISTANCE_TO_LEVEL <= distance <= MAX_DISTANCE_TO_LEVEL:

            # Для бычьего сжатия верхняя линия должна
            # быть относительно плоской/растущей.
            if upper_slope >= -width * 0.02:

                return {
                    "strategy": "Trendline Compression Breakout",
                    "direction": "LONG",
                    "level": current_upper,
                    "quality": 17,
                    "distance": distance
                }

    else:

        distance = (
            price - current_lower
        ) / price

        if MIN_DISTANCE_TO_LEVEL <= distance <= MAX_DISTANCE_TO_LEVEL:

            if lower_slope <= width * 0.02:

                return {
                    "strategy": "Trendline Compression Breakout",
                    "direction": "SHORT",
                    "level": current_lower,
                    "quality": 17,
                    "distance": distance
                }

    return None


# ============================================================
# MOMENTUM
# ============================================================

def momentum_setup(c5, direction):

    if len(c5) < 50:
        return None

    c = closes(c5)
    h = highs(c5)
    l = lows(c5)
    v = volumes(c5)

    e9 = ema(c, 9)
    e20 = ema(c, 20)

    price = c[-1]

    avg_v = average_volume(c5, 20)

    if avg_v <= 0:
        return None

    volume_ratio = v[-1] / avg_v

    r = rsi(c5)

    # Локальный импульсный уровень.
    local_high = np.max(h[-13:-1])
    local_low = np.min(l[-13:-1])

    if direction == "LONG":

        distance = (
            local_high - price
        ) / price

        if (
            MIN_DISTANCE_TO_LEVEL
            <= distance
            <= MAX_DISTANCE_TO_LEVEL
            and e9[-1] > e20[-1]
            and price > e9[-1]
            and 52 <= r <= 75
            and volume_ratio >= VOLUME_CONFIRM_MULTIPLIER
        ):

            quality = 15

            if volume_ratio >= 1.5:
                quality += 3

            if r >= 55:
                quality += 2

            return {
                "strategy": "Momentum Breakout",
                "direction": "LONG",
                "level": local_high,
                "quality": min(20, quality),
                "distance": distance,
                "volume_ratio": volume_ratio
            }

    else:

        distance = (
            price - local_low
        ) / price

        if (
            MIN_DISTANCE_TO_LEVEL
            <= distance
            <= MAX_DISTANCE_TO_LEVEL
            and e9[-1] < e20[-1]
            and price < e9[-1]
            and 25 <= r <= 48
            and volume_ratio >= VOLUME_CONFIRM_MULTIPLIER
        ):

            quality = 15

            if volume_ratio >= 1.5:
                quality += 3

            if r <= 45:
                quality += 2

            return {
                "strategy": "Momentum Breakout",
                "direction": "SHORT",
                "level": local_low,
                "quality": min(20, quality),
                "distance": distance,
                "volume_ratio": volume_ratio
            }

    return None


# ============================================================
# 5M CONFIRMATION
# ============================================================

def five_min_confirmation(c5, direction):

    if len(c5) < 40:
        return 0, {}

    c = closes(c5)
    v = volumes(c5)

    e9 = ema(c, 9)
    e20 = ema(c, 20)

    avg_v = average_volume(c5, 20)

    if avg_v <= 0:
        return 0, {}

    volume_ratio = v[-1] / avg_v

    score = 0

    if direction == "LONG":

        if e9[-1] > e20[-1]:
            score += 7

        if c[-1] > e9[-1]:
            score += 5

        if c[-1] > c[-2]:
            score += 3

        if volume_ratio >= 1.25:
            score += 5

    else:

        if e9[-1] < e20[-1]:
            score += 7

        if c[-1] < e9[-1]:
            score += 5

        if c[-1] < c[-2]:
            score += 3

        if volume_ratio >= 1.25:
            score += 5

    return min(20, score), {
        "volume_ratio": volume_ratio
    }


# ============================================================
# OI SCORE
# ============================================================

def oi_confirmation(inst_id, direction):

    current_oi = get_open_interest(inst_id)

    if current_oi is None:
        return 0, "UNAVAILABLE", None

    delta = get_oi_change(inst_id)

    if delta is None:
        return 5, "AVAILABLE", current_oi

    if delta >= 0.40:
        return 10, "RISING", current_oi

    if delta >= 0:
        return 7, "STABLE+", current_oi

    if delta > -0.40:
        return 4, "FLAT", current_oi

    return 1, "FALLING", current_oi


# ============================================================
# VOLATILITY / RISK
# ============================================================

def build_trade(c5, setup, direction):

    price = closes(c5)[-1]

    level = setup["level"]

    a = atr(c5)

    if a <= 0:
        return None

    # PRE-ENTRY зона.
    if direction == "LONG":

        entry_low = level * 0.9995
        entry_high = level * 1.0003

        # Не отправляем сигнал, если цена уже слишком далеко
        # ушла за уровень.
        if price > level * 1.0008:
            return None

        entry = (
            entry_low + entry_high
        ) / 2

        # SL за уровень + ATR buffer.
        sl = min(
            level - a * 0.65,
            entry * 0.992
        )

        risk = entry - sl

        if risk <= 0:
            return None

        tp1 = entry + risk * TP1_R
        tp2 = entry + risk * TP2_R
        tp3 = entry + risk * TP3_R

    else:

        entry_low = level * 0.9997
        entry_high = level * 1.0005

        if price < level * 0.9992:
            return None

        entry = (
            entry_low + entry_high
        ) / 2

        sl = max(
            level + a * 0.65,
            entry * 1.008
        )

        risk = sl - entry

        if risk <= 0:
            return None

        tp1 = entry - risk * TP1_R
        tp2 = entry - risk * TP2_R
        tp3 = entry - risk * TP3_R

    risk_pct = abs(risk / entry) * 100

    if not (
        MIN_RISK_PCT
        <= risk_pct
        <= MAX_RISK_PCT
    ):
        return None

    return {
        "entry_low": entry_low,
        "entry_high": entry_high,
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "risk_pct": risk_pct,
        "atr": a
    }


# ============================================================
# SCORE
# ============================================================

def calculate_score(
    direction,
    setup,
    confirm_score,
    oi_score,
    oi_state,
    c1h,
    c15,
    c5,
    volume_usd
):

    score = 0

    # 1H structure: 20
    score += 20

    # Level quality: 20
    score += int(
        clamp(
            setup["quality"],
            0,
            20
        )
    )

    # 5M confirmation: 20
    score += int(
        clamp(
            confirm_score,
            0,
            20
        )
    )

    # OI: 10
    score += int(
        clamp(
            oi_score,
            0,
            10
        )
    )

    # 15M structure / compression: 15
    score += 15

    # Liquidity: 10
    if volume_usd >= 500_000_000:
        score += 10
    elif volume_usd >= 200_000_000:
        score += 9
    elif volume_usd >= 100_000_000:
        score += 8
    else:
        score += 6

    # Волатильность / риск: 5
    a = atr(c5)
    price = closes(c5)[-1]

    atr_pct = (
        a / price * 100
        if price > 0 else 999
    )

    if 0.25 <= atr_pct <= 2.0:
        score += 5
    elif atr_pct <= 3.0:
        score += 3

    return int(
        clamp(
            score,
            0,
            100
        )
    )


# ============================================================
# SCORE LABEL
# ============================================================

def score_label(score):

    if score >= 95:
        return "ELITE"

    if score >= 90:
        return "PREMIUM"

    if score >= 85:
        return "STRONG"

    return "STANDARD"


# ============================================================
# CHART
# ============================================================

def make_chart(
    c5,
    symbol,
    direction,
    setup,
    trade
):

    # Берём последние 60 свечей.
    candles = c5[-60:]

    fig, ax = plt.subplots(
        figsize=(12, 6.5),
        dpi=150
    )

    fig.patch.set_facecolor("#0b0f14")
    ax.set_facecolor("#0b0f14")

    for i, candle in enumerate(candles):

        o = candle["open"]
        h = candle["high"]
        l = candle["low"]
        c = candle["close"]

        color = (
            "#00d084"
            if c >= o
            else "#ff4d6d"
        )

        # Wick
        ax.plot(
            [i, i],
            [l, h],
            color=color,
            linewidth=1
        )

        # Body
        body_low = min(o, c)
        body_height = abs(c - o)

        if body_height == 0:
            body_height = max(
                abs(h - l) * 0.03,
                c * 0.00001
            )

        rect = Rectangle(
            (
                i - 0.32,
                body_low
            ),
            0.64,
            body_height,
            facecolor=color,
            edgecolor=color,
            linewidth=0.8
        )

        ax.add_patch(rect)

    # Главный уровень
    level = setup["level"]

    ax.axhline(
        level,
        color="#ffd166",
        linewidth=1.6,
        linestyle="--"
    )

    # Entry zone
    ax.axhspan(
        trade["entry_low"],
        trade["entry_high"],
        color="#00b4d8",
        alpha=0.18
    )

    # SL
    ax.axhline(
        trade["sl"],
        color="#ff4d6d",
        linewidth=1.4,
        linestyle=":"
    )

    # TP
    ax.axhline(
        trade["tp1"],
        color="#00d084",
        linewidth=1.0,
        linestyle=":"
    )

    ax.axhline(
        trade["tp2"],
        color="#00d084",
        linewidth=1.0,
        linestyle=":"
    )

    ax.axhline(
        trade["tp3"],
        color="#00d084",
        linewidth=1.2,
        linestyle=":"
    )

    ax.text(
        len(candles) - 1,
        level,
        f"  LEVEL {fmt_price(level)}",
        color="#ffd166",
        fontsize=9,
        va="bottom"
    )

    ax.text(
        len(candles) - 1,
        trade["entry"],
        f"  ENTRY {fmt_price(trade['entry'])}",
        color="#00b4d8",
        fontsize=9
    )

    ax.text(
        len(candles) - 1,
        trade["sl"],
        f"  SL {fmt_price(trade['sl'])}",
        color="#ff4d6d",
        fontsize=9
    )

    ax.text(
        len(candles) - 1,
        trade["tp1"],
        f"  TP1 {fmt_price(trade['tp1'])}",
        color="#00d084",
        fontsize=8
    )

    ax.text(
        len(candles) - 1,
        trade["tp2"],
        f"  TP2 {fmt_price(trade['tp2'])}",
        color="#00d084",
        fontsize=8
    )

    ax.text(
        len(candles) - 1,
        trade["tp3"],
        f"  TP3 {fmt_price(trade['tp3'])}",
        color="#00d084",
        fontsize=8
    )

    ax.set_title(
        f"{symbol}USDT • 5M • {direction} • PRE-ENTRY",
        color="white",
        fontsize=15,
        fontweight="bold",
        pad=12
    )

    ax.grid(
        alpha=0.08,
        color="white"
    )

    ax.tick_params(
        colors="#9aa4b2"
    )

    for spine in ax.spines.values():
        spine.set_color("#202832")

    ax.set_xlim(
        -1,
        len(candles) + 4
    )

    plt.tight_layout()

    image = io.BytesIO()

    plt.savefig(
        image,
        format="png",
        facecolor=fig.get_facecolor(),
        bbox_inches="tight"
    )

    plt.close(fig)

    image.seek(0)

    return image


# ============================================================
# TELEGRAM MESSAGE
# ============================================================

def build_signal_message(
    market,
    direction,
    setup,
    trade,
    score,
    oi_state,
    oi_usd
):

    symbol = market["symbol"]

    label = score_label(score)

    volume = market["volume_usd"]

    risk_text = f"{trade['risk_pct']:.2f}%"

    direction_emoji = (
        "🔥 LONG"
        if direction == "LONG"
        else "🔻 SHORT"
    )

    strategy = setup["strategy"]

    if direction == "LONG":

        logic = (
            "Цена находится непосредственно перед уровнем сопротивления. "
            "1H подтверждает бычье направление, 15M формирует сетап, "
            "а 5M даёт подтверждение для подготовки пробойного входа."
        )

    else:

        logic = (
            "Цена находится непосредственно перед уровнем поддержки. "
            "1H подтверждает медвежье направление, 15M формирует сетап, "
            "а 5M даёт подтверждение для подготовки пробойного входа."
        )

    if oi_usd:
        oi_text = fmt_volume(oi_usd)
    else:
        oi_text = "N/A"

    return f"""
🔥 <b>{symbol}USDT — {direction_emoji}</b>

💰 <b>Текущая цена:</b> {fmt_price(market['price'])}
💵 <b>24H объём:</b> {fmt_volume(volume)}

⭐ <b>SIGNAL SCORE: {score}/100 — {label}</b>

🧠 <b>ЛОГИКА СДЕЛКИ</b>

Стратегия: <b>{strategy}</b>

{logic}

📊 <b>1H:</b> {direction} STRUCTURE
📊 <b>15M:</b> SETUP
⚡ <b>5M:</b> ENTRY CONFIRMATION

💧 <b>ЛИКВИДНОСТЬ:</b> HIGH
📦 <b>ОБЪЁМ:</b> HIGH
⚡ <b>OI:</b> {oi_state}
💼 <b>OI:</b> {oi_text}

🎯 <b>ТОЧКА ВХОДА</b>

{fmt_price(trade['entry_low'])} — {fmt_price(trade['entry_high'])}

🛑 <b>STOP LOSS</b>

{fmt_price(trade['sl'])}
Риск: −{risk_text}

🪜 <b>ЗАКРЫТИЕ ЛЕСЕНКОЙ</b>

TP1 — 30%
{fmt_price(trade['tp1'])}

TP2 — 30%
{fmt_price(trade['tp2'])}

TP3 — 40%
{fmt_price(trade['tp3'])}

🔒 После TP1 → SL в BE

📍 <b>Основной уровень:</b> {fmt_price(setup['level'])}

📈 <b>Рабочий таймфрейм входа:</b> 5M
🧭 <b>Фильтр направления:</b> 1H
🔎 <b>Формирование сетапа:</b> 15M

⚠️ <b>ВАЖНО</b>

Это <b>PRE-ENTRY</b> сигнал около уровня.

Не догоняем рынок после сильной свечи.

Если цена уже ушла далеко от указанной зоны —
сделку пропускаем.

<b>Качество важнее количества.</b>
"""


# ============================================================
# SIGNAL LIMITS
# ============================================================

def cleanup_signal_history():

    global signal_times

    cutoff = time.time() - 24 * 3600

    signal_times = [
        x for x in signal_times
        if x >= cutoff
    ]


def can_send_signal(inst_id):

    cleanup_signal_history()

    if len(signal_times) >= MAX_SIGNALS_PER_DAY:
        return False, "DAILY LIMIT"

    if signal_times:

        last_global = max(signal_times)

        if (
            time.time() - last_global
            < GLOBAL_SIGNAL_GAP_MIN * 60
        ):
            return False, "GLOBAL COOLDOWN"

    if inst_id in cooldowns:

        if (
            time.time() - cooldowns[inst_id]
            < SYMBOL_COOLDOWN_MIN * 60
        ):
            return False, "SYMBOL COOLDOWN"

    return True, "OK"


# ============================================================
# MORNING MESSAGE
# ============================================================

def morning_greeting():

    global last_greeting_date

    current = now_kyiv()

    # Отправляем после 08:00.
    if current.hour < 8:
        return

    today = current.date()

    if last_greeting_date == today:
        return

    text = """
☀️ <b>Доброе утро, ребята!</b>

🚀 Начинаем новый торговый день.

Сегодня работаем только качественные
скальперские сетапы на фьючерсах.

🎯 1H — направление
🔎 15M — формирование сетапа
⚡ 5M — подтверждение входа

💎 Минимальный SIGNAL SCORE — <b>80/100</b>.

Не догоняем цену.
Не увеличиваем риск после убытка.
Не входим в сделку, если цена уже ушла
от зоны PRE-ENTRY.

<b>Качество важнее количества.</b>

Удачного торгового дня! 🟢
"""

    try:

        bot.send_message(
            CHANNEL_ID,
            text
        )

        last_greeting_date = today

        log.info("MORNING GREETING SENT")

    except Exception as e:

        log.error(
            "MORNING GREETING ERROR | %s",
            e
        )


# ============================================================
# STARTUP
# ============================================================

def send_startup():

    global last_startup_message

    if last_startup_message:
        return

    text = """
🚀 <b>QUANTUM SCALPER V5 ONLINE</b>

🟢 OKX Market Data
🟢 24H USD Volume Filter
🟢 Open Interest
🟢 1H Structure
🟢 15M Setup
🟢 5M Confirmation
🟢 Telegram
🟢 Candlestick Charts

🧠 <b>3 стратегии:</b>

1️⃣ Horizontal Level Breakout
2️⃣ Trendline Compression Breakout
3️⃣ Momentum Breakout

⭐ <b>Минимальный Score: 80/100</b>

💰 <b>Минимальный 24H объём: $60M</b>

⏱ Сканирование: каждые 30 секунд
🎯 PRE-ENTRY режим
🛡 Контроль частоты сигналов

<b>Качество важнее количества.</b>

🟢 READY → ACTIVE
"""

    try:

        bot.send_message(
            CHANNEL_ID,
            text
        )

        last_startup_message = True

        log.info("STARTUP MESSAGE SENT")

    except Exception as e:

        log.error(
            "STARTUP MESSAGE ERROR | %s",
            e
        )


# ============================================================
# ANALYZE SYMBOL
# ============================================================

def analyze_market(market):

    inst_id = market["instId"]

    log.info(
        "ANALYSE | %s | VOL=%s",
        inst_id,
        fmt_volume(market["volume_usd"])
    )

    c1h = get_candles(
        inst_id,
        "1H",
        100
    )

    if len(c1h) < 60:
        return None

    direction = get_1h_direction(c1h)

    if not direction:
        return None

    time.sleep(0.10)

    c15 = get_candles(
        inst_id,
        "15m",
        100
    )

    if len(c15) < 40:
        return None

    time.sleep(0.10)

    c5 = get_candles(
        inst_id,
        "5m",
        100
    )

    if len(c5) < 50:
        return None

    # ========================================================
    # ИЩЕМ ТРИ СТРАТЕГИИ
    # ========================================================

    setups = []

    horizontal = horizontal_setup(
        c15,
        direction
    )

    if horizontal:
        setups.append(horizontal)

    trendline = trendline_setup(
        c15,
        direction
    )

    if trendline:
        setups.append(trendline)

    momentum = momentum_setup(
        c5,
        direction
    )

    if momentum:
        setups.append(momentum)

    if not setups:
        return None

    # Берём лучший сетап.
    setup = max(
        setups,
        key=lambda x: x["quality"]
    )

    # ========================================================
    # 5M CONFIRMATION
    # ========================================================

    confirm_score, confirm_data = five_min_confirmation(
        c5,
        direction
    )

    if confirm_score < 12:
        return None

    # ========================================================
    # TRADE
    # ========================================================

    trade = build_trade(
        c5,
        setup,
        direction
    )

    if not trade:
        return None

    # ========================================================
    # OI
    # ========================================================

    oi_score, oi_state, oi_usd = oi_confirmation(
        inst_id,
        direction
    )

    # ========================================================
    # SCORE
    # ========================================================

    score = calculate_score(
        direction=direction,
        setup=setup,
        confirm_score=confirm_score,
        oi_score=oi_score,
        oi_state=oi_state,
        c1h=c1h,
        c15=c15,
        c5=c5,
        volume_usd=market["volume_usd"]
    )

    log.info(
        "CANDIDATE | %s | %s | %s | SCORE=%s | OI=%s | VOL=%s",
        inst_id,
        direction,
        setup["strategy"],
        score,
        oi_state,
        fmt_volume(market["volume_usd"])
    )

    if score < MIN_SCORE:
        return None

    return {
        "market": market,
        "direction": direction,
        "setup": setup,
        "trade": trade,
        "score": score,
        "oi_state": oi_state,
        "oi_usd": oi_usd,
        "candles_5m": c5
    }


# ============================================================
# SEND SIGNAL
# ============================================================

def send_signal(signal):

    market = signal["market"]
    inst_id = market["instId"]

    allowed, reason = can_send_signal(
        inst_id
    )

    if not allowed:

        log.info(
            "SIGNAL BLOCKED | %s | %s",
            inst_id,
            reason
        )

        return False

    symbol = market["symbol"]

    try:

        chart = make_chart(
            signal["candles_5m"],
            symbol,
            signal["direction"],
            signal["setup"],
            signal["trade"]
        )

        # ----------------------------------------------------
        # СНАЧАЛА ФОТО
        # ----------------------------------------------------

        bot.send_photo(
            CHANNEL_ID,
            chart,
            caption=(
                f"📊 <b>{symbol}USDT</b> • "
                f"5M • "
                f"{signal['direction']} • "
                f"<b>PRE-ENTRY</b>"
            )
        )

        # ----------------------------------------------------
        # ПОТОМ ПОЛНЫЙ СИГНАЛ
        # ----------------------------------------------------

        message = build_signal_message(
            market=market,
            direction=signal["direction"],
            setup=signal["setup"],
            trade=signal["trade"],
            score=signal["score"],
            oi_state=signal["oi_state"],
            oi_usd=signal["oi_usd"]
        )

        bot.send_message(
            CHANNEL_ID,
            message
        )

        cooldowns[inst_id] = time.time()
        signal_times.append(time.time())

        log.info(
            "SIGNAL SENT | %s | %s | %s | SCORE=%s",
            inst_id,
            signal["direction"],
            signal["setup"]["strategy"],
            signal["score"]
        )

        return True

    except Exception as e:

        log.error(
            "SIGNAL SEND ERROR | %s | %s",
            inst_id,
            e
        )

        return False


# ============================================================
# SCANNER
# ============================================================

def scanner_loop():

    log.info("=" * 55)
    log.info("%s STARTING", APP_VERSION)
    log.info(
        "MIN 24H USD VOLUME: $%s",
        f"{MIN_VOLUME_USD:,}"
    )
    log.info(
        "MIN SCORE: %s",
        MIN_SCORE
    )
    log.info(
        "MAX SIGNALS / DAY: %s",
        MAX_SIGNALS_PER_DAY
    )
    log.info(
        "SYMBOL COOLDOWN: %s min",
        SYMBOL_COOLDOWN_MIN
    )
    log.info(
        "GLOBAL SIGNAL GAP: %s min",
        GLOBAL_SIGNAL_GAP_MIN
    )
    log.info(
        "SCAN INTERVAL: %ss",
        SCAN_INTERVAL
    )
    log.info(
        "TIMEZONE: Europe/Kyiv"
    )
    log.info("=" * 55)

    send_startup()

    scan_number = 0

    while True:

        scan_number += 1

        started = time.time()

        try:

            morning_greeting()

            markets = get_markets()

            log.info(
                "SCAN #%s | MARKETS >= $60M: %s",
                scan_number,
                len(markets)
            )

            if not markets:

                log.warning(
                    "NO MARKETS PASSED $60M FILTER"
                )

                time.sleep(
                    SCAN_INTERVAL
                )

                continue

            candidates = []

            for market in markets:

                try:

                    result = analyze_market(
                        market
                    )

                    if result:
                        candidates.append(result)

                    # Небольшая пауза между запросами,
                    # чтобы не долбить API.
                    time.sleep(0.15)

                except Exception as e:

                    log.error(
                        "ANALYSIS ERROR | %s | %s",
                        market["instId"],
                        e
                    )

            # =================================================
            # ВАЖНО:
            # За один цикл отправляем только ЛУЧШИЙ сигнал.
            # =================================================

            if candidates:

                candidates.sort(
                    key=lambda x: x["score"],
                    reverse=True
                )

                best = candidates[0]

                log.info(
                    "BEST CANDIDATE | %s | SCORE=%s",
                    best["market"]["instId"],
                    best["score"]
                )

                send_signal(best)

            elapsed = time.time() - started

            log.info(
                "SCAN #%s COMPLETE | %.1fs",
                scan_number,
                elapsed
            )

            sleep_time = max(
                5,
                SCAN_INTERVAL - elapsed
            )

            time.sleep(
                sleep_time
            )

        except Exception as e:

            log.exception(
                "SCANNER ERROR | %s",
                e
            )

            time.sleep(15)


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    threading.Thread(
        target=run_web_server,
        daemon=True
    ).start()

    scanner_loop()
