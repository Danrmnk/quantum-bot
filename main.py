import os
import time
import math
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
# QUANTUM SCALPER V5
#
# DIGASH-INSPIRED MARKET STRUCTURE ENGINE
#
# PRE-BREAKOUT SCALPING
#
# FEATURES
# ------------------------------------------------------------
# - Multi-TF horizontal levels
# - Multi-TF trendlines
# - Daily high / low
# - Round-number levels
# - Price-density clustering
# - Swing highs / lows
# - Trendline touch quality
# - Compression
# - Consolidation / accumulation
# - Volume spike
# - NATR
# - OI
# - Funding
# - BTC correlation
# - Multi-TF directional structure
# - Breakout / retest context
# - Pre-breakout execution filter
# - Structural invalidation
#
# IMPORTANT
# ------------------------------------------------------------
# This bot DOES NOT TRADE.
# It analyzes OKX public market data and publishes signals.
#
# READY has NO TIMER.
# READY remains active while the setup remains structurally valid.
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


# ============================================================
# LIQUIDITY
# ============================================================

MIN_24H_VOLUME_USD = float(
    os.getenv(
        "MIN_24H_VOLUME_USD",
        "60000000"
    )
)

MAX_SYMBOLS = int(
    os.getenv(
        "MAX_SYMBOLS",
        "80"
    )
)


# ============================================================
# SIGNAL QUALITY
# ============================================================

MIN_SCORE = int(
    os.getenv(
        "MIN_SCORE",
        "82"
    )
)

MIN_EXECUTION_SCORE = int(
    os.getenv(
        "MIN_EXECUTION_SCORE",
        "72"
    )
)

COOLDOWN_MINUTES = int(
    os.getenv(
        "COOLDOWN_MINUTES",
        "60"
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


# ============================================================
# PRE-BREAKOUT DISTANCES
# ============================================================

MIN_PREBREAK_DISTANCE_PCT = float(
    os.getenv(
        "MIN_PREBREAK_DISTANCE_PCT",
        "0.08"
    )
)

MAX_PREBREAK_DISTANCE_PCT = float(
    os.getenv(
        "MAX_PREBREAK_DISTANCE_PCT",
        "0.75"
    )
)

MIN_ENTRY_DISTANCE_ATR = float(
    os.getenv(
        "MIN_ENTRY_DISTANCE_ATR",
        "0.35"
    )
)

MAX_ENTRY_DISTANCE_ATR = float(
    os.getenv(
        "MAX_ENTRY_DISTANCE_ATR",
        "3.50"
    )
)

MAX_CHASE_PCT = float(
    os.getenv(
        "MAX_CHASE_PCT",
        "0.45"
    )
)


# ============================================================
# STRUCTURE
# ============================================================

LEVEL_CLUSTER_PCT = float(
    os.getenv(
        "LEVEL_CLUSTER_PCT",
        "0.12"
    )
)

LEVEL_TOUCH_TOLERANCE_PCT = float(
    os.getenv(
        "LEVEL_TOUCH_TOLERANCE_PCT",
        "0.16"
    )
)

TRENDLINE_TOUCH_TOLERANCE_PCT = float(
    os.getenv(
        "TRENDLINE_TOUCH_TOLERANCE_PCT",
        "0.18"
    )
)

MIN_TRENDLINE_TOUCHES = int(
    os.getenv(
        "MIN_TRENDLINE_TOUCHES",
        "3"
    )
)

MIN_COMPRESSION_SCORE = int(
    os.getenv(
        "MIN_COMPRESSION_SCORE",
        "16"
    )
)

MIN_FORMATION_BARS = int(
    os.getenv(
        "MIN_FORMATION_BARS",
        "12"
    )
)


# ============================================================
# MORNING
# ============================================================

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


# ============================================================
# DATABASE
# ============================================================

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
    "User-Agent": "QuantumScalper/5.0",
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
    expires_at REAL
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
class LevelCandidate:
    price: float
    tf: str
    strength: int
    touches: int
    source: str
    distance_pct: float


@dataclass
class TrendlineCandidate:
    direction: str
    slope: float
    intercept: float
    start_index: int
    end_index: int
    touches: int
    score: int
    current_value: float
    source_tf: str
    points: List[Tuple[int, float]]


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
    execution_score: int

    liquidity: str
    volume_grade: str
    oi_status: str
    funding_status: str
    btc_correlation: float

    level_tf: str

    reason: str

    volume_24h: float
    breakout_volume_ratio: float

    atr_pct: float
    natr_pct: float

    trendline: Optional[TrendlineCandidate]

    structure_state: str

    candles_5m: List[Candle]


@dataclass
class ActiveReady:
    setup: Setup
    created_at: float

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

btc_cache = {
    "candles_5m": [],
    "timestamp": 0.0
}


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
        return f"{price:,.2f}".replace(",", " ")

    if price >= 100:
        return f"{price:,.2f}".replace(",", " ")

    if price >= 1:
        return f"{price:,.4f}".replace(",", " ")

    if price >= 0.01:
        return f"{price:.6f}".rstrip("0").rstrip(".")

    return f"{price:.10f}".rstrip("0").rstrip(".")


def clamp(
    value: float,
    low: float,
    high: float
) -> float:

    return max(
        low,
        min(high, value)
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

    if ratio >= 1.0:
        return "NORMAL"

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


def median(
    values: List[float]
) -> float:

    if not values:
        return 0.0

    values = sorted(values)

    n = len(values)

    mid = n // 2

    if n % 2:
        return values[mid]

    return (
        values[mid - 1]
        + values[mid]
    ) / 2.0


# ============================================================
# OKX REQUEST
# ============================================================

def okx_get(
    path: str,
    params: dict,
    retries: int = 3
) -> dict:

    url = OKX_BASE_URL + path
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
        f"{retries} attempts: {last_error}"
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

            volume_usd = (
                vol_ccy_24h
                * last
            )

            result[inst_id] = {
                "last": last,
                "high24h": float(
                    item.get(
                        "high24h",
                        0
                    ) or 0
                ),
                "low24h": float(
                    item.get(
                        "low24h",
                        0
                    ) or 0
                ),
                "vol_ccy_24h": vol_ccy_24h,
                "vol24h_usd": volume_usd,
                "ts": int(
                    item.get(
                        "ts",
                        0
                    ) or 0
                ),
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
                min(limit, 300)
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
                    ts=int(row[0]),
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5] or 0),
                    quote_volume=float(row[7] or 0),
                    confirmed=str(row[8]) == "1"
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
# FUNDING
# ============================================================

def get_funding_rate(
    inst_id: str
) -> Optional[float]:

    try:

        payload = okx_get(
            "/api/v5/public/funding-rate",
            {
                "instId": inst_id
            }
        )

        data = payload.get(
            "data",
            []
        )

        if not data:
            return None

        funding = data[0].get(
            "fundingRate"
        )

        if funding in (
            None,
            ""
        ):
            return None

        return float(
            funding
        )

    except Exception as exc:

        log.warning(
            "FUNDING FAILED | %s | %s",
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

    if not values or period <= 0:
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

    if len(candles) < period + 1:
        return 0.0

    trs = []

    for i in range(
        1,
        len(candles)
    ):

        current = candles[i]
        previous = candles[i - 1]

        trs.append(
            max(
                current.high - current.low,
                abs(
                    current.high
                    - previous.close
                ),
                abs(
                    current.low
                    - previous.close
                )
            )
        )

    if len(trs) < period:
        return 0.0

    return (
        sum(trs[-period:])
        / period
    )


def natr(
    candles: List[Candle],
    period: int = 14
) -> float:

    if not candles:
        return 0.0

    value = atr(
        candles,
        period
    )

    close = candles[-1].close

    if close <= 0:
        return 0.0

    return (
        value
        / close
        * 100.0
    )


def volume_ratio(
    candles: List[Candle],
    lookback: int = 20
) -> float:

    if len(candles) < lookback + 1:
        return 0.0

    current = candles[-1].quote_volume

    history = [
        c.quote_volume
        for c in candles[
            -lookback - 1:-1
        ]
        if c.quote_volume > 0
    ]

    if not history:
        return 0.0

    average = (
        sum(history)
        / len(history)
    )

    if average <= 0:
        return 0.0

    return current / average


def returns(
    candles: List[Candle]
) -> List[float]:

    result = []

    for i in range(
        1,
        len(candles)
    ):

        previous = candles[i - 1].close
        current = candles[i].close

        if previous <= 0:
            continue

        result.append(
            math.log(
                current / previous
            )
        )

    return result


def correlation(
    a: List[float],
    b: List[float]
) -> float:

    n = min(
        len(a),
        len(b)
    )

    if n < 10:
        return 0.0

    a = a[-n:]
    b = b[-n:]

    mean_a = (
        sum(a) / n
    )

    mean_b = (
        sum(b) / n
    )

    numerator = sum(
        (
            a[i] - mean_a
        )
        * (
            b[i] - mean_b
        )
        for i in range(n)
    )

    denominator_a = math.sqrt(
        sum(
            (
                x - mean_a
            ) ** 2
            for x in a
        )
    )

    denominator_b = math.sqrt(
        sum(
            (
                x - mean_b
            ) ** 2
            for x in b
        )
    )

    denominator = (
        denominator_a
        * denominator_b
    )

    if denominator <= 0:
        return 0.0

    return clamp(
        numerator / denominator,
        -1.0,
        1.0
    )


# ============================================================
# PIVOTS / SWINGS
# ============================================================

def pivot_highs(
    candles: List[Candle],
    left: int = 2,
    right: int = 2
) -> List[Tuple[int, float]]:

    result = []

    if len(candles) <= left + right:
        return result

    for i in range(
        left,
        len(candles) - right
    ):

        value = candles[i].high

        if all(
            candles[j].high <= value
            for j in range(
                i - left,
                i + right + 1
            )
            if j != i
        ):

            result.append(
                (i, value)
            )

    return result


def pivot_lows(
    candles: List[Candle],
    left: int = 2,
    right: int = 2
) -> List[Tuple[int, float]]:

    result = []

    if len(candles) <= left + right:
        return result

    for i in range(
        left,
        len(candles) - right
    ):

        value = candles[i].low

        if all(
            candles[j].low >= value
            for j in range(
                i - left,
                i + right + 1
            )
            if j != i
        ):

            result.append(
                (i, value)
            )

    return result


# ============================================================
# ROUND LEVELS
# ============================================================

def round_step(
    price: float
) -> float:

    if price >= 10000:
        return 1000.0

    if price >= 1000:
        return 100.0

    if price >= 100:
        return 10.0

    if price >= 10:
        return 1.0

    if price >= 1:
        return 0.1

    if price >= 0.1:
        return 0.01

    if price >= 0.01:
        return 0.001

    if price >= 0.001:
        return 0.0001

    return price * 0.05


def round_levels(
    current: float
) -> List[float]:

    step = round_step(
        current
    )

    if step <= 0:
        return []

    center = round(
        current / step
    ) * step

    return [
        center + step * offset
        for offset in range(
            -8,
            9
        )
        if center + step * offset > 0
    ]


# ============================================================
# LEVEL TOUCH COUNT
# ============================================================

def count_level_touches(
    candles: List[Candle],
    level: float,
    direction: str,
    tolerance_pct: float = LEVEL_TOUCH_TOLERANCE_PCT
) -> int:

    if level <= 0:
        return 0

    tolerance = (
        tolerance_pct
        / 100.0
    )

    touches = 0

    for candle in candles:

        if direction == "LONG":

            distance = abs(
                candle.high - level
            ) / level

        else:

            distance = abs(
                candle.low - level
            ) / level

        if distance <= tolerance:
            touches += 1

    return touches


# ============================================================
# HORIZONTAL LEVEL CANDIDATES
# ============================================================

def collect_horizontal_levels(
    candles: List[Candle],
    tf: str,
    current: float,
    direction: str
) -> List[LevelCandidate]:

    result = []

    if len(candles) < 30:
        return result

    highs = pivot_highs(
        candles,
        2,
        2
    )

    lows = pivot_lows(
        candles,
        2,
        2
    )

    pivots = (
        highs
        if direction == "LONG"
        else lows
    )

    for _, price in pivots:

        if direction == "LONG":

            if price <= current:
                continue

        else:

            if price >= current:
                continue

        distance = abs(
            pct(
                price,
                current
            )
        )

        if (
            distance
            < MIN_PREBREAK_DISTANCE_PCT
        ):
            continue

        if (
            distance
            > MAX_PREBREAK_DISTANCE_PCT * 2.5
        ):
            continue

        touches = count_level_touches(
            candles,
            price,
            direction
        )

        strength = 0

        if touches >= 2:
            strength += 10

        if touches >= 3:
            strength += 8

        if touches >= 4:
            strength += 7

        if touches >= 5:
            strength += 5

        recency_bonus = 0

        if len(candles) >= 20:

            recent_window = candles[
                -20:
            ]

            for candle in recent_window:

                if direction == "LONG":

                    if abs(
                        candle.high - price
                    ) / price <= 0.002:

                        recency_bonus += 2

                else:

                    if abs(
                        candle.low - price
                    ) / price <= 0.002:

                        recency_bonus += 2

            recency_bonus = min(
                recency_bonus,
                8
            )

        strength += recency_bonus

        result.append(
            LevelCandidate(
                price=price,
                tf=tf,
                strength=min(
                    strength,
                    35
                ),
                touches=touches,
                source="PIVOT",
                distance_pct=distance
            )
        )

    return result


# ============================================================
# DAILY HIGH / LOW
# ============================================================

def daily_extreme_level(
    candles: List[Candle],
    current: float,
    direction: str
) -> Optional[LevelCandidate]:

    if not candles:
        return None

    recent = candles[
        -min(
            len(candles),
            96
        ):
    ]

    if direction == "LONG":

        price = max(
            c.high
            for c in recent
        )

        if price <= current:
            return None

        source = "DAILY_HIGH"

    else:

        price = min(
            c.low
            for c in recent
        )

        if price >= current:
            return None

        source = "DAILY_LOW"

    distance = abs(
        pct(
            price,
            current
        )
    )

    if (
        distance
        > MAX_PREBREAK_DISTANCE_PCT * 2.5
    ):
        return None

    return LevelCandidate(
        price=price,
        tf="1D",
        strength=24,
        touches=2,
        source=source,
        distance_pct=distance
    )


# ============================================================
# DENSITY CLUSTERING
# ============================================================

def cluster_levels(
    candidates: List[LevelCandidate]
) -> List[LevelCandidate]:

    if not candidates:
        return []

    candidates = sorted(
        candidates,
        key=lambda x: x.price
    )

    clusters: List[
        List[LevelCandidate]
    ] = []

    for candidate in candidates:

        placed = False

        for cluster in clusters:

            center = median(
                [
                    x.price
                    for x in cluster
                ]
            )

            if center <= 0:
                continue

            distance = (
                abs(
                    candidate.price
                    - center
                )
                / center
                * 100.0
            )

            if (
                distance
                <= LEVEL_CLUSTER_PCT
            ):

                cluster.append(
                    candidate
                )

                placed = True
                break

        if not placed:

            clusters.append(
                [candidate]
            )

    result = []

    for cluster in clusters:

        prices = [
            x.price
            for x in cluster
        ]

        weighted_sum = 0.0
        weight_total = 0.0

        for item in cluster:

            weight = max(
                1,
                item.strength
            )

            weighted_sum += (
                item.price
                * weight
            )

            weight_total += weight

        center = (
            weighted_sum
            / weight_total
        )

        strength = min(
            50,
            sum(
                x.strength
                for x in cluster
            )
        )

        touches = sum(
            x.touches
            for x in cluster
        )

        source_items = [
            x.source
            for x in cluster
        ]

        tf_items = [
            x.tf
            for x in cluster
        ]

        if "1D" in tf_items:
            tf = "1D"

        elif "4H" in tf_items:
            tf = "4H"

        elif "1H" in tf_items:
            tf = "1H"

        elif "15M" in tf_items:
            tf = "15M"

        else:
            tf = tf_items[0]

        source = (
            "+".join(
                sorted(
                    set(
                        source_items
                    )
                )
            )
        )

        result.append(
            LevelCandidate(
                price=center,
                tf=tf,
                strength=strength,
                touches=touches,
                source=source,
                distance_pct=0.0
            )
        )

    return result


# ============================================================
# BEST HORIZONTAL LEVEL
# ============================================================

def find_best_horizontal_level(
    candles_15m: List[Candle],
    candles_1h: List[Candle],
    candles_4h: List[Candle],
    current: float,
    direction: str
) -> Optional[LevelCandidate]:

    candidates = []

    candidates.extend(
        collect_horizontal_levels(
            candles_15m,
            "15M",
            current,
            direction
        )
    )

    candidates.extend(
        collect_horizontal_levels(
            candles_1h,
            "1H",
            current,
            direction
        )
    )

    candidates.extend(
        collect_horizontal_levels(
            candles_4h,
            "4H",
            current,
            direction
        )
    )

    daily = daily_extreme_level(
        candles_1h,
        current,
        direction
    )

    if daily is not None:
        candidates.append(
            daily
        )

    # Round levels are useful as secondary
    # confluence, not as standalone strong levels.
    for price in round_levels(
        current
    ):

        if direction == "LONG":

            if price <= current:
                continue

        else:

            if price >= current:
                continue

        distance = abs(
            pct(
                price,
                current
            )
        )

        if (
            distance
            < MIN_PREBREAK_DISTANCE_PCT
        ):
            continue

        if (
            distance
            > MAX_PREBREAK_DISTANCE_PCT * 2.0
        ):
            continue

        candidates.append(
            LevelCandidate(
                price=price,
                tf="ROUND",
                strength=8,
                touches=1,
                source="ROUND",
                distance_pct=distance
            )
        )

    clustered = cluster_levels(
        candidates
    )

    if not clustered:
        return None

    for item in clustered:

        item.distance_pct = abs(
            pct(
                item.price,
                current
            )
        )

    valid = []

    for item in clustered:

        if (
            item.distance_pct
            < MIN_PREBREAK_DISTANCE_PCT
        ):
            continue

        if (
            item.distance_pct
            > MAX_PREBREAK_DISTANCE_PCT
        ):
            continue

        valid.append(
            item
        )

    if not valid:
        return None

    # Score balances:
    # strength + multi-TF confluence +
    # reasonable distance.
    def ranking(
        item: LevelCandidate
    ) -> float:

        distance_penalty = (
            item.distance_pct
            * 8.0
        )

        tf_bonus = 0

        if item.tf == "4H":
            tf_bonus = 12

        elif item.tf == "1H":
            tf_bonus = 9

        elif item.tf == "1D":
            tf_bonus = 11

        elif item.tf == "15M":
            tf_bonus = 6

        return (
            item.strength
            + tf_bonus
            - distance_penalty
        )

    valid.sort(
        key=ranking,
        reverse=True
    )

    return valid[0]


# ============================================================
# TRENDLINE FIT
# ============================================================

def linear_fit(
    points: List[Tuple[int, float]]
) -> Optional[
    Tuple[float, float]
]:

    if len(points) < 2:
        return None

    xs = [
        float(x)
        for x, _ in points
    ]

    ys = [
        float(y)
        for _, y in points
    ]

    x_mean = sum(xs) / len(xs)
    y_mean = sum(ys) / len(ys)

    denominator = sum(
        (
            x - x_mean
        ) ** 2
        for x in xs
    )

    if denominator <= 0:
        return None

    slope = sum(
        (
            xs[i] - x_mean
        )
        * (
            ys[i] - y_mean
        )
        for i in range(
            len(xs)
        )
    ) / denominator

    intercept = (
        y_mean
        - slope * x_mean
    )

    return (
        slope,
        intercept
    )


# ============================================================
# TRENDLINE QUALITY
# ============================================================

def trendline_error_pct(
    price: float,
    line_value: float
) -> float:

    if line_value == 0:
        return 999.0

    return (
        abs(
            price
            - line_value
        )
        / line_value
        * 100.0
    )


def build_trendline_candidate(
    candles: List[Candle],
    direction: str,
    tf: str
) -> Optional[TrendlineCandidate]:

    if len(candles) < 40:
        return None

    recent = candles[
        -50:
    ]

    if direction == "LONG":

        pivots = pivot_lows(
            recent,
            2,
            2
        )

    else:

        pivots = pivot_highs(
            recent,
            2,
            2
        )

    if len(pivots) < 3:
        return None

    best = None

    # Try several combinations of the latest pivots.
    pivot_pool = pivots[
        -8:
    ]

    for i in range(
        len(pivot_pool) - 2
    ):

        for j in range(
            i + 1,
            len(pivot_pool) - 1
        ):

            p1 = pivot_pool[i]
            p2 = pivot_pool[j]

            if p2[0] <= p1[0]:
                continue

            fit = linear_fit(
                [
                    p1,
                    p2
                ]
            )

            if fit is None:
                continue

            slope, intercept = fit

            if direction == "LONG":

                if slope <= 0:
                    continue

            else:

                if slope >= 0:
                    continue

            touches = 0
            accepted_points = []

            for point in pivot_pool:

                x, y = point

                line_value = (
                    slope * x
                    + intercept
                )

                error = trendline_error_pct(
                    y,
                    line_value
                )

                if (
                    error
                    <= TRENDLINE_TOUCH_TOLERANCE_PCT
                ):

                    # A trendline should not be crossed
                    # heavily by pivot points.
                    if direction == "LONG":

                        if y < line_value * 0.997:
                            continue

                    else:

                        if y > line_value * 1.003:
                            continue

                    touches += 1
                    accepted_points.append(
                        point
                    )

            if touches < MIN_TRENDLINE_TOUCHES:
                continue

            end_index = (
                len(recent) - 1
            )

            current_value = (
                slope * end_index
                + intercept
            )

            # Check slope is meaningful relative
            # to market price.
            normalized_slope = (
                slope
                / max(
                    current_value,
                    1e-12
                )
                * 100.0
            )

            if abs(
                normalized_slope
            ) < 0.01:
                continue

            score = 0

            if touches >= 3:
                score += 15

            if touches >= 4:
                score += 7

            if touches >= 5:
                score += 5

            if len(
                accepted_points
            ) >= 3:
                score += 5

            if best is None or score > best.score:

                best = TrendlineCandidate(
                    direction=direction,
                    slope=slope,
                    intercept=intercept,
                    start_index=p1[0],
                    end_index=end_index,
                    touches=touches,
                    score=min(
                        score,
                        35
                    ),
                    current_value=current_value,
                    source_tf=tf,
                    points=accepted_points
                )

    return best


# ============================================================
# TRENDLINE PRESSURE
# ============================================================

def trendline_pressure(
    candles: List[Candle],
    direction: str,
    tf: str
) -> Tuple[
    Optional[TrendlineCandidate],
    int,
    bool
]:

    line = build_trendline_candidate(
        candles,
        direction,
        tf
    )

    if line is None:
        return None, 0, False

    current = candles[-1].close

    distance = (
        abs(
            current
            - line.current_value
        )
        / current
        * 100.0
    )

    # Price must remain reasonably close to
    # the trendline. Otherwise it is not
    # a current compression structure.
    if distance > 1.2:
        return line, 0, False

    score = line.score

    if distance <= 0.25:
        score += 8

    elif distance <= 0.45:
        score += 5

    elif distance <= 0.75:
        score += 2

    score = min(
        score,
        40
    )

    return (
        line,
        score,
        score >= 18
    )


# ============================================================
# MARKET STRUCTURE
# ============================================================

def market_structure(
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

    highs = pivot_highs(
        candles[-50:],
        2,
        2
    )

    lows = pivot_lows(
        candles[-50:],
        2,
        2
    )

    if (
        ema20 > ema50
        and len(highs) >= 2
        and len(lows) >= 2
        and highs[-1][1] >= highs[-2][1]
        and lows[-1][1] >= lows[-2][1]
    ):
        return "LONG"

    if (
        ema20 < ema50
        and len(highs) >= 2
        and len(lows) >= 2
        and highs[-1][1] <= highs[-2][1]
        and lows[-1][1] <= lows[-2][1]
    ):
        return "SHORT"

    return "NEUTRAL"


# ============================================================
# STRUCTURE BIAS SCORE
# ============================================================

def structure_bias_score(
    candles_1h: List[Candle],
    candles_15m: List[Candle],
    candles_5m: List[Candle],
    direction: str
) -> Tuple[int, bool, str]:

    s1 = market_structure(
        candles_1h
    )

    s15 = market_structure(
        candles_15m
    )

    s5 = market_structure(
        candles_5m
    )

    score = 0

    if s1 == direction:
        score += 14

    if s15 == direction:
        score += 10

    if s5 == direction:
        score += 6

    # We do not require all TFs to agree.
    # A pre-breakout often has a neutral lower TF.
    valid = (
        s1 == direction
        and s15 in (
            direction,
            "NEUTRAL"
        )
    )

    reason = (
        f"Структура: 1H={s1}, "
        f"15M={s15}, 5M={s5}."
    )

    return (
        score,
        valid,
        reason
    )


# ============================================================
# APPROACH QUALITY
# ============================================================

def approach_quality(
    candles: List[Candle],
    level: float,
    direction: str
) -> Tuple[int, bool, str]:

    if len(candles) < 18:
        return 0, False, ""

    recent = candles[
        -18:
    ]

    score = 0

    closes = [
        c.close
        for c in recent
    ]

    if direction == "LONG":

        below = [
            c
            for c in recent
            if c.close < level
        ]

        if len(below) < 10:
            return 0, False, ""

        start_distance = (
            level
            - recent[0].close
        )

        end_distance = (
            level
            - recent[-1].close
        )

        if start_distance > 0 and end_distance > 0:

            if end_distance < start_distance:
                score += 10

            if (
                end_distance
                < start_distance * 0.65
            ):
                score += 5

        lows = [
            c.low
            for c in recent
        ]

        rising_count = 0

        for i in range(
            1,
            len(lows)
        ):

            if lows[i] > lows[i - 1]:
                rising_count += 1

        if rising_count >= 9:
            score += 8

        elif rising_count >= 7:
            score += 5

        closes_below = [
            c.close
            for c in recent
            if c.close < level
        ]

        if len(closes_below) >= 10:
            score += 5

        valid = (
            score >= 15
            and recent[-1].close < level
        )

        reason = (
            "Цена последовательно поджимается "
            "к сопротивлению, а минимумы повышаются."
        )

    else:

        above = [
            c
            for c in recent
            if c.close > level
        ]

        if len(above) < 10:
            return 0, False, ""

        start_distance = (
            recent[0].close
            - level
        )

        end_distance = (
            recent[-1].close
            - level
        )

        if start_distance > 0 and end_distance > 0:

            if end_distance < start_distance:
                score += 10

            if (
                end_distance
                < start_distance * 0.65
            ):
                score += 5

        highs = [
            c.high
            for c in recent
        ]

        falling_count = 0

        for i in range(
            1,
            len(highs)
        ):

            if highs[i] < highs[i - 1]:
                falling_count += 1

        if falling_count >= 9:
            score += 8

        elif falling_count >= 7:
            score += 5

        if len(above) >= 10:
            score += 5

        valid = (
            score >= 15
            and recent[-1].close > level
        )

        reason = (
            "Цена последовательно поджимается "
            "к поддержке, а максимумы снижаются."
        )

    return (
        score,
        valid,
        reason
    )


# ============================================================
# COMPRESSION
# ============================================================

def compression_metrics(
    candles: List[Candle]
) -> Tuple[
    float,
    float,
    float
]:

    if len(candles) < 30:
        return 0.0, 0.0, 0.0

    recent = candles[
        -24:
    ]

    first = recent[
        :8
    ]

    last = recent[
        -8:
    ]

    first_range = (
        max(
            c.high
            for c in first
        )
        - min(
            c.low
            for c in first
        )
    )

    last_range = (
        max(
            c.high
            for c in last
        )
        - min(
            c.low
            for c in last
        )
    )

    first_avg_body = median(
        [
            abs(
                c.close
                - c.open
            )
            for c in first
        ]
    )

    last_avg_body = median(
        [
            abs(
                c.close
                - c.open
            )
            for c in last
        ]
    )

    if first_range <= 0:
        range_compression = 0.0

    else:
        range_compression = clamp(
            (
                1.0
                - last_range
                / first_range
            ),
            -1.0,
            1.0
        )

    if first_avg_body <= 0:
        body_compression = 0.0

    else:
        body_compression = clamp(
            (
                1.0
                - last_avg_body
                / first_avg_body
            ),
            -1.0,
            1.0
        )

    widths = []

    for candle in recent:

        widths.append(
            candle.high
            - candle.low
        )

    if len(widths) >= 12:

        first_avg = (
            sum(
                widths[:6]
            )
            / 6
        )

        last_avg = (
            sum(
                widths[-6:]
            )
            / 6
        )

        if first_avg > 0:
            volatility_compression = clamp(
                (
                    1.0
                    - last_avg
                    / first_avg
                ),
                -1.0,
                1.0
            )
        else:
            volatility_compression = 0.0

    else:
        volatility_compression = 0.0

    return (
        range_compression,
        body_compression,
        volatility_compression
    )


def compression_score(
    candles: List[Candle],
    direction: str
) -> Tuple[
    int,
    bool,
    str
]:

    if len(candles) < 30:
        return 0, False, ""

    recent = candles[
        -24:
    ]

    score = 0

    (
        range_comp,
        body_comp,
        volatility_comp
    ) = compression_metrics(
        candles
    )

    if range_comp >= 0.10:
        score += 7

    if range_comp >= 0.18:
        score += 6

    if range_comp >= 0.28:
        score += 5

    if body_comp >= 0.10:
        score += 3

    if volatility_comp >= 0.12:
        score += 4

    highs = pivot_highs(
        recent,
        2,
        2
    )

    lows = pivot_lows(
        recent,
        2,
        2
    )

    directional_structure = False

    if direction == "LONG":

        if len(lows) >= 3:

            values = [
                x[1]
                for x in lows[-3:]
            ]

            if (
                values[0]
                < values[1]
                < values[2]
            ):

                directional_structure = True
                score += 10

        if len(highs) >= 2:

            last_highs = [
                x[1]
                for x in highs[-2:]
            ]

            # Resistance should not expand upward
            # aggressively before breakout.
            if (
                last_highs[-1]
                <= last_highs[0]
                * 1.002
            ):
                score += 4

    else:

        if len(highs) >= 3:

            values = [
                x[1]
                for x in highs[-3:]
            ]

            if (
                values[0]
                > values[1]
                > values[2]
            ):

                directional_structure = True
                score += 10

        if len(lows) >= 2:

            last_lows = [
                x[1]
                for x in lows[-2:]
            ]

            if (
                last_lows[-1]
                >= last_lows[0]
                * 0.998
            ):
                score += 4

    valid = (
        score >= MIN_COMPRESSION_SCORE
        and directional_structure
    )

    if direction == "LONG":

        reason = (
            "Формируется сжатие перед сопротивлением "
            "с повышающимися минимумами."
        )

    else:

        reason = (
            "Формируется сжатие перед поддержкой "
            "с понижающимися максимумами."
        )

    return (
        min(score, 35),
        valid,
        reason
    )


# ============================================================
# CONSOLIDATION / ACCUMULATION
# ============================================================

def consolidation_score(
    candles: List[Candle],
    level: float,
    direction: str
) -> Tuple[
    int,
    bool
]:

    if len(candles) < 20:
        return 0, False

    recent = candles[
        -20:
    ]

    ranges = [
        c.high - c.low
        for c in recent
        if c.high > c.low
    ]

    if len(ranges) < 15:
        return 0, False

    avg_range = (
        sum(ranges)
        / len(ranges)
    )

    if avg_range <= 0:
        return 0, False

    last_ranges = ranges[
        -6:
    ]

    last_avg = (
        sum(last_ranges)
        / len(last_ranges)
    )

    score = 0

    if last_avg < avg_range:
        score += 6

    if last_avg < avg_range * 0.80:
        score += 5

    if last_avg < avg_range * 0.65:
        score += 4

    near_level = 0

    for candle in recent:

        if direction == "LONG":

            distance = (
                level
                - candle.close
            ) / level * 100

        else:

            distance = (
                candle.close
                - level
            ) / level * 100

        if 0 <= distance <= 0.75:
            near_level += 1

    if near_level >= 8:
        score += 6

    if near_level >= 12:
        score += 4

    return (
        min(score, 25),
        score >= 12
    )


# ============================================================
# PRE-BREAKOUT CANDLE PRESSURE
# ============================================================

def prebreakout_candle_quality(
    candles: List[Candle],
    level: float,
    direction: str
) -> Tuple[
    int,
    bool,
    str
]:

    if len(candles) < 6:
        return 0, False, ""

    recent = candles[
        -6:
    ]

    score = 0

    if direction == "LONG":

        bullish = sum(
            1
            for c in recent
            if c.close > c.open
        )

        if bullish >= 3:
            score += 5

        if bullish >= 4:
            score += 3

        if recent[-1].close > recent[-3].close:
            score += 4

        if recent[-1].low > recent[-4].low:
            score += 5

        distance = (
            level
            - recent[-1].close
        ) / level * 100

        if (
            0.05
            <= distance
            <= 0.55
        ):
            score += 5

        valid = (
            score >= 13
            and recent[-1].close < level
        )

        reason = (
            "5M подтверждает постепенное давление "
            "покупателей перед уровнем."
        )

    else:

        bearish = sum(
            1
            for c in recent
            if c.close < c.open
        )

        if bearish >= 3:
            score += 5

        if bearish >= 4:
            score += 3

        if recent[-1].close < recent[-3].close:
            score += 4

        if recent[-1].high < recent[-4].high:
            score += 5

        distance = (
            recent[-1].close
            - level
        ) / level * 100

        if (
            0.05
            <= distance
            <= 0.55
        ):
            score += 5

        valid = (
            score >= 13
            and recent[-1].close > level
        )

        reason = (
            "5M подтверждает постепенное давление "
            "продавцов перед уровнем."
        )

    return (
        score,
        valid,
        reason
    )


# ============================================================
# BREAKOUT DETECTION
# ============================================================

def detect_real_breakout(
    candles: List[Candle],
    level: float,
    direction: str
) -> Tuple[
    bool,
    int,
    str
]:

    if len(candles) < 3:
        return False, 0, ""

    current = candles[-1]
    previous = candles[-2]

    body = abs(
        current.close
        - current.open
    )

    candle_range = (
        current.high
        - current.low
    )

    if candle_range <= 0:
        return False, 0, ""

    body_ratio = (
        body
        / candle_range
    )

    if direction == "LONG":

        crossed = (
            previous.close <= level
            and current.close > level
        )

        if not crossed:
            return False, 0, ""

        close_distance = (
            current.close
            - level
        ) / level * 100

        upper_wick = (
            current.high
            - current.close
        )

        if body_ratio < 0.45:
            return False, 0, ""

        if upper_wick > body * 1.5:
            return False, 0, ""

        if close_distance > MAX_CHASE_PCT:
            return False, 0, ""

        return (
            True,
            25,
            "5M подтверждает пробой сопротивления."
        )

    crossed = (
        previous.close >= level
        and current.close < level
    )

    if not crossed:
        return False, 0, ""

    close_distance = (
        level
        - current.close
    ) / level * 100

    lower_wick = (
        current.close
        - current.low
    )

    if body_ratio < 0.45:
        return False, 0, ""

    if lower_wick > body * 1.5:
        return False, 0, ""

    if close_distance > MAX_CHASE_PCT:
        return False, 0, ""

    return (
        True,
        25,
        "5M подтверждает пробой поддержки."
    )


# ============================================================
# BREAKOUT RETEST CONTEXT
# ============================================================

def detect_retest_context(
    candles: List[Candle],
    level: float,
    direction: str
) -> Tuple[
    bool,
    int
]:

    if len(candles) < 8:
        return False, 0

    recent = candles[
        -8:
    ]

    tolerance = (
        level
        * 0.0015
    )

    if direction == "LONG":

        breakout_index = None

        for i in range(
            1,
            len(recent)
        ):

            if (
                recent[i - 1].close
                <= level
                and recent[i].close
                > level
            ):

                breakout_index = i

        if breakout_index is None:
            return False, 0

        after = recent[
            breakout_index + 1:
        ]

        if not after:
            return False, 0

        touched = any(
            abs(
                c.low
                - level
            ) <= tolerance
            for c in after
        )

        held = all(
            c.close >= level * 0.998
            for c in after[-3:]
        )

        if touched and held:
            return True, 14

    else:

        breakout_index = None

        for i in range(
            1,
            len(recent)
        ):

            if (
                recent[i - 1].close
                >= level
                and recent[i].close
                < level
            ):

                breakout_index = i

        if breakout_index is None:
            return False, 0

        after = recent[
            breakout_index + 1:
        ]

        if not after:
            return False, 0

        touched = any(
            abs(
                c.high
                - level
            ) <= tolerance
            for c in after
        )

        held = all(
            c.close <= level * 1.002
            for c in after[-3:]
        )

        if touched and held:
            return True, 14

    return False, 0


# ============================================================
# MOMENTUM
# ============================================================

def detect_momentum(
    candles: List[Candle],
    direction: str
) -> Tuple[
    bool,
    int,
    str
]:

    if len(candles) < 30:
        return False, 0, ""

    closes = [
        c.close
        for c in candles
    ]

    ema9 = ema(
        closes,
        9
    )[-1]

    ema21 = ema(
        closes,
        21
    )[-1]

    current = candles[-1]

    window = candles[
        -13:-1
    ]

    if direction == "LONG":

        previous_high = max(
            c.high
            for c in window
        )

        valid = (
            ema9 > ema21
            and current.close > previous_high
        )

    else:

        previous_low = min(
            c.low
            for c in window
        )

        valid = (
            ema9 < ema21
            and current.close < previous_low
        )

    if not valid:
        return False, 0, ""

    return (
        True,
        8,
        "5M momentum подтверждает направление."
    )


# ============================================================
# BTC CORRELATION
# ============================================================

def get_btc_candles_cached() -> List[Candle]:

    current = now_ts()

    if (
        btc_cache["candles_5m"]
        and current
        - btc_cache["timestamp"]
        < 45
    ):

        return btc_cache[
            "candles_5m"
        ]

    try:

        candles = get_candles(
            "BTC-USDT-SWAP",
            "5m",
            100
        )

        btc_cache[
            "candles_5m"
        ] = candles

        btc_cache[
            "timestamp"
        ] = current

        return candles

    except Exception:

        log.exception(
            "BTC CACHE UPDATE FAILED"
        )

        return btc_cache[
            "candles_5m"
        ]


def btc_correlation(
    candles_5m: List[Candle]
) -> float:

    btc = get_btc_candles_cached()

    if len(btc) < 20:
        return 0.0

    return correlation(
        returns(
            candles_5m
        ),
        returns(
            btc
        )
    )


# ============================================================
# EXECUTION QUALITY
# ============================================================

def execution_quality(
    current: float,
    level: float,
    atr_value: float,
    direction: str,
    formation_score: int,
    compression_ok: bool,
    approach_ok: bool,
    trendline_ok: bool,
    candle_ok: bool,
    v_ratio: float
) -> Tuple[
    int,
    bool,
    str
]:

    if atr_value <= 0:
        return 0, False, ""

    if direction == "LONG":

        distance = (
            level
            - current
        )

    else:

        distance = (
            current
            - level
        )

    if distance <= 0:
        return 0, False, ""

    distance_pct = (
        distance
        / current
        * 100.0
    )

    distance_atr = (
        distance
        / atr_value
    )

    score = 0

    # The signal should be early enough
    # for the channel, but not so early that
    # the level is irrelevant.
    if (
        MIN_ENTRY_DISTANCE_ATR
        <= distance_atr
        <= MAX_ENTRY_DISTANCE_ATR
    ):
        score += 18

    elif (
        distance_atr
        < MIN_ENTRY_DISTANCE_ATR
    ):
        score += 4

    else:
        score -= 8

    if (
        MIN_PREBREAK_DISTANCE_PCT
        <= distance_pct
        <= MAX_PREBREAK_DISTANCE_PCT
    ):
        score += 10

    if compression_ok:
        score += 10

    if approach_ok:
        score += 8

    if trendline_ok:
        score += 8

    if candle_ok:
        score += 6

    # Normal volume is acceptable before breakout.
    if v_ratio >= 0.80:
        score += 5

    if v_ratio >= 1.10:
        score += 4

    if formation_score >= 70:
        score += 10

    score = int(
        clamp(
            score,
            0,
            100
        )
    )

    valid = (
        score
        >= MIN_EXECUTION_SCORE
        and distance > 0
        and distance_pct
        <= MAX_PREBREAK_DISTANCE_PCT
    )

    reason = (
        f"Execution distance: "
        f"{distance_pct:.2f}% / "
        f"{distance_atr:.2f} ATR."
    )

    return (
        score,
        valid,
        reason
    )


# ============================================================
# STRUCTURAL INVALIDATION
# ============================================================

def setup_still_valid(
    setup: Setup,
    candles_15m: List[Candle],
    candles_5m: List[Candle],
    current_price: float
) -> Tuple[
    bool,
    str
]:

    if len(candles_15m) < 20:
        return False, "Недостаточно 15M данных."

    if len(candles_5m) < 10:
        return False, "Недостаточно 5M данных."

    level = setup.level

    # --------------------------------------------------------
    # PRICE CHASE
    # --------------------------------------------------------

    if setup.direction == "LONG":

        if current_price > (
            level
            * (
                1
                + MAX_CHASE_PCT / 100.0
            )
        ):

            return (
                False,
                "Цена ушла слишком далеко после уровня."
            )

    else:

        if current_price < (
            level
            * (
                1
                - MAX_CHASE_PCT / 100.0
            )
        ):

            return (
                False,
                "Цена ушла слишком далеко после уровня."
            )

    # --------------------------------------------------------
    # STRUCTURAL BREAK
    # --------------------------------------------------------

    recent = candles_15m[
        -12:
    ]

    if setup.direction == "LONG":

        # Several closes above level before actual
        # activation means the old pre-breakout setup
        # is no longer the same setup.
        closes_above = sum(
            1
            for c in recent
            if c.close > level
        )

        if closes_above >= 3:
            return (
                False,
                "Структура пробоя уже сформировалась."
            )

        lows = [
            c.low
            for c in recent
        ]

        if len(lows) >= 5:

            lower_count = sum(
                1
                for i in range(
                    len(lows) - 4,
                    len(lows)
                )
                if lows[i] < lows[i - 1]
            )

            if lower_count >= 3:
                return (
                    False,
                    "Восходящая структура разрушена."
                )

    else:

        closes_below = sum(
            1
            for c in recent
            if c.close < level
        )

        if closes_below >= 3:
            return (
                False,
                "Структура пробоя уже сформировалась."
            )

        highs = [
            c.high
            for c in recent
        ]

        if len(highs) >= 5:

            higher_count = sum(
                1
                for i in range(
                    len(highs) - 4,
                    len(highs)
                )
                if highs[i] > highs[i - 1]
            )

            if higher_count >= 3:
                return (
                    False,
                    "Нисходящая структура разрушена."
                )

    return True, ""


# ============================================================
# ENTRY ZONE
# ============================================================

def build_entry_zone(
    level: float,
    atr_value: float,
    direction: str
) -> Tuple[
    float,
    float
]:

    # Entry is centered close to the breakout
    # but leaves a small execution band.
    zone = clamp(
        atr_value * 0.22,
        level * 0.0005,
        level * 0.0018
    )

    if direction == "LONG":

        low = level
        high = (
            level
            + zone
        )

    else:

        low = (
            level
            - zone
        )
        high = level

    return (
        low,
        high
    )


# ============================================================
# STOP LOSS
# ============================================================

def build_stop_loss(
    candles_15m: List[Candle],
    level: float,
    atr_value: float,
    direction: str
) -> Optional[float]:

    recent = candles_15m[
        -24:
    ]

    if len(recent) < 12:
        return None

    if direction == "LONG":

        swing_lows = pivot_lows(
            recent,
            2,
            2
        )

        candidates = [
            value
            for _, value in swing_lows
            if value < level
        ]

        if candidates:

            structural_low = max(
                candidates[
                    -4:
                ]
            )

        else:

            structural_low = min(
                c.low
                for c in recent
            )

        sl = (
            structural_low
            - atr_value * 0.25
        )

        if sl >= level:
            return None

        return sl

    swing_highs = pivot_highs(
        recent,
        2,
        2
    )

    candidates = [
        value
        for _, value in swing_highs
        if value > level
    ]

    if candidates:

        structural_high = min(
            candidates[
                -4:
            ]
        )

    else:

        structural_high = max(
            c.high
            for c in recent
        )

    sl = (
        structural_high
        + atr_value * 0.25
    )

    if sl <= level:
        return None

    return sl


# ============================================================
# TAKE PROFITS
# ============================================================

def build_take_profits(
    level: float,
    sl: float,
    direction: str
) -> Tuple[
    float,
    float,
    float
]:

    risk = abs(
        level
        - sl
    )

    if direction == "LONG":

        return (
            level + risk * 1.0,
            level + risk * 2.0,
            level + risk * 3.0
        )

    return (
        level - risk * 1.0,
        level - risk * 2.0,
        level - risk * 3.0
    )


# ============================================================
# OBSTACLE AFTER BREAKOUT
# ============================================================

def next_opposite_level(
    current: float,
    direction: str,
    candles_15m: List[Candle],
    candles_1h: List[Candle]
) -> Optional[float]:

    if direction == "LONG":

        highs = pivot_highs(
            candles_1h,
            2,
            2
        )

        levels = [
            price
            for _, price in highs
            if price > current
        ]

    else:

        lows = pivot_lows(
            candles_1h,
            2,
            2
        )

        levels = [
            price
            for _, price in lows
            if price < current
        ]

    if not levels:
        return None

    if direction == "LONG":

        return min(
            levels
        )

    return max(
        levels
    )


# ============================================================
# ANALYZE SYMBOL
# ============================================================

def analyze_symbol(
    inst_id: str,
    ticker: dict,
    candles_4h: List[Candle],
    candles_1h: List[Candle],
    candles_15m: List[Candle],
    candles_5m: List[Candle]
) -> Optional[Setup]:

    if len(candles_4h) < 50:
        return None

    if len(candles_1h) < 70:
        return None

    if len(candles_15m) < 70:
        return None

    if len(candles_5m) < 70:
        return None

    confirmed_5m = [
        c
        for c in candles_5m
        if c.confirmed
    ]

    confirmed_15m = [
        c
        for c in candles_15m
        if c.confirmed
    ]

    confirmed_1h = [
        c
        for c in candles_1h
        if c.confirmed
    ]

    confirmed_4h = [
        c
        for c in candles_4h
        if c.confirmed
    ]

    if len(confirmed_5m) < 40:
        return None

    current = float(
        ticker["last"]
    )

    if current <= 0:
        return None

    # --------------------------------------------------------
    # DIRECTION
    # --------------------------------------------------------

    bias_1h = market_structure(
        confirmed_1h
    )

    bias_4h = market_structure(
        confirmed_4h
    )

    possible_directions = []

    if bias_1h in (
        "LONG",
        "SHORT"
    ):
        possible_directions.append(
            bias_1h
        )

    elif bias_4h in (
        "LONG",
        "SHORT"
    ):
        possible_directions.append(
            bias_4h
        )

    # If both are neutral there is no directional edge.
    if not possible_directions:
        return None

    direction = possible_directions[0]

    # Stronger 4H opposing structure is a warning.
    if (
        bias_4h in (
            "LONG",
            "SHORT"
        )
        and bias_4h != direction
    ):
        return None

    # --------------------------------------------------------
    # LEVEL
    # --------------------------------------------------------

    level_candidate = find_best_horizontal_level(
        confirmed_15m,
        confirmed_1h,
        confirmed_4h,
        current,
        direction
    )

    if level_candidate is None:
        return None

    level = level_candidate.price

    distance_pct = abs(
        pct(
            current,
            level
        )
    )

    if (
        distance_pct
        < MIN_PREBREAK_DISTANCE_PCT
    ):
        return None

    if (
        distance_pct
        > MAX_PREBREAK_DISTANCE_PCT
    ):
        return None

    if level_candidate.touches < 2:
        return None

    # --------------------------------------------------------
    # ATR / NATR
    # --------------------------------------------------------

    atr_value = atr(
        confirmed_5m,
        14
    )

    if atr_value <= 0:
        return None

    natr_pct = natr(
        confirmed_5m,
        14
    )

    if natr_pct < 0.03:
        return None

    if natr_pct > 2.5:
        return None

    # --------------------------------------------------------
    # MULTI-TF STRUCTURE
    # --------------------------------------------------------

    (
        structure_points,
        structure_ok,
        structure_reason
    ) = structure_bias_score(
        confirmed_1h,
        confirmed_15m,
        confirmed_5m,
        direction
    )

    if not structure_ok:
        return None

    # --------------------------------------------------------
    # APPROACH
    # --------------------------------------------------------

    (
        approach_points,
        approach_ok,
        approach_reason
    ) = approach_quality(
        confirmed_15m,
        level,
        direction
    )

    if not approach_ok:
        return None

    # --------------------------------------------------------
    # COMPRESSION
    # --------------------------------------------------------

    (
        compression_points,
        compression_ok,
        compression_reason
    ) = compression_score(
        confirmed_15m,
        direction
    )

    if not compression_ok:
        return None

    # --------------------------------------------------------
    # TRENDLINE
    # --------------------------------------------------------

    (
        trendline,
        trendline_points,
        trendline_ok
    ) = trendline_pressure(
        confirmed_15m,
        direction,
        "15M"
    )

    # --------------------------------------------------------
    # CONSOLIDATION
    # --------------------------------------------------------

    (
        consolidation_points,
        consolidation_ok
    ) = consolidation_score(
        confirmed_15m,
        level,
        direction
    )

    if not consolidation_ok:
        return None

    # --------------------------------------------------------
    # 5M PRESSURE
    # --------------------------------------------------------

    (
        candle_points,
        candle_ok,
        candle_reason
    ) = prebreakout_candle_quality(
        confirmed_5m,
        level,
        direction
    )

    if not candle_ok:
        return None

    # --------------------------------------------------------
    # BREAKOUT
    # --------------------------------------------------------

    (
        breakout_ok,
        breakout_points,
        breakout_reason
    ) = detect_real_breakout(
        confirmed_5m,
        level,
        direction
    )

    # --------------------------------------------------------
    # RETEST
    # --------------------------------------------------------

    (
        retest_ok,
        retest_points
    ) = detect_retest_context(
        confirmed_5m,
        level,
        direction
    )

    # --------------------------------------------------------
    # MOMENTUM
    # --------------------------------------------------------

    (
        momentum_ok,
        momentum_points,
        momentum_reason
    ) = detect_momentum(
        confirmed_5m,
        direction
    )

    # --------------------------------------------------------
    # VOLUME
    # --------------------------------------------------------

    v_ratio = volume_ratio(
        confirmed_5m,
        20
    )

    if v_ratio < 0.70:
        return None

    # --------------------------------------------------------
    # BTC CORRELATION
    # --------------------------------------------------------

    btc_corr = btc_correlation(
        confirmed_5m
    )

    # --------------------------------------------------------
    # OI
    # --------------------------------------------------------

    oi_value = get_open_interest(
        inst_id
    )

    if oi_value is not None:
        oi_status = "AVAILABLE"
    else:
        oi_status = "N/A"

    # --------------------------------------------------------
    # FUNDING
    # --------------------------------------------------------

    funding = get_funding_rate(
        inst_id
    )

    if funding is None:
        funding_status = "N/A"

    elif abs(funding) < 0.0005:
        funding_status = "NEUTRAL"

    elif funding > 0:
        funding_status = "POSITIVE"

    else:
        funding_status = "NEGATIVE"

    # --------------------------------------------------------
    # FORMATION SCORE
    # --------------------------------------------------------

    formation_score = 0

    formation_score += min(
        structure_points,
        25
    )

    formation_score += min(
        level_candidate.strength,
        30
    )

    formation_score += min(
        approach_points,
        20
    )

    formation_score += min(
        compression_points,
        30
    )

    formation_score += min(
        consolidation_points,
        18
    )

    if trendline_ok:
        formation_score += min(
            trendline_points,
            30
        )

    if candle_ok:
        formation_score += min(
            candle_points,
            15
        )

    if retest_ok:
        formation_score += retest_points

    if momentum_ok:
        formation_score += momentum_points

    if v_ratio >= 1.0:
        formation_score += 4

    if v_ratio >= 1.25:
        formation_score += 5

    if v_ratio >= 1.50:
        formation_score += 5

    if oi_value is not None:
        formation_score += 2

    # BTC correlation is contextual, not mandatory.
    if (
        direction == "LONG"
        and btc_corr >= 0.30
    ):
        formation_score += 3

    elif (
        direction == "SHORT"
        and btc_corr >= 0.30
    ):
        formation_score += 3

    formation_score = int(
        clamp(
            formation_score,
            0,
            100
        )
    )

    # --------------------------------------------------------
    # PRE-BREAKOUT EXECUTION SCORE
    # --------------------------------------------------------

    (
        execution_score,
        execution_ok,
        execution_reason
    ) = execution_quality(
        current,
        level,
        atr_value,
        direction,
        formation_score,
        compression_ok,
        approach_ok,
        trendline_ok,
        candle_ok,
        v_ratio
    )

    if not execution_ok:
        return None

    # --------------------------------------------------------
    # DO NOT PUBLISH WEAK FORMATIONS
    # --------------------------------------------------------

    if formation_score < MIN_SCORE:
        return None

    # --------------------------------------------------------
    # PRE-BREAKOUT MODE
    #
    # If the market has already broken the level,
    # we do NOT send the old "READY" setup.
    # The channel is for preparation before the move.
    # --------------------------------------------------------

    if breakout_ok:
        return None

    # --------------------------------------------------------
    # CURRENT PRICE MUST STILL BE BEFORE LEVEL
    # --------------------------------------------------------

    if direction == "LONG":

        if current >= level:
            return None

    else:

        if current <= level:
            return None

    # --------------------------------------------------------
    # ENTRY ZONE
    # --------------------------------------------------------

    (
        entry_low,
        entry_high
    ) = build_entry_zone(
        level,
        atr_value,
        direction
    )

    # Current price should be before entry.
    # If it is already inside/through the entry,
    # the signal is too late.
    if direction == "LONG":

        if current >= entry_low:
            return None

    else:

        if current <= entry_high:
            return None

    # --------------------------------------------------------
    # STOP LOSS
    # --------------------------------------------------------

    sl = build_stop_loss(
        confirmed_15m,
        level,
        atr_value,
        direction
    )

    if sl is None:
        return None

    risk = abs(
        level
        - sl
    )

    if risk <= 0:
        return None

    risk_pct = (
        risk
        / level
        * 100.0
    )

    if risk_pct < 0.15:
        return None

    if risk_pct > 1.80:
        return None

    # --------------------------------------------------------
    # TAKE PROFITS
    # --------------------------------------------------------

    (
        tp1,
        tp2,
        tp3
    ) = build_take_profits(
        level,
        sl,
        direction
    )

    # --------------------------------------------------------
    # OBSTACLE PROTECTION
    # --------------------------------------------------------

    obstacle = next_opposite_level(
        level,
        direction,
        confirmed_15m,
        confirmed_1h
    )

    if obstacle is not None:

        if direction == "LONG":

            available = (
                obstacle
                - level
            )

        else:

            available = (
                level
                - obstacle
            )

        if available > 0:

            required = (
                tp1
                - level
                if direction == "LONG"
                else
                level
                - tp1
            )

            if available < required * 0.75:
                return None

    # --------------------------------------------------------
    # STRATEGY
    # --------------------------------------------------------

    if trendline_ok:

        strategy = (
            "Trendline Compression Breakout"
        )

        strategy_reason = (
            f"{compression_reason} "
            f"Наклонная подтверждена "
            f"{trendline.touches} касаниями."
        )

    else:

        strategy = (
            "Horizontal Level Compression Breakout"
        )

        strategy_reason = (
            f"{compression_reason} "
            f"Уровень подтверждён "
            f"{level_candidate.touches} реакциями."
        )

    final_reason = (
        f"{strategy_reason} "
        f"{approach_reason} "
        f"{structure_reason} "
        f"{execution_reason}"
    )

    # --------------------------------------------------------
    # LIQUIDITY
    # --------------------------------------------------------

    volume_24h = float(
        ticker["vol24h_usd"]
    )

    if volume_24h < MIN_24H_VOLUME_USD:
        return None

    if volume_24h >= 1_000_000_000:

        liquidity = "HIGH"
        formation_score += 5

    elif volume_24h >= 250_000_000:

        liquidity = "GOOD"
        formation_score += 3

    else:

        liquidity = "MEDIUM"

    formation_score = int(
        clamp(
            formation_score,
            0,
            100
        )
    )

    if formation_score < MIN_SCORE:
        return None

    # --------------------------------------------------------
    # FINAL SCORE
    # --------------------------------------------------------

    final_score = int(
        round(
            formation_score * 0.70
            + execution_score * 0.30
        )
    )

    final_score = int(
        clamp(
            final_score,
            0,
            100
        )
    )

    if final_score < MIN_SCORE:
        return None

    # Prevent suspicious perfect scores.
    # A 100 should require extremely strong confluence.
    if final_score >= 100:

        if not (
            trendline_ok
            and level_candidate.touches >= 4
            and compression_points >= 25
            and execution_score >= 90
        ):
            final_score = 99

    # --------------------------------------------------------
    # RETURN SETUP
    # --------------------------------------------------------

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

        score=final_score,
        execution_score=execution_score,

        liquidity=liquidity,
        volume_grade=grade_volume(
            v_ratio
        ),
        oi_status=oi_status,
        funding_status=funding_status,
        btc_correlation=btc_corr,

        level_tf=level_candidate.tf,

        reason=final_reason,

        volume_24h=volume_24h,
        breakout_volume_ratio=v_ratio,

        atr_pct=(
            atr_value
            / current
            * 100.0
        ),

        natr_pct=natr_pct,

        trendline=trendline,

        structure_state=(
            f"1H={bias_1h} "
            f"| 4H={bias_4h}"
        ),

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
        (inst_id,)
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

    if len(
        signals_hour
    ) >= MAX_SIGNALS_PER_HOUR:
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
        figsize=(12, 7),
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

    # --------------------------------------------------------
    # LEVEL
    # --------------------------------------------------------

    ax.axhline(
        setup.level,
        color="#f5c542",
        linewidth=2.2,
        linestyle="--",
        label="BREAKOUT LEVEL"
    )

    # --------------------------------------------------------
    # ENTRY ZONE
    # --------------------------------------------------------

    ax.axhspan(
        setup.entry_low,
        setup.entry_high,
        color="#00aaff",
        alpha=0.12
    )

    # --------------------------------------------------------
    # SL
    # --------------------------------------------------------

    ax.axhline(
        setup.sl,
        color="#ff3b30",
        linewidth=1.7,
        linestyle="-.",
        label="SL"
    )

    # --------------------------------------------------------
    # TP
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # TRENDLINE
    # --------------------------------------------------------

    if setup.trendline is not None:

        line = setup.trendline

        x1 = 0
        x2 = len(candles) - 1

        y1 = (
            line.slope * x1
            + line.intercept
        )

        y2 = (
            line.slope * x2
            + line.intercept
        )

        ax.plot(
            [x1, x2],
            [y1, y2],
            color="#00e5ff",
            linewidth=2.0,
            alpha=0.90,
            label="TRENDLINE"
        )

    # --------------------------------------------------------
    # LABELS
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # TITLE
    # --------------------------------------------------------

    ax.set_title(
        (
            f"{setup.coin}USDT | "
            f"{setup.direction} | "
            f"{setup.strategy}\n"
            f"Score {setup.score}/100 | "
            f"Execution {setup.execution_score}/100 | "
            f"5M"
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
            "Формация находится перед уровнем. "
            "Цена поджимается к зоне возможного пробоя."
        )

    elif state == "ACTIVE":

        state_line = (
            "🟢 *ENTRY ACTIVE*\n"
            "Уровень пробит. "
            "Рабочая зона активна."
        )

    else:

        state_line = state

    risk = (
        abs(
            setup.level
            - setup.sl
        )
        / setup.level
        * 100
    )

    volume_m = (
        setup.volume_24h
        / 1_000_000
    )

    corr_text = (
        f"{setup.btc_correlation:.2f}"
    )

    trendline_text = (
        "YES"
        if setup.trendline is not None
        else "NO"
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

        f"📐 *Trendline:* "
        f"`{trendline_text}`\n"

        f"📦 *NATR:* "
        f"`{setup.natr_pct:.2f}%`\n"

        f"🧭 *BTC correlation:* "
        f"`{corr_text}`\n"

        f"💧 *Ликвидность:* "
        f"`{setup.liquidity}`\n"

        f"📦 *Volume grade:* "
        f"`{setup.volume_grade}`\n"

        f"⚡ *OI:* "
        f"`{setup.oi_status}`\n"

        f"💵 *Funding:* "
        f"`{setup.funding_status}`\n\n"

        f"⭐ *SIGNAL SCORE:* "
        f"`{setup.score}/100` "
        f"{label}\n"

        f"🎯 *EXECUTION SCORE:* "
        f"`{setup.execution_score}/100`\n\n"

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

            sent_photo = bot.send_photo(
                CHANNEL_ID,
                photo,
                caption=caption,
                parse_mode="Markdown",
                show_caption_above_media=True
            )

        text = build_signal_text(
            setup,
            state
        )

        sent_text = bot.send_message(
            CHANNEL_ID,
            text,
            parse_mode="Markdown"
        )

        log.info(
            "TELEGRAM SENT | %s | %s | score=%s execution=%s",
            setup.coin,
            setup.direction,
            setup.score,
            setup.execution_score
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
            ?, ?, ?, ?, ?, NULL
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
            created
        )
    )

    db.commit()


# ============================================================
# INVALIDATE READY
# ============================================================

def invalidate_ready(
    inst_id: str,
    reason: str
):

    ready = ready_setups.pop(
        inst_id,
        None
    )

    if ready is None:
        return

    setup = ready.setup

    log.info(
        "READY INVALIDATED | %s | %s",
        inst_id,
        reason
    )

    db.execute(
        """
        UPDATE signals
        SET status = 'INVALIDATED'
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
            inst_id,
        )
    )

    db.commit()

    try:

        bot.send_message(
            CHANNEL_ID,
            (
                f"🔴 *SETUP INVALIDATED — "
                f"{setup.coin}USDT*\n\n"
                f"{reason}\n\n"
                f"*Рынок не догоняем.*"
            ),
            parse_mode="Markdown"
        )

    except Exception:

        log.exception(
            "INVALIDATION TELEGRAM ERROR"
        )


# ============================================================
# CHECK READY STRUCTURE
# ============================================================

def monitor_ready_setup(
    inst_id: str,
    ticker: dict
):

    ready = ready_setups.get(
        inst_id
    )

    if ready is None:
        return

    setup = ready.setup

    current_price = float(
        ticker["last"]
    )

    try:

        candles_15m = get_candles(
            inst_id,
            "15m",
            60
        )

        candles_5m = get_candles(
            inst_id,
            "5m",
            60
        )

        confirmed_15m = [
            c
            for c in candles_15m
            if c.confirmed
        ]

        confirmed_5m = [
            c
            for c in candles_5m
            if c.confirmed
        ]

        valid, reason = setup_still_valid(
            setup,
            confirmed_15m,
            confirmed_5m,
            current_price
        )

        if not valid:

            invalidate_ready(
                inst_id,
                reason
            )

            return

        check_activation(
            inst_id,
            current_price
        )

    except Exception:

        log.exception(
            "READY MONITOR ERROR | %s",
            inst_id
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
            setup.level
            * (
                1
                + MAX_CHASE_PCT / 100.0
            )
        ):

            invalidate_ready(
                inst_id,
                "Цена пробила уровень слишком далеко для безопасного входа."
            )

            return

    else:

        if current_price <= setup.level:
            triggered = True

        if current_price < (
            setup.level
            * (
                1
                - MAX_CHASE_PCT / 100.0
            )
        ):

            invalidate_ready(
                inst_id,
                "Цена пробила уровень слишком далеко для безопасного входа."
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
        "🚀 *QUANTUM SCALPER V5 ONLINE*\n\n"

        "OKX: 🟢\n"
        "Telegram: 🟢\n"
        "Scanner: 🟢\n\n"

        "🧠 *SEARCH ENGINE*\n"
        "• Multi-TF Horizontal Levels\n"
        "• Trendline Detection\n"
        "• Density Clustering\n"
        "• Daily High / Low\n"
        "• Round Levels\n"
        "• Compression\n"
        "• Accumulation\n"
        "• Volume / NATR\n"
        "• OI / Funding\n"
        "• BTC Correlation\n"
        "• Pre-Breakout Execution Filter\n\n"

        f"💧 Minimum 24H turnover: "
        f"`${MIN_24H_VOLUME_USD / 1_000_000:.0f}M`\n"

        f"⭐ Minimum Score: "
        f"`{MIN_SCORE}/100`\n"

        f"🎯 Minimum Execution Score: "
        f"`{MIN_EXECUTION_SCORE}/100`\n"

        f"🔒 Cooldown: "
        f"`{COOLDOWN_MINUTES} min`\n"

        f"📊 Max symbols: "
        f"`{MAX_SYMBOLS}`\n\n"

        "*NO READY TIMER*\n"
        "*READY остаётся активным, пока структура действительна.*\n\n"

        "*PRE-BREAKOUT MODE ACTIVE*\n"
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
    # MONITOR EXISTING READY
    # --------------------------------------------------------

    for inst_id in list(
        ready_setups.keys()
    ):

        ticker = tickers.get(
            inst_id
        )

        if ticker is None:
            continue

        try:

            monitor_ready_setup(
                inst_id,
                ticker
            )

        except Exception:

            log.exception(
                "READY MONITOR FAILED | %s",
                inst_id
            )

    # --------------------------------------------------------
    # LIQUIDITY
    # --------------------------------------------------------

    liquid = []

    for inst_id, data in tickers.items():

        volume_usd = float(
            data.get(
                "vol24h_usd",
                0
            )
        )

        if (
            volume_usd
            >= MIN_24H_VOLUME_USD
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
        "liquid=%s | selected=%s",
        len(tickers),
        len(liquid),
        len(selected)
    )

    # --------------------------------------------------------
    # ANALYSIS
    # --------------------------------------------------------

    for inst_id, ticker in selected:

        try:

            current_price = float(
                ticker["last"]
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

            candles_4h = get_candles(
                inst_id,
                "4H",
                100
            )

            candles_1h = get_candles(
                inst_id,
                "1H",
                120
            )

            candles_15m = get_candles(
                inst_id,
                "15m",
                120
            )

            candles_5m = get_candles(
                inst_id,
                "5m",
                120
            )

            setup = analyze_symbol(
                inst_id,
                ticker,
                candles_4h,
                candles_1h,
                candles_15m,
                candles_5m
            )

            if setup is None:
                continue

            log.info(
                "CANDIDATE | %s | %s | "
                "%s | score=%s | execution=%s | "
                "level=%s | distance=%.3f%% | "
                "volume=$%.1fM | BTCcorr=%.2f",
                setup.coin,
                setup.direction,
                setup.strategy,
                setup.score,
                setup.execution_score,
                fmt_price(setup.level),
                abs(
                    pct(
                        setup.current_price,
                        setup.level
                    )
                ),
                setup.volume_24h
                / 1_000_000,
                setup.btc_correlation
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
                "score=%s | execution=%s | "
                "strategy=%s",
                setup.coin,
                setup.score,
                setup.execution_score,
                setup.strategy
            )

            time.sleep(
                1.5
            )

        except Exception as exc:

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
        "QUANTUM SCALPER V5 STARTING"
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
        "MIN_EXECUTION_SCORE=%s",
        MIN_EXECUTION_SCORE
    )

    log.info(
        "COOLDOWN=%s min",
        COOLDOWN_MINUTES
    )

    log.info(
        "PREBREAK_DISTANCE=%s%%",
        MAX_PREBREAK_DISTANCE_PCT
    )

    log.info(
        "SCAN_INTERVAL=%ss",
        SCAN_INTERVAL_SECONDS
    )

    log.info(
        "READY TIMER=DISABLED"
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
            )
            >= MIN_24H_VOLUME_USD
        )

        log.info(
            "OKX LIQUID: %s",
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
