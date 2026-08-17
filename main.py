```python
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
# QUANTUM SCALPER V6
# ============================================================
#
# Основные изменения V6:
#
# 1. Реальное сравнение OI между сканами.
# 2. Funding Rate как дополнительный фильтр.
# 3. Предварительный отбор ликвидных рынков.
# 4. Более строгий анализ 1H / 15M / 5M.
# 5. Support / Resistance разделены.
# 6. Third Touch проверяет реакцию от уровня.
# 7. Добавлен Breakout + Retest.
# 8. Улучшен Score.
# 9. Сигнал не отправляется, если цена уже убежала.
# 10. Cooldown учитывает сетап и уровень.
# 11. API ошибки не роняют сканер.
# 12. Сканирование ограничено TOP_MARKETS.
#
# ВАЖНО:
# Этот бот только анализирует рынок и отправляет сигналы.
# Ордеры на биржу он НЕ выставляет.
# ============================================================


# ============================================================
# ENV
# ============================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHANNEL_ID = os.getenv("CHANNEL_ID", "")

OKX_BASE = os.getenv(
    "OKX_BASE",
    "https://www.okx.com"
)


# ============================================================
# CONFIG
# ============================================================

MIN_VOLUME_USD = 60_000_000

# Минимальный score для публикации.
MIN_SCORE = 80

# Сколько рынков анализировать после фильтра ликвидности.
MAX_MARKETS = 45

# Интервал между началами сканов.
SCAN_INTERVAL = 30

# Между запросами к OKX.
REQUEST_DELAY = 0.08

TIMEZONE = ZoneInfo("Europe/Kyiv")

# Один и тот же сетап не повторяем 3 часа.
SIGNAL_COOLDOWN = 60 * 60 * 3

# Если цена ушла дальше 0.60% от рабочей зоны —
# сетап устарел.
MAX_ENTRY_DISTANCE = 0.006

# Максимальный допустимый риск.
MAX_RISK = 0.018

# Минимальная сила breakout-свечи.
MIN_BODY_RATIO = 0.45

# Минимальный volume spike.
MIN_VOLUME_RATIO = 1.15

# Для сильного breakout.
STRONG_VOLUME_RATIO = 1.50

# Допуск около уровня.
LEVEL_TOLERANCE = 0.0015

# Минимальная дистанция между касаниями.
MIN_TOUCH_GAP_MINUTES = 10

# Минимальная реакция от уровня для подтверждения touch.
REACTION_ATR = 0.60

# Сколько свечей смотреть назад для уровней.
LEVEL_LOOKBACK = 100


# ============================================================
# SESSION
# ============================================================

SESSION = requests.Session()

SESSION.headers.update({
    "User-Agent": "QuantumScalperV6/1.0"
})


# ============================================================
# TELEGRAM
# ============================================================

bot = telebot.TeleBot(
    TELEGRAM_TOKEN
)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

log = logging.getLogger("QUANTUM")


# ============================================================
# STATE
# ============================================================

signal_history = {}

# Последний OI по инструменту.
oi_history = {}

# Последний funding.
funding_history = {}

last_morning_date = None

scan_number = 0


# ============================================================
# HTTP SERVER FOR RENDER
# ============================================================

class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):

        self.send_response(200)

        self.send_header(
            "Content-Type",
            "text/plain; charset=utf-8"
        )

        self.end_headers()

        self.wfile.write(
            b"QUANTUM SCALPER V6 ONLINE"
        )

    def do_HEAD(self):

        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        return


def run_web_server():

    port = int(
        os.getenv("PORT", "10000")
    )

    server = HTTPServer(
        ("0.0.0.0", port),
        HealthHandler
    )

    log.info(
        "WEB SERVER: 0.0.0.0:%s",
        port
    )

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

    return max(
        low,
        min(high, value)
    )


def average(values):

    if not values:
        return 0.0

    return sum(values) / len(values)


# ============================================================
# OKX API
# ============================================================

def okx_get(
    path,
    params=None,
    timeout=8
):

    try:

        response = SESSION.get(
            OKX_BASE + path,
            params=params,
            timeout=timeout
        )

        response.raise_for_status()

        data = response.json()

        if data.get("code") != "0":

            log.warning(
                "OKX API CODE | %s | %s",
                path,
                data.get("msg")
            )

            return None

        return data.get(
            "data",
            []
        )

    except Exception as exc:

        log.warning(
            "OKX ERROR | %s | %s",
            path,
            exc
        )

        return None


# ============================================================
# TICKERS
# ============================================================

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

        inst_id = item.get(
            "instId",
            ""
        )

        # Работаем только с USDT perpetual.
        if not inst_id.endswith(
            "-USDT-SWAP"
        ):
            continue

        last = safe_float(
            item.get("last")
        )

        if last <= 0:
            continue

        # Для derivatives OKX сообщает
        # volCcy24h в базовой валюте.
        base_volume = safe_float(
            item.get("volCcy24h")
        )

        volume_usd = (
            base_volume * last
        )

        if volume_usd < MIN_VOLUME_USD:
            continue

        markets.append({

            "inst_id": inst_id,

            "symbol": inst_id.split("-")[0],

            "price": last,

            "volume_usd": volume_usd,

            "high24h": safe_float(
                item.get("high24h")
            ),

            "low24h": safe_float(
                item.get("low24h")
            ),

            "open24h": safe_float(
                item.get("open24h")
            )
        })

    markets.sort(
        key=lambda x: x["volume_usd"],
        reverse=True
    )

    return markets[:MAX_MARKETS]


# ============================================================
# CANDLES
# ============================================================

def get_candles(
    inst_id,
    bar,
    limit=120
):

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
    return [
        candle
        for candle in candles
        if candle["confirmed"]
    ]


# ============================================================
# OPEN INTEREST
# ============================================================

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


def get_oi_state(inst_id):

    current = get_open_interest(
        inst_id
    )

    if current is None:

        return {
            "current": None,
            "change": None,
            "state": "UNAVAILABLE"
        }

    previous = oi_history.get(
        inst_id
    )

    # Сохраняем текущее значение.
    oi_history[inst_id] = current

    if previous is None or previous <= 0:

        return {
            "current": current,
            "change": None,
            "state": "WAITING"
        }

    change = (
        current - previous
    ) / previous

    if change >= 0.002:

        state = "RISING"

    elif change <= -0.002:

        state = "FALLING"

    else:

        state = "FLAT"

    return {
        "current": current,
        "change": change,
        "state": state
    }


# ============================================================
# FUNDING
# ============================================================

def get_funding_rate(inst_id):

    data = okx_get(
        "/api/v5/public/funding-rate",
        {
            "instId": inst_id
        }
    )

    if not data:
        return None

    return safe_float(
        data[0].get("fundingRate")
    )


def funding_state(inst_id):

    rate = get_funding_rate(
        inst_id
    )

    if rate is None:

        return {
            "rate": None,
            "state": "UNAVAILABLE"
        }

    funding_history[inst_id] = rate

    # 0.01% = 0.0001
    if rate >= 0.0003:

        state = "HIGH_POSITIVE"

    elif rate <= -0.0003:

        state = "HIGH_NEGATIVE"

    elif rate > 0:

        state = "POSITIVE"

    elif rate < 0:

        state = "NEGATIVE"

    else:

        state = "FLAT"

    return {
        "rate": rate,
        "state": state
    }


# ============================================================
# EMA
# ============================================================

def ema(values, period):

    if len(values) < period:
        return None

    multiplier = 2 / (
        period + 1
    )

    result = average(
        values[:period]
    )

    for price in values[period:]:

        result = (
            (price - result)
            * multiplier
            + result
        )

    return result


# ============================================================
# ATR
# ============================================================

def atr(
    candles,
    period=14
):

    if len(candles) < period + 1:
        return 0.0

    trs = []

    for i in range(
        1,
        len(candles)
    ):

        current = candles[i]

        previous = candles[i - 1]

        tr = max(

            current["high"]
            - current["low"],

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

    return average(
        trs[-period:]
    )


# ============================================================
# CANDLE HELPERS
# ============================================================

def candle_body(candle):

    return abs(
        candle["close"]
        - candle["open"]
    )


def candle_range(candle):

    return max(
        candle["high"]
        - candle["low"],
        1e-12
    )


def candle_body_ratio(candle):

    return (
        candle_body(candle)
        / candle_range(candle)
    )


def bullish(candle):

    return (
        candle["close"]
        > candle["open"]
    )


def bearish(candle):

    return (
        candle["close"]
        < candle["open"]
    )


# ============================================================
# VOLUME
# ============================================================

def volume_ratio(
    candles,
    period=20
):

    if len(candles) < period + 1:
        return 1.0

    previous = candles[
        -period - 1:-1
    ]

    avg = average([
        x["volume"]
        for x in previous
    ])

    if avg <= 0:
        return 1.0

    return (
        candles[-1]["volume"]
        / avg
    )


# ============================================================
# MARKET STRUCTURE
# ============================================================

def structure_1h(candles):

    if len(candles) < 40:
        return "NEUTRAL"

    recent = candles[-12:]

    previous = candles[-24:-12]

    recent_high = max(
        x["high"]
        for x in recent
    )

    previous_high = max(
        x["high"]
        for x in previous
    )

    recent_low = min(
        x["low"]
        for x in recent
    )

    previous_low = min(
        x["low"]
        for x in previous
    )

    closes = [
        x["close"]
        for x in candles
    ]

    e20 = ema(
        closes,
        20
    )

    current = candles[-1]["close"]

    bullish_structure = (

        recent_high > previous_high

        and recent_low > previous_low

        and (
            e20 is None
            or current > e20
        )
    )

    bearish_structure = (

        recent_high < previous_high

        and recent_low < previous_low

        and (
            e20 is None
            or current < e20
        )
    )

    if bullish_structure:
        return "BULLISH"

    if bearish_structure:
        return "BEARISH"

    return "NEUTRAL"


# ============================================================
# LOCAL LEVELS
# ============================================================

def find_local_levels(candles):

    if len(candles) < 30:
        return []

    data = candles[
        -LEVEL_LOOKBACK:
    ]

    points = []

    for i in range(
        2,
        len(data) - 2
    ):

        c = data[i]

        left = data[
            i - 2:i
        ]

        right = data[
            i + 1:i + 3
        ]

        if (
            c["high"]
            >= max(
                x["high"]
                for x in left
            )
            and
            c["high"]
            >= max(
                x["high"]
                for x in right
            )
        ):

            points.append({
                "price": c["high"],
                "type": "RESISTANCE",
                "ts": c["ts"]
            })

        if (
            c["low"]
            <= min(
                x["low"]
                for x in left
            )
            and
            c["low"]
            <= min(
                x["low"]
                for x in right
            )
        ):

            points.append({
                "price": c["low"],
                "type": "SUPPORT",
                "ts": c["ts"]
            })

    if not points:
        return []

    clusters = []

    for point in points:

        merged = False

        for level in clusters:

            tolerance = (
                level["price"]
                * LEVEL_TOLERANCE
            )

            if (
                abs(
                    point["price"]
                    - level["price"]
                )
                <= tolerance
                and
                point["type"]
                == level["type"]
            ):

                level["price"] = (

                    level["price"]
                    * level["touches"]
                    + point["price"]

                ) / (
                    level["touches"] + 1
                )

                level["touches"] += 1

                level["last_ts"] = max(
                    level["last_ts"],
                    point["ts"]
                )

                merged = True

                break

        if not merged:

            clusters.append({

                "price": point["price"],

                "type": point["type"],

                "touches": 1,

                "last_ts": point["ts"]
            })

    return clusters


# ============================================================
# LEVEL QUALITY
# ============================================================

def level_quality(
    level,
    candles
):

    if not level:
        return 0

    score = 0

    touches = level["touches"]

    if touches >= 5:
        score += 10

    elif touches >= 4:
        score += 8

    elif touches >= 3:
        score += 6

    elif touches >= 2:
        score += 3

    # Более свежий уровень ценнее.
    latest_ts = candles[-1]["ts"]

    age_hours = (
        latest_ts
        - level["last_ts"]
    ) / 3_600_000

    if age_hours <= 24:
        score += 5

    elif age_hours <= 72:
        score += 3

    return min(
        score,
        15
    )


# ============================================================
# BEST SUPPORT / RESISTANCE
# ============================================================

def nearest_level(
    levels,
    price,
    level_type
):

    candidates = [
        level
        for level in levels
        if level["type"] == level_type
    ]

    if not candidates:
        return None

    candidates.sort(
        key=lambda x:
        abs(
            x["price"]
            - price
        )
    )

    return candidates[0]


# ============================================================
# TOUCH DETECTION
# ============================================================

def level_touched(
    candle,
    level_price
):

    tolerance = (
        level_price
        * LEVEL_TOLERANCE
    )

    return (
        candle["low"]
        <= level_price + tolerance
        and
        candle["high"]
        >= level_price - tolerance
    )


# ============================================================
# THIRD TOUCH
# ============================================================

def detect_third_touch(
    candles,
    level
):

    if not level:
        return None

    if len(candles) < 40:
        return None

    price = level["price"]

    atr_value = atr(
        candles,
        14
    )

    if atr_value <= 0:
        return None

    touches = []

    last_touch_index = None

    for i, candle in enumerate(
        candles[:-2]
    ):

        if not level_touched(
            candle,
            price
        ):
            continue

        if last_touch_index is not None:

            gap = (
                i
                - last_touch_index
            )

            # 5M candles.
            if gap < 2:
                continue

        touches.append({
            "index": i,
            "candle": candle
        })

        last_touch_index = i

    if len(touches) < 3:
        return None

    selected = touches[-3:]

    reactions = []

    for item in selected[:-1]:

        idx = item["index"]

        future = candles[
            idx + 1:
            min(
                idx + 5,
                len(candles)
            )
        ]

        if not future:
            continue

        future_high = max(
            x["high"]
            for x in future
        )

        future_low = min(
            x["low"]
            for x in future
        )

        # Для resistance нужен нормальный отбой вниз.
        if level["type"] == "RESISTANCE":

            reaction = (
                price
                - future_low
            )

            if reaction >= (
                atr_value
                * REACTION_ATR
            ):

                reactions.append(
                    "BEARISH"
                )

        # Для support нужен отбой вверх.
        else:

            reaction = (
                future_high
                - price
            )

            if reaction >= (
                atr_value
                * REACTION_ATR
            ):

                reactions.append(
                    "BULLISH"
                )

    if len(reactions) < 1:
        return None

    return {

        "type": level["type"],

        "level": price,

        "touches": 3,

        "last_touch": selected[-1],

        "reaction_quality":
            len(reactions)
    }


# ============================================================
# BREAKOUT
# ============================================================

def breakout_signal(
    candles,
    level,
    direction
):

    if not level:
        return False

    if len(candles) < 5:
        return False

    price = level["price"]

    current = candles[-1]

    previous = candles[-2]

    vr = volume_ratio(
        candles
    )

    body_ratio = (
        candle_body_ratio(
            current
        )
    )

    if direction == "LONG":

        crossed = (

            previous["close"]
            <= price

            and

            current["close"]
            > price
        )

        strong = (

            bullish(current)

            and
            body_ratio
            >= MIN_BODY_RATIO

            and
            vr
            >= MIN_VOLUME_RATIO
        )

        return (
            crossed
            and strong
        )

    crossed = (

        previous["close"]
        >= price

        and

        current["close"]
        < price
    )

    strong = (

        bearish(current)

        and
        body_ratio
        >= MIN_BODY_RATIO

        and
        vr
        >= MIN_VOLUME_RATIO
    )

    return (
        crossed
        and strong
    )


# ============================================================
# BREAKOUT RETEST
# ============================================================

def breakout_retest(
    candles,
    level,
    direction
):

    if not level:
        return False

    if len(candles) < 8:
        return False

    price = level["price"]

    atr_value = atr(
        candles,
        14
    )

    if atr_value <= 0:
        return False

    # Ищем breakout в последних 6 свечах.
    search_from = max(
        2,
        len(candles) - 7
    )

    breakout_index = None

    for i in range(
        search_from,
        len(candles) - 1
    ):

        previous = candles[i - 1]

        current = candles[i]

        if direction == "LONG":

            if (
                previous["close"]
                <= price

                and

                current["close"]
                > price

                and

                bullish(current)

                and

                candle_body_ratio(
                    current
                ) >= 0.45
            ):

                breakout_index = i

        else:

            if (
                previous["close"]
                >= price

                and

                current["close"]
                < price

                and

                bearish(current)

                and

                candle_body_ratio(
                    current
                ) >= 0.45
            ):

                breakout_index = i

    if breakout_index is None:
        return False

    retest_candles = candles[
        breakout_index + 1:
    ]

    if not retest_candles:
        return False

    current = candles[-1]

    tolerance = max(
        price * LEVEL_TOLERANCE,
        atr_value * 0.20
    )

    touched = any(

        abs(
            candle["low"]
            - price
        ) <= tolerance

        or

        abs(
            candle["high"]
            - price
        ) <= tolerance

        or

        (
            candle["low"]
            <= price
            <= candle["high"]
        )

        for candle
        in retest_candles
    )

    if not touched:
        return False

    if direction == "LONG":

        return (
            current["close"] > price
            and bullish(current)
            and candle_body_ratio(current) >= 0.35
        )

    return (
        current["close"] < price
        and bearish(current)
        and candle_body_ratio(current) >= 0.35
    )


# ============================================================
# TRENDLINE COMPRESSION
# ============================================================

def trendline_compression(
    candles,
    direction
):

    if len(candles) < 35:
        return False, None

    recent = candles[-20:]

    highs = [
        x["high"]
        for x in recent
    ]

    lows = [
        x["low"]
        for x in recent
    ]

    first_width = (
        highs[0]
        - lows[0]
    )

    last_width = (
        highs[-1]
        - lows[-1]
    )

    if first_width <= 0:
        return False, None

    compression = (
        last_width
        / first_width
    ) < 0.70

    if not compression:
        return False, None

    current = candles[-1]

    vr = volume_ratio(
        candles
    )

    if direction == "LONG":

        resistance = max(
            highs[:-1]
        )

        breakout = (

            current["close"]
            > resistance

            and
            bullish(current)

            and
            candle_body_ratio(current)
            >= 0.45

            and
            vr >= MIN_VOLUME_RATIO
        )

        return (
            breakout,
            resistance
        )

    support = min(
        lows[:-1]
    )

    breakout = (

        current["close"]
        < support

        and
        bearish(current)

        and
        candle_body_ratio(current)
        >= 0.45

        and
        vr >= MIN_VOLUME_RATIO
    )

    return (
        breakout,
        support
    )


# ============================================================
# MOMENTUM
# ============================================================

def momentum_signal(
    candles,
    direction
):

    if len(candles) < 30:
        return False

    closes = [
        x["close"]
        for x in candles
    ]

    e9 = ema(
        closes,
        9
    )

    e20 = ema(
        closes,
        20
    )

    if (
        e9 is None
        or e20 is None
    ):
        return False

    current = candles[-1]

    vr = volume_ratio(
        candles
    )

    body_ratio = (
        candle_body_ratio(
            current
        )
    )

    if direction == "LONG":

        local_high = max(
            x["high"]
            for x in candles[-8:-1]
        )

        return (

            e9 > e20

            and

            current["close"]
            > local_high

            and

            bullish(current)

            and

            body_ratio >= 0.50

            and

            vr >= 1.25
        )

    local_low = min(
        x["low"]
        for x in candles[-8:-1]
    )

    return (

        e9 < e20

        and

        current["close"]
        < local_low

        and

        bearish(current)

        and

        body_ratio >= 0.50

        and

        vr >= 1.25
    )


# ============================================================
# 15M TREND
# ============================================================

def trend_15m(candles):

    closes = [
        x["close"]
        for x in candles
    ]

    e9 = ema(
        closes,
        9
    )

    e20 = ema(
        closes,
        20
    )

    if (
        e9 is None
        or e20 is None
    ):
        return "NEUTRAL"

    if e9 > e20:
        return "BULLISH"

    if e9 < e20:
        return "BEARISH"

    return "NEUTRAL"


# ============================================================
# 5M TREND
# ============================================================

def trend_5m(candles):

    closes = [
        x["close"]
        for x in candles
    ]

    e9 = ema(
        closes,
        9
    )

    e20 = ema(
        closes,
        20
    )

    if (
        e9 is None
        or e20 is None
    ):
        return "NEUTRAL"

    if e9 > e20:
        return "BULLISH"

    if e9 < e20:
        return "BEARISH"

    return "NEUTRAL"


# ============================================================
# OI + PRICE CONFIRMATION
# ============================================================

def oi_score(
    direction,
    oi_state
):

    state = oi_state["state"]

    if state == "RISING":

        return 10

    if state == "FLAT":

        return 3

    if state == "FALLING":

        # Falling OI во время breakout —
        # не идеальное подтверждение.
        return 0

    return 0


# ============================================================
# FUNDING SCORE
# ============================================================

def funding_score(
    direction,
    funding
):

    state = funding["state"]

    if state == "UNAVAILABLE":
        return 0

    if direction == "LONG":

        # Очень положительный funding
        # может означать crowded longs.
        if state == "HIGH_POSITIVE":
            return 0

        if state == "POSITIVE":
            return 2

        if state == "NEGATIVE":
            return 4

        if state == "HIGH_NEGATIVE":
            return 5

    else:

        if state == "HIGH_NEGATIVE":
            return 0

        if state == "NEGATIVE":
            return 2

        if state == "POSITIVE":
            return 4

        if state == "HIGH_POSITIVE":
            return 5

    return 0


# ============================================================
# SCORE
# ============================================================

def calculate_score(
    direction,
    structure,
    trend15,
    trend5,
    candles5,
    level,
    strategy,
    oi_data,
    funding_data
):

    score = 0

    # --------------------------------------------------------
    # 1H STRUCTURE
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # 15M
    # --------------------------------------------------------

    if (
        direction == "LONG"
        and trend15 == "BULLISH"
    ):

        score += 15

    elif (
        direction == "SHORT"
        and trend15 == "BEARISH"
    ):

        score += 15

    elif trend15 == "NEUTRAL":

        score += 3

    # --------------------------------------------------------
    # 5M
    # --------------------------------------------------------

    if (
        direction == "LONG"
        and trend5 == "BULLISH"
    ):

        score += 10

    elif (
        direction == "SHORT"
        and trend5 == "BEARISH"
    ):

        score += 10

    # --------------------------------------------------------
    # VOLUME
    # --------------------------------------------------------

    vr = volume_ratio(
        candles5
    )

    if vr >= 2.0:

        score += 15

    elif vr >= 1.5:

        score += 12

    elif vr >= 1.2:

        score += 8

    elif vr >= 1.15:

        score += 5

    # --------------------------------------------------------
    # LEVEL
    # --------------------------------------------------------

    score += level_quality(
        level,
        candles5
    )

    # --------------------------------------------------------
    # STRATEGY
    # --------------------------------------------------------

    if strategy == "Breakout + Retest":

        score += 15

    elif strategy == "Third Touch":

        score += 12

    elif strategy == "Horizontal Breakout":

        score += 10

    elif strategy == "Compression Breakout":

        score += 10

    elif strategy == "Momentum Breakout":

        score += 7

    # --------------------------------------------------------
    # OI
    # --------------------------------------------------------

    score += oi_score(
        direction,
        oi_data
    )

    # --------------------------------------------------------
    # FUNDING
    # --------------------------------------------------------

    score += funding_score(
        direction,
        funding_data
    )

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
# TRADE BUILD
# ============================================================

def build_trade(
    direction,
    entry_level,
    candles5
):

    if not candles5:
        return None

    price = candles5[-1]["close"]

    atr_value = atr(
        candles5,
        14
    )

    if atr_value <= 0:
        return None

    # --------------------------------------------------------
    # LONG
    # --------------------------------------------------------

    if direction == "LONG":

        structural_sl = min(
            x["low"]
            for x in candles5[-6:]
        )

        sl = (
            structural_sl
            - atr_value * 0.20
        )

        risk = (
            price - sl
        ) / price

        if risk <= 0:
            return None

        if risk > MAX_RISK:
            return None

        tp1 = (
            price
            + price * risk * 1.0
        )

        tp2 = (
            price
            + price * risk * 2.0
        )

        tp3 = (
            price
            + price * risk * 3.0
        )

    # --------------------------------------------------------
    # SHORT
    # --------------------------------------------------------

    else:

        structural_sl = max(
            x["high"]
            for x in candles5[-6:]
        )

        sl = (
            structural_sl
            + atr_value * 0.20
        )

        risk = (
            sl - price
        ) / price

        if risk <= 0:
            return None

        if risk > MAX_RISK:
            return None

        tp1 = (
            price
            - price * risk * 1.0
        )

        tp2 = (
            price
            - price * risk * 2.0
        )

        tp3 = (
            price
            - price * risk * 3.0
        )

    # --------------------------------------------------------
    # ENTRY ZONE
    # --------------------------------------------------------

    buffer = max(
        atr_value * 0.20,
        price * 0.0005
    )

    if direction == "LONG":

        entry_low = min(
            entry_level,
            price
        )

        entry_high = (
            max(
                entry_level,
                price
            )
            + buffer
        )

    else:

        entry_low = (
            min(
                entry_level,
                price
            )
            - buffer
        )

        entry_high = max(
            entry_level,
            price
        )

    return {

        "price": price,

        "entry_low":
            entry_low,

        "entry_high":
            entry_high,

        "sl": sl,

        "tp1": tp1,

        "tp2": tp2,

        "tp3": tp3,

        "risk": risk,

        "atr": atr_value
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

    ax.set_facecolor(
        "#0b1020"
    )

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

        body_bottom = min(
            o,
            c
        )

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

    # LEVEL
    ax.axhline(
        level,
        color="#ffd166",
        linewidth=1.8,
        linestyle="--"
    )

    # ENTRY
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
    for tp in (
        trade["tp1"],
        trade["tp2"],
        trade["tp3"]
    ):

        ax.axhline(
            tp,
            color="#00d084",
            linewidth=1,
            linestyle=":"
        )

    ax.set_title(
        (
            f"{symbol}USDT — "
            f"{direction} | 5M"
        ),
        color="white",
        fontsize=15,
        fontweight="bold"
    )

    ax.tick_params(
        colors="white"
    )

    for spine in ax.spines.values():

        spine.set_color(
            "#26314a"
        )

    ax.grid(
        alpha=0.12,
        color="white"
    )

    ax.text(
        0.01,
        0.97,
        (
            f"LEVEL "
            f"{format_price(level)}"
        ),
        transform=ax.transAxes,
        color="#ffd166",
        va="top",
        fontsize=10
    )

    ax.text(
        0.99,
        0.03,
        "QUANTUM SCALPER V6",
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
# TELEGRAM
# ============================================================

def send_signal(
    market,
    trade,
    direction,
    strategy,
    score,
    structure,
    trend15,
    trend5,
    oi_data,
    funding_data,
    level,
    candles15
):

    symbol = market["symbol"]

    label = score_label(
        score
    )

    volume_m = (
        market["volume_usd"]
        / 1_000_000
    )

    vr = volume_ratio(
        candles15
    )

    oi_change = oi_data.get(
        "change"
    )

    if oi_change is None:

        oi_text = (
            oi_data["state"]
        )

    else:

        arrow = (
            "↑"
            if oi_change > 0
            else "↓"
            if oi_change < 0
            else "→"
        )

        oi_text = (
            f"{oi_change * 100:+.2f}% "
            f"{arrow}"
        )

    funding_rate = (
        funding_data.get("rate")
    )

    if funding_rate is None:

        funding_text = (
            funding_data["state"]
        )

    else:

        funding_text = (
            f"{funding_rate * 100:+.4f}%"
        )

    message = (

        f"🔥 <b>{symbol}USDT — "
        f"{direction}</b>\n\n"

        f"💰 <b>Цена:</b> "
        f"<code>"
        f"{format_price(trade['price'])}"
        f"</code>\n"

        f"💵 <b>24H объём:</b> "
        f"${volume_m:,.1f}M\n\n"

        f"⭐ <b>SIGNAL SCORE: "
        f"{score}/100 — {label}</b>\n\n"

        f"🧠 <b>ЛОГИКА</b>\n\n"

        f"Стратегия: "
        f"<b>{strategy}</b>\n\n"

        f"1H структура: "
        f"<b>{structure}</b>\n"

        f"15M тренд: "
        f"<b>{trend15}</b>\n"

        f"5M тренд: "
        f"<b>{trend5}</b>\n\n"

        f"💧 <b>LIQUIDITY:</b> HIGH\n"

        f"📦 <b>VOLUME:</b> "
        f"{'HIGH' if vr >= 1.5 else 'NORMAL'}\n"

        f"⚡ <b>OI:</b> "
        f"{oi_text}\n"

        f"💸 <b>FUNDING:</b> "
        f"{funding_text}\n\n"

        f"🎯 <b>PRE-ENTRY ZONE</b>\n\n"

        f"<code>"
        f"{format_price(trade['entry_low'])}"
        f" — "
        f"{format_price(trade['entry_high'])}"
        f"</code>\n\n"

        f"🛑 <b>STOP LOSS</b>\n\n"

        f"<code>"
        f"{format_price(trade['sl'])}"
        f"</code>\n"

        f"Риск: "
        f"−{trade['risk'] * 100:.2f}%\n\n"

        f"🪜 <b>ЗАКРЫТИЕ ЛЕСЕНКОЙ</b>\n\n"

        f"TP1 — 30%\n"
        f"<code>"
        f"{format_price(trade['tp1'])}"
        f"</code>\n\n"

        f"TP2 — 30%\n"
        f"<code>"
        f"{format_price(trade['tp2'])}"
        f"</code>\n\n"

        f"TP3 — 40%\n"
        f"<code>"
        f"{format_price(trade['tp3'])}"
        f"</code>\n\n"

        f"🔒 После TP1 → SL в BE\n\n"

        f"📍 <b>Уровень:</b> "
        f"<code>"
        f"{format_price(level)}"
        f"</code>\n\n"

        f"⚠️ <b>ВАЖНО</b>\n\n"

        f"Это PRE-ENTRY / рабочая зона.\n"

        f"Не догоняем цену после "
        f"сильной свечи.\n\n"

        f"Если цена уже ушла дальше "
        f"рабочей зоны — пропускаем.\n\n"

        f"<b>Качество важнее количества.</b>"
    )

    chart = create_chart(
        symbol,
        candles15[-45:],
        trade,
        level,
        direction
    )

    try:

        with open(
            chart,
            "rb"
        ) as photo:

            bot.send_photo(
                CHANNEL_ID,
                photo,
                caption=message,
                parse_mode="HTML"
            )

        log.info(
            (
                "SIGNAL SENT | %s | "
                "%s | %s | SCORE=%s"
            ),
            market["inst_id"],
            direction,
            strategy,
            score
        )

        return True

    except Exception as exc:

        log.exception(
            "TELEGRAM ERROR: %s",
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

        "Сегодня работаем спокойно "
        "и без спешки.\n\n"

        "📊 Ищем качественные "
        "скальперские сетапы.\n"

        "🎯 Не догоняем цену.\n"

        "🛑 Соблюдаем риск-менеджмент.\n"

        "💰 Не увеличиваем риск "
        "после убыточной сделки.\n\n"

        f"Бот анализирует USDT perpetual "
        f"с объёмом от "
        f"<b>${MIN_VOLUME_USD / 1_000_000:.0f}M</b> "
        f"за 24H.\n\n"

        f"⭐ Сигналы публикуются только "
        f"при Score <b>{MIN_SCORE}+</b>.\n\n"

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
# DEDUPLICATION
# ============================================================

def can_send_signal(
    inst_id,
    direction,
    strategy,
    level
):

    now = time.time()

    strategy_key = (
        f"{inst_id}|"
        f"{direction}|"
        f"{strategy}"
    )

    level_key = (
        f"{inst_id}|"
        f"{direction}|"
        f"LEVEL|"
        f"{round(level, 8)}"
    )

    previous_strategy = (
        signal_history.get(
            strategy_key
        )
    )

    if (
        previous_strategy is not None
        and
        now - previous_strategy
        < SIGNAL_COOLDOWN
    ):

        return False

    previous_level = (
        signal_history.get(
            level_key
        )
    )

    if (
        previous_level is not None
        and
        now - previous_level
        < SIGNAL_COOLDOWN
    ):

        return False

    signal_history[
        strategy_key
    ] = now

    signal_history[
        level_key
    ] = now

    return True


# ============================================================
# DIRECTION FILTER
# ============================================================

def direction_allowed(
    direction,
    structure,
    trend15
):

    if direction == "LONG":

        if structure == "BEARISH":
            return False

        if trend15 == "BEARISH":
            return False

    else:

        if structure == "BULLISH":
            return False

        if trend15 == "BULLISH":
            return False

    return True


# ============================================================
# ANALYSE MARKET
# ============================================================

def analyse_market(market):

    inst_id = market["inst_id"]

    try:

        # ----------------------------------------------------
        # DATA
        # ----------------------------------------------------

        candles1h = get_candles(
            inst_id,
            "1H",
            100
        )

        time.sleep(
            REQUEST_DELAY
        )

        candles15 = get_candles(
            inst_id,
            "15m",
            100
        )

        time.sleep(
            REQUEST_DELAY
        )

        candles5 = get_candles(
            inst_id,
            "5m",
            100
        )

        if (
            len(candles1h) < 40
            or
            len(candles15) < 40
            or
            len(candles5) < 40
        ):

            return

        # ----------------------------------------------------
        # STRUCTURE
        # ----------------------------------------------------

        structure = structure_1h(
            candles1h
        )

        trend15 = trend_15m(
            candles15
        )

        trend5 = trend_5m(
            candles5
        )

        price = market["price"]

        # ----------------------------------------------------
        # LEVELS
        # ----------------------------------------------------

        levels = find_local_levels(
            candles15
        )

        support = nearest_level(
            levels,
            price,
            "SUPPORT"
        )

        resistance = nearest_level(
            levels,
            price,
            "RESISTANCE"
        )

        candidates = []

        # ----------------------------------------------------
        # THIRD TOUCH
        # ----------------------------------------------------

        # LONG from support.
        if support:

            third = detect_third_touch(
                candles5,
                support
            )

            if (
                third
                and
                direction_allowed(
                    "LONG",
                    structure,
                    trend15
                )
            ):

                candidates.append({

                    "direction": "LONG",

                    "strategy": "Third Touch",

                    "level": support["price"],

                    "level_data": support
                })

        # SHORT from resistance.
        if resistance:

            third = detect_third_touch(
                candles5,
                resistance
            )

            if (
                third
                and
                direction_allowed(
                    "SHORT",
                    structure,
                    trend15
                )
            ):

                candidates.append({

                    "direction": "SHORT",

                    "strategy": "Third Touch",

                    "level":
                        resistance["price"],

                    "level_data":
                        resistance
                })

        # ----------------------------------------------------
        # BREAKOUT + RETEST
        # ----------------------------------------------------

        if resistance:

            if (
                breakout_retest(
                    candles5,
                    resistance,
                    "LONG"
                )
                and
                direction_allowed(
                    "LONG",
                    structure,
                    trend15
                )
            ):

                candidates.append({

                    "direction": "LONG",

                    "strategy":
                        "Breakout + Retest",

                    "level":
                        resistance["price"],

                    "level_data":
                        resistance
                })

        if support:

            if (
                breakout_retest(
                    candles5,
                    support,
                    "SHORT"
                )
                and
                direction_allowed(
                    "SHORT",
                    structure,
                    trend15
                )
            ):

                candidates.append({

                    "direction": "SHORT",

                    "strategy":
                        "Breakout + Retest",

                    "level":
                        support["price"],

                    "level_data":
                        support
                })

        # ----------------------------------------------------
        # HORIZONTAL BREAKOUT
        # ----------------------------------------------------

        if resistance:

            if (
                breakout_signal(
                    candles5,
                    resistance,
                    "LONG"
                )
                and
                direction_allowed(
                    "LONG",
                    structure,
                    trend15
                )
            ):

                candidates.append({

                    "direction": "LONG",

                    "strategy":
                        "Horizontal Breakout",

                    "level":
                        resistance["price"],

                    "level_data":
                        resistance
                })

        if support:

            if (
                breakout_signal(
                    candles5,
                    support,
                    "SHORT"
                )
                and
                direction_allowed(
                    "SHORT",
                    structure,
                    trend15
                )
            ):

                candidates.append({

                    "direction": "SHORT",

                    "strategy":
                        "Horizontal Breakout",

                    "level":
                        support["price"],

                    "level_data":
                        support
                })

        # ----------------------------------------------------
        # COMPRESSION
        # ----------------------------------------------------

        for direction in (
            "LONG",
            "SHORT"
        ):

            if not direction_allowed(
                direction,
                structure,
                trend15
            ):
                continue

            breakout, level = (
                trendline_compression(
                    candles5,
                    direction
                )
            )

            if breakout:

                candidates.append({

                    "direction": direction,

                    "strategy":
                        "Compression Breakout",

                    "level": level,

                    "level_data": {

                        "price": level,

                        "type":
                            "RESISTANCE"
                            if direction == "LONG"
                            else
                            "SUPPORT",

                        "touches": 2,

                        "last_ts":
                            candles5[-1]["ts"]
                    }
                })

        # ----------------------------------------------------
        # MOMENTUM
        # ----------------------------------------------------

        for direction in (
            "LONG",
            "SHORT"
        ):

            if not direction_allowed(
                direction,
                structure,
                trend15
            ):
                continue

            if momentum_signal(
                candles5,
                direction
            ):

                if direction == "LONG":

                    level = max(
                        x["high"]
                        for x in candles5[-8:-1]
                    )

                    level_type = (
                        "RESISTANCE"
                    )

                else:

                    level = min(
                        x["low"]
                        for x in candles5[-8:-1]
                    )

                    level_type = (
                        "SUPPORT"
                    )

                candidates.append({

                    "direction": direction,

                    "strategy":
                        "Momentum Breakout",

                    "level": level,

                    "level_data": {

                        "price": level,

                        "type":
                            level_type,

                        "touches": 1,

                        "last_ts":
                            candles5[-1]["ts"]
                    }
                })

        # ----------------------------------------------------
        # NO CANDIDATES
        # ----------------------------------------------------

        if not candidates:
            return

        # ----------------------------------------------------
        # OI / FUNDING
        #
        # Только после появления кандидатов.
        # Это сильно снижает количество API запросов.
        # ----------------------------------------------------

        oi_data = get_oi_state(
            inst_id
        )

        time.sleep(
            REQUEST_DELAY
        )

        funding_data = funding_state(
            inst_id
        )

        # ----------------------------------------------------
        # SCORE
        # ----------------------------------------------------

        best = None

        for candidate in candidates:

            score = calculate_score(

                candidate["direction"],

                structure,

                trend15,

                trend5,

                candles5,

                candidate["level_data"],

                candidate["strategy"],

                oi_data,

                funding_data
            )

            if score < MIN_SCORE:
                continue

            # ------------------------------------------------
            # TRADE
            # ------------------------------------------------

            trade = build_trade(

                candidate["direction"],

                candidate["level"],

                candles5
            )

            if trade is None:
                continue

            # ------------------------------------------------
            # DISTANCE
            # ------------------------------------------------

            distance = (
                abs(
                    trade["price"]
                    - candidate["level"]
                )
                /
                trade["price"]
            )

            if (
                distance
                > MAX_ENTRY_DISTANCE
            ):
                continue

            # ------------------------------------------------
            # AVOID CHASING
            # ------------------------------------------------

            # Если текущая свеча огромная —
            # не догоняем импульс.
            current = candles5[-1]

            current_range = (
                candle_range(current)
            )

            current_atr = atr(
                candles5,
                14
            )

            if (
                current_atr > 0
                and
                current_range
                > current_atr * 2.8
            ):

                log.info(
                    (
                        "SKIP EXTENDED CANDLE | "
                        "%s"
                    ),
                    inst_id
                )

                continue

            candidate_result = {

                **candidate,

                "score": score,

                "trade": trade
            }

            if (
                best is None
                or
                score
                > best["score"]
            ):

                best = candidate_result

        if best is None:
            return

        # ----------------------------------------------------
        # FINAL OI FILTER
        # ----------------------------------------------------

        # Для очень сильных сигналов OI не обязателен.
        # Но при противоположном falling OI
        # не публикуем слабые сигналы.
        if (
            oi_data["state"] == "FALLING"
            and
            best["score"] < 90
        ):

            log.info(
                (
                    "SKIP FALLING OI | "
                    "%s | SCORE=%s"
                ),
                inst_id,
                best["score"]
            )

            return

        # ----------------------------------------------------
        # DEDUPLICATION
        # ----------------------------------------------------

        if not can_send_signal(

            inst_id,

            best["direction"],

            best["strategy"],

            best["level"]
        ):

            log.info(
                (
                    "SIGNAL BLOCKED "
                    "BY COOLDOWN | %s"
                ),
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

            trend15,

            trend5,

            oi_data,

            funding_data,

            best["level"],

            candles15
        )

    except Exception as exc:

        log.exception(
            "ANALYSIS ERROR | %s | %s",
            inst_id,
            exc
        )


# ============================================================
# CLEAN OLD HISTORY
# ============================================================

def cleanup_history():

    now = time.time()

    expired = []

    for key, timestamp in (
        signal_history.items()
    ):

        if (
            now - timestamp
            > SIGNAL_COOLDOWN * 2
        ):

            expired.append(key)

    for key in expired:

        signal_history.pop(
            key,
            None
        )


# ============================================================
# SCAN
# ============================================================

def perform_scan():

    global scan_number

    scan_number += 1

    started = time.time()

    log.info(
        "=========================================="
    )

    log.info(
        "SCAN #%s START",
        scan_number
    )

    markets = get_tickers()

    log.info(
        "LIQUID MARKETS: %s",
        len(markets)
    )

    for index, market in enumerate(
        markets,
        start=1
    ):

        log.info(
            (
                "ANALYSE %s/%s | %s | "
                "VOL=$%.1fM"
            ),
            index,
            len(markets),
            market["inst_id"],
            market["volume_usd"]
            / 1_000_000
        )

        analyse_market(
            market
        )

        time.sleep(
            REQUEST_DELAY
        )

    elapsed = (
        time.time()
        - started
    )

    log.info(
        (
            "SCAN #%s COMPLETE | "
            "%.1fs"
        ),
        scan_number,
        elapsed
    )

    cleanup_history()

    return elapsed


# ============================================================
# MAIN LOOP
# ============================================================

def bot_loop():

    log.info(
        "=========================================="
    )

    log.info(
        "QUANTUM SCALPER V6 STARTING"
    )

    log.info(
        "MIN VOLUME: $%s",
        f"{MIN_VOLUME_USD:,.0f}"
    )

    log.info(
        "MIN SCORE: %s",
        MIN_SCORE
    )

    log.info(
        "MAX MARKETS: %s",
        MAX_MARKETS
    )

    log.info(
        "SCAN INTERVAL: %ss",
        SCAN_INTERVAL
    )

    log.info(
        "MAX RISK: %.2f%%",
        MAX_RISK * 100
    )

    log.info(
        "TIMEZONE: Europe/Kyiv"
    )

    log.info(
        "=========================================="
    )

    send_morning_message()

    # Планируем начало следующего скана
    # по абсолютному времени.
    next_scan = time.monotonic()

    while True:

        try:

            send_morning_message()

            elapsed = perform_scan()

            # Следующий запуск ровно через
            # SCAN_INTERVAL после запланированного времени,
            # а не после окончания анализа.
            next_scan += SCAN_INTERVAL

            delay = (
                next_scan
                - time.monotonic()
            )

            # Если скан занял больше интервала,
            # запускаем следующий почти сразу.
            if delay < 0:

                next_scan = (
                    time.monotonic()
                    + 1
                )

                delay = 1

            log.info(
                "NEXT SCAN IN %.1fs",
                delay
            )

            time.sleep(
                delay
            )

        except Exception as exc:

            log.exception(
                "SCANNER ERROR: %s",
                exc
            )

            time.sleep(5)


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    if not TELEGRAM_TOKEN:

        raise RuntimeError(
            "TELEGRAM_TOKEN "
            "environment variable is missing"
        )

    if not CHANNEL_ID:

        raise RuntimeError(
            "CHANNEL_ID "
            "environment variable is missing"
        )

    threading.Thread(
        target=run_web_server,
        daemon=True
    ).start()

    bot_loop()
```
