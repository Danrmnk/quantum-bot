import os
import time
import math
import threading
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
import telebot

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle


# ============================================================
# QUANTUM SCALPER V5
# ============================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHANNEL_ID = os.getenv("CHANNEL_ID", "")

OKX_BASE = "https://www.okx.com"

MIN_VOLUME_USD = 60_000_000
MIN_SCORE = 80

SCAN_INTERVAL = 30

# Сколько одновременно анализировать.
# Берём самые ликвидные монеты, которые прошли $60M.
MAX_MARKETS = 80

TIMEZONE = ZoneInfo("Europe/Kyiv")

# Не даём повторять тот же самый сетап.
SIGNAL_COOLDOWN = 60 * 60 * 3

# Если цена ушла дальше этой дистанции от PRE-ENTRY —
# сигнал считается устаревшим.
MAX_ENTRY_DISTANCE = 0.006

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "QuantumScalperV5/1.0"
})

bot = telebot.TeleBot(TELEGRAM_TOKEN)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

log = logging.getLogger("QUANTUM")


# ============================================================
# СОСТОЯНИЕ
# ============================================================

signal_history = {}
last_morning_date = None
scan_number = 0


# ============================================================
# HTTP SERVER FOR RENDER
# ============================================================

class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"QUANTUM SCALPER V5 ONLINE"
        )

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        return


def run_web_server():
    port = int(os.getenv("PORT", "10000"))

    server = HTTPServer(
        ("0.0.0.0", port),
        HealthHandler
    )

    log.info("WEB SERVER: 0.0.0.0:%s", port)
    server.serve_forever()


# ============================================================
# UTILS
# ============================================================

def now_kyiv():
    return datetime.now(TIMEZONE)


def safe_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def format_price(value):
    value = safe_float(value)

    if value == 0:
        return "0"

    if value >= 1000:
        return f"{value:,.2f}"

    if value >= 100:
        return f"{value:.2f}"

    if value >= 1:
        return f"{value:.4f}".rstrip("0").rstrip(".")

    if value >= 0.01:
        return f"{value:.5f}".rstrip("0").rstrip(".")

    if value >= 0.0001:
        return f"{value:.7f}".rstrip("0").rstrip(".")

    return f"{value:.10f}".rstrip("0").rstrip(".")


def percent(value):
    return f"{value * 100:.2f}%"


def clamp(value, low, high):
    return max(low, min(high, value))


# ============================================================
# OKX API
# ============================================================

def okx_get(path, params=None, timeout=8):

    try:
        response = SESSION.get(
            OKX_BASE + path,
            params=params,
            timeout=timeout
        )

        response.raise_for_status()

        data = response.json()

        if data.get("code") != "0":
            return None

        return data.get("data", [])

    except Exception as exc:
        log.warning("OKX ERROR %s | %s", path, exc)
        return None


def get_tickers():

    data = okx_get(
        "/api/v5/market/tickers",
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

        # OKX volCcy24h для деривативов нельзя
        # напрямую показывать как USD.
        #
        # Переводим базовый объём в приблизительный USD.
        base_volume = safe_float(item.get("volCcy24h"))
        volume_usd = base_volume * last

        if volume_usd < MIN_VOLUME_USD:
            continue

        markets.append({
            "inst_id": inst_id,
            "symbol": inst_id.split("-")[0],
            "price": last,
            "volume_usd": volume_usd,
            "high24h": safe_float(item.get("high24h")),
            "low24h": safe_float(item.get("low24h")),
        })

    markets.sort(
        key=lambda x: x["volume_usd"],
        reverse=True
    )

    return markets[:MAX_MARKETS]


def get_candles(inst_id, bar, limit=120):

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
            "volume_quote": safe_float(row[7]),
            "confirmed": row[8] == "1"
        })

    # Используем только закрытые свечи.
    confirmed = [
        x for x in candles
        if x["confirmed"]
    ]

    return confirmed


def get_open_interest(inst_id):

    data = okx_get(
        "/api/v5/public/open-interest",
        {
            "instType": "SWAP",
            "instId": inst_id
        }
    )

    if not data:
        return None

    return safe_float(
        data[0].get("oi")
    )


# ============================================================
# TECHNICAL FUNCTIONS
# ============================================================

def ema(values, period):

    if len(values) < period:
        return None

    multiplier = 2 / (period + 1)

    result = sum(values[:period]) / period

    for price in values[period:]:
        result = (
            (price - result) * multiplier
            + result
        )

    return result


def average(values):

    if not values:
        return 0

    return sum(values) / len(values)


def atr(candles, period=14):

    if len(candles) < period + 1:
        return 0

    trs = []

    for i in range(1, len(candles)):

        current = candles[i]
        previous = candles[i - 1]

        tr = max(
            current["high"] - current["low"],
            abs(
                current["high"]
                - previous["close"]
            ),
            abs(
                current["low"]
                - previous["close"]
            )
        )

        trs.append(tr)

    return average(trs[-period:])


def candle_body(candle):

    return abs(
        candle["close"] - candle["open"]
    )


def candle_range(candle):

    return max(
        candle["high"] - candle["low"],
        1e-12
    )


def bullish(candle):

    return candle["close"] > candle["open"]


def bearish(candle):

    return candle["close"] < candle["open"]


def volume_ratio(candles, period=20):

    if len(candles) < period + 1:
        return 1

    previous = candles[-period-1:-1]

    avg = average(
        [x["volume"] for x in previous]
    )

    if avg <= 0:
        return 1

    return candles[-1]["volume"] / avg


# ============================================================
# MARKET STRUCTURE
# ============================================================

def structure_1h(candles):

    if len(candles) < 30:
        return "NEUTRAL"

    recent = candles[-12:]
    previous = candles[-24:-12]

    recent_high = max(x["high"] for x in recent)
    previous_high = max(x["high"] for x in previous)

    recent_low = min(x["low"] for x in recent)
    previous_low = min(x["low"] for x in previous)

    if (
        recent_high > previous_high
        and recent_low > previous_low
    ):
        return "BULLISH"

    if (
        recent_high < previous_high
        and recent_low < previous_low
    ):
        return "BEARISH"

    return "NEUTRAL"


# ============================================================
# LEVEL DETECTION
# ============================================================

def cluster_levels(candles):

    """
    Ищем уровни по локальным экстремумам.
    """

    if len(candles) < 30:
        return []

    points = []

    for i in range(2, len(candles) - 2):

        c = candles[i]

        left = candles[i - 2:i]
        right = candles[i + 1:i + 3]

        if (
            c["high"]
            >= max(x["high"] for x in left)
            and
            c["high"]
            >= max(x["high"] for x in right)
        ):
            points.append(c["high"])

        if (
            c["low"]
            <= min(x["low"] for x in left)
            and
            c["low"]
            <= min(x["low"] for x in right)
        ):
            points.append(c["low"])

    if not points:
        return []

    points.sort()

    levels = []

    for point in points:

        merged = False

        for level in levels:

            tolerance = point * 0.0015

            if abs(point - level["price"]) <= tolerance:

                level["price"] = (
                    level["price"] * level["touches"]
                    + point
                ) / (level["touches"] + 1)

                level["touches"] += 1

                merged = True
                break

        if not merged:

            levels.append({
                "price": point,
                "touches": 1
            })

    levels.sort(
        key=lambda x: x["touches"],
        reverse=True
    )

    return levels[:15]


def nearest_level(candles, price):

    levels = cluster_levels(candles)

    if not levels:
        return None

    levels.sort(
        key=lambda x: abs(x["price"] - price)
    )

    return levels[0]


# ============================================================
# THIRD TOUCH
# ============================================================

def detect_third_touch(candles, level):

    if not level:
        return None

    if len(candles) < 30:
        return None

    price = level["price"]

    tolerance = price * 0.0015

    touches = []

    for candle in candles[:-2]:

        touched = (
            candle["low"] <= price + tolerance
            and
            candle["high"] >= price - tolerance
        )

        if touched:
            touches.append(candle)

    if len(touches) < 3:
        return None

    # Берём последние три независимых касания.
    selected = []

    for candle in touches:

        if not selected:
            selected.append(candle)
            continue

        previous = selected[-1]

        # Минимум 2 свечи между касаниями.
        if candle["ts"] - previous["ts"] >= 10 * 60 * 1000:
            selected.append(candle)

    if len(selected) < 3:
        return None

    last_touch = selected[-1]

    distance = abs(
        candles[-1]["close"] - price
    ) / price

    if distance > 0.004:
        return None

    # Определяем, сопротивление это или поддержка.
    reactions = []

    for touch in selected:

        after = [
            x for x in candles
            if x["ts"] > touch["ts"]
        ][:4]

        if not after:
            continue

        future_price = after[-1]["close"]

        if future_price < price:
            reactions.append("RESISTANCE")
        elif future_price > price:
            reactions.append("SUPPORT")

    if not reactions:
        return None

    resistance_count = reactions.count("RESISTANCE")
    support_count = reactions.count("SUPPORT")

    if resistance_count >= support_count:
        level_type = "RESISTANCE"
    else:
        level_type = "SUPPORT"

    return {
        "type": level_type,
        "touches": 3,
        "level": price,
        "last_touch": last_touch
    }


# ============================================================
# HORIZONTAL BREAKOUT
# ============================================================

def horizontal_breakout(candles, level, direction):

    if not level or len(candles) < 25:
        return False

    price = level["price"]

    current = candles[-1]
    previous = candles[-2]

    vol_ratio = volume_ratio(candles)

    body_ratio = (
        candle_body(current)
        / candle_range(current)
    )

    if direction == "LONG":

        crossed = (
            previous["close"] <= price
            and current["close"] > price
        )

        strong = (
            current["close"] > current["open"]
            and body_ratio >= 0.45
            and vol_ratio >= 1.15
        )

        return crossed and strong

    else:

        crossed = (
            previous["close"] >= price
            and current["close"] < price
        )

        strong = (
            current["close"] < current["open"]
            and body_ratio >= 0.45
            and vol_ratio >= 1.15
        )

        return crossed and strong


# ============================================================
# TRENDLINE COMPRESSION
# ============================================================

def trendline_compression(candles, direction):

    if len(candles) < 35:
        return False, None

    recent = candles[-20:]

    highs = [x["high"] for x in recent]
    lows = [x["low"] for x in recent]

    high_slope = highs[-1] - highs[0]
    low_slope = lows[-1] - lows[0]

    first_width = highs[0] - lows[0]
    last_width = highs[-1] - lows[-1]

    if first_width <= 0:
        return False, None

    compression = (
        last_width / first_width
    ) < 0.70

    if not compression:
        return False, None

    current = candles[-1]

    vol_ratio = volume_ratio(candles)

    if direction == "LONG":

        breakout = (
            current["close"] > max(highs[:-1])
            and bullish(current)
            and vol_ratio >= 1.15
        )

        return breakout, max(highs[:-1])

    else:

        breakout = (
            current["close"] < min(lows[:-1])
            and bearish(current)
            and vol_ratio >= 1.15
        )

        return breakout, min(lows[:-1])


# ============================================================
# MOMENTUM
# ============================================================

def momentum_signal(candles, direction):

    if len(candles) < 30:
        return False

    closes = [
        x["close"]
        for x in candles
    ]

    e9 = ema(closes, 9)
    e20 = ema(closes, 20)

    if e9 is None or e20 is None:
        return False

    current = candles[-1]

    vol_ratio = volume_ratio(candles)

    body_ratio = (
        candle_body(current)
        / candle_range(current)
    )

    if direction == "LONG":

        local_high = max(
            x["high"]
            for x in candles[-8:-1]
        )

        return (
            e9 > e20
            and current["close"] > local_high
            and bullish(current)
            and body_ratio >= 0.50
            and vol_ratio >= 1.25
        )

    else:

        local_low = min(
            x["low"]
            for x in candles[-8:-1]
        )

        return (
            e9 < e20
            and current["close"] < local_low
            and bearish(current)
            and body_ratio >= 0.50
            and vol_ratio >= 1.25
        )


# ============================================================
# OI
# ============================================================

def oi_confirmation(inst_id, previous_oi):

    current_oi = get_open_interest(inst_id)

    if current_oi is None:
        return None, "UNAVAILABLE"

    if previous_oi is None or previous_oi <= 0:
        return current_oi, "WAITING"

    change = (
        current_oi - previous_oi
    ) / previous_oi

    if change > 0.002:
        return current_oi, "RISING"

    if change < -0.002:
        return current_oi, "FALLING"

    return current_oi, "FLAT"


# ============================================================
# SCORE
# ============================================================

def calculate_score(
    direction,
    structure,
    candles_5m,
    candles_15m,
    level,
    strategy,
    oi_state
):

    score = 0

    # 1H structure
    if (
        direction == "LONG"
        and structure == "BULLISH"
    ):
        score += 20

    elif (
        direction == "SHORT"
        and structure == "BEARISH"
    ):
        score += 20

    elif structure == "NEUTRAL":
        score += 5

    # 15M trend
    closes15 = [
        x["close"]
        for x in candles_15m
    ]

    e9_15 = ema(closes15, 9)
    e20_15 = ema(closes15, 20)

    if e9_15 and e20_15:

        if (
            direction == "LONG"
            and e9_15 > e20_15
        ):
            score += 15

        elif (
            direction == "SHORT"
            and e9_15 < e20_15
        ):
            score += 15

    # 5M EMA
    closes5 = [
        x["close"]
        for x in candles_5m
    ]

    e9_5 = ema(closes5, 9)
    e20_5 = ema(closes5, 20)

    if e9_5 and e20_5:

        if (
            direction == "LONG"
            and e9_5 > e20_5
        ):
            score += 10

        elif (
            direction == "SHORT"
            and e9_5 < e20_5
        ):
            score += 10

    # Volume
    vr = volume_ratio(candles_5m)

    if vr >= 2:
        score += 15
    elif vr >= 1.5:
        score += 12
    elif vr >= 1.2:
        score += 8

    # Level quality
    if level:

        touches = level.get("touches", 0)

        if touches >= 4:
            score += 10
        elif touches >= 3:
            score += 8
        elif touches >= 2:
            score += 5

    # Strategy
    if strategy == "Horizontal Level Breakout":
        score += 10

    elif strategy == "Third Touch / Level Retest":
        score += 8

    elif strategy == "Trendline Compression Breakout":
        score += 10

    elif strategy == "Momentum Breakout":
        score += 7

    # OI
    if oi_state == "RISING":
        score += 10
    elif oi_state == "FLAT":
        score += 3

    return int(clamp(score, 0, 100))


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
# BUILD TRADE
# ============================================================

def build_trade(
    direction,
    entry_level,
    candles_5m,
    strategy
):

    price = candles_5m[-1]["close"]

    a = atr(candles_5m, 14)

    if a <= 0:
        return None

    # Для скальпинга SL рассчитывается
    # от структуры + ATR, а не произвольные 1%.
    if direction == "LONG":

        structural_sl = min(
            x["low"]
            for x in candles_5m[-6:]
        )

        sl = structural_sl - a * 0.20

        risk = (
            price - sl
        ) / price

        if risk <= 0 or risk > 0.018:
            return None

        tp1 = price + risk * price * 1.0
        tp2 = price + risk * price * 2.0
        tp3 = price + risk * price * 3.0

    else:

        structural_sl = max(
            x["high"]
            for x in candles_5m[-6:]
        )

        sl = structural_sl + a * 0.20

        risk = (
            sl - price
        ) / price

        if risk <= 0 or risk > 0.018:
            return None

        tp1 = price - risk * price * 1.0
        tp2 = price - risk * price * 2.0
        tp3 = price - risk * price * 3.0

    # Зона входа небольшая.
    entry_buffer = max(
        a * 0.20,
        price * 0.0005
    )

    if direction == "LONG":

        entry_low = min(
            entry_level,
            price
        )

        entry_high = max(
            entry_level,
            price
        ) + entry_buffer

    else:

        entry_low = min(
            entry_level,
            price
        ) - entry_buffer

        entry_high = max(
            entry_level,
            price
        )

    return {
        "price": price,
        "entry_low": entry_low,
        "entry_high": entry_high,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "risk": risk,
        "strategy": strategy
    }


# ============================================================
# CHART
# ============================================================

def create_chart(
    symbol,
    candles,
    trade,
    level,
    direction
):

    filename = (
        f"/tmp/"
        f"{symbol}_"
        f"{int(time.time())}.png"
    )

    data = candles[-45:]

    fig, ax = plt.subplots(
        figsize=(12, 6),
        facecolor="#0b1020"
    )

    ax.set_facecolor("#0b1020")

    width = 0.62

    for i, candle in enumerate(data):

        o = candle["open"]
        h = candle["high"]
        l = candle["low"]
        c = candle["close"]

        color = (
            "#00d084"
            if c >= o
            else "#ff4d6d"
        )

        ax.plot(
            [i, i],
            [l, h],
            color=color,
            linewidth=1
        )

        body_bottom = min(o, c)
        body_height = max(
            abs(c - o),
            max(
                (h - l) * 0.005,
                1e-12
            )
        )

        rect = Rectangle(
            (
                i - width / 2,
                body_bottom
            ),
            width,
            body_height,
            facecolor=color,
            edgecolor=color
        )

        ax.add_patch(rect)

    # Level
    ax.axhline(
        level,
        color="#ffd166",
        linewidth=1.8,
        linestyle="--",
        label="LEVEL"
    )

    # Entry zone
    ax.axhspan(
        trade["entry_low"],
        trade["entry_high"],
        color="#00aaff",
        alpha=0.15
    )

    # SL
    ax.axhline(
        trade["sl"],
        color="#ff3864",
        linewidth=1.5,
        linestyle=":"
    )

    # TP
    ax.axhline(
        trade["tp1"],
        color="#00d084",
        linewidth=1,
        linestyle=":"
    )

    ax.axhline(
        trade["tp2"],
        color="#00d084",
        linewidth=1,
        linestyle=":"
    )

    ax.axhline(
        trade["tp3"],
        color="#00d084",
        linewidth=1,
        linestyle=":"
    )

    ax.set_title(
        f"{symbol}USDT — {direction} | 5M",
        color="white",
        fontsize=15,
        fontweight="bold"
    )

    ax.tick_params(
        colors="white"
    )

    for spine in ax.spines.values():
        spine.set_color("#26314a")

    ax.grid(
        alpha=0.12,
        color="white"
    )

    ax.text(
        0.01,
        0.97,
        f"LEVEL {format_price(level)}",
        transform=ax.transAxes,
        color="#ffd166",
        va="top",
        fontsize=10
    )

    ax.text(
        0.99,
        0.03,
        "QUANTUM SCALPER V5",
        transform=ax.transAxes,
        color="#75809a",
        ha="right",
        fontsize=9
    )

    plt.tight_layout()

    fig.savefig(
        filename,
        dpi=130,
        facecolor=fig.get_facecolor()
    )

    plt.close(fig)

    return filename


# ============================================================
# TELEGRAM MESSAGE
# ============================================================

def send_signal(
    market,
    trade,
    direction,
    strategy,
    score,
    structure,
    oi_state,
    level,
    candles_15m
):

    symbol = market["symbol"]

    label = score_label(score)

    volume_m = (
        market["volume_usd"]
        / 1_000_000
    )

    vr = volume_ratio(
        candles_15m
    )

    message = (
        f"🔥 <b>{symbol}USDT — {direction}</b>\n\n"

        f"💰 <b>Текущая цена:</b> "
        f"<code>{format_price(trade['price'])}</code>\n"

        f"💵 <b>24H объём:</b> "
        f"${volume_m:,.1f}M\n\n"

        f"⭐ <b>SIGNAL SCORE: "
        f"{score}/100 — {label}</b>\n\n"

        f"🧠 <b>ЛОГИКА СДЕЛКИ</b>\n\n"

        f"Стратегия: "
        f"<b>{strategy}</b>\n\n"

        f"Цена находится в рабочей зоне "
        f"возле уровня.\n"

        f"1H подтверждает направление: "
        f"<b>{structure}</b>\n"

        f"15M формирует сетап.\n"

        f"5M используется для входа и "
        f"подтверждения.\n\n"

        f"📊 <b>1H:</b> {structure}\n"
        f"📊 <b>15M:</b> SETUP\n"
        f"⚡ <b>5M:</b> ENTRY\n\n"

        f"💧 <b>ЛИКВИДНОСТЬ:</b> HIGH\n"
        f"📦 <b>VOLUME:</b> "
        f"{'HIGH' if vr >= 1.5 else 'NORMAL'}\n"
        f"⚡ <b>OI:</b> {oi_state}\n\n"

        f"🎯 <b>ТОЧКА ВХОДА</b>\n\n"

        f"<code>"
        f"{format_price(trade['entry_low'])}"
        f" — "
        f"{format_price(trade['entry_high'])}"
        f"</code>\n\n"

        f"🛑 <b>STOP LOSS</b>\n\n"

        f"<code>{format_price(trade['sl'])}</code>\n"
        f"Риск: −{trade['risk'] * 100:.2f}%\n\n"

        f"🪜 <b>ЗАКРЫТИЕ ЛЕСЕНКОЙ</b>\n\n"

        f"TP1 — 30%\n"
        f"<code>{format_price(trade['tp1'])}</code>\n\n"

        f"TP2 — 30%\n"
        f"<code>{format_price(trade['tp2'])}</code>\n\n"

        f"TP3 — 40%\n"
        f"<code>{format_price(trade['tp3'])}</code>\n\n"

        f"🔒 После TP1 → SL в BE\n\n"

        f"📍 <b>Основной уровень:</b> "
        f"<code>{format_price(level)}</code>\n\n"

        f"📈 <b>Рабочий таймфрейм:</b> 5M\n"
        f"🧭 <b>Фильтр направления:</b> 1H\n"
        f"🔎 <b>Формирование сетапа:</b> 15M\n\n"

        f"⚠️ <b>ВАЖНО</b>\n\n"

        f"Это PRE-ENTRY / рабочая зона.\n"
        f"Не догоняем цену после сильной свечи.\n\n"

        f"Если рынок уже ушёл далеко от "
        f"указанной зоны — сделку пропускаем.\n\n"

        f"<b>Качество важнее количества.</b>"
    )

    chart = create_chart(
        symbol,
        candles_15m[-45:],
        trade,
        level,
        direction
    )

    try:

        with open(chart, "rb") as photo:

            bot.send_photo(
                CHANNEL_ID,
                photo,
                caption=message,
                parse_mode="HTML"
            )

        log.info(
            "SIGNAL SENT | %s | %s | %s | SCORE=%s",
            market["inst_id"],
            direction,
            strategy,
            score
        )

        return True

    except Exception as exc:

        log.exception(
            "TELEGRAM SIGNAL ERROR: %s",
            exc
        )

        return False

    finally:

        try:
            os.remove(chart)
        except Exception:
            pass


# ============================================================
# MORNING MESSAGE
# ============================================================

def send_morning_message():

    global last_morning_date

    today = now_kyiv().date()

    if last_morning_date == today:
        return

    message = (
        "☀️ <b>ДОБРОЕ УТРО, РЕБЯТА!</b>\n\n"

        "Начинаем новый торговый день. 🔥\n\n"

        "Сегодня работаем спокойно и без спешки.\n\n"

        "📊 Ищем качественные скальперские сетапы.\n"
        "🎯 Не догоняем цену.\n"
        "🛑 Соблюдаем риск-менеджмент.\n"
        "💰 Не увеличиваем риск после убыточной сделки.\n\n"

        "Бот анализирует ликвидные USDT-фьючерсы "
        "с объёмом от <b>$60M за 24H</b>.\n\n"

        "⭐ Сигналы публикуются только при "
        "Score <b>80+</b>.\n\n"

        "<b>Качество важнее количества.</b>\n\n"

        "Удачного торгового дня! 🚀"
    )

    try:

        bot.send_message(
            CHANNEL_ID,
            message,
            parse_mode="HTML"
        )

        last_morning_date = today

        log.info(
            "MORNING MESSAGE SENT | %s",
            today
        )

    except Exception as exc:

        log.exception(
            "MORNING MESSAGE ERROR: %s",
            exc
        )


# ============================================================
# SIGNAL DEDUPLICATION
# ============================================================

def can_send_signal(
    inst_id,
    direction,
    strategy,
    level
):

    key = (
        f"{inst_id}|"
        f"{direction}|"
        f"{strategy}"
    )

    previous = signal_history.get(key)

    if previous is not None:

        if time.time() - previous < SIGNAL_COOLDOWN:
            return False

    # Уровень тоже учитываем.
    level_key = (
        f"{inst_id}|"
        f"{direction}|"
        f"{round(level, 8)}"
    )

    previous_level = signal_history.get(level_key)

    if previous_level is not None:

        if time.time() - previous_level < SIGNAL_COOLDOWN:
            return False

    signal_history[key] = time.time()
    signal_history[level_key] = time.time()

    return True


# ============================================================
# ANALYSE MARKET
# ============================================================

def analyse_market(market):

    inst_id = market["inst_id"]

    try:

        candles_1h = get_candles(
            inst_id,
            "1H",
            100
        )

        candles_15m = get_candles(
            inst_id,
            "15m",
            100
        )

        candles_5m = get_candles(
            inst_id,
            "5m",
            100
        )

        if (
            len(candles_1h) < 40
            or len(candles_15m) < 40
            or len(candles_5m) < 40
        ):
            return

        structure = structure_1h(
            candles_1h
        )

        # ----------------------------------------------------
        # LEVELS
        # ----------------------------------------------------

        level_data = nearest_level(
            candles_15m,
            market["price"]
        )

        level = (
            level_data["price"]
            if level_data
            else None
        )

        candidates = []

        # ----------------------------------------------------
        # THIRD TOUCH
        # ----------------------------------------------------

        third = detect_third_touch(
            candles_5m,
            level_data
        )

        if third:

            if third["type"] == "SUPPORT":

                # Поддержка рассматривается как LONG.
                if structure in (
                    "BULLISH",
                    "NEUTRAL"
                ):

                    candidates.append({
                        "direction": "LONG",
                        "strategy": "Third Touch / Level Retest",
                        "level": third["level"]
                    })

            elif third["type"] == "RESISTANCE":

                # Сопротивление рассматривается как SHORT.
                if structure in (
                    "BEARISH",
                    "NEUTRAL"
                ):

                    candidates.append({
                        "direction": "SHORT",
                        "strategy": "Third Touch / Level Retest",
                        "level": third["level"]
                    })

        # ----------------------------------------------------
        # HORIZONTAL BREAKOUT
        # ----------------------------------------------------

        if level is not None:

            # Пробой вверх.
            if horizontal_breakout(
                candles_5m,
                {"price": level},
                "LONG"
            ):

                if structure in (
                    "BULLISH",
                    "NEUTRAL"
                ):

                    candidates.append({
                        "direction": "LONG",
                        "strategy": "Horizontal Level Breakout",
                        "level": level
                    })

            # Пробой вниз.
            if horizontal_breakout(
                candles_5m,
                {"price": level},
                "SHORT"
            ):

                if structure in (
                    "BEARISH",
                    "NEUTRAL"
                ):

                    candidates.append({
                        "direction": "SHORT",
                        "strategy": "Horizontal Level Breakout",
                        "level": level
                    })

        # ----------------------------------------------------
        # TRENDLINE
        # ----------------------------------------------------

        for direction in ("LONG", "SHORT"):

            if (
                direction == "LONG"
                and structure == "BEARISH"
            ):
                continue

            if (
                direction == "SHORT"
                and structure == "BULLISH"
            ):
                continue

            breakout, trend_level = (
                trendline_compression(
                    candles_5m,
                    direction
                )
            )

            if breakout:

                candidates.append({
                    "direction": direction,
                    "strategy": "Trendline Compression Breakout",
                    "level": trend_level
                })

        # ----------------------------------------------------
        # MOMENTUM
        # ----------------------------------------------------

        for direction in ("LONG", "SHORT"):

            if (
                direction == "LONG"
                and structure == "BEARISH"
            ):
                continue

            if (
                direction == "SHORT"
                and structure == "BULLISH"
            ):
                continue

            if momentum_signal(
                candles_5m,
                direction
            ):

                recent_level = (
                    max(
                        x["high"]
                        for x in candles_5m[-8:-1]
                    )
                    if direction == "LONG"
                    else
                    min(
                        x["low"]
                        for x in candles_5m[-8:-1]
                    )
                )

                candidates.append({
                    "direction": direction,
                    "strategy": "Momentum Breakout",
                    "level": recent_level
                })

        if not candidates:
            return

        # ----------------------------------------------------
        # OI
        # ----------------------------------------------------

        current_oi = get_open_interest(
            inst_id
        )

        if current_oi is None:
            oi_state = "UNAVAILABLE"
        else:
            oi_state = "WAITING"

        # ----------------------------------------------------
        # BEST CANDIDATE
        # ----------------------------------------------------

        best = None

        for candidate in candidates:

            score = calculate_score(
                candidate["direction"],
                structure,
                candles_5m,
                candles_15m,
                level_data,
                candidate["strategy"],
                oi_state
            )

            if score < MIN_SCORE:
                continue

            trade = build_trade(
                candidate["direction"],
                candidate["level"],
                candles_5m,
                candidate["strategy"]
            )

            if trade is None:
                continue

            # Если PRE-ENTRY слишком далеко от текущей цены —
            # не отправляем.
            distance = abs(
                trade["price"]
                - candidate["level"]
            ) / trade["price"]

            if distance > MAX_ENTRY_DISTANCE:
                continue

            candidate_result = {
                **candidate,
                "score": score,
                "trade": trade
            }

            if (
                best is None
                or score > best["score"]
            ):
                best = candidate_result

        if best is None:
            return

        # ----------------------------------------------------
        # DUPLICATE PROTECTION
        # ----------------------------------------------------

        if not can_send_signal(
            inst_id,
            best["direction"],
            best["strategy"],
            best["level"]
        ):

            log.info(
                "SIGNAL BLOCKED BY COOLDOWN | %s",
                inst_id
            )

            return

        # ----------------------------------------------------
        # SEND
        # ----------------------------------------------------

        send_signal(
            market,
            best["trade"],
            best["direction"],
            best["strategy"],
            best["score"],
            structure,
            oi_state,
            best["level"],
            candles_15m
        )

    except Exception as exc:

        log.exception(
            "ANALYSIS ERROR | %s | %s",
            inst_id,
            exc
        )


# ============================================================
# MAIN SCANNER
# ============================================================

def bot_loop():

    global scan_number

    log.info("=" * 60)
    log.info("QUANTUM SCALPER V5 STARTING")
    log.info("MIN 24H VOLUME: $%s", f"{MIN_VOLUME_USD:,.0f}")
    log.info("MIN SCORE: %s", MIN_SCORE)
    log.info("MAX MARKETS: %s", MAX_MARKETS)
    log.info("SCAN INTERVAL: %ss", SCAN_INTERVAL)
    log.info("TIMEZONE: Europe/Kyiv")
    log.info("=" * 60)

    send_morning_message()

    while True:

        scan_number += 1

        started = time.time()

        try:

            log.info(
                "=============================="
            )

            log.info(
                "SCAN #%s",
                scan_number
            )

            # Проверяем утреннее сообщение каждый цикл.
            # Поэтому оно не зависит от наличия сигналов.
            send_morning_message()

            markets = get_tickers()

            log.info(
                "Markets >= $60M: %s",
                len(markets)
            )

            for index, market in enumerate(
                markets,
                start=1
            ):

                log.info(
                    "ANALYSE %s/%s | %s | VOL=$%.2fM",
                    index,
                    len(markets),
                    market["inst_id"],
                    market["volume_usd"] / 1_000_000
                )

                analyse_market(market)

                # Небольшая пауза для API.
                time.sleep(0.15)

            elapsed = time.time() - started

            log.info(
                "SCAN #%s COMPLETE | %.1fs",
                scan_number,
                elapsed
            )

        except Exception as exc:

            log.exception(
                "SCANNER ERROR: %s",
                exc
            )

        # Следующий цикл.
        time.sleep(
            max(
                5,
                SCAN_INTERVAL
            )
        )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    if not TELEGRAM_TOKEN:
        raise RuntimeError(
            "TELEGRAM_TOKEN environment variable is missing"
        )

    if not CHANNEL_ID:
        raise RuntimeError(
            "CHANNEL_ID environment variable is missing"
        )

    threading.Thread(
        target=run_web_server,
        daemon=True
    ).start()

    bot_loop()
