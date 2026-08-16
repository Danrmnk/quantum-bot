import os
import time
import threading
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests
import telebot

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle


# ============================================================
# QUANTUM SCALPER V3
# FREE RENDER WEB SERVICE
#
# НЕ ТОРГУЕТ АВТОМАТИЧЕСКИ.
# Только анализирует рынок OKX и отправляет сигналы Telegram.
# ============================================================


# ============================================================
# ENV
# ============================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()

PORT = int(os.getenv("PORT", "10000"))

# Минимальный score.
MIN_SCORE = int(os.getenv("MIN_SCORE", "80"))

# Минимальный 24H оборот в USD.
MIN_VOLUME_USD = float(
    os.getenv("MIN_VOLUME_USD", "20000000")
)

# Сколько наиболее ликвидных монет проверять.
MAX_SYMBOLS = int(
    os.getenv("MAX_SYMBOLS", "15")
)

# Интервал между полными сканами.
SCAN_INTERVAL = int(
    os.getenv("SCAN_INTERVAL", "30")
)

# Повторный сигнал по той же монете.
COOLDOWN_MINUTES = int(
    os.getenv("COOLDOWN_MINUTES", "60")
)

# READY живёт максимум столько.
READY_MINUTES = int(
    os.getenv("READY_MINUTES", "12")
)

# Максимум новых сигналов за час.
MAX_SIGNALS_PER_HOUR = int(
    os.getenv("MAX_SIGNALS_PER_HOUR", "5")
)

# Не входить, если цена уже слишком далеко убежала.
MAX_CHASE_PERCENT = float(
    os.getenv("MAX_CHASE_PERCENT", "0.45")
)

# Morning greeting.
MORNING_ENABLED = (
    os.getenv(
        "MORNING_ENABLED",
        "true"
    ).lower() == "true"
)

MORNING_HOUR = int(
    os.getenv("MORNING_HOUR", "9")
)

MORNING_MINUTE = int(
    os.getenv("MORNING_MINUTE", "0")
)

KYIV = ZoneInfo("Europe/Kyiv")


# ============================================================
# VALIDATION
# ============================================================

if not TELEGRAM_TOKEN:
    raise RuntimeError(
        "TELEGRAM_TOKEN is missing"
    )

if not CHANNEL_ID:
    raise RuntimeError(
        "CHANNEL_ID is missing"
    )


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s | "
        "%(levelname)s | "
        "%(message)s"
    )
)

log = logging.getLogger("QUANTUM")


# ============================================================
# TELEGRAM
# ============================================================

bot = telebot.TeleBot(
    TELEGRAM_TOKEN
)


# ============================================================
# HTTP / OKX
# ============================================================

session = requests.Session()

session.headers.update({
    "User-Agent": "QuantumScalperV3/1.0",
    "Accept": "application/json",
})


OKX_URL = "https://www.okx.com"


# ============================================================
# STATE
# ============================================================

state_lock = threading.Lock()

cooldowns = {}

ready_signals = {}

signals_last_hour = []

oi_previous = {}

last_scan_timestamp = 0

scan_number = 0

signals_today = 0

last_morning_date = None


# ============================================================
# HELPERS
# ============================================================

def current_time():
    return time.time()


def kyiv_now():
    return datetime.now(KYIV)


def fmt_price(value):
    try:
        value = float(value)

        if value >= 10000:
            return f"{value:,.0f}".replace(",", " ")

        if value >= 1000:
            return f"{value:,.2f}".replace(",", " ")

        if value >= 100:
            return f"{value:.2f}"

        if value >= 1:
            return (
                f"{value:.4f}"
                .rstrip("0")
                .rstrip(".")
            )

        return (
            f"{value:.8f}"
            .rstrip("0")
            .rstrip(".")
        )

    except Exception:
        return str(value)


def pct_distance(a, b):
    if b == 0:
        return 999.0

    return abs(a - b) / b * 100.0


def score_label(score):
    if score >= 95:
        return "🚀 ELITE"

    if score >= 90:
        return "🔥 PREMIUM"

    if score >= 85:
        return "💎 STRONG"

    return "⚡ SIGNAL"


def volume_grade(volume):
    if volume >= 1_000_000_000:
        return "VERY HIGH"

    if volume >= 250_000_000:
        return "HIGH"

    if volume >= 100_000_000:
        return "GOOD"

    return "MEDIUM"


def clean_old_hour_signals():
    now = current_time()

    with state_lock:
        signals_last_hour[:] = [
            x
            for x in signals_last_hour
            if now - x < 3600
        ]


# ============================================================
# OKX REQUEST
# ============================================================

def okx_get(
    endpoint,
    params=None,
    retries=3
):
    url = OKX_URL + endpoint

    last_error = None

    for attempt in range(
        1,
        retries + 1
    ):
        try:

            response = session.get(
                url,
                params=params,
                timeout=8
            )

            response.raise_for_status()

            data = response.json()

            if data.get("code") != "0":
                raise RuntimeError(
                    data.get(
                        "msg",
                        "Unknown OKX error"
                    )
                )

            return data

        except Exception as error:

            last_error = error

            log.warning(
                "OKX request failed "
                "%s/%s: %s",
                attempt,
                retries,
                error
            )

            time.sleep(
                attempt * 1.5
            )

    raise RuntimeError(
        f"OKX request failed: {last_error}"
    )


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

    markets = []

    for item in data.get(
        "data",
        []
    ):

        inst_id = item.get(
            "instId",
            ""
        )

        if not inst_id.endswith(
            "-USDT-SWAP"
        ):
            continue

        try:

            price = float(
                item["last"]
            )

            # Для SWAP volCcy24h — объём
            # в базовой валюте.
            # Переводим приблизительно
            # в USD через текущую цену.
            vol_base = float(
                item.get(
                    "volCcy24h",
                    0
                ) or 0
            )

            volume_usd = (
                vol_base * price
            )

            high = float(
                item.get(
                    "high24h",
                    0
                ) or 0
            )

            low = float(
                item.get(
                    "low24h",
                    0
                ) or 0
            )

            if price <= 0:
                continue

            if volume_usd < MIN_VOLUME_USD:
                continue

            markets.append({
                "inst_id": inst_id,
                "price": price,
                "volume_usd": volume_usd,
                "high": high,
                "low": low,
            })

        except Exception:
            continue

    markets.sort(
        key=lambda x: x[
            "volume_usd"
        ],
        reverse=True
    )

    return markets[
        :MAX_SYMBOLS
    ]


# ============================================================
# CANDLES
# ============================================================

def get_candles(
    inst_id,
    timeframe,
    limit=100
):

    data = okx_get(
        "/api/v5/market/candles",
        {
            "instId": inst_id,
            "bar": timeframe,
            "limit": str(limit),
        }
    )

    candles = []

    for row in reversed(
        data.get(
            "data",
            []
        )
    ):

        try:

            candles.append({
                "ts": int(row[0]),

                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),

                "volume": float(row[5]),

                # Для OKX candle row[7]
                # является объёмом в quote currency.
                "quote_volume": float(
                    row[7]
                ),

                # row[8] = confirm
                "confirmed": (
                    str(row[8]) == "1"
                ),
            })

        except Exception:
            continue

    return candles


# ============================================================
# OPEN INTEREST
# ============================================================

def get_open_interest(
    inst_id
):

    data = okx_get(
        "/api/v5/public/open-interest",
        {
            "instType": "SWAP",
            "instId": inst_id,
        }
    )

    rows = data.get(
        "data",
        []
    )

    if not rows:
        return None

    try:

        # OI в контрактах.
        return float(
            rows[0]["oi"]
        )

    except Exception:

        return None


# ============================================================
# EMA
# ============================================================

def calculate_ema(
    values,
    period
):

    if not values:
        return []

    multiplier = 2.0 / (
        period + 1
    )

    result = [
        values[0]
    ]

    for value in values[1:]:

        result.append(
            (
                value
                * multiplier
            )
            +
            (
                result[-1]
                * (
                    1
                    - multiplier
                )
            )
        )

    return result


# ============================================================
# ATR
# ============================================================

def calculate_atr(
    candles,
    period=14
):

    if len(candles) < (
        period + 2
    ):
        return 0

    true_ranges = []

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
            ),
        )

        true_ranges.append(tr)

    if len(true_ranges) < period:
        return 0

    return (
        sum(
            true_ranges[-period:]
        )
        / period
    )


# ============================================================
# VOLUME RATIO
# ============================================================

def get_volume_ratio(
    candles
):

    if len(candles) < 22:
        return 0

    current = candles[-1][
        "quote_volume"
    ]

    previous = [
        x["quote_volume"]
        for x in candles[
            -21:-1
        ]
        if x["quote_volume"] > 0
    ]

    if not previous:
        return 0

    average = (
        sum(previous)
        / len(previous)
    )

    if average <= 0:
        return 0

    return current / average


# ============================================================
# PIVOTS
# ============================================================

def pivot_highs(
    candles
):

    levels = []

    for i in range(
        2,
        len(candles) - 2
    ):

        h = candles[i][
            "high"
        ]

        if (
            h >= candles[i - 1]["high"]
            and h >= candles[i - 2]["high"]
            and h >= candles[i + 1]["high"]
            and h >= candles[i + 2]["high"]
        ):
            levels.append(h)

    return levels


def pivot_lows(
    candles
):

    levels = []

    for i in range(
        2,
        len(candles) - 2
    ):

        low = candles[i][
            "low"
        ]

        if (
            low <= candles[i - 1]["low"]
            and low <= candles[i - 2]["low"]
            and low <= candles[i + 1]["low"]
            and low <= candles[i + 2]["low"]
        ):
            levels.append(low)

    return levels


# ============================================================
# 1H DIRECTION
# ============================================================

def get_1h_direction(
    candles
):

    confirmed = [
        c
        for c in candles
        if c["confirmed"]
    ]

    if len(confirmed) < 60:
        return None

    closes = [
        c["close"]
        for c in confirmed
    ]

    ema20 = calculate_ema(
        closes,
        20
    )[-1]

    ema50 = calculate_ema(
        closes,
        50
    )[-1]

    recent = confirmed[
        -12:
    ]

    first = recent[:6]
    second = recent[6:]

    first_high = max(
        x["high"]
        for x in first
    )

    second_high = max(
        x["high"]
        for x in second
    )

    first_low = min(
        x["low"]
        for x in first
    )

    second_low = min(
        x["low"]
        for x in second
    )

    bullish = (
        ema20 > ema50
        and second_high >= first_high
        and second_low >= first_low
    )

    bearish = (
        ema20 < ema50
        and second_high <= first_high
        and second_low <= first_low
    )

    if bullish:
        return "LONG"

    if bearish:
        return "SHORT"

    return None


# ============================================================
# LEVEL FINDER
# ============================================================

def find_key_level(
    candles_15m,
    candles_1h,
    price,
    direction
):

    candidates = []

    # --------------------------------------------------------
    # 1H major levels
    # --------------------------------------------------------

    if len(candles_1h) >= 30:

        historical = (
            candles_1h[-25:-1]
        )

        major_high = max(
            x["high"]
            for x in historical
        )

        major_low = min(
            x["low"]
            for x in historical
        )

        if direction == "LONG":

            if major_high > price:
                candidates.append(
                    (
                        major_high,
                        "1H"
                    )
                )

        else:

            if major_low < price:
                candidates.append(
                    (
                        major_low,
                        "1H"
                    )
                )

    # --------------------------------------------------------
    # 15M pivots
    # --------------------------------------------------------

    if direction == "LONG":

        for level in pivot_highs(
            candles_15m
        )[-20:]:

            if level > price:
                candidates.append(
                    (
                        level,
                        "15M"
                    )
                )

    else:

        for level in pivot_lows(
            candles_15m
        )[-20:]:

            if level < price:
                candidates.append(
                    (
                        level,
                        "15M"
                    )
                )

    if not candidates:
        return None, None

    if direction == "LONG":

        candidates.sort(
            key=lambda x: x[0]
        )

    else:

        candidates.sort(
            key=lambda x: x[0],
            reverse=True
        )

    return candidates[0]


# ============================================================
# COMPRESSION / TRENDLINE
# ============================================================

def check_compression(
    candles,
    direction
):

    if len(candles) < 30:
        return False, 0

    recent = candles[
        -20:
    ]

    ranges = [
        x["high"] - x["low"]
        for x in recent
    ]

    first_avg = (
        sum(ranges[:8])
        / 8
    )

    last_avg = (
        sum(ranges[-8:])
        / 8
    )

    if first_avg <= 0:
        return False, 0

    compression = (
        1
        - (
            last_avg
            / first_avg
        )
    )

    points = 0

    if compression >= 0.15:
        points += 10

    if compression >= 0.25:
        points += 5

    valid = False

    if direction == "LONG":

        lows = pivot_lows(
            recent
        )

        if len(lows) >= 2:

            if lows[-1] >= lows[-2]:

                valid = True
                points += 10

    else:

        highs = pivot_highs(
            recent
        )

        if len(highs) >= 2:

            if highs[-1] <= highs[-2]:

                valid = True
                points += 10

    return (
        valid,
        min(points, 25)
    )


# ============================================================
# OI CONFIRMATION
# ============================================================

def update_oi(
    inst_id,
    current_oi
):

    if current_oi is None:
        return False, 0.0

    with state_lock:

        previous = oi_previous.get(
            inst_id
        )

        oi_previous[
            inst_id
        ] = current_oi

    if previous is None:
        return False, 0.0

    if previous <= 0:
        return False, 0.0

    change = (
        (
            current_oi
            - previous
        )
        / previous
        * 100
    )

    # Для breakout хотим рост OI.
    confirmed = (
        change >= 0.20
    )

    return confirmed, change


# ============================================================
# ANALYZER
# ============================================================

def analyze_market(
    ticker,
    candles_1h,
    candles_15m,
    candles_5m,
    oi_confirmed,
    oi_change
):

    price = ticker[
        "price"
    ]

    # ========================================================
    # 1H = DIRECTION
    # ========================================================

    direction = get_1h_direction(
        candles_1h
    )

    if not direction:
        return None

    # ========================================================
    # LEVEL
    # ========================================================

    level, level_tf = (
        find_key_level(
            candles_15m,
            candles_1h,
            price,
            direction
        )
    )

    if level is None:
        return None

    distance = pct_distance(
        price,
        level
    )

    # Нам нужен рынок возле уровня.
    if distance > 0.65:
        return None

    # ========================================================
    # CONFIRMED 5M
    # ========================================================

    confirmed_5m = [
        x
        for x in candles_5m
        if x["confirmed"]
    ]

    if len(confirmed_5m) < 40:
        return None

    current = confirmed_5m[-1]
    previous = confirmed_5m[-2]

    score = 0

    strategy = None

    reason = ""

    # ========================================================
    # 1H STRUCTURE
    # ========================================================

    score += 15

    # ========================================================
    # LEVEL QUALITY
    # ========================================================

    if level_tf == "1H":
        score += 20
    else:
        score += 14

    # ========================================================
    # STRATEGY 1
    # HORIZONTAL LEVEL BREAKOUT
    # ========================================================

    if direction == "LONG":

        breakout = (
            previous["close"] <= level
            and current["close"] > level
        )

        near_level = (
            distance <= 0.40
        )

        if breakout:

            strategy = (
                "Horizontal Level Breakout"
            )

            score += 25

            reason = (
                "5M свеча закрылась выше "
                "ключевого сопротивления."
            )

        elif near_level:

            strategy = (
                "Horizontal Level Breakout"
            )

            score += 10

            reason = (
                "Цена сжимается возле "
                "ключевого сопротивления "
                "перед возможным пробоем."
            )

    else:

        breakout = (
            previous["close"] >= level
            and current["close"] < level
        )

        near_level = (
            distance <= 0.40
        )

        if breakout:

            strategy = (
                "Horizontal Level Breakout"
            )

            score += 25

            reason = (
                "5M свеча закрылась ниже "
                "ключевой поддержки."
            )

        elif near_level:

            strategy = (
                "Horizontal Level Breakout"
            )

            score += 10

            reason = (
                "Цена сжимается возле "
                "ключевой поддержки "
                "перед возможным пробоем."
            )

    # ========================================================
    # STRATEGY 2
    # TRENDLINE COMPRESSION
    # ========================================================

    compression, compression_points = (
        check_compression(
            candles_15m,
            direction
        )
    )

    if compression:

        strategy = (
            "Trendline Compression Breakout"
        )

        score += compression_points

        if direction == "LONG":

            reason = (
                "15M показывает сжатие "
                "с повышающимися минимумами "
                "перед сопротивлением."
            )

        else:

            reason = (
                "15M показывает сжатие "
                "с понижающимися максимумами "
                "перед поддержкой."
            )

    # ========================================================
    # STRATEGY 3
    # MOMENTUM BREAKOUT
    # ========================================================

    closes = [
        x["close"]
        for x in confirmed_5m
    ]

    ema9 = calculate_ema(
        closes,
        9
    )[-1]

    ema21 = calculate_ema(
        closes,
        21
    )[-1]

    previous_high = max(
        x["high"]
        for x in confirmed_5m[
            -13:-1
        ]
    )

    previous_low = min(
        x["low"]
        for x in confirmed_5m[
            -13:-1
        ]
    )

    momentum_long = (
        direction == "LONG"
        and ema9 > ema21
        and current["close"]
        > previous_high
    )

    momentum_short = (
        direction == "SHORT"
        and ema9 < ema21
        and current["close"]
        < previous_low
    )

    if (
        momentum_long
        or momentum_short
    ):

        strategy = (
            "Momentum Breakout"
        )

        score += 15

        reason = (
            "5M показывает импульсный "
            "выход из локального диапазона."
        )

    if strategy is None:
        return None

    # ========================================================
    # VOLUME
    # ========================================================

    vol_ratio = get_volume_ratio(
        confirmed_5m
    )

    if vol_ratio >= 1.20:
        score += 8

    if vol_ratio >= 1.50:
        score += 5

    if vol_ratio >= 2.00:
        score += 5

    # ========================================================
    # OI
    # ========================================================

    if oi_confirmed:

        score += 10

    # ========================================================
    # LIQUIDITY
    # ========================================================

    volume = ticker[
        "volume_usd"
    ]

    if volume >= 1_000_000_000:

        score += 5

    elif volume >= 250_000_000:

        score += 4

    elif volume >= 100_000_000:

        score += 2

    # ========================================================
    # ATR
    # ========================================================

    atr = calculate_atr(
        confirmed_5m
    )

    if atr <= 0:
        return None

    atr_percent = (
        atr
        / price
        * 100
    )

    # Слишком мёртвый рынок.
    if atr_percent < 0.03:
        return None

    # Слишком бешеный рынок.
    if atr_percent > 2.0:
        return None

    # ========================================================
    # SCORE
    # ========================================================

    score = max(
        0,
        min(
            100,
            int(score)
        )
    )

    if score < MIN_SCORE:
        return None

    # ========================================================
    # ENTRY ZONE
    # ========================================================

    zone_percent = min(
        max(
            atr_percent * 0.35,
            0.08
        ),
        0.22
    )

    entry_low = (
        level
        * (
            1
            - zone_percent / 100
        )
    )

    entry_high = (
        level
        * (
            1
            + zone_percent / 100
        )
    )

    # ========================================================
    # STRUCTURAL SL
    # ========================================================

    recent_15m = (
        candles_15m[-18:]
    )

    if direction == "LONG":

        structure_low = min(
            x["low"]
            for x in recent_15m
        )

        sl = (
            structure_low
            - atr * 0.25
        )

        if sl >= price:
            return None

        risk = price - sl

    else:

        structure_high = max(
            x["high"]
            for x in recent_15m
        )

        sl = (
            structure_high
            + atr * 0.25
        )

        if sl <= price:
            return None

        risk = sl - price

    risk_percent = (
        risk
        / price
        * 100
    )

    # Скальперский риск.
    if risk_percent < 0.15:
        return None

    if risk_percent > 1.80:
        return None

    # ========================================================
    # TAKE PROFITS
    # ========================================================

    if direction == "LONG":

        tp1 = price + risk * 1.0
        tp2 = price + risk * 2.0
        tp3 = price + risk * 3.0

    else:

        tp1 = price - risk * 1.0
        tp2 = price - risk * 2.0
        tp3 = price - risk * 3.0

    # ========================================================
    # CHASE FILTER
    # ========================================================

    if direction == "LONG":

        if price > (
            level
            * (
                1
                + MAX_CHASE_PERCENT / 100
            )
        ):
            return None

    else:

        if price < (
            level
            * (
                1
                - MAX_CHASE_PERCENT / 100
            )
        ):
            return None

    return {
        "inst_id": ticker[
            "inst_id"
        ],

        "coin": ticker[
            "inst_id"
        ].replace(
            "-USDT-SWAP",
            ""
        ),

        "direction": direction,

        "strategy": strategy,

        "price": price,

        "level": level,

        "level_tf": level_tf,

        "entry_low": entry_low,

        "entry_high": entry_high,

        "sl": sl,

        "tp1": tp1,

        "tp2": tp2,

        "tp3": tp3,

        "risk_percent": risk_percent,

        "score": score,

        "volume_usd": volume,

        "volume_ratio": vol_ratio,

        "liquidity": volume_grade(
            volume
        ),

        "oi_change": oi_change,

        "oi_confirmed": oi_confirmed,

        "atr_percent": atr_percent,

        "reason": reason,

        "candles": confirmed_5m[
            -70:
        ],
    }


# ============================================================
# CHART
# ============================================================

def create_chart(
    signal
):

    filename = (
        "/tmp/"
        f'{signal["coin"]}_'
        f'{int(time.time() * 1000)}.png'
    )

    candles = signal[
        "candles"
    ]

    fig, ax = plt.subplots(
        figsize=(12, 7),
        dpi=130
    )

    fig.patch.set_facecolor(
        "#080d19"
    )

    ax.set_facecolor(
        "#080d19"
    )

    candle_width = 0.62

    for i, candle in enumerate(
        candles
    ):

        bullish = (
            candle["close"]
            >= candle["open"]
        )

        color = (
            "#18c98b"
            if bullish
            else "#ef5350"
        )

        ax.plot(
            [i, i],
            [
                candle["low"],
                candle["high"]
            ],
            color=color,
            linewidth=1
        )

        body_low = min(
            candle["open"],
            candle["close"]
        )

        body_height = abs(
            candle["close"]
            - candle["open"]
        )

        if body_height <= 0:
            body_height = (
                candle["close"]
                * 0.00001
            )

        rect = Rectangle(
            (
                i
                - candle_width / 2,
                body_low
            ),
            candle_width,
            body_height,
            facecolor=color,
            edgecolor=color
        )

        ax.add_patch(rect)

    # LEVEL
    ax.axhline(
        signal["level"],
        color="#f6c945",
        linewidth=2,
        linestyle="--"
    )

    # ENTRY
    ax.axhspan(
        signal["entry_low"],
        signal["entry_high"],
        color="#00a8ff",
        alpha=0.13
    )

    # SL
    ax.axhline(
        signal["sl"],
        color="#ff453a",
        linewidth=1.8,
        linestyle="-."
    )

    # TP
    for tp in [
        signal["tp1"],
        signal["tp2"],
        signal["tp3"]
    ]:

        ax.axhline(
            tp,
            color="#ffd166",
            linewidth=1.2,
            alpha=0.85
        )

    last_x = (
        len(candles) - 1
    )

    ax.text(
        last_x,
        signal["level"],
        " LEVEL",
        color="#f6c945",
        fontsize=10,
        fontweight="bold"
    )

    ax.text(
        last_x,
        signal["sl"],
        " SL",
        color="#ff453a",
        fontsize=10,
        fontweight="bold"
    )

    ax.text(
        last_x,
        signal["tp1"],
        " TP1",
        color="#ffd166",
        fontsize=9
    )

    ax.text(
        last_x,
        signal["tp2"],
        " TP2",
        color="#ffd166",
        fontsize=9
    )

    ax.text(
        last_x,
        signal["tp3"],
        " TP3",
        color="#ffd166",
        fontsize=9
    )

    title = (
        f'{signal["coin"]}USDT  '
        f'{signal["direction"]}\n'
        f'{signal["strategy"]}  |  '
        f'SCORE {signal["score"]}/100'
    )

    ax.set_title(
        title,
        color="white",
        fontsize=14,
        fontweight="bold"
    )

    ax.grid(
        color="white",
        alpha=0.08
    )

    ax.tick_params(
        colors="#9ca3af"
    )

    for spine in ax.spines.values():
        spine.set_color(
            "#263247"
        )

    plt.tight_layout()

    fig.savefig(
        filename,
        facecolor=fig.get_facecolor(),
        bbox_inches="tight"
    )

    plt.close(fig)

    return filename


# ============================================================
# TELEGRAM TEXT
# ============================================================

def signal_caption(
    signal
):

    return (
        f'🔥 *{signal["coin"]}USDT — '
        f'{signal["direction"]}*\n'
        f'{score_label(signal["score"])}\n'
        f'{signal["strategy"]}'
    )


def signal_text(
    signal
):

    oi_text = (
        f'CONFIRMED '
        f'(+{signal["oi_change"]:.2f}%)'
        if signal["oi_confirmed"]
        else
        f'FLAT '
        f'({signal["oi_change"]:+.2f}%)'
    )

    return (
        f'🔥 *{signal["coin"]}USDT — '
        f'{signal["direction"]}*\n\n'

        f'💰 *Цена:* '
        f'`{fmt_price(signal["price"])}`\n'

        f'📊 *Объём 24H:* '
        f'`${signal["volume_usd"] / 1_000_000:.1f}M`\n'

        f'📈 *Объём 5M:* '
        f'`{signal["volume_ratio"]:.2f}x`\n\n'

        f'🧠 *ЛОГИКА СДЕЛКИ*\n'
        f'{signal["reason"]}\n\n'

        f'🎯 *ТОЧКА ВХОДА*\n'
        f'`{fmt_price(signal["entry_low"])}` – '
        f'`{fmt_price(signal["entry_high"])}`\n\n'

        f'🛑 *STOP LOSS*\n'
        f'`{fmt_price(signal["sl"])}`\n'
        f'Риск: `−{signal["risk_percent"]:.2f}%`\n\n'

        f'🪜 *ЗАКРЫТИЕ ЛЕСЕНКОЙ*\n'
        f'TP1 — 30% → '
        f'`{fmt_price(signal["tp1"])}`\n'

        f'TP2 — 30% → '
        f'`{fmt_price(signal["tp2"])}`\n'

        f'TP3 — 40% → '
        f'`{fmt_price(signal["tp3"])}`\n\n'

        f'🔒 После TP1 → SL в BE\n\n'

        f'📊 *Стратегия:*\n'
        f'`{signal["strategy"]}`\n\n'

        f'📍 *Основной уровень:*\n'
        f'`{signal["level_tf"]}` — '
        f'`{fmt_price(signal["level"])}`\n\n'

        f'💧 *Ликвидность:* '
        f'`{signal["liquidity"]}`\n'

        f'⚡ *OI:* `{oi_text}`\n\n'

        f'⭐ *SIGNAL SCORE:* '
        f'`{signal["score"]}/100` '
        f'{score_label(signal["score"])}\n\n'

        f'🟡 *READY*\n'
        f'Сетап активен до '
        f'`{READY_MINUTES} мин`.\n\n'

        f'⚠️ Не догоняем рынок. '
        f'Не увеличиваем риск после убытка.\n\n'

        f'*Качество важнее количества.*'
    )


# ============================================================
# SEND SIGNAL
# ============================================================

def send_signal(
    signal
):

    chart_file = None

    try:

        chart_file = create_chart(
            signal
        )

        with open(
            chart_file,
            "rb"
        ) as image:

            bot.send_photo(
                CHANNEL_ID,
                image,
                caption=signal_caption(
                    signal
                ),
                parse_mode="Markdown"
            )

        message = bot.send_message(
            CHANNEL_ID,
            signal_text(
                signal
            ),
            parse_mode="Markdown"
        )

        log.info(
            "TELEGRAM SIGNAL SENT | "
            "%s | %s | score=%s",
            signal["coin"],
            signal["direction"],
            signal["score"]
        )

        return message.message_id

    except Exception as error:

        log.exception(
            "TELEGRAM SIGNAL ERROR: %s",
            error
        )

        return None

    finally:

        if chart_file:

            try:
                os.remove(
                    chart_file
                )
            except Exception:
                pass


# ============================================================
# READY / ACTIVE
# ============================================================

def can_create_signal(
    inst_id
):

    clean_old_hour_signals()

    now = current_time()

    with state_lock:

        if inst_id in ready_signals:
            return False

        last = cooldowns.get(
            inst_id,
            0
        )

        if (
            now - last
            < COOLDOWN_MINUTES * 60
        ):
            return False

        if len(
            signals_last_hour
        ) >= MAX_SIGNALS_PER_HOUR:
            return False

    return True


def check_ready_signal(
    inst_id,
    current_price
):

    with state_lock:

        signal = ready_signals.get(
            inst_id
        )

    if not signal:
        return

    age = (
        current_time()
        - signal["created"]
    )

    # ========================================================
    # EXPIRE
    # ========================================================

    if age > (
        READY_MINUTES * 60
    ):

        with state_lock:
            ready_signals.pop(
                inst_id,
                None
            )

        log.info(
            "READY EXPIRED | %s",
            inst_id
        )

        return

    # ========================================================
    # CHASE PROTECTION
    # ========================================================

    level = signal[
        "level"
    ]

    distance = pct_distance(
        current_price,
        level
    )

    if distance > (
        MAX_CHASE_PERCENT
    ):
        return

    # ========================================================
    # BREAKOUT
    # ========================================================

    direction = signal[
        "direction"
    ]

    triggered = False

    if direction == "LONG":

        if current_price >= level:
            triggered = True

    else:

        if current_price <= level:
            triggered = True

    if not triggered:
        return

    try:

        bot.send_message(
            CHANNEL_ID,
            (
                f'🟢 *ENTRY ACTIVE — '
                f'{signal["coin"]}USDT '
                f'{direction}*\n\n'

                f'💥 Уровень '
                f'`{fmt_price(level)}` '
                f'подтверждён.\n\n'

                f'🎯 Entry: '
                f'`{fmt_price(signal["entry_low"])}` – '
                f'`{fmt_price(signal["entry_high"])}`\n\n'

                f'🛑 SL: '
                f'`{fmt_price(signal["sl"])}`\n\n'

                f'🎯 TP1: '
                f'`{fmt_price(signal["tp1"])}`\n'

                f'🎯 TP2: '
                f'`{fmt_price(signal["tp2"])}`\n'

                f'🏆 TP3: '
                f'`{fmt_price(signal["tp3"])}`\n\n'

                f'🔒 После TP1 → SL в BE\n\n'

                f'⭐ Score: '
                f'`{signal["score"]}/100`'
            ),
            parse_mode="Markdown"
        )

        log.info(
            "ENTRY ACTIVE | %s | %s",
            signal["coin"],
            direction
        )

    except Exception:

        log.exception(
            "ACTIVE TELEGRAM ERROR"
        )

    with state_lock:

        ready_signals.pop(
            inst_id,
            None
        )

        cooldowns[
            inst_id
        ] = current_time()


# ============================================================
# MORNING MESSAGE
# ============================================================

def send_morning_message():

    global last_morning_date

    if not MORNING_ENABLED:
        return

    now = kyiv_now()

    today = now.date()

    if (
        now.hour != MORNING_HOUR
        or now.minute != MORNING_MINUTE
    ):
        return

    if last_morning_date == today:
        return

    text = (
        "🌅 *ДОБРОЕ УТРО, РЕБЯТА!*\n\n"

        "Начинаем новый торговый день. 🚀\n\n"

        "Сегодня работаем спокойно "
        "и строго по системе:\n\n"

        "🎯 Ждём только качественные сетапы.\n"
        "🚫 Не догоняем рынок.\n"
        "🛑 Соблюдаем риск-менеджмент.\n"
        "💰 Не перегружаем депозит.\n"
        "⏳ Если хорошего входа нет — ждём.\n\n"

        "У нас нет задачи брать каждое "
        "движение рынка.\n\n"

        "*Качество важнее количества.*\n\n"

        "Всем продуктивного торгового дня! 🔥"
    )

    try:

        bot.send_message(
            CHANNEL_ID,
            text,
            parse_mode="Markdown"
        )

        last_morning_date = today

        log.info(
            "MORNING MESSAGE SENT"
        )

    except Exception:

        log.exception(
            "MORNING MESSAGE ERROR"
        )


# ============================================================
# SCANNER
# ============================================================

def scanner_loop():

    global last_scan_timestamp
    global scan_number
    global signals_today

    log.info(
        "SCANNER STARTED"
    )

    while True:

        try:

            scan_number += 1

            last_scan_timestamp = (
                current_time()
            )

            log.info(
                "=============================="
            )

            log.info(
                "SCAN #%s",
                scan_number
            )

            # ------------------------------------------------
            # TICKERS
            # ------------------------------------------------

            tickers = get_tickers()

            log.info(
                "Liquid markets: %s",
                len(tickers)
            )

            for ticker in tickers:

                inst_id = ticker[
                    "inst_id"
                ]

                try:

                    # ----------------------------------------
                    # Existing READY
                    # ----------------------------------------

                    check_ready_signal(
                        inst_id,
                        ticker["price"]
                    )

                    if not can_create_signal(
                        inst_id
                    ):
                        continue

                    # ----------------------------------------
                    # 1H
                    # ----------------------------------------

                    candles_1h = (
                        get_candles(
                            inst_id,
                            "1H",
                            100
                        )
                    )

                    if len(
                        candles_1h
                    ) < 60:
                        continue

                    # ----------------------------------------
                    # 15M
                    # ----------------------------------------

                    candles_15m = (
                        get_candles(
                            inst_id,
                            "15m",
                            100
                        )
                    )

                    if len(
                        candles_15m
                    ) < 50:
                        continue

                    # ----------------------------------------
                    # 5M
                    # ----------------------------------------

                    candles_5m = (
                        get_candles(
                            inst_id,
                            "5m",
                            100
                        )
                    )

                    if len(
                        candles_5m
                    ) < 50:
                        continue

                    # ----------------------------------------
                    # OI
                    # ----------------------------------------

                    current_oi = (
                        get_open_interest(
                            inst_id
                        )
                    )

                    oi_confirmed, oi_change = (
                        update_oi(
                            inst_id,
                            current_oi
                        )
                    )

                    # ----------------------------------------
                    # ANALYZE
                    # ----------------------------------------

                    signal = (
                        analyze_market(
                            ticker,
                            candles_1h,
                            candles_15m,
                            candles_5m,
                            oi_confirmed,
                            oi_change
                        )
                    )

                    if signal is None:
                        continue

                    log.info(
                        "CANDIDATE | %s | %s | "
                        "%s | SCORE=%s | "
                        "OI=%+.2f%%",
                        signal["coin"],
                        signal["direction"],
                        signal["strategy"],
                        signal["score"],
                        signal["oi_change"]
                    )

                    # ----------------------------------------
                    # TELEGRAM
                    # ----------------------------------------

                    message_id = (
                        send_signal(
                            signal
                        )
                    )

                    if message_id is None:

                        log.error(
                            "SIGNAL NOT REGISTERED "
                            "BECAUSE TELEGRAM FAILED"
                        )

                        continue

                    # ----------------------------------------
                    # SAVE READY
                    # ----------------------------------------

                    with state_lock:

                        ready_signals[
                            inst_id
                        ] = {
                            **signal,
                            "created": (
                                current_time()
                            ),
                            "message_id": (
                                message_id
                            )
                        }

                        cooldowns[
                            inst_id
                        ] = current_time()

                        signals_last_hour.append(
                            current_time()
                        )

                        signals_today += 1

                    log.info(
                        "READY CREATED | %s",
                        signal["coin"]
                    )

                    # Не бомбим API.
                    time.sleep(
                        0.15
                    )

                except Exception as error:

                    # Одна монета НИКОГДА
                    # не должна убивать scanner.
                    log.exception(
                        "SYMBOL ERROR | %s | %s",
                        inst_id,
                        error
                    )

                    continue

            log.info(
                "SCAN #%s COMPLETE",
                scan_number
            )

        except Exception as error:

            log.exception(
                "SCANNER CRITICAL ERROR | %s",
                error
            )

            time.sleep(10)

        time.sleep(
            SCAN_INTERVAL
        )


# ============================================================
# WATCHDOG
# ============================================================

def watchdog_loop():

    while True:

        try:

            if last_scan_timestamp:

                age = (
                    current_time()
                    - last_scan_timestamp
                )

                if age > 180:

                    log.warning(
                        "WATCHDOG: scanner "
                        "has not started scan "
                        "for %s seconds",
                        int(age)
                    )

        except Exception:

            log.exception(
                "WATCHDOG ERROR"
            )

        time.sleep(30)


# ============================================================
# WEB SERVER
# ============================================================

class HealthHandler(
    BaseHTTPRequestHandler
):

    def log_message(
        self,
        format,
        *args
    ):
        # Не засоряем Render Logs.
        return

    def do_GET(self):

        if self.path == "/":

            self.send_response(
                200
            )

            self.send_header(
                "Content-Type",
                "text/plain; charset=utf-8"
            )

            self.end_headers()

            self.wfile.write(
                b"QUANTUM SCALPER V3 ONLINE"
            )

            return

        if self.path == "/health":

            if last_scan_timestamp:

                scanner_age = int(
                    current_time()
                    - last_scan_timestamp
                )

            else:

                scanner_age = -1

            with state_lock:

                ready_count = len(
                    ready_signals
                )

            body = (
                "{"
                '"status":"ok",'
                f'"scan_number":{scan_number},'
                f'"scanner_age":{scanner_age},'
                f'"ready":{ready_count}'
                "}"
            )

            self.send_response(
                200
            )

            self.send_header(
                "Content-Type",
                "application/json"
            )

            self.end_headers()

            self.wfile.write(
                body.encode()
            )

            return

        self.send_response(
            404
        )

        self.end_headers()


def run_web_server():

    server = ThreadingHTTPServer(
        (
            "0.0.0.0",
            PORT
        ),
        HealthHandler
    )

    log.info(
        "WEB SERVER: 0.0.0.0:%s",
        PORT
    )

    server.serve_forever()


# ============================================================
# STARTUP MESSAGE
# ============================================================

def send_startup_message():

    try:

        bot.send_message(
            CHANNEL_ID,
            (
                "🚀 *QUANTUM SCALPER V3 ONLINE*\n\n"

                "🟢 OKX Market Data\n"
                "🟢 Open Interest\n"
                "🟢 1H Structure\n"
                "🟢 15M Setup\n"
                "🟢 5M Confirmation\n"
                "🟢 Telegram\n"
                "🟢 Render Web Service\n\n"

                "🧠 *3 стратегии:*\n"
                "1️⃣ Horizontal Level Breakout\n"
                "2️⃣ Trendline Compression Breakout\n"
                "3️⃣ Momentum Breakout\n\n"

                "⭐ Минимальный Score: "
                f"`{MIN_SCORE}/100`\n\n"

                "🟡 READY → 🟢 ACTIVE\n\n"

                "*Качество важнее количества.*"
            ),
            parse_mode="Markdown"
        )

        log.info(
            "STARTUP MESSAGE SENT"
        )

    except Exception:

        log.exception(
            "STARTUP TELEGRAM ERROR"
        )


# ============================================================
# MAIN
# ============================================================

def main():

    log.info(
        "======================================"
    )

    log.info(
        "QUANTUM SCALPER V3 STARTING"
    )

    log.info(
        "MIN SCORE: %s",
        MIN_SCORE
    )

    log.info(
        "MIN VOLUME: $%s",
        f"{MIN_VOLUME_USD:,.0f}"
    )

    log.info(
        "MAX SYMBOLS: %s",
        MAX_SYMBOLS
    )

    log.info(
        "SCAN INTERVAL: %ss",
        SCAN_INTERVAL
    )

    log.info(
        "TIMEZONE: Europe/Kyiv"
    )

    log.info(
        "======================================"
    )

    # --------------------------------------------------------
    # WEB
    # --------------------------------------------------------

    threading.Thread(
        target=run_web_server,
        daemon=True
    ).start()

    # --------------------------------------------------------
    # SCANNER
    # --------------------------------------------------------

    threading.Thread(
        target=scanner_loop,
        daemon=True
    ).start()

    # --------------------------------------------------------
    # WATCHDOG
    # --------------------------------------------------------

    threading.Thread(
        target=watchdog_loop,
        daemon=True
    ).start()

    # --------------------------------------------------------
    # TELEGRAM
    # --------------------------------------------------------

    send_startup_message()

    # --------------------------------------------------------
    # MAIN
    # --------------------------------------------------------

    while True:

        try:

            send_morning_message()

        except Exception:

            log.exception(
                "MAIN LOOP ERROR"
            )

        time.sleep(20)


# ============================================================

if __name__ == "__main__":
    main()
