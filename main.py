import os
import time
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional, List, Dict, Tuple

import requests
import telebot

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle


# ============================================================
# QUANTUM SCALPER V3
#
# OKX PUBLIC MARKET DATA
# -> LIQUIDITY FILTER
# -> 1H STRUCTURE
# -> 15M LEVEL / COMPRESSION
# -> 5M TRIGGER
# -> TELEGRAM READY
# -> ACTIVE
#
# НЕ ТОРГУЕТ.
# Только анализирует рынок и публикует сигналы.
# ============================================================


# ============================================================
# CONFIG
# ============================================================

TELEGRAM_TOKEN = os.getenv(
    "TELEGRAM_TOKEN",
    os.getenv("BOT_TOKEN", "")
).strip()

CHANNEL_ID = os.getenv(
    "CHANNEL_ID",
    ""
).strip()

OKX_BASE_URL = os.getenv(
    "OKX_BASE_URL",
    "https://www.okx.com"
).rstrip("/")

TIMEZONE = os.getenv(
    "BOT_TIMEZONE",
    "Europe/Kyiv"
)

# ------------------------------------------------------------
# LIQUIDITY
# ------------------------------------------------------------

# Ищем только пары с >= $60M оборота за 24 часа.
MIN_24H_VOLUME_USD = float(
    os.getenv(
        "MIN_24H_VOLUME_USD",
        "60000000"
    )
)

# Максимум самых ликвидных монет для глубокого анализа.
MAX_SYMBOLS = int(
    os.getenv(
        "MAX_SYMBOLS",
        "35"
    )
)

# ------------------------------------------------------------
# SIGNAL
# ------------------------------------------------------------

MIN_SCORE = int(
    os.getenv(
        "MIN_SCORE",
        "80"
    )
)

COOLDOWN_MINUTES = int(
    os.getenv(
        "COOLDOWN_MINUTES",
        "60"
    )
)

READY_TTL_MINUTES = int(
    os.getenv(
        "READY_TTL_MINUTES",
        "12"
    )
)

MAX_CHASE_PCT = float(
    os.getenv(
        "MAX_CHASE_PCT",
        "0.45"
    )
)

MAX_SIGNALS_PER_HOUR = int(
    os.getenv(
        "MAX_SIGNALS_PER_HOUR",
        "6"
    )
)

SCAN_INTERVAL_SECONDS = int(
    os.getenv(
        "SCAN_INTERVAL_SECONDS",
        "25"
    )
)

HTTP_TIMEOUT = int(
    os.getenv(
        "HTTP_TIMEOUT",
        "10"
    )
)

# ------------------------------------------------------------
# MORNING
# ------------------------------------------------------------

MORNING_HOUR = int(
    os.getenv(
        "MORNING_HOUR",
        "9"
    )
)

MORNING_MINUTE = int(
    os.getenv(
        "MORNING_MINUTE",
        "0"
    )
)

MORNING_ENABLED = os.getenv(
    "MORNING_ENABLED",
    "true"
).lower() == "true"

# ------------------------------------------------------------
# DATABASE
# ------------------------------------------------------------

DB_PATH = os.getenv(
    "DB_PATH",
    "quantum_state.db"
)


# ============================================================
# ENV VALIDATION
# ============================================================

if not TELEGRAM_TOKEN:
    raise RuntimeError(
        "TELEGRAM_TOKEN is missing."
    )

if not CHANNEL_ID:
    raise RuntimeError(
        "CHANNEL_ID is missing."
    )


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

log = logging.getLogger(
    "QUANTUM"
)


# ============================================================
# TELEGRAM
# ============================================================

bot = telebot.TeleBot(
    TELEGRAM_TOKEN,
    parse_mode="Markdown"
)


# ============================================================
# HTTP
# ============================================================

session = requests.Session()

session.headers.update({
    "User-Agent": "QuantumScalper/3.0",
    "Accept": "application/json",
})


# ============================================================
# DATABASE
# ============================================================

db = sqlite3.connect(
    DB_PATH,
    check_same_thread=False
)

db.execute("""
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    inst_id TEXT NOT NULL,
    direction TEXT NOT NULL,
    strategy TEXT NOT NULL,
    level REAL NOT NULL,
    entry_low REAL NOT NULL,
    entry_high REAL NOT NULL,
    sl REAL NOT NULL,
    tp1 REAL NOT NULL,
    tp2 REAL NOT NULL,
    tp3 REAL NOT NULL,
    score INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    activated_at REAL,
    expires_at REAL NOT NULL
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS bot_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
""")

db.commit()


# ============================================================
# DATA CLASSES
# ============================================================

@dataclass
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float
    confirmed: bool


@dataclass
class Setup:
    inst_id: str
    coin: str
    direction: str
    strategy: str

    level: float
    current_price: float

    entry_low: float
    entry_high: float

    sl: float

    tp1: float
    tp2: float
    tp3: float

    score: int

    liquidity: str
    volume_grade: str
    oi_status: str

    level_tf: str

    reason: str

    volume_24h: float
    breakout_volume_ratio: float

    atr_pct: float

    candles_5m: List[Candle]


@dataclass
class ActiveReady:
    setup: Setup
    created_at: float
    expires_at: float

    telegram_message_id: Optional[int] = None
    photo_message_id: Optional[int] = None


# ============================================================
# MEMORY STATE
# ============================================================

ready_setups: Dict[
    str,
    ActiveReady
] = {}

signals_hour: List[float] = []

last_scan_ts = 0.0
scan_count = 0
signals_today = 0

last_morning_date = None


# ============================================================
# HELPERS
# ============================================================

def now_ts() -> float:
    return time.time()


def local_now() -> datetime:
    return datetime.now(
        ZoneInfo(TIMEZONE)
    )


def fmt_price(price: float) -> str:

    if price >= 1000:
        return f"{price:,.2f}".replace(
            ",",
            " "
        )

    if price >= 100:
        return f"{price:,.2f}".replace(
            ",",
            " "
        )

    if price >= 1:
        return f"{price:,.4f}".replace(
            ",",
            " "
        )

    if price >= 0.01:
        return f"{price:.6f}".rstrip(
            "0"
        ).rstrip(".")

    return f"{price:.10f}".rstrip(
        "0"
    ).rstrip(".")


def clamp(
    value: float,
    low: float,
    high: float
) -> float:

    return max(
        low,
        min(
            high,
            value
        )
    )


def pct(
    a: float,
    b: float
) -> float:

    if b == 0:
        return 0.0

    return (
        (a - b)
        / b
        * 100.0
    )


def get_coin(
    inst_id: str
) -> str:

    return inst_id.replace(
        "-USDT-SWAP",
        ""
    )


def grade_volume(
    ratio: float
) -> str:

    if ratio >= 2.0:
        return "VERY HIGH"

    if ratio >= 1.5:
        return "HIGH"

    if ratio >= 1.25:
        return "GOOD"

    return "NORMAL"


def grade_liquidity(
    volume: float
) -> str:

    if volume >= 1_000_000_000:
        return "HIGH"

    if volume >= 250_000_000:
        return "GOOD"

    if volume >= 60_000_000:
        return "MEDIUM"

    return "LOW"


def score_label(
    score: int
) -> str:

    if score >= 95:
        return "🚀 ELITE"

    if score >= 90:
        return "🔥 PREMIUM"

    if score >= 85:
        return "💎 STRONG"

    return "⚡ SIGNAL"


# ============================================================
# OKX REQUEST
# ============================================================

def okx_get(
    path: str,
    params: dict,
    retries: int = 3
) -> dict:

    url = (
        OKX_BASE_URL
        + path
    )

    last_error = None

    for attempt in range(
        1,
        retries + 1
    ):

        try:

            response = session.get(
                url,
                params=params,
                timeout=HTTP_TIMEOUT
            )

            response.raise_for_status()

            payload = response.json()

            if not isinstance(
                payload,
                dict
            ):
                raise RuntimeError(
                    "OKX returned invalid JSON object."
                )

            code = str(
                payload.get(
                    "code",
                    ""
                )
            )

            if code != "0":

                raise RuntimeError(
                    "OKX code=%s msg=%s"
                    % (
                        code,
                        payload.get(
                            "msg",
                            ""
                        )
                    )
                )

            return payload

        except Exception as exc:

            last_error = exc

            log.warning(
                "OKX REQUEST FAILED | "
                "attempt=%s/%s | path=%s | error=%s",
                attempt,
                retries,
                path,
                exc
            )

            if attempt < retries:
                time.sleep(
                    min(
                        attempt * 2,
                        6
                    )
                )

    raise RuntimeError(
        "OKX request failed after "
        f"{retries} attempts: "
        f"{last_error}"
    )


# ============================================================
# INSTRUMENTS
# ============================================================

def get_instruments() -> List[str]:

    payload = okx_get(
        "/api/v5/public/instruments",
        {
            "instType": "SWAP"
        }
    )

    result = []

    for item in payload.get(
        "data",
        []
    ):

        inst_id = str(
            item.get(
                "instId",
                ""
            )
        )

        state = str(
            item.get(
                "state",
                ""
            )
        )

        if not inst_id.endswith(
            "-USDT-SWAP"
        ):
            continue

        if state != "live":
            continue

        result.append(
            inst_id
        )

    return result


# ============================================================
# TICKERS
# ============================================================

def get_tickers() -> Dict[str, dict]:
    """
    OKX SWAP ticker.

    ВАЖНО:
    Для SWAP используем volCcy24h.
    Это объём базовой валюты.

    Для получения приблизительного USDT оборота:
        volCcy24h * last

    Старый ошибочный volCcyQuote24h здесь НЕ используется.
    """

    payload = okx_get(
        "/api/v5/market/tickers",
        {
            "instType": "SWAP"
        }
    )

    result = {}

    for item in payload.get(
        "data",
        []
    ):

        inst_id = str(
            item.get(
                "instId",
                ""
            )
        )

        if not inst_id.endswith(
            "-USDT-SWAP"
        ):
            continue

        try:

            last = float(
                item.get(
                    "last",
                    0
                ) or 0
            )

            if last <= 0:
                continue

            vol_ccy_24h = float(
                item.get(
                    "volCcy24h",
                    0
                ) or 0
            )

            # Для USDT-SWAP:
            # базовый объём * цена ≈ USDT оборот.
            volume_usd = (
                vol_ccy_24h
                * last
            )

            high24h = float(
                item.get(
                    "high24h",
                    0
                ) or 0
            )

            low24h = float(
                item.get(
                    "low24h",
                    0
                ) or 0
            )

            ts = int(
                item.get(
                    "ts",
                    0
                ) or 0
            )

            result[inst_id] = {
                "last": last,
                "high24h": high24h,
                "low24h": low24h,
                "vol_ccy_24h": vol_ccy_24h,
                "vol24h_usd": volume_usd,
                "ts": ts,
            }

        except (
            TypeError,
            ValueError
        ):
            continue

    return result


# ============================================================
# CANDLES
# ============================================================

def get_candles(
    inst_id: str,
    bar: str,
    limit: int = 120
) -> List[Candle]:

    payload = okx_get(
        "/api/v5/market/candles",
        {
            "instId": inst_id,
            "bar": bar,
            "limit": str(
                min(
                    limit,
                    300
                )
            )
        }
    )

    candles = []

    for row in reversed(
        payload.get(
            "data",
            []
        )
    ):

        try:

            candles.append(
                Candle(
                    ts=int(
                        row[0]
                    ),
                    open=float(
                        row[1]
                    ),
                    high=float(
                        row[2]
                    ),
                    low=float(
                        row[3]
                    ),
                    close=float(
                        row[4]
                    ),
                    volume=float(
                        row[5] or 0
                    ),
                    quote_volume=float(
                        row[7] or 0
                    ),
                    confirmed=(
                        str(
                            row[8]
                        ) == "1"
                    )
                )
            )

        except (
            IndexError,
            TypeError,
            ValueError
        ):
            continue

    return candles


# ============================================================
# OPEN INTEREST
# ============================================================

def get_open_interest(
    inst_id: str
) -> Optional[float]:

    try:

        payload = okx_get(
            "/api/v5/public/open-interest",
            {
                "instType": "SWAP",
                "instId": inst_id
            }
        )

        data = payload.get(
            "data",
            []
        )

        if not data:
            return None

        item = data[0]

        oi_usd = item.get(
            "oiUsd"
        )

        if oi_usd not in (
            None,
            ""
        ):

            return float(
                oi_usd
            )

        oi = item.get(
            "oi"
        )

        if oi not in (
            None,
            ""
        ):

            return float(
                oi
            )

    except Exception as exc:

        log.warning(
            "OI FAILED | %s | %s",
            inst_id,
            exc
        )

    return None


# ============================================================
# INDICATORS
# ============================================================

def ema(
    values: List[float],
    period: int
) -> List[float]:

    if not values:
        return []

    if period <= 0:
        return []

    k = 2.0 / (
        period + 1.0
    )

    result = [
        values[0]
    ]

    for value in values[1:]:

        result.append(
            value * k
            + result[-1] * (
                1.0 - k
            )
        )

    return result


def atr(
    candles: List[Candle],
    period: int = 14
) -> float:

    if len(candles) < (
        period + 1
    ):
        return 0.0

    trs = []

    for i in range(
        1,
        len(candles)
    ):

        current = candles[i]
        previous = candles[i - 1]

        tr = max(
            current.high
            - current.low,

            abs(
                current.high
                - previous.close
            ),

            abs(
                current.low
                - previous.close
            )
        )

        trs.append(
            tr
        )

    if len(trs) < period:
        return 0.0

    return (
        sum(
            trs[-period:]
        )
        / period
    )


def volume_ratio(
    candles: List[Candle],
    lookback: int = 20
) -> float:

    if len(candles) < (
        lookback + 1
    ):
        return 0.0

    current = (
        candles[-1]
        .quote_volume
    )

    history = [
        candle.quote_volume
        for candle in candles[
            -lookback - 1:-1
        ]
        if candle.quote_volume > 0
    ]

    if not history:
        return 0.0

    average = (
        sum(history)
        / len(history)
    )

    if average <= 0:
        return 0.0

    return (
        current
        / average
    )


# ============================================================
# MARKET STRUCTURE
# ============================================================

def structure_1h(
    candles: List[Candle]
) -> str:

    if len(candles) < 60:
        return "NEUTRAL"

    closes = [
        c.close
        for c in candles
    ]

    ema20 = ema(
        closes,
        20
    )[-1]

    ema50 = ema(
        closes,
        50
    )[-1]

    recent = candles[-12:]

    first_half = recent[:6]
    second_half = recent[6:]

    first_high = max(
        c.high
        for c in first_half
    )

    second_high = max(
        c.high
        for c in second_half
    )

    first_low = min(
        c.low
        for c in first_half
    )

    second_low = min(
        c.low
        for c in second_half
    )

    if (
        ema20 > ema50
        and second_high >= first_high
        and second_low >= first_low
    ):
        return "LONG"

    if (
        ema20 < ema50
        and second_high <= first_high
        and second_low <= first_low
    ):
        return "SHORT"

    return "NEUTRAL"


# ============================================================
# PIVOTS
# ============================================================

def pivot_highs(
    candles: List[Candle],
    left: int = 2,
    right: int = 2
) -> List[Tuple[int, float]]:

    result = []

    if len(candles) <= (
        left + right
    ):
        return result

    for i in range(
        left,
        len(candles) - right
    ):

        value = candles[i].high

        valid = True

        for j in range(
            i - left,
            i + right + 1
        ):

            if j == i:
                continue

            if candles[j].high > value:
                valid = False
                break

        if valid:
            result.append(
                (
                    i,
                    value
                )
            )

    return result


def pivot_lows(
    candles: List[Candle],
    left: int = 2,
    right: int = 2
) -> List[Tuple[int, float]]:

    result = []

    if len(candles) <= (
        left + right
    ):
        return result

    for i in range(
        left,
        len(candles) - right
    ):

        value = candles[i].low

        valid = True

        for j in range(
            i - left,
            i + right + 1
        ):

            if j == i:
                continue

            if candles[j].low < value:
                valid = False
                break

        if valid:
            result.append(
                (
                    i,
                    value
                )
            )

    return result


# ============================================================
# LEVEL FINDER
# ============================================================

def nearest_level(
    candles_15m: List[Candle],
    candles_1h: List[Candle],
    current: float,
    direction: str
) -> Tuple[
    Optional[float],
    str
]:

    candidates = []

    # --------------------------------------------------------
    # 1D APPROXIMATION
    # --------------------------------------------------------

    if len(candles_1h) >= 30:

        day = candles_1h[
            -25:-1
        ]

        if day:

            day_high = max(
                c.high
                for c in day
            )

            day_low = min(
                c.low
                for c in day
            )

            if (
                direction == "LONG"
                and day_high > current
            ):
                candidates.append(
                    (
                        day_high,
                        "1D"
                    )
                )

            if (
                direction == "SHORT"
                and day_low < current
            ):
                candidates.append(
                    (
                        day_low,
                        "1D"
                    )
                )

    # --------------------------------------------------------
    # 15M PIVOTS
    # --------------------------------------------------------

    highs = pivot_highs(
        candles_15m
    )

    lows = pivot_lows(
        candles_15m
    )

    if direction == "LONG":

        for _, level in highs[-15:]:

            if level > current:
                candidates.append(
                    (
                        level,
                        "15M"
                    )
                )

    else:

        for _, level in lows[-15:]:

            if level < current:
                candidates.append(
                    (
                        level,
                        "15M"
                    )
                )

    if not candidates:
        return None, ""

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
# COMPRESSION
# ============================================================

def compression_score(
    candles: List[Candle],
    direction: str
) -> Tuple[
    int,
    bool
]:

    if len(candles) < 25:
        return 0, False

    recent = candles[-20:]

    ranges = [
        c.high - c.low
        for c in recent
        if c.high > c.low
    ]

    if len(ranges) < 15:
        return 0, False

    first_slice = ranges[:8]
    last_slice = ranges[-8:]

    first_avg = (
        sum(first_slice)
        / len(first_slice)
    )

    last_avg = (
        sum(last_slice)
        / len(last_slice)
    )

    if first_avg <= 0:
        return 0, False

    compression = (
        1.0
        - (
            last_avg
            / first_avg
        )
    )

    highs = pivot_highs(
        recent
    )

    lows = pivot_lows(
        recent
    )

    score = 0
    valid = False

    if direction == "LONG":

        if len(lows) >= 2:

            last_two = [
                value
                for _, value
                in lows[-3:]
            ]

            if all(
                last_two[i]
                <= last_two[i + 1]
                for i in range(
                    len(last_two) - 1
                )
            ):

                score += 10
                valid = True

    else:

        if len(highs) >= 2:

            last_two = [
                value
                for _, value
                in highs[-3:]
            ]

            if all(
                last_two[i]
                >= last_two[i + 1]
                for i in range(
                    len(last_two) - 1
                )
            ):

                score += 10
                valid = True

    if compression >= 0.15:
        score += 10

    if compression >= 0.25:
        score += 5

    return min(
        score,
        25
    ), valid


# ============================================================
# STRATEGY 1
# HORIZONTAL LEVEL BREAKOUT
# ============================================================

def detect_horizontal_breakout(
    direction: str,
    current: float,
    level: float,
    previous: Candle,
    current_candle: Candle
) -> Tuple[
    bool,
    int,
    str
]:

    if level <= 0:
        return False, 0, ""

    if direction == "LONG":

        near = (
            current >= level * 0.9975
            and current <= level * 1.0025
        )

        breakout = (
            current_candle.close > level
            and previous.close <= level
        )

        if breakout:

            return (
                True,
                20,
                "Цена подошла к ключевому сопротивлению, "
                "а подтверждённая 5M свеча закрылась выше уровня."
            )

        if near:

            return (
                True,
                10,
                "Цена находится непосредственно у ключевого "
                "сопротивления. Ждём подтверждённый выход вверх."
            )

    else:

        near = (
            current >= level * 0.9975
            and current <= level * 1.0025
        )

        breakout = (
            current_candle.close < level
            and previous.close >= level
        )

        if breakout:

            return (
                True,
                20,
                "Цена подошла к ключевой поддержке, "
                "а подтверждённая 5M свеча закрылась ниже уровня."
            )

        if near:

            return (
                True,
                10,
                "Цена находится непосредственно у ключевой "
                "поддержки. Ждём подтверждённый выход вниз."
            )

    return False, 0, ""


# ============================================================
# STRATEGY 2
# TRENDLINE COMPRESSION BREAKOUT
# ============================================================

def detect_compression(
    candles_15m: List[Candle],
    direction: str
) -> Tuple[
    bool,
    int,
    str
]:

    points, valid = compression_score(
        candles_15m,
        direction
    )

    if not valid:
        return False, 0, ""

    if points < 15:
        return False, 0, ""

    if direction == "LONG":

        reason = (
            "На 15M сформировалось сжатие "
            "с повышающимися минимумами "
            "перед сопротивлением."
        )

    else:

        reason = (
            "На 15M сформировалось сжатие "
            "с понижающимися максимумами "
            "перед поддержкой."
        )

    return (
        True,
        points,
        reason
    )


# ============================================================
# STRATEGY 3
# MOMENTUM BREAKOUT
# ============================================================

def detect_momentum(
    candles_5m: List[Candle],
    direction: str
) -> Tuple[
    bool,
    int,
    str
]:

    if len(candles_5m) < 30:
        return False, 0, ""

    closes = [
        c.close
        for c in candles_5m
    ]

    ema9 = ema(
        closes,
        9
    )[-1]

    ema21 = ema(
        closes,
        21
    )[-1]

    current = candles_5m[-1]

    previous_window = candles_5m[
        -13:-1
    ]

    if len(
        previous_window
    ) < 5:
        return False, 0, ""

    if direction == "LONG":

        previous_high = max(
            c.high
            for c in previous_window
        )

        valid = (
            ema9 > ema21
            and current.close > previous_high
        )

    else:

        previous_low = min(
            c.low
            for c in previous_window
        )

        valid = (
            ema9 < ema21
            and current.close < previous_low
        )

    if not valid:
        return False, 0, ""

    return (
        True,
        15,
        "5M показывает импульсный выход "
        "из локального диапазона с подтверждением EMA."
    )


# ============================================================
# ANALYZE SYMBOL
# ============================================================

def analyze_symbol(
    inst_id: str,
    ticker: dict,
    candles_1h: List[Candle],
    candles_15m: List[Candle],
    candles_5m: List[Candle]
) -> Optional[Setup]:

    if len(candles_1h) < 60:
        return None

    if len(candles_15m) < 60:
        return None

    if len(candles_5m) < 60:
        return None

    confirmed_5m = [
        c
        for c in candles_5m
        if c.confirmed
    ]

    if len(confirmed_5m) < 30:
        return None

    current = float(
        ticker["last"]
    )

    if current <= 0:
        return None

    trend = structure_1h(
        candles_1h
    )

    if trend not in (
        "LONG",
        "SHORT"
    ):
        return None

    direction = trend

    level, level_tf = nearest_level(
        candles_15m,
        candles_1h,
        current,
        direction
    )

    if level is None:
        return None

    distance = abs(
        pct(
            current,
            level
        )
    )

    if distance > 0.40:
        return None

    atr_value = atr(
        confirmed_5m,
        14
    )

    if atr_value <= 0:
        return None

    atr_pct = (
        atr_value
        / current
        * 100.0
    )

    if atr_pct < 0.03:
        return None

    if atr_pct > 2.5:
        return None

    v_ratio = volume_ratio(
        confirmed_5m,
        20
    )

    score = 0

    # --------------------------------------------------------
    # 1H STRUCTURE
    # --------------------------------------------------------

    score += 15

    # --------------------------------------------------------
    # LEVEL
    # --------------------------------------------------------

    if level_tf == "1D":
        score += 20
    else:
        score += 12

    # --------------------------------------------------------
    # STRATEGIES
    # --------------------------------------------------------

    current_candle = confirmed_5m[-1]
    previous = confirmed_5m[-2]

    strategies = []

    horizontal_ok, horizontal_points, horizontal_reason = (
        detect_horizontal_breakout(
            direction,
            current,
            level,
            previous,
            current_candle
        )
    )

    if horizontal_ok:
        strategies.append(
            (
                "Horizontal Level Breakout",
                horizontal_points,
                horizontal_reason
            )
        )

    compression_ok, compression_points, compression_reason = (
        detect_compression(
            candles_15m,
            direction
        )
    )

    if compression_ok:
        strategies.append(
            (
                "Trendline Compression Breakout",
                compression_points,
                compression_reason
            )
        )

    momentum_ok, momentum_points, momentum_reason = (
        detect_momentum(
            confirmed_5m,
            direction
        )
    )

    if momentum_ok:
        strategies.append(
            (
                "Momentum Breakout",
                momentum_points,
                momentum_reason
            )
        )

    if not strategies:
        return None

    # Берём наиболее сильную стратегию.
    strategies.sort(
        key=lambda item: item[1],
        reverse=True
    )

    strategy, strategy_points, reason = (
        strategies[0]
    )

    score += strategy_points

    # Если одновременно подтверждаются несколько
    # стратегий — даём небольшой бонус.
    if len(strategies) >= 2:
        score += 5

    if len(strategies) >= 3:
        score += 5

    # --------------------------------------------------------
    # VOLUME CONFIRMATION
    # --------------------------------------------------------

    if v_ratio >= 1.25:
        score += 10

    if v_ratio >= 1.50:
        score += 3

    if v_ratio >= 2.00:
        score += 4

    # --------------------------------------------------------
    # LIQUIDITY
    # --------------------------------------------------------

    volume_24h = float(
        ticker["vol24h_usd"]
    )

    if volume_24h < MIN_24H_VOLUME_USD:
        return None

    if volume_24h >= 1_000_000_000:

        score += 5
        liquidity = "HIGH"

    elif volume_24h >= 250_000_000:

        score += 3
        liquidity = "GOOD"

    else:

        liquidity = "MEDIUM"

    # --------------------------------------------------------
    # OI
    # --------------------------------------------------------

    oi_value = get_open_interest(
        inst_id
    )

    if oi_value is not None:

        oi_status = "AVAILABLE"
        score += 2

    else:

        oi_status = "N/A"

    # --------------------------------------------------------
    # CHASE PROTECTION
    # --------------------------------------------------------

    if direction == "LONG":

        if current > level * (
            1
            + MAX_CHASE_PCT / 100.0
        ):
            return None

    else:

        if current < level * (
            1
            - MAX_CHASE_PCT / 100.0
        ):
            return None

    # --------------------------------------------------------
    # ENTRY ZONE
    # --------------------------------------------------------

    zone_pct = clamp(
        atr_pct * 0.35,
        0.08,
        0.25
    )

    entry_low = (
        level
        * (
            1
            - zone_pct / 100.0
        )
    )

    entry_high = (
        level
        * (
            1
            + zone_pct / 100.0
        )
    )

    # --------------------------------------------------------
    # STRUCTURAL STOP
    # --------------------------------------------------------

    recent_15 = candles_15m[
        -18:
    ]

    if len(recent_15) < 10:
        return None

    if direction == "LONG":

        structural_low = min(
            c.low
            for c in recent_15
        )

        sl = (
            structural_low
            - atr_value * 0.25
        )

        if sl >= current:
            return None

        risk = (
            current
            - sl
        )

    else:

        structural_high = max(
            c.high
            for c in recent_15
        )

        sl = (
            structural_high
            + atr_value * 0.25
        )

        if sl <= current:
            return None

        risk = (
            sl
            - current
        )

    if risk <= 0:
        return None

    risk_pct = (
        risk
        / current
        * 100.0
    )

    if risk_pct < 0.15:
        risk_pct = 0.15

    if risk_pct > 1.80:
        return None

    # --------------------------------------------------------
    # TAKE PROFITS
    # --------------------------------------------------------

    if direction == "LONG":

        tp1 = current + risk * 1.0
        tp2 = current + risk * 2.0
        tp3 = current + risk * 3.0

    else:

        tp1 = current - risk * 1.0
        tp2 = current - risk * 2.0
        tp3 = current - risk * 3.0

    score = int(
        clamp(
            score,
            0,
            100
        )
    )

    if score < MIN_SCORE:
        return None

    return Setup(
        inst_id=inst_id,
        coin=get_coin(inst_id),
        direction=direction,
        strategy=strategy,
        level=level,
        current_price=current,
        entry_low=entry_low,
        entry_high=entry_high,
        sl=sl,
        tp1=tp1,
        tp2=tp2,
        tp3=tp3,
        score=score,
        liquidity=liquidity,
        volume_grade=grade_volume(
            v_ratio
        ),
        oi_status=oi_status,
        level_tf=level_tf,
        reason=reason,
        volume_24h=volume_24h,
        breakout_volume_ratio=v_ratio,
        atr_pct=atr_pct,
        candles_5m=confirmed_5m[
            -80:
        ]
    )


# ============================================================
# SIGNAL CONTROL
# ============================================================

def can_send_new_signal(
    inst_id: str
) -> bool:

    current = now_ts()

    if inst_id in ready_setups:
        return False

    row = db.execute(
        """
        SELECT created_at
        FROM signals
        WHERE inst_id = ?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (
            inst_id,
        )
    ).fetchone()

    if row:

        last_time = float(
            row[0]
        )

        if (
            current
            - last_time
            < COOLDOWN_MINUTES * 60
        ):
            return False

    cutoff = (
        current
        - 3600
    )

    signals_hour[:] = [
        value
        for value in signals_hour
        if value >= cutoff
    ]

    if len(signals_hour) >= (
        MAX_SIGNALS_PER_HOUR
    ):
        return False

    return True


# ============================================================
# CHART
# ============================================================

def make_chart(
    setup: Setup
) -> str:

    candles = setup.candles_5m[
        -70:
    ]

    if len(candles) < 10:
        raise RuntimeError(
            "Not enough candles for chart."
        )

    safe_coin = (
        setup.coin
        .replace(
            "/",
            "_"
        )
        .replace(
            "\\",
            "_"
        )
    )

    path = (
        f"/tmp/quantum_"
        f"{safe_coin}_"
        f"{int(time.time() * 1000)}.png"
    )

    fig, ax = plt.subplots(
        figsize=(
            12,
            7
        ),
        dpi=140
    )

    fig.patch.set_facecolor(
        "#0b1020"
    )

    ax.set_facecolor(
        "#0b1020"
    )

    width = 0.65

    for i, candle in enumerate(
        candles
    ):

        color = (
            "#16c784"
            if candle.close >= candle.open
            else "#ea3943"
        )

        ax.plot(
            [i, i],
            [
                candle.low,
                candle.high
            ],
            color=color,
            linewidth=1.0
        )

        body_low = min(
            candle.open,
            candle.close
        )

        body_height = abs(
            candle.close
            - candle.open
        )

        if body_height == 0:

            body_height = (
                candle.close
                * 0.00001
            )

        rect = Rectangle(
            (
                i - width / 2,
                body_low
            ),
            width,
            body_height,
            facecolor=color,
            edgecolor=color,
            linewidth=0.5
        )

        ax.add_patch(
            rect
        )

    # LEVEL
    ax.axhline(
        setup.level,
        color="#f5c542",
        linewidth=2.0,
        linestyle="--",
        label="LEVEL"
    )

    # ENTRY
    ax.axhspan(
        setup.entry_low,
        setup.entry_high,
        color="#00aaff",
        alpha=0.10
    )

    # SL
    ax.axhline(
        setup.sl,
        color="#ff3b30",
        linewidth=1.7,
        linestyle="-.",
        label="SL"
    )

    # TP
    for tp in (
        setup.tp1,
        setup.tp2,
        setup.tp3
    ):

        ax.axhline(
            tp,
            color="#ffd166",
            linewidth=1.2
        )

    last_x = len(candles) - 1

    ax.text(
        last_x,
        setup.level,
        " LEVEL",
        color="#f5c542",
        va="bottom",
        fontsize=10,
        fontweight="bold"
    )

    ax.text(
        last_x,
        setup.sl,
        " SL",
        color="#ff3b30",
        va="bottom",
        fontsize=10,
        fontweight="bold"
    )

    ax.text(
        last_x,
        setup.tp1,
        " TP1",
        color="#ffd166",
        va="bottom",
        fontsize=9
    )

    ax.text(
        last_x,
        setup.tp2,
        " TP2",
        color="#ffd166",
        va="bottom",
        fontsize=9
    )

    ax.text(
        last_x,
        setup.tp3,
        " TP3",
        color="#ffd166",
        va="bottom",
        fontsize=9
    )

    ax.set_title(
        (
            f"{setup.coin}USDT | "
            f"{setup.direction} | "
            f"{setup.strategy}\n"
            f"Score {setup.score}/100 | 5M"
        ),
        color="white",
        fontsize=15,
        fontweight="bold",
        pad=15
    )

    ax.grid(
        alpha=0.12,
        color="white"
    )

    ax.tick_params(
        colors="#9ca3af"
    )

    for spine in ax.spines.values():
        spine.set_color(
            "#29334d"
        )

    ax.legend(
        facecolor="#111827",
        labelcolor="white",
        loc="upper left"
    )

    plt.tight_layout()

    fig.savefig(
        path,
        facecolor=fig.get_facecolor(),
        bbox_inches="tight"
    )

    plt.close(
        fig
    )

    return path


# ============================================================
# TELEGRAM SIGNAL
# ============================================================

def build_signal_text(
    setup: Setup,
    state: str = "READY"
) -> str:

    label = score_label(
        setup.score
    )

    if state == "READY":

        state_line = (
            "🟡 *SETUP READY*\n"
            "Цена находится в рабочей зоне. "
            "Не догоняем рынок."
        )

    elif state == "ACTIVE":

        state_line = (
            "🟢 *ENTRY ACTIVE*\n"
            "Триггер выполнен. "
            "Рабочая зона активна."
        )

    else:

        state_line = state

    risk = (
        abs(
            setup.current_price
            - setup.sl
        )
        / setup.current_price
        * 100
    )

    volume_m = (
        setup.volume_24h
        / 1_000_000
    )

    return (
        f"🔥 *{setup.coin}USDT — "
        f"{setup.direction}*\n\n"

        f"💰 *Цена:* "
        f"`{fmt_price(setup.current_price)}`\n"

        f"📊 *24H оборот:* "
        f"${volume_m:,.1f}M\n"

        f"📈 *Volume confirmation:* "
        f"`{setup.breakout_volume_ratio:.2f}x`\n\n"

        f"{state_line}\n\n"

        f"🧠 *ЛОГИКА СДЕЛКИ*\n"
        f"{setup.reason}\n\n"

        f"🎯 *ТОЧКА ВХОДА*\n"
        f"`{fmt_price(setup.entry_low)}` – "
        f"`{fmt_price(setup.entry_high)}`\n\n"

        f"🛑 *STOP LOSS*\n"
        f"`{fmt_price(setup.sl)}`\n"
        f"Риск: `−{risk:.2f}%`\n\n"

        f"🪜 *ЗАКРЫТИЕ ЛЕСЕНКОЙ*\n"

        f"TP1 — 30%\n"
        f"`{fmt_price(setup.tp1)}`\n\n"

        f"TP2 — 30%\n"
        f"`{fmt_price(setup.tp2)}`\n\n"

        f"TP3 — 40%\n"
        f"`{fmt_price(setup.tp3)}`\n\n"

        f"🔒 После TP1 → SL в BE\n\n"

        f"📊 *Стратегия:*\n"
        f"`{setup.strategy}`\n\n"

        f"📍 *Основной уровень:*\n"
        f"`{setup.level_tf}` — "
        f"`{fmt_price(setup.level)}`\n\n"

        f"💧 *Ликвидность:* "
        f"`{setup.liquidity}`\n"

        f"📦 *Volume grade:* "
        f"`{setup.volume_grade}`\n"

        f"⚡ *OI:* "
        f"`{setup.oi_status}`\n\n"

        f"⭐ *SIGNAL SCORE:* "
        f"`{setup.score}/100` "
        f"{label}\n\n"

        f"⏱ *READY действует:* "
        f"`{READY_TTL_MINUTES} мин`\n\n"

        f"⚠️ *Соблюдаем управление риском.*\n"
        f"Не догоняем рынок и не входим после "
        f"сильного движения.\n"
        f"*Качество важнее количества.*"
    )


# ============================================================
# SEND PHOTO + TEXT
# ============================================================

def send_photo_and_text(
    setup: Setup,
    state: str = "READY"
) -> Tuple[
    Optional[int],
    Optional[int]
]:

    chart_path = None

    try:

        chart_path = make_chart(
            setup
        )

        caption = (
            f"🔥 *{setup.coin}USDT — "
            f"{setup.direction}*\n"
            f"{score_label(setup.score)} · "
            f"{setup.strategy}"
        )

        with open(
            chart_path,
            "rb"
        ) as photo:

            sent_photo = (
                bot.send_photo(
                    CHANNEL_ID,
                    photo,
                    caption=caption,
                    parse_mode="Markdown",
                    show_caption_above_media=True
                )
            )

        text = build_signal_text(
            setup,
            state
        )

        sent_text = (
            bot.send_message(
                CHANNEL_ID,
                text,
                parse_mode="Markdown"
            )
        )

        log.info(
            "TELEGRAM SENT | %s | %s | score=%s",
            setup.coin,
            setup.direction,
            setup.score
        )

        return (
            sent_photo.message_id,
            sent_text.message_id
        )

    except Exception as exc:

        log.exception(
            "TELEGRAM SEND FAILED | %s | %s",
            setup.inst_id,
            exc
        )

        return None, None

    finally:

        if chart_path:

            try:
                os.remove(
                    chart_path
                )
            except OSError:
                pass


# ============================================================
# MORNING MESSAGE
# ============================================================

def send_morning_message():

    global last_morning_date

    if not MORNING_ENABLED:
        return

    current = local_now()

    if (
        current.hour != MORNING_HOUR
        or current.minute != MORNING_MINUTE
    ):
        return

    today = current.date()

    if last_morning_date == today:
        return

    message = (
        "🌅 *ДОБРОЕ УТРО, РЕБЯТА!*\n\n"

        "Начинаем новый торговый день.\n"
        "Работаем спокойно и только по правилам.\n\n"

        "🎯 Ждём точные сетапы.\n"
        "🚫 Не догоняем движение.\n"
        "🛑 Не увеличиваем риск.\n"
        "💰 Не используем весь депозит "
        "в одной позиции.\n"
        "⏳ Нет хорошего входа — просто ждём.\n\n"

        "*Качество важнее количества.*\n\n"

        "Всем продуктивного торгового дня! 🚀"
    )

    try:

        bot.send_message(
            CHANNEL_ID,
            message,
            parse_mode="Markdown"
        )

        last_morning_date = today

        log.info(
            "MORNING MESSAGE SENT"
        )

    except Exception:

        log.exception(
            "MORNING MESSAGE FAILED"
        )


# ============================================================
# DATABASE SIGNAL
# ============================================================

def save_signal(
    setup: Setup,
    status: str
):

    created = now_ts()

    db.execute(
        """
        INSERT INTO signals (
            inst_id,
            direction,
            strategy,
            level,
            entry_low,
            entry_high,
            sl,
            tp1,
            tp2,
            tp3,
            score,
            status,
            created_at,
            expires_at
        )
        VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?
        )
        """,
        (
            setup.inst_id,
            setup.direction,
            setup.strategy,
            setup.level,
            setup.entry_low,
            setup.entry_high,
            setup.sl,
            setup.tp1,
            setup.tp2,
            setup.tp3,
            setup.score,
            status,
            created,
            created
            + READY_TTL_MINUTES * 60
        )
    )

    db.commit()


# ============================================================
# EXPIRE READY
# ============================================================

def expire_old_ready():

    current = now_ts()

    expired = []

    for inst_id, ready in list(
        ready_setups.items()
    ):

        if current >= ready.expires_at:
            expired.append(
                inst_id
            )

    for inst_id in expired:

        ready = ready_setups.pop(
            inst_id,
            None
        )

        if ready is None:
            continue

        setup = ready.setup

        log.info(
            "READY EXPIRED | %s",
            inst_id
        )

        try:

            bot.send_message(
                CHANNEL_ID,
                (
                    f"🔴 *SETUP EXPIRED — "
                    f"{setup.coin}USDT*\n\n"
                    f"Цена не дала своевременный вход.\n"
                    f"*Рынок не догоняем.*"
                ),
                parse_mode="Markdown"
            )

        except Exception:

            log.exception(
                "EXPIRATION TELEGRAM ERROR"
            )


# ============================================================
# ACTIVATION
# ============================================================

def check_activation(
    inst_id: str,
    current_price: float
):

    ready = ready_setups.get(
        inst_id
    )

    if ready is None:
        return

    setup = ready.setup

    triggered = False

    if setup.direction == "LONG":

        if current_price >= setup.level:
            triggered = True

        if current_price > (
            setup.entry_high
            * (
                1
                + MAX_CHASE_PCT / 100.0
            )
        ):

            ready_setups.pop(
                inst_id,
                None
            )

            log.info(
                "READY CANCELLED CHASE | %s",
                inst_id
            )

            return

    else:

        if current_price <= setup.level:
            triggered = True

        if current_price < (
            setup.entry_low
            * (
                1
                - MAX_CHASE_PCT / 100.0
            )
        ):

            ready_setups.pop(
                inst_id,
                None
            )

            log.info(
                "READY CANCELLED CHASE | %s",
                inst_id
            )

            return

    if not triggered:
        return

    log.info(
        "ENTRY ACTIVE | %s | %s",
        setup.coin,
        setup.direction
    )

    try:

        bot.send_message(
            CHANNEL_ID,
            (
                f"🟢 *ENTRY ACTIVE — "
                f"{setup.coin}USDT "
                f"{setup.direction}*\n\n"

                f"Цена пересекла уровень "
                f"`{fmt_price(setup.level)}`.\n\n"

                f"Рабочая зона:\n"
                f"`{fmt_price(setup.entry_low)}` – "
                f"`{fmt_price(setup.entry_high)}`\n\n"

                f"🛑 SL: "
                f"`{fmt_price(setup.sl)}`\n"

                f"🎯 TP1: "
                f"`{fmt_price(setup.tp1)}`\n"

                f"🎯 TP2: "
                f"`{fmt_price(setup.tp2)}`\n"

                f"🏆 TP3: "
                f"`{fmt_price(setup.tp3)}`"
            ),
            parse_mode="Markdown"
        )

    except Exception:

        log.exception(
            "ACTIVE TELEGRAM ERROR"
        )

    db.execute(
        """
        UPDATE signals
        SET status = 'ACTIVE',
            activated_at = ?
        WHERE id = (
            SELECT id
            FROM signals
            WHERE inst_id = ?
              AND status = 'READY'
            ORDER BY created_at DESC
            LIMIT 1
        )
        """,
        (
            now_ts(),
            inst_id
        )
    )

    db.commit()

    ready_setups.pop(
        inst_id,
        None
    )


# ============================================================
# DAILY COUNTER
# ============================================================

def reset_daily_counter():

    global signals_today

    current_date = local_now().date()

    stored = db.execute(
        """
        SELECT value
        FROM bot_state
        WHERE key = 'counter_date'
        """
    ).fetchone()

    if stored is None:

        db.execute(
            """
            INSERT OR REPLACE INTO bot_state
            (key, value)
            VALUES (?, ?)
            """,
            (
                "counter_date",
                str(current_date)
            )
        )

        db.commit()

        signals_today = 0

        return

    if stored[0] != str(
        current_date
    ):

        db.execute(
            """
            INSERT OR REPLACE INTO bot_state
            (key, value)
            VALUES (?, ?)
            """,
            (
                "counter_date",
                str(current_date)
            )
        )

        db.commit()

        signals_today = 0


# ============================================================
# STARTUP MESSAGE
# ============================================================

def startup_message():

    message = (
        "🚀 *QUANTUM SCALPER V3 ONLINE*\n\n"

        "OKX: 🟢\n"
        "Telegram: 🟢\n"
        "Scanner: 🟢\n\n"

        "🧠 *STRATEGIES*\n"
        "• Horizontal Level Breakout\n"
        "• Trendline Compression Breakout\n"
        "• Momentum Breakout\n\n"

        f"💧 Minimum 24H turnover: "
        f"`$60M`\n"

        f"⭐ Minimum Score: "
        f"`{MIN_SCORE}/100`\n"

        f"⏱ READY TTL: "
        f"`{READY_TTL_MINUTES} min`\n"

        f"🔒 Cooldown: "
        f"`{COOLDOWN_MINUTES} min`\n"

        f"📊 Max symbols: "
        f"`{MAX_SYMBOLS}`\n\n"

        "*Качество важнее количества.*"
    )

    try:

        bot.send_message(
            CHANNEL_ID,
            message,
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
# MARKET SCAN
# ============================================================

def scan_market():

    global last_scan_ts
    global scan_count
    global signals_today

    last_scan_ts = now_ts()

    scan_count += 1

    log.info(
        "=============================="
    )

    log.info(
        "SCAN #%s START",
        scan_count
    )

    # --------------------------------------------------------
    # TICKERS
    # --------------------------------------------------------

    tickers = get_tickers()

    # --------------------------------------------------------
    # LIQUIDITY
    # --------------------------------------------------------

    liquid = []

    for inst_id, data in (
        tickers.items()
    ):

        volume_usd = float(
            data.get(
                "vol24h_usd",
                0
            )
        )

        if volume_usd >= (
            MIN_24H_VOLUME_USD
        ):

            liquid.append(
                (
                    inst_id,
                    data
                )
            )

    liquid.sort(
        key=lambda item: item[1][
            "vol24h_usd"
        ],
        reverse=True
    )

    selected = liquid[
        :MAX_SYMBOLS
    ]

    log.info(
        "MARKET | tickers=%s | "
        "liquid>=60M=%s | selected=%s",
        len(tickers),
        len(liquid),
        len(selected)
    )

    # --------------------------------------------------------
    # DIAGNOSTIC
    # --------------------------------------------------------

    if selected:

        top_names = []

        for inst_id, data in selected[:5]:

            top_names.append(
                (
                    f"{get_coin(inst_id)}:"
                    f"${data['vol24h_usd'] / 1_000_000:.0f}M"
                )
            )

        log.info(
            "TOP LIQUID | %s",
            " | ".join(top_names)
        )

    else:

        # Это очень важно.
        # Если вдруг снова 0 — сразу увидим реальную
        # причину в логах.
        top_any = sorted(
            tickers.items(),
            key=lambda item: item[1].get(
                "vol24h_usd",
                0
            ),
            reverse=True
        )[:5]

        if top_any:

            diagnostic = []

            for inst_id, data in top_any:

                diagnostic.append(
                    (
                        f"{get_coin(inst_id)}:"
                        f"${data.get('vol24h_usd', 0) / 1_000_000:.2f}M"
                    )
                )

            log.warning(
                "NO LIQUID SYMBOLS | TOP=%s",
                " | ".join(
                    diagnostic
                )
            )

    # --------------------------------------------------------
    # ANALYSIS
    # --------------------------------------------------------

    for inst_id, ticker in selected:

        try:

            current_price = float(
                ticker["last"]
            )

            # Existing READY first.
            check_activation(
                inst_id,
                current_price
            )

            if inst_id in ready_setups:
                continue

            if not can_send_new_signal(
                inst_id
            ):
                continue

            # ------------------------------------------------
            # CANDLES
            # ------------------------------------------------

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

            setup = analyze_symbol(
                inst_id,
                ticker,
                candles_1h,
                candles_15m,
                candles_5m
            )

            if setup is None:
                continue

            log.info(
                "CANDIDATE | %s | %s | "
                "%s | score=%s | "
                "volume=$%.1fM",
                setup.coin,
                setup.direction,
                setup.strategy,
                setup.score,
                setup.volume_24h / 1_000_000
            )

            # ------------------------------------------------
            # TELEGRAM
            # ------------------------------------------------

            photo_id, text_id = (
                send_photo_and_text(
                    setup,
                    "READY"
                )
            )

            if text_id is None:

                log.warning(
                    "SIGNAL NOT SAVED | "
                    "Telegram failed | %s",
                    inst_id
                )

                continue

            created = now_ts()

            ready_setups[
                inst_id
            ] = ActiveReady(
                setup=setup,
                created_at=created,
                expires_at=(
                    created
                    + READY_TTL_MINUTES * 60
                ),
                telegram_message_id=text_id,
                photo_message_id=photo_id
            )

            save_signal(
                setup,
                "READY"
            )

            signals_hour.append(
                created
            )

            signals_today += 1

            log.info(
                "READY CREATED | %s | "
                "score=%s",
                setup.coin,
                setup.score
            )

            time.sleep(
                1.5
            )

        except Exception as exc:

            # Одна монета никогда не должна
            # остановить весь сканер.
            log.exception(
                "SYMBOL ERROR | %s | %s",
                inst_id,
                exc
            )

            continue

    log.info(
        "SCAN #%s COMPLETE",
        scan_count
    )


# ============================================================
# HEARTBEAT
# ============================================================

def heartbeat():

    if not last_scan_ts:
        return

    age = (
        now_ts()
        - last_scan_ts
    )

    if age > 180:

        log.warning(
            "WATCHDOG | scanner "
            "has not completed a scan "
            "for %.0f seconds",
            age
        )


# ============================================================
# MAIN
# ============================================================

def main():

    log.info(
        "=================================================="
    )

    log.info(
        "QUANTUM SCALPER V3 STARTING"
    )

    log.info(
        "Timezone=%s",
        TIMEZONE
    )

    log.info(
        "MAX_SYMBOLS=%s",
        MAX_SYMBOLS
    )

    log.info(
        "MIN_24H_VOLUME_USD=$%s",
        f"{MIN_24H_VOLUME_USD:,.0f}"
    )

    log.info(
        "MIN_SCORE=%s",
        MIN_SCORE
    )

    log.info(
        "READY_TTL=%s min",
        READY_TTL_MINUTES
    )

    log.info(
        "COOLDOWN=%s min",
        COOLDOWN_MINUTES
    )

    log.info(
        "SCAN_INTERVAL=%ss",
        SCAN_INTERVAL_SECONDS
    )

    log.info(
        "=================================================="
    )

    # --------------------------------------------------------
    # TELEGRAM TEST
    # --------------------------------------------------------

    startup_message()

    # --------------------------------------------------------
    # OKX TEST
    # --------------------------------------------------------

    try:

        instruments = get_instruments()

        log.info(
            "OKX LIVE INSTRUMENTS: %s",
            len(instruments)
        )

        tickers = get_tickers()

        log.info(
            "OKX TICKERS: %s",
            len(tickers)
        )

        liquid_count = sum(
            1
            for data in tickers.values()
            if data.get(
                "vol24h_usd",
                0
            ) >= MIN_24H_VOLUME_USD
        )

        log.info(
            "OKX LIQUID >= $60M: %s",
            liquid_count
        )

    except Exception:

        log.exception(
            "INITIAL OKX CONNECTION FAILED"
        )

    # --------------------------------------------------------
    # MAIN LOOP
    # --------------------------------------------------------

    while True:

        try:

            reset_daily_counter()

            send_morning_message()

            expire_old_ready()

            scan_market()

            heartbeat()

            log.info(
                "NEXT SCAN IN %ss",
                SCAN_INTERVAL_SECONDS
            )

            time.sleep(
                SCAN_INTERVAL_SECONDS
            )

        except KeyboardInterrupt:

            log.info(
                "Shutdown requested."
            )

            break

        except Exception as exc:

            log.exception(
                "MAIN LOOP ERROR: %s",
                exc
            )

            log.warning(
                "RECOVERY | retrying in 15 seconds..."
            )

            time.sleep(
                15
            )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()
