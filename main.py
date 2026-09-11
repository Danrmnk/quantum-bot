import os
import time
import json
import math
import logging
import sqlite3
from dataclasses import dataclass, field
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
# TOP SIGNAL ENGINE
#
# STRATEGIES:
#
# 1. HORIZONTAL BREAKOUT
# 2. DIAGONAL / TRENDLINE BREAKOUT
# 3. COMPRESSION EXPANSION
# 4. LIQUIDITY SWEEP REVERSAL
#
# ENGINE:
#
# ALL USDT SWAPS
#       ↓
# LIQUIDITY FILTER
#       ↓
# FAST MARKET RANKING
#       ↓
# MARKET REGIME
#       ↓
# MULTI-TIMEFRAME LEVEL ENGINE
#       ↓
# LEVEL AGE / FRESHNESS
#       ↓
# STRATEGY ENGINE
#       ↓
# VOLUME / ATR / OI
#       ↓
# BTC / ETH CONTEXT
#       ↓
# RELATIVE STRENGTH
#       ↓
# EXPECTED MOVE
#       ↓
# RETEST QUALITY
#       ↓
# FAKEOUT FILTER
#       ↓
# RISK / REWARD
#       ↓
# SETUP SCORE
# MOMENTUM SCORE
# TRADE SCORE
#       ↓
# QUANTUM SCORE
#       ↓
# RANKING
#       ↓
# TOP SIGNALS
#       ↓
# TELEGRAM
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


# ============================================================
# MARKET FILTER
# ============================================================

MIN_24H_VOLUME_USD = float(
    os.getenv(
        "MIN_24H_VOLUME_USD",
        "30000000"
    )
)

MIN_CANDIDATE_VOLUME_USD = float(
    os.getenv(
        "MIN_CANDIDATE_VOLUME_USD",
        "30000000"
    )
)

MAX_SYMBOLS = int(
    os.getenv(
        "MAX_SYMBOLS",
        "80"
    )
)

MAX_CANDIDATES = int(
    os.getenv(
        "MAX_CANDIDATES",
        "45"
    )
)

# После анализа всех кандидатов только лучшие
# могут попасть в ranking.
MAX_RANKED_SIGNALS = int(
    os.getenv(
        "MAX_RANKED_SIGNALS",
        "8"
    )
)


# ============================================================
# SCORE
# ============================================================

MIN_SCORE = int(
    os.getenv(
        "MIN_SCORE",
        "78"
    )
)

ELITE_SCORE = int(
    os.getenv(
        "ELITE_SCORE",
        "92"
    )
)


# ============================================================
# READY / ACTIVE
# ============================================================

READY_TTL_MINUTES = int(
    os.getenv(
        "READY_TTL_MINUTES",
        "15"
    )
)

COOLDOWN_MINUTES = int(
    os.getenv(
        "COOLDOWN_MINUTES",
        "45"
    )
)

MAX_CHASE_PCT = float(
    os.getenv(
        "MAX_CHASE_PCT",
        "0.55"
    )
)

PRE_TRIGGER_DISTANCE_PCT = float(
    os.getenv(
        "PRE_TRIGGER_DISTANCE_PCT",
        "0.35"
    )
)


# ============================================================
# SIGNAL LIMITS
# ============================================================

MAX_SIGNALS_PER_HOUR = int(
    os.getenv(
        "MAX_SIGNALS_PER_HOUR",
        "8"
    )
)

MAX_SIGNALS_PER_DAY = int(
    os.getenv(
        "MAX_SIGNALS_PER_DAY",
        "40"
    )
)


# ============================================================
# SCANNER
# ============================================================

SCAN_INTERVAL_SECONDS = int(
    os.getenv(
        "SCAN_INTERVAL_SECONDS",
        "20"
    )
)

HTTP_TIMEOUT = int(
    os.getenv(
        "HTTP_TIMEOUT",
        "10"
    )
)

REQUEST_RETRIES = int(
    os.getenv(
        "REQUEST_RETRIES",
        "3"
    )
)

CANDLE_CACHE_SECONDS = int(
    os.getenv(
        "CANDLE_CACHE_SECONDS",
        "12"
    )
)


# ============================================================
# OI
# ============================================================

OI_MAX_REQUESTS_PER_SCAN = int(
    os.getenv(
        "OI_MAX_REQUESTS_PER_SCAN",
        "15"
    )
)

OI_LOOKBACK_MINUTES = int(
    os.getenv(
        "OI_LOOKBACK_MINUTES",
        "10"
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
# OPTIONAL SECTOR MAP
#
# Example:
#
# SECTOR_MAP='{
#   "SOL":"L1",
#   "SUI":"L1",
#   "DOGE":"MEME",
#   "PEPE":"MEME"
# }'
#
# If empty, sector analysis is disabled.
# ============================================================

try:
    SECTOR_MAP = json.loads(
        os.getenv(
            "SECTOR_MAP",
            "{}"
        )
    )
except Exception:
    SECTOR_MAP = {}


# ============================================================
# VALIDATION
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
    format="%(asctime)s | %(levelname)s | %(message)s"
)

log = logging.getLogger(
    "QUANTUM_V5"
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
    setup_score INTEGER NOT NULL DEFAULT 0,
    momentum_score INTEGER NOT NULL DEFAULT 0,
    trade_score INTEGER NOT NULL DEFAULT 0,
    expected_move_pct REAL NOT NULL DEFAULT 0,
    risk_reward REAL NOT NULL DEFAULT 0,
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

db.execute("""
CREATE TABLE IF NOT EXISTS signal_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER,
    inst_id TEXT NOT NULL,
    strategy TEXT NOT NULL,
    direction TEXT NOT NULL,
    score INTEGER NOT NULL,
    result TEXT,
    pnl_r REAL,
    max_favorable_pct REAL,
    max_adverse_pct REAL,
    created_at REAL NOT NULL,
    closed_at REAL
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
class Level:
    price: float
    timeframe: str
    strength: int
    kind: str
    ts: int
    distance_pct: float = 0.0


@dataclass
class LevelCluster:
    price: float
    strength: int
    timeframes: List[str]
    kinds: List[str]
    touches: int
    first_ts: int
    last_ts: int
    age_hours: float
    freshness_score: float
    distance_pct: float


@dataclass
class StrategyResult:
    name: str
    points: int
    valid: bool
    state: str
    reason: str
    trigger_price: float = 0.0
    confidence: int = 0


@dataclass
class Setup:
    inst_id: str
    coin: str

    direction: str
    strategy: str

    level: float
    level_strength: int
    level_tf: str

    level_age_hours: float
    level_freshness: float
    level_touches: int

    current_price: float

    entry_low: float
    entry_high: float

    sl: float

    tp1: float
    tp2: float
    tp3: float

    score: int
    setup_score: int
    momentum_score: int
    trade_score: int

    liquidity: str
    volume_grade: str
    oi_status: str
    oi_change_pct: float

    market_regime: str
    market_context: str
    relative_strength: float
    sector_context: str

    expected_move_pct: float
    risk_pct: float
    risk_reward: float

    retest_quality: int
    fakeout_risk: int

    breakout_volume_ratio: float
    atr_pct: float

    setup_state: str

    reason: str

    volume_24h: float

    candles_5m: List[Candle]


@dataclass
class ActiveReady:
    setup: Setup
    created_at: float
    expires_at: float

    telegram_photo_id: Optional[int] = None
    telegram_text_id: Optional[int] = None


# ============================================================
# MEMORY
# ============================================================

ready_setups: Dict[
    str,
    ActiveReady
] = {}

signals_hour: List[float] = []

signals_today = 0

last_morning_date = None

last_day = None

scan_count = 0


# ============================================================
# CANDLE CACHE
# ============================================================

candle_cache: Dict[
    Tuple[str, str],
    Tuple[float, List[Candle]]
] = {}


# ============================================================
# OI CACHE
# ============================================================

oi_cache: Dict[
    str,
    Tuple[float, float]
] = {}

previous_oi: Dict[
    str,
    float
] = {}


# ============================================================
# BASIC HELPERS
# ============================================================

def now_ts() -> float:
    return time.time()


def local_now() -> datetime:
    return datetime.now(
        ZoneInfo(TIMEZONE)
    )


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


def fmt_price(
    price: float
) -> str:

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


def score_label(
    score: int
) -> str:

    if score >= 95:
        return "🚀 ELITE"

    if score >= 90:
        return "🔥 PREMIUM"

    if score >= 85:
        return "💎 STRONG"

    if score >= 80:
        return "⚡ HIGH QUALITY"

    return "🟡 SIGNAL"


def grade_volume(
    ratio: float
) -> str:

    if ratio >= 3.0:
        return "EXTREME"

    if ratio >= 2.5:
        return "VERY HIGH"

    if ratio >= 2.0:
        return "HIGH"

    if ratio >= 1.5:
        return "GOOD"

    if ratio >= 1.2:
        return "NORMAL+"

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


# ============================================================
# OKX REQUEST
# ============================================================

def okx_get(
    path: str,
    params: dict,
    retries: int = REQUEST_RETRIES
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
                    "Invalid JSON."
                )

            code = str(
                payload.get(
                    "code",
                    ""
                )
            )

            if code != "0":

                raise RuntimeError(
                    f"OKX code={code} "
                    f"msg={payload.get('msg', '')}"
                )

            return payload

        except Exception as exc:

            last_error = exc

            log.warning(
                "OKX FAILED | %s | "
                "attempt=%s/%s | %s",
                path,
                attempt,
                retries,
                exc
            )

            if attempt < retries:

                time.sleep(
                    min(
                        attempt * 1.5,
                        5
                    )
                )

    raise RuntimeError(
        f"OKX request failed: {last_error}"
    )


# ============================================================
# INSTRUMENTS
# ============================================================

def get_instruments() -> Dict[str, dict]:

    payload = okx_get(
        "/api/v5/public/instruments",
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

        if item.get(
            "state"
        ) != "live":
            continue

        try:

            result[inst_id] = {
                "ctVal": float(
                    item.get(
                        "ctVal",
                        0
                    ) or 0
                ),
                "ctMult": float(
                    item.get(
                        "ctMult",
                        1
                    ) or 1
                ),
                "lotSz": float(
                    item.get(
                        "lotSz",
                        0
                    ) or 0
                ),
                "tickSz": float(
                    item.get(
                        "tickSz",
                        0
                    ) or 0
                )
            }

        except Exception:

            result[inst_id] = {}

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

            vol24h = float(
                item.get(
                    "vol24h",
                    0
                ) or 0
            )

            vol_ccy = float(
                item.get(
                    "volCcy24h",
                    0
                ) or 0
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

            open24h = float(
                item.get(
                    "open24h",
                    0
                ) or 0
            )

            turnover = (
                vol_ccy
                * last
            )

            fallback = (
                vol24h
                * last
            )

            if turnover <= 0:
                turnover = fallback

            if (
                fallback > 0
                and turnover > fallback * 1000
            ):
                turnover = fallback

            result[inst_id] = {
                "last": last,
                "open24h": open24h,
                "high24h": high,
                "low24h": low,
                "vol24h": vol24h,
                "vol_ccy_24h": vol_ccy,
                "vol24h_usd": turnover,
                "ts": int(
                    item.get(
                        "ts",
                        0
                    ) or 0
                )
            }

        except Exception:
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

    key = (
        inst_id,
        bar
    )

    cached = candle_cache.get(
        key
    )

    if cached:

        cached_ts, data = cached

        if (
            now_ts()
            - cached_ts
            < CANDLE_CACHE_SECONDS
        ):
            return data

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
                    confirmed=str(
                        row[8]
                    ) == "1"
                )
            )

        except Exception:
            continue

    candle_cache[
        key
    ] = (
        now_ts(),
        candles
    )

    return candles


# ============================================================
# OI
# ============================================================

def get_open_interest(
    inst_id: str
) -> Optional[float]:

    cached = oi_cache.get(
        inst_id
    )

    if cached:

        cached_ts, value = cached

        if (
            now_ts()
            - cached_ts
            < 60
        ):
            return value

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

        value = item.get(
            "oiUsd"
        )

        if value in (
            None,
            ""
        ):
            value = item.get(
                "oi"
            )

        if value in (
            None,
            ""
        ):
            return None

        value = float(
            value
        )

        oi_cache[
            inst_id
        ] = (
            now_ts(),
            value
        )

        return value

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

        c = candles[i]
        p = candles[i - 1]

        trs.append(
            max(
                c.high - c.low,
                abs(
                    c.high
                    - p.close
                ),
                abs(
                    c.low
                    - p.close
                )
            )
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

    return (
        current
        / average
    )


def candle_body_ratio(
    candle: Candle
) -> float:

    rng = (
        candle.high
        - candle.low
    )

    if rng <= 0:
        return 0.0

    return (
        abs(
            candle.close
            - candle.open
        )
        / rng
    )


def atr_pct(
    candles: List[Candle]
) -> float:

    if not candles:
        return 0.0

    price = candles[-1].close

    if price <= 0:
        return 0.0

    return (
        atr(
            candles,
            14
        )
        / price
        * 100.0
    )


# ============================================================
# PIVOTS
# ============================================================

def pivot_highs(
    candles: List[Candle],
    left: int = 2,
    right: int = 2
) -> List[Tuple[int, float]]:

    result = []

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
# MARKET STRUCTURE
# ============================================================

def structure_direction(
    candles: List[Candle]
) -> str:

    if len(candles) < 60:
        return "NEUTRAL"

    closes = [
        c.close
        for c in candles
    ]

    e20 = ema(
        closes,
        20
    )[-1]

    e50 = ema(
        closes,
        50
    )[-1]

    recent = candles[-20:]

    first = recent[:10]
    second = recent[10:]

    first_high = max(
        c.high
        for c in first
    )

    second_high = max(
        c.high
        for c in second
    )

    first_low = min(
        c.low
        for c in first
    )

    second_low = min(
        c.low
        for c in second
    )

    if (
        e20 > e50
        and second_high >= first_high
        and second_low >= first_low
    ):
        return "LONG"

    if (
        e20 < e50
        and second_high <= first_high
        and second_low <= first_low
    ):
        return "SHORT"

    return "NEUTRAL"


def structure_strength(
    candles: List[Candle],
    direction: str
) -> int:

    if len(candles) < 60:
        return 0

    closes = [
        c.close
        for c in candles
    ]

    e20 = ema(
        closes,
        20
    )[-1]

    e50 = ema(
        closes,
        50
    )[-1]

    current = closes[-1]

    points = 0

    if direction == "LONG":

        if e20 > e50:
            points += 10

        if current > e20:
            points += 5

    else:

        if e20 < e50:
            points += 10

        if current < e20:
            points += 5

    return points


# ============================================================
# MARKET REGIME
# ============================================================

def market_regime(
    candles_15: List[Candle],
    candles_5: List[Candle]
) -> Tuple[str, int]:

    if len(candles_15) < 40:
        return "UNKNOWN", 0

    current_atr = atr_pct(
        candles_15
    )

    recent = candles_15[-20:]

    ranges = [
        (
            c.high
            - c.low
        )
        for c in recent
    ]

    if not ranges:
        return "UNKNOWN", 0

    avg_range = (
        sum(ranges)
        / len(ranges)
    )

    older = candles_15[-40:-20]

    old_ranges = [
        c.high - c.low
        for c in older
    ]

    if not old_ranges:
        return "UNKNOWN", 0

    old_avg = (
        sum(old_ranges)
        / len(old_ranges)
    )

    expansion = (
        avg_range / old_avg
        if old_avg > 0
        else 1
    )

    direction = structure_direction(
        candles_15
    )

    if (
        expansion < 0.70
        and current_atr < 1.0
    ):
        return "COMPRESSED", 90

    if expansion > 1.35:

        if direction != "NEUTRAL":
            return "EXPANSION", 90

        return "CHAOTIC", 70

    if direction != "NEUTRAL":
        return "TRENDING", 85

    return "RANGING", 75


# ============================================================
# LEVEL ENGINE
# ============================================================

def add_level(
    levels: List[Level],
    price: float,
    timeframe: str,
    strength: int,
    kind: str,
    ts: int,
    current: float
):

    if price <= 0:
        return

    distance = abs(
        pct(
            current,
            price
        )
    )

    if distance > 3.0:
        return

    levels.append(
        Level(
            price=price,
            timeframe=timeframe,
            strength=strength,
            kind=kind,
            ts=ts,
            distance_pct=distance
        )
    )


def build_levels(
    current: float,
    direction: str,
    candles: Dict[str, List[Candle]]
) -> List[Level]:

    levels = []

    weights = {
        "1D": 30,
        "4H": 26,
        "1H": 22,
        "30m": 17,
        "15m": 15,
        "5m": 11
    }

    for tf, data in candles.items():

        if len(data) < 25:
            continue

        strength = weights.get(
            tf,
            10
        )

        # --------------------------------------------
        # PIVOTS
        # --------------------------------------------

        highs = pivot_highs(
            data
        )[-40:]

        lows = pivot_lows(
            data
        )[-40:]

        # For LONG we care primarily about resistance.
        # For SHORT primarily about support.
        if direction == "LONG":

            for idx, price in highs:

                if price > current:

                    add_level(
                        levels,
                        price,
                        tf,
                        strength,
                        "SWING_HIGH",
                        data[idx].ts,
                        current
                    )

        else:

            for idx, price in lows:

                if price < current:

                    add_level(
                        levels,
                        price,
                        tf,
                        strength,
                        "SWING_LOW",
                        data[idx].ts,
                        current
                    )

        # --------------------------------------------
        # RECENT RANGE
        # --------------------------------------------

        recent = data[-80:]

        if direction == "LONG":

            high = max(
                c.high
                for c in recent
            )

            if high > current:

                add_level(
                    levels,
                    high,
                    tf,
                    max(
                        strength - 4,
                        5
                    ),
                    "RANGE_HIGH",
                    recent[-1].ts,
                    current
                )

        else:

            low = min(
                c.low
                for c in recent
            )

            if low < current:

                add_level(
                    levels,
                    low,
                    tf,
                    max(
                        strength - 4,
                        5
                    ),
                    "RANGE_LOW",
                    recent[-1].ts,
                    current
                )

        # --------------------------------------------
        # VERY RECENT INTRADAY LEVEL
        # --------------------------------------------

        if tf in (
            "5m",
            "15m",
            "30m"
        ):

            recent_short = data[
                -36:
            ]

            if direction == "LONG":

                high = max(
                    c.high
                    for c in recent_short
                )

                if high > current:

                    add_level(
                        levels,
                        high,
                        tf,
                        strength + 5,
                        "FRESH_INTRADAY_HIGH",
                        recent_short[-1].ts,
                        current
                    )

            else:

                low = min(
                    c.low
                    for c in recent_short
                )

                if low < current:

                    add_level(
                        levels,
                        low,
                        tf,
                        strength + 5,
                        "FRESH_INTRADAY_LOW",
                        recent_short[-1].ts,
                        current
                    )

    return levels


def cluster_levels(
    levels: List[Level],
    current: float
) -> List[LevelCluster]:

    if not levels:
        return []

    levels = sorted(
        levels,
        key=lambda x: x.price
    )

    clusters: List[
        List[Level]
    ] = []

    for level in levels:

        placed = False

        for cluster in clusters:

            avg = (
                sum(
                    x.price
                    for x in cluster
                )
                / len(cluster)
            )

            distance = abs(
                pct(
                    level.price,
                    avg
                )
            )

            # Для intraday используем узкую зону,
            # но допускаем чуть больше для volatile coins.
            width = 0.16

            if level.distance_pct > 1:
                width += 0.05

            if (
                distance <= width
            ):

                cluster.append(
                    level
                )

                placed = True
                break

        if not placed:

            clusters.append(
                [level]
            )

    result = []

    current_time = now_ts() * 1000

    for cluster in clusters:

        total_weight = sum(
            max(
                x.strength,
                1
            )
            for x in cluster
        )

        weighted_price = (
            sum(
                x.price
                * max(
                    x.strength,
                    1
                )
                for x in cluster
            )
            / total_weight
        )

        tfs = list(
            dict.fromkeys(
                x.timeframe
                for x in cluster
            )
        )

        kinds = list(
            dict.fromkeys(
                x.kind
                for x in cluster
            )
        )

        first_ts = min(
            x.ts
            for x in cluster
        )

        last_ts = max(
            x.ts
            for x in cluster
        )

        age_hours = max(
            0.0,
            (
                current_time
                - last_ts
            )
            / 3_600_000
        )

        # Freshness:
        # recent levels receive a bonus,
        # old levels retain value.
        if age_hours <= 1:
            freshness = 100
        elif age_hours <= 3:
            freshness = 95
        elif age_hours <= 6:
            freshness = 88
        elif age_hours <= 12:
            freshness = 78
        elif age_hours <= 24:
            freshness = 68
        elif age_hours <= 72:
            freshness = 55
        else:
            freshness = 40

        strength = min(
            100,
            total_weight
            + max(
                0,
                len(tfs) - 1
            ) * 9
            + max(
                0,
                len(cluster) - 2
            ) * 4
        )

        result.append(
            LevelCluster(
                price=weighted_price,
                strength=strength,
                timeframes=tfs,
                kinds=kinds,
                touches=len(cluster),
                first_ts=first_ts,
                last_ts=last_ts,
                age_hours=age_hours,
                freshness_score=freshness,
                distance_pct=abs(
                    pct(
                        current,
                        weighted_price
                    )
                )
            )
        )

    result.sort(
        key=lambda x: (
            x.distance_pct,
            -x.strength
        )
    )

    return result


def best_level(
    clusters: List[LevelCluster],
    current: float
) -> Optional[LevelCluster]:

    candidates = []

    for cluster in clusters:

        if cluster.distance_pct > 1.5:
            continue

        distance_penalty = (
            cluster.distance_pct
            * 18
        )

        freshness_bonus = (
            cluster.freshness_score
            * 0.08
        )

        intraday_bonus = 0

        if any(
            tf in (
                "5m",
                "15m",
                "30m"
            )
            for tf in cluster.timeframes
        ):
            intraday_bonus += 5

        if len(
            cluster.timeframes
        ) >= 2:
            intraday_bonus += 5

        quality = (
            cluster.strength
            + freshness_bonus
            + intraday_bonus
            - distance_penalty
        )

        candidates.append(
            (
                quality,
                cluster
            )
        )

    if not candidates:
        return None

    candidates.sort(
        key=lambda x: x[0],
        reverse=True
    )

    return candidates[0][1]


# ============================================================
# TRENDLINE ENGINE
# ============================================================

def linear_slope(
    points: List[Tuple[int, float]]
) -> float:

    if len(points) < 2:
        return 0.0

    xs = [
        float(p[0])
        for p in points
    ]

    ys = [
        float(p[1])
        for p in points
    ]

    x_mean = sum(xs) / len(xs)
    y_mean = sum(ys) / len(ys)

    numerator = sum(
        (
            x - x_mean
        )
        * (
            y - y_mean
        )
        for x, y in zip(
            xs,
            ys
        )
    )

    denominator = sum(
        (
            x - x_mean
        ) ** 2
        for x in xs
    )

    if denominator == 0:
        return 0.0

    return (
        numerator
        / denominator
    )


def trendline_breakout(
    candles: List[Candle],
    direction: str
) -> StrategyResult:

    if len(candles) < 40:

        return StrategyResult(
            "Diagonal Breakout",
            0,
            False,
            "NONE",
            ""
        )

    recent = candles[-40:]

    if direction == "LONG":

        pivots = pivot_highs(
            recent,
            2,
            2
        )

        points = [
            (
                idx,
                price
            )
            for idx, price in pivots
            if idx >= 5
        ][-4:]

        if len(points) < 2:

            return StrategyResult(
                "Diagonal Breakout",
                0,
                False,
                "NONE",
                ""
            )

        slope = linear_slope(
            points
        )

        # Falling resistance.
        if slope >= 0:
            return StrategyResult(
                "Diagonal Breakout",
                0,
                False,
                "NONE",
                ""
            )

        x = len(recent) - 1

        base_x, base_y = points[0]

        line_now = (
            base_y
            + slope
            * (
                x - base_x
            )
        )

        current = recent[-1]

        distance = pct(
            current.close,
            line_now
        )

        crossed = (
            current.close
            > line_now
            and recent[-2].close
            <= (
                base_y
                + slope
                * (
                    x - 1
                    - base_x
                )
            )
        )

        if not crossed:

            return StrategyResult(
                "Diagonal Breakout",
                0,
                False,
                "NONE",
                ""
            )

        body = candle_body_ratio(
            current
        )

        points_score = 25

        if body >= 0.55:
            points_score += 4

        if len(points) >= 3:
            points_score += 5

        return StrategyResult(
            "Diagonal Breakout",
            min(
                points_score,
                35
            ),
            True,
            "ACTIVE",
            "Цена пробила нисходящую "
            "трендовую сопротивления с "
            "подтверждённым закрытием.",
            current.close,
            min(
                100,
                70
                + len(points) * 8
            )
        )

    # SHORT
    pivots = pivot_lows(
        recent,
        2,
        2
    )

    points = [
        (
            idx,
            price
        )
        for idx, price in pivots
        if idx >= 5
    ][-4:]

    if len(points) < 2:

        return StrategyResult(
            "Diagonal Breakout",
            0,
            False,
            "NONE",
            ""
        )

    slope = linear_slope(
        points
    )

    # Rising support.
    if slope <= 0:

        return StrategyResult(
            "Diagonal Breakout",
            0,
            False,
            "NONE",
            ""
        )

    x = len(recent) - 1

    base_x, base_y = points[0]

    line_now = (
        base_y
        + slope
        * (
            x - base_x
        )
    )

    current = recent[-1]

    crossed = (
        current.close
        < line_now
        and recent[-2].close
        >= (
            base_y
            + slope
            * (
                x - 1
                - base_x
            )
        )
    )

    if not crossed:

        return StrategyResult(
            "Diagonal Breakout",
            0,
            False,
            "NONE",
            ""
        )

    body = candle_body_ratio(
        current
    )

    points_score = 25

    if body >= 0.55:
        points_score += 4

    if len(points) >= 3:
        points_score += 5

    return StrategyResult(
        "Diagonal Breakout",
        min(
            points_score,
            35
        ),
        True,
        "ACTIVE",
        "Цена пробила восходящую "
        "трендовую поддержки с "
        "подтверждённым закрытием.",
        current.close,
        min(
            100,
            70
            + len(points) * 8
        )
    )


# ============================================================
# COMPRESSION
# ============================================================

def compression_strategy(
    candles: List[Candle],
    direction: str
) -> StrategyResult:

    if len(candles) < 35:

        return StrategyResult(
            "Compression Expansion",
            0,
            False,
            "NONE",
            ""
        )

    recent = candles[-24:]

    ranges = [
        c.high - c.low
        for c in recent
    ]

    first = ranges[:12]
    last = ranges[-12:]

    first_avg = (
        sum(first)
        / len(first)
    )

    last_avg = (
        sum(last)
        / len(last)
    )

    if first_avg <= 0:
        return StrategyResult(
            "Compression Expansion",
            0,
            False,
            "NONE",
            ""
        )

    compression = (
        1
        - last_avg / first_avg
    )

    current = recent[-1]

    body = candle_body_ratio(
        current
    )

    expansion_ratio = (
        (
            current.high
            - current.low
        )
        / last_avg
        if last_avg > 0
        else 0
    )

    if compression < 0.10:
        return StrategyResult(
            "Compression Expansion",
            0,
            False,
            "NONE",
            ""
        )

    # Directional trigger.
    if direction == "LONG":

        previous_high = max(
            c.high
            for c in recent[-13:-1]
        )

        trigger = (
            current.close
            > previous_high
        )

    else:

        previous_low = min(
            c.low
            for c in recent[-13:-1]
        )

        trigger = (
            current.close
            < previous_low
        )

    # READY can exist before actual expansion.
    if not trigger:

        if compression >= 0.20:

            return StrategyResult(
                "Compression Expansion",
                min(
                    22,
                    int(
                        12
                        + compression
                        * 30
                    )
                ),
                True,
                "READY",
                "Рынок находится в фазе "
                "сжатия; ожидаем expansion "
                "из диапазона."
            )

        return StrategyResult(
            "Compression Expansion",
            0,
            False,
            "NONE",
            ""
        )

    points = 25

    if compression >= 0.18:
        points += 4

    if compression >= 0.28:
        points += 4

    if expansion_ratio >= 1.4:
        points += 4

    if body >= 0.55:
        points += 4

    return StrategyResult(
        "Compression Expansion",
        min(
            points,
            40
        ),
        True,
        "ACTIVE",
        "После сжатия диапазона "
        "произошло направленное "
        "расширение.",
        current.close,
        90
    )


# ============================================================
# HORIZONTAL BREAKOUT
# ============================================================

def horizontal_breakout(
    candles: List[Candle],
    level: LevelCluster,
    direction: str
) -> StrategyResult:

    if len(candles) < 10:

        return StrategyResult(
            "Horizontal Level Breakout",
            0,
            False,
            "NONE",
            ""
        )

    current = candles[-1]
    previous = candles[-2]

    level_price = level.price

    if direction == "LONG":

        crossed = (
            current.close > level_price
            and previous.close <= level_price
        )

        if crossed:

            body = candle_body_ratio(
                current
            )

            points = 27

            if body >= 0.55:
                points += 5

            if level.touches >= 3:
                points += 4

            if len(
                level.timeframes
            ) >= 2:
                points += 4

            return StrategyResult(
                "Horizontal Level Breakout",
                min(
                    points,
                    40
                ),
                True,
                "ACTIVE",
                "Подтверждённый 5M breakout "
                "сильного горизонтального "
                "уровня.",
                current.close,
                95
            )

    else:

        crossed = (
            current.close < level_price
            and previous.close >= level_price
        )

        if crossed:

            body = candle_body_ratio(
                current
            )

            points = 27

            if body >= 0.55:
                points += 5

            if level.touches >= 3:
                points += 4

            if len(
                level.timeframes
            ) >= 2:
                points += 4

            return StrategyResult(
                "Horizontal Level Breakout",
                min(
                    points,
                    40
                ),
                True,
                "ACTIVE",
                "Подтверждённый 5M breakout "
                "сильного горизонтального "
                "уровня.",
                current.close,
                95
            )

    # PRE-BREAKOUT.
    distance = abs(
        pct(
            current.close,
            level_price
        )
    )

    if distance <= PRE_TRIGGER_DISTANCE_PCT:

        return StrategyResult(
            "Horizontal Level Breakout",
            18,
            True,
            "READY",
            "Цена подошла к сильному "
            "горизонтальному уровню; "
            "ожидаем подтверждение пробоя.",
            level_price,
            80
        )

    return StrategyResult(
        "Horizontal Level Breakout",
        0,
        False,
        "NONE",
        ""
    )


# ============================================================
# LIQUIDITY SWEEP
# ============================================================

def liquidity_sweep(
    candles: List[Candle],
    direction: str
) -> StrategyResult:

    if len(candles) < 30:

        return StrategyResult(
            "Liquidity Sweep Reversal",
            0,
            False,
            "NONE",
            ""
        )

    recent = candles[-15:]

    previous = candles[-2]
    current = candles[-1]

    if direction == "SHORT":

        prior_high = max(
            c.high
            for c in candles[-15:-2]
        )

        swept = (
            previous.high
            > prior_high
        )

        rejection = (
            current.close
            < prior_high
        )

        bearish_body = (
            current.close
            < current.open
        )

        if swept and rejection:

            points = 28

            if bearish_body:
                points += 5

            if candle_body_ratio(
                current
            ) >= 0.55:
                points += 4

            return StrategyResult(
                "Liquidity Sweep Reversal",
                min(
                    points,
                    40
                ),
                True,
                "ACTIVE",
                "Цена сняла ликвидность "
                "над локальным high и "
                "вернулась под уровень.",
                current.close,
                92
            )

    else:

        prior_low = min(
            c.low
            for c in candles[-15:-2]
        )

        swept = (
            previous.low
            < prior_low
        )

        rejection = (
            current.close
            > prior_low
        )

        bullish_body = (
            current.close
            > current.open
        )

        if swept and rejection:

            points = 28

            if bullish_body:
                points += 5

            if candle_body_ratio(
                current
            ) >= 0.55:
                points += 4

            return StrategyResult(
                "Liquidity Sweep Reversal",
                min(
                    points,
                    40
                ),
                True,
                "ACTIVE",
                "Цена сняла ликвидность "
                "под локальным low и "
                "вернулась выше уровня.",
                current.close,
                92
            )

    return StrategyResult(
        "Liquidity Sweep Reversal",
        0,
        False,
        "NONE",
        ""
    )


# ============================================================
# VOLUME / MOMENTUM
# ============================================================

def momentum_score(
    candles: List[Candle],
    direction: str
) -> Tuple[int, bool]:

    if len(candles) < 35:
        return 0, False

    closes = [
        c.close
        for c in candles
    ]

    e9 = ema(
        closes,
        9
    )[-1]

    e21 = ema(
        closes,
        21
    )[-1]

    current = candles[-1]

    score = 0

    if direction == "LONG":

        if e9 > e21:
            score += 15

        if current.close > e9:
            score += 8

        if current.close > current.open:
            score += 5

        valid = (
            e9 > e21
            and current.close > e9
        )

    else:

        if e9 < e21:
            score += 15

        if current.close < e9:
            score += 8

        if current.close < current.open:
            score += 5

        valid = (
            e9 < e21
            and current.close < e9
        )

    return (
        min(
            score,
            30
        ),
        valid
    )


# ============================================================
# RETEST QUALITY
# ============================================================

def retest_quality(
    candles: List[Candle],
    level: float,
    direction: str
) -> int:

    if len(candles) < 10:
        return 0

    recent = candles[-10:]

    touches = 0
    valid_rejections = 0

    for candle in recent:

        distance_high = abs(
            pct(
                candle.high,
                level
            )
        )

        distance_low = abs(
            pct(
                candle.low,
                level
            )
        )

        if min(
            distance_high,
            distance_low
        ) <= 0.20:

            touches += 1

            if direction == "LONG":

                if (
                    candle.close
                    >= candle.open
                ):
                    valid_rejections += 1

            else:

                if (
                    candle.close
                    <= candle.open
                ):
                    valid_rejections += 1

    if touches == 0:
        return 0

    if valid_rejections >= 3:
        return 20

    if valid_rejections == 2:
        return 15

    if valid_rejections == 1:
        return 8

    return 3


# ============================================================
# FAKEOUT DETECTOR
# ============================================================

def fakeout_risk(
    candles: List[Candle],
    level: float,
    direction: str
) -> int:

    if len(candles) < 5:
        return 0

    recent = candles[-5:]

    risk = 0

    for candle in recent:

        body = candle_body_ratio(
            candle
        )

        rng = (
            candle.high
            - candle.low
        )

        if rng <= 0:
            continue

        upper_wick = (
            candle.high
            - max(
                candle.open,
                candle.close
            )
        )

        lower_wick = (
            min(
                candle.open,
                candle.close
            )
            - candle.low
        )

        if direction == "LONG":

            if (
                candle.high > level
                and candle.close < level
                and upper_wick / rng > 0.45
            ):
                risk += 18

        else:

            if (
                candle.low < level
                and candle.close > level
                and lower_wick / rng > 0.45
            ):
                risk += 18

        if body < 0.25:
            risk += 4

    return min(
        risk,
        40
    )


# ============================================================
# EXPECTED MOVE
# ============================================================

def next_obstacle(
    current: float,
    direction: str,
    clusters: List[LevelCluster]
) -> Optional[LevelCluster]:

    candidates = []

    for cluster in clusters:

        if direction == "LONG":

            if cluster.price <= current:
                continue

        else:

            if cluster.price >= current:
                continue

        candidates.append(
            cluster
        )

    if not candidates:
        return None

    candidates.sort(
        key=lambda x: x.distance_pct
    )

    return candidates[0]


def expected_move_engine(
    current: float,
    direction: str,
    level: LevelCluster,
    clusters: List[LevelCluster],
    atr_value: float
) -> Tuple[
    float,
    float,
    Optional[LevelCluster]
]:

    obstacle = next_obstacle(
        current,
        direction,
        [
            c
            for c in clusters
            if abs(
                c.price
                - level.price
            ) > 0
        ]
    )

    if obstacle:

        if direction == "LONG":

            raw_move = (
                obstacle.price
                - current
            )

        else:

            raw_move = (
                current
                - obstacle.price
            )

    else:

        raw_move = (
            atr_value * 4.5
        )

    if raw_move <= 0:
        return 0.0, 0.0, obstacle

    expected_pct = (
        raw_move
        / current
        * 100
    )

    # If next obstacle is too close, no room.
    quality = clamp(
        expected_pct / 2.0,
        0,
        1
    )

    return (
        expected_pct,
        quality,
        obstacle
    )


# ============================================================
# MARKET CONTEXT
# ============================================================

def market_context(
    tickers: Dict[str, dict],
    coin_ticker: dict
) -> Tuple[
    float,
    str
]:

    btc = tickers.get(
        "BTC-USDT-SWAP"
    )

    eth = tickers.get(
        "ETH-USDT-SWAP"
    )

    coin_change = pct(
        coin_ticker.get(
            "last",
            0
        ),
        coin_ticker.get(
            "open24h",
            0
        )
    )

    btc_change = (
        pct(
            btc.get("last", 0),
            btc.get("open24h", 0)
        )
        if btc
        else 0
    )

    eth_change = (
        pct(
            eth.get("last", 0),
            eth.get("open24h", 0)
        )
        if eth
        else 0
    )

    market_change = (
        btc_change * 0.6
        + eth_change * 0.4
    )

    relative_strength = (
        coin_change
        - market_change
    )

    if relative_strength >= 3:
        context = "VERY STRONG"
    elif relative_strength >= 1.5:
        context = "STRONG"
    elif relative_strength >= 0.5:
        context = "POSITIVE"
    elif relative_strength <= -3:
        context = "VERY WEAK"
    elif relative_strength <= -1.5:
        context = "WEAK"
    elif relative_strength <= -0.5:
        context = "NEGATIVE"
    else:
        context = "NEUTRAL"

    return (
        relative_strength,
        context
    )


# ============================================================
# SECTOR CONTEXT
# ============================================================

def sector_context(
    coin: str,
    tickers: Dict[str, dict]
) -> str:

    sector = SECTOR_MAP.get(
        coin
    )

    if not sector:
        return "N/A"

    members = [
        name
        for name, value
        in SECTOR_MAP.items()
        if value == sector
    ]

    changes = []

    for member in members:

        ticker = tickers.get(
            f"{member}-USDT-SWAP"
        )

        if not ticker:
            continue

        change = pct(
            ticker.get(
                "last",
                0
            ),
            ticker.get(
                "open24h",
                0
            )
        )

        changes.append(
            change
        )

    if not changes:
        return f"{sector}: N/A"

    avg = (
        sum(changes)
        / len(changes)
    )

    if avg >= 2:
        mood = "HOT"
    elif avg >= 0.5:
        mood = "POSITIVE"
    elif avg <= -2:
        mood = "WEAK"
    elif avg <= -0.5:
        mood = "NEGATIVE"
    else:
        mood = "NEUTRAL"

    return (
        f"{sector}: {mood} "
        f"({avg:+.2f}%)"
    )


# ============================================================
# FAST MARKET RANK
# ============================================================

def fast_market_score(
    ticker: dict
) -> float:

    volume = float(
        ticker.get(
            "vol24h_usd",
            0
        )
        or 0
    )

    price = float(
        ticker.get(
            "last",
            0
        )
        or 0
    )

    high = float(
        ticker.get(
            "high24h",
            0
        )
        or 0
    )

    low = float(
        ticker.get(
            "low24h",
            0
        )
        or 0
    )

    if price <= 0:
        return 0

    score = 0

    if volume >= 1_000_000_000:
        score += 30
    elif volume >= 500_000_000:
        score += 26
    elif volume >= 250_000_000:
        score += 22
    elif volume >= 100_000_000:
        score += 17
    elif volume >= 60_000_000:
        score += 12
    elif volume >= 30_000_000:
        score += 7
    else:
        return 0

    if high > low:

        range_pct = (
            high - low
        ) / price * 100

        if range_pct >= 1:
            score += 5

        if range_pct >= 2:
            score += 5

        if range_pct >= 4:
            score += 5

        if range_pct >= 7:
            score += 5

    return score


# ============================================================
# OI ANALYSIS
# ============================================================

def oi_analysis(
    inst_id: str,
    current_oi: Optional[float]
) -> Tuple[
    str,
    float,
    int
]:

    if current_oi is None:

        return (
            "N/A",
            0.0,
            0
        )

    old = previous_oi.get(
        inst_id
    )

    previous_oi[
        inst_id
    ] = current_oi

    if old is None or old <= 0:

        return (
            "AVAILABLE",
            0.0,
            2
        )

    change = (
        current_oi
        - old
    ) / old * 100

    if change >= 5:

        return (
            "OI RISING",
            change,
            5
        )

    if change >= 1:

        return (
            "OI UP",
            change,
            3
        )

    if change <= -5:

        return (
            "OI FALLING",
            change,
            -2
        )

    return (
        "OI FLAT",
        change,
        1
    )


# ============================================================
# ENTRY / SL / TP
# ============================================================

def build_trade_levels(
    current: float,
    level: LevelCluster,
    direction: str,
    c15: List[Candle],
    atr_value: float
) -> Optional[
    Tuple[
        float,
        float,
        float,
        float,
        float,
        float,
        float
    ]
]:

    if atr_value <= 0:
        return None

    zone_pct = clamp(
        (
            atr_value
            / current
            * 100
        )
        * 0.45,
        0.08,
        0.30
    )

    entry_low = (
        level.price
        * (
            1
            - zone_pct / 100
        )
    )

    entry_high = (
        level.price
        * (
            1
            + zone_pct / 100
        )
    )

    recent = c15[-24:]

    if direction == "LONG":

        structural_low = min(
            c.low
            for c in recent
        )

        sl = (
            structural_low
            - atr_value * 0.35
        )

        if sl >= current:
            return None

        risk = (
            current
            - sl
        )

        if level.price > current:

            entry = level.price

        else:

            entry = current

        tp1 = (
            entry
            + risk * 1.2
        )

        tp2 = (
            entry
            + risk * 2.2
        )

        tp3 = (
            entry
            + risk * 3.2
        )

    else:

        structural_high = max(
            c.high
            for c in recent
        )

        sl = (
            structural_high
            + atr_value * 0.35
        )

        if sl <= current:
            return None

        risk = (
            sl
            - current
        )

        if level.price < current:

            entry = level.price

        else:

            entry = current

        tp1 = (
            entry
            - risk * 1.2
        )

        tp2 = (
            entry
            - risk * 2.2
        )

        tp3 = (
            entry
            - risk * 3.2
        )

    risk_pct = (
        risk
        / current
        * 100
    )

    if risk_pct < 0.15:
        return None

    if risk_pct > 2.20:
        return None

    return (
        entry_low,
        entry_high,
        sl,
        tp1,
        tp2,
        tp3,
        risk_pct
    )


# ============================================================
# ANALYZE SYMBOL
# ============================================================

def analyze_symbol(
    inst_id: str,
    ticker: dict,
    candles: Dict[str, List[Candle]],
    tickers: Dict[str, dict],
    oi_value: Optional[float] = None
) -> Optional[Setup]:

    current = float(
        ticker.get(
            "last",
            0
        )
        or 0
    )

    if current <= 0:
        return None

    volume_24h = float(
        ticker.get(
            "vol24h_usd",
            0
        )
        or 0
    )

    if volume_24h < MIN_CANDIDATE_VOLUME_USD:
        return None

    c4h = candles.get(
        "4H",
        []
    )

    c1h = candles.get(
        "1H",
        []
    )

    c30 = candles.get(
        "30m",
        []
    )

    c15 = candles.get(
        "15m",
        []
    )

    c5 = [
        c
        for c in candles.get(
            "5m",
            []
        )
        if c.confirmed
    ]

    if len(c4h) < 50:
        return None

    if len(c1h) < 60:
        return None

    if len(c15) < 50:
        return None

    if len(c5) < 35:
        return None

    # --------------------------------------------------------
    # STRUCTURE
    # --------------------------------------------------------

    structure_1h = structure_direction(
        c1h
    )

    structure_4h = structure_direction(
        c4h
    )

    # If 1H is neutral, 15M/30M may still provide
    # a legitimate scalping direction.
    structure_15 = structure_direction(
        c15
    )

    if structure_1h != "NEUTRAL":

        direction = structure_1h

    elif structure_15 != "NEUTRAL":

        direction = structure_15

    else:

        return None

    score = 0

    setup_score = 0
    momentum_points = 0
    trade_score = 0

    # --------------------------------------------------------
    # HIGHER TF CONTEXT
    # --------------------------------------------------------

    if structure_1h == direction:

        setup_score += 12

    if structure_4h == direction:

        setup_score += 10

    elif structure_4h != "NEUTRAL":

        setup_score -= 4

    if structure_15 == direction:

        setup_score += 8

    setup_score += structure_strength(
        c1h,
        direction
    )

    setup_score += (
        structure_strength(
            c4h,
            direction
        )
        // 2
    )

    # --------------------------------------------------------
    # LEVELS
    # --------------------------------------------------------

    level_data = {
        "1D": candles.get(
            "1D",
            []
        ),
        "4H": c4h,
        "1H": c1h,
        "30m": c30,
        "15m": c15,
        "5m": c5
    }

    levels = build_levels(
        current,
        direction,
        level_data
    )

    clusters = cluster_levels(
        levels,
        current
    )

    level = best_level(
        clusters,
        current
    )

    if level is None:
        return None

    if level.distance_pct > 1.20:
        return None

    setup_score += int(
        clamp(
            level.strength * 0.38,
            6,
            30
        )
    )

    setup_score += int(
        level.freshness_score * 0.08
    )

    if level.touches >= 3:
        setup_score += 5

    if len(
        level.timeframes
    ) >= 2:
        setup_score += 8

    if len(
        level.timeframes
    ) >= 3:
        setup_score += 5

    # --------------------------------------------------------
    # ATR
    # --------------------------------------------------------

    atr_value = atr(
        c5,
        14
    )

    if atr_value <= 0:
        return None

    volatility = (
        atr_value
        / current
        * 100
    )

    if volatility < 0.025:
        return None

    if volatility > 3.5:
        return None

    # --------------------------------------------------------
    # MARKET REGIME
    # --------------------------------------------------------

    regime, regime_confidence = (
        market_regime(
            c15,
            c5
        )
    )

    if regime == "CHAOTIC":

        setup_score -= 8

    elif regime == "COMPRESSED":

        setup_score += 5

    elif regime == "TRENDING":

        setup_score += 5

    # --------------------------------------------------------
    # VOLUME
    # --------------------------------------------------------

    v_ratio = volume_ratio(
        c5,
        20
    )

    if v_ratio >= 1.2:
        setup_score += 4

    if v_ratio >= 1.5:
        setup_score += 6

    if v_ratio >= 2.0:
        setup_score += 7

    if v_ratio >= 2.5:
        setup_score += 5

    # --------------------------------------------------------
    # STRATEGIES
    # --------------------------------------------------------

    strategies = []

    strategies.append(
        horizontal_breakout(
            c5,
            level,
            direction
        )
    )

    strategies.append(
        trendline_breakout(
            c15,
            direction
        )
    )

    strategies.append(
        compression_strategy(
            c15,
            direction
        )
    )

    strategies.append(
        liquidity_sweep(
            c5,
            direction
        )
    )

    strategies = [
        s
        for s in strategies
        if s.valid
    ]

    if not strategies:
        return None

    strategies.sort(
        key=lambda x: (
            x.points,
            x.confidence
        ),
        reverse=True
    )

    strategy_result = strategies[0]

    setup_score += (
        strategy_result.points
    )

    # Multi-strategy confirmation.
    if len(strategies) >= 2:
        setup_score += 7

    if len(strategies) >= 3:
        setup_score += 7

    # --------------------------------------------------------
    # MOMENTUM
    # --------------------------------------------------------

    momentum_points, momentum_ok = (
        momentum_score(
            c5,
            direction
        )
    )

    if momentum_ok:

        momentum_score_value = (
            momentum_points
        )

    else:

        momentum_score_value = 0

    # --------------------------------------------------------
    # MARKET CONTEXT
    # --------------------------------------------------------

    relative_strength, market_mood = (
        market_context(
            tickers,
            ticker
        )
    )

    if direction == "LONG":

        if relative_strength >= 2:
            trade_score += 8

        elif relative_strength >= 1:
            trade_score += 5

        elif relative_strength <= -2:
            trade_score -= 7

    else:

        if relative_strength <= -2:
            trade_score += 8

        elif relative_strength <= -1:
            trade_score += 5

        elif relative_strength >= 2:
            trade_score -= 7

    sector_mood = sector_context(
        get_coin(inst_id),
        tickers
    )

    # --------------------------------------------------------
    # OI
    # --------------------------------------------------------

    oi_status, oi_change, oi_points = (
        oi_analysis(
            inst_id,
            oi_value
        )
    )

    trade_score += oi_points

    # --------------------------------------------------------
    # RETEST / FAKEOUT
    # --------------------------------------------------------

    retest = retest_quality(
        c5,
        level.price,
        direction
    )

    fakeout = fakeout_risk(
        c5,
        level.price,
        direction
    )

    trade_score += int(
        retest * 0.45
    )

    trade_score -= fakeout

    if fakeout >= 25:

        # We don't completely kill every setup,
        # because a sweep itself may be a valid strategy.
        if (
            strategy_result.name
            != "Liquidity Sweep Reversal"
        ):
            return None

    # --------------------------------------------------------
    # EXPECTED MOVE
    # --------------------------------------------------------

    expected_pct, expected_quality, obstacle = (
        expected_move_engine(
            current,
            direction,
            level,
            clusters,
            atr_value
        )
    )

    if expected_pct < 0.60:

        return None

    if expected_pct >= 1.0:
        trade_score += 8

    if expected_pct >= 1.5:
        trade_score += 8

    if expected_pct >= 2.0:
        trade_score += 8

    if expected_pct >= 3.0:
        trade_score += 5

    # --------------------------------------------------------
    # LIQUIDITY
    # --------------------------------------------------------

    liquidity = grade_liquidity(
        volume_24h
    )

    if liquidity == "HIGH":
        setup_score += 6

    elif liquidity == "GOOD":
        setup_score += 4

    elif liquidity == "MEDIUM":
        setup_score += 2

    # --------------------------------------------------------
    # TRADE LEVELS
    # --------------------------------------------------------

    trade_levels = build_trade_levels(
        current,
        level,
        direction,
        c15,
        atr_value
    )

    if trade_levels is None:
        return None

    (
        entry_low,
        entry_high,
        sl,
        tp1,
        tp2,
        tp3,
        risk_pct
    ) = trade_levels

    # --------------------------------------------------------
    # CHASE FILTER
    # --------------------------------------------------------

    if direction == "LONG":

        if current > (
            level.price
            * (
                1
                + MAX_CHASE_PCT / 100
            )
        ):

            # A very strong move may still be useful,
            # but only if expected move is exceptional.
            if expected_pct < 3.0:
                return None

    else:

        if current < (
            level.price
            * (
                1
                - MAX_CHASE_PCT / 100
            )
        ):

            if expected_pct < 3.0:
                return None

    # --------------------------------------------------------
    # RISK / REWARD
    # --------------------------------------------------------

    if risk_pct <= 0:
        return None

    rr = (
        expected_pct
        / risk_pct
    )

    if rr < 1.8:
        return None

    if rr >= 3:
        trade_score += 7

    if rr >= 4:
        trade_score += 7

    if rr >= 5:
        trade_score += 5

    # --------------------------------------------------------
    # FINAL THREE SCORES
    # --------------------------------------------------------

    setup_score = int(
        clamp(
            setup_score,
            0,
            100
        )
    )

    momentum_score_value = int(
        clamp(
            momentum_score_value
            + strategy_result.points
            // 2,
            0,
            100
        )
    )

    trade_score = int(
        clamp(
            trade_score,
            0,
            100
        )
    )

    # Weighted final score.
    final_score = int(
        clamp(
            setup_score * 0.40
            + momentum_score_value * 0.25
            + trade_score * 0.35,
            0,
            100
        )
    )

    # Strong expected move bonus.
    if expected_pct >= 2.0:

        final_score = min(
            100,
            final_score + 4
        )

    if final_score < MIN_SCORE:
        return None

    # --------------------------------------------------------
    # REASON
    # --------------------------------------------------------

    tf_text = ", ".join(
        level.timeframes
    )

    reason_parts = [
        strategy_result.reason,
        (
            f"Ключевая зона: "
            f"{tf_text}; "
            f"сила {level.strength}/100."
        ),
        (
            f"Возраст уровня: "
            f"{level.age_hours:.1f}h; "
            f"freshness {level.freshness_score:.0f}/100."
        ),
        (
            f"Expected move: "
            f"{expected_pct:.2f}%."
        ),
        (
            f"R:R ≈ {rr:.1f}."
        )
    ]

    if retest > 0:

        reason_parts.append(
            f"Retest quality: {retest}/20."
        )

    if fakeout > 0:

        reason_parts.append(
            f"Fakeout risk: {fakeout}/40."
        )

    if market_mood != "NEUTRAL":

        reason_parts.append(
            f"Market context: {market_mood}."
        )

    if sector_mood != "N/A":

        reason_parts.append(
            f"Sector: {sector_mood}."
        )

    return Setup(
        inst_id=inst_id,
        coin=get_coin(inst_id),
        direction=direction,
        strategy=strategy_result.name,
        level=level.price,
        level_strength=level.strength,
        level_tf=tf_text,
        level_age_hours=level.age_hours,
        level_freshness=level.freshness_score,
        level_touches=level.touches,
        current_price=current,
        entry_low=entry_low,
        entry_high=entry_high,
        sl=sl,
        tp1=tp1,
        tp2=tp2,
        tp3=tp3,
        score=final_score,
        setup_score=setup_score,
        momentum_score=momentum_score_value,
        trade_score=trade_score,
        liquidity=liquidity,
        volume_grade=grade_volume(
            v_ratio
        ),
        oi_status=oi_status,
        oi_change_pct=oi_change,
        market_regime=regime,
        market_context=market_mood,
        relative_strength=relative_strength,
        sector_context=sector_mood,
        expected_move_pct=expected_pct,
        risk_pct=risk_pct,
        risk_reward=rr,
        retest_quality=retest,
        fakeout_risk=fakeout,
        breakout_volume_ratio=v_ratio,
        atr_pct=volatility,
        setup_state=strategy_result.state,
        reason=" ".join(
            reason_parts
        ),
        volume_24h=volume_24h,
        candles_5m=c5[-80:]
    )


# ============================================================
# SIGNAL RANKING
# ============================================================

def ranking_bonus(
    setup: Setup
) -> float:

    bonus = 0

    if setup.expected_move_pct >= 2:
        bonus += 5

    if setup.expected_move_pct >= 3:
        bonus += 5

    if setup.risk_reward >= 4:
        bonus += 5

    if setup.risk_reward >= 5:
        bonus += 4

    if setup.level_touches >= 3:
        bonus += 3

    if setup.level_freshness >= 85:
        bonus += 3

    if setup.retest_quality >= 15:
        bonus += 3

    if setup.fakeout_risk <= 5:
        bonus += 3

    if setup.market_regime == "TRENDING":
        bonus += 2

    if setup.market_regime == "COMPRESSED":
        bonus += 2

    if setup.setup_state == "ACTIVE":
        bonus += 4

    return bonus


def rank_setups(
    setups: List[Setup]
) -> List[Setup]:

    scored = []

    for setup in setups:

        rank_score = (
            setup.score
            + ranking_bonus(setup)
        )

        scored.append(
            (
                rank_score,
                setup
            )
        )

    scored.sort(
        key=lambda x: (
            x[0],
            x[1].expected_move_pct,
            x[1].risk_reward
        ),
        reverse=True
    )

    result = []

    for _, setup in scored:

        # Prevent same coin from filling the whole ranking.
        if any(
            x.inst_id == setup.inst_id
            for x in result
        ):
            continue

        result.append(
            setup
        )

        if len(
            result
        ) >= MAX_RANKED_SIGNALS:
            break

    return result


# ============================================================
# SIGNAL CONTROL
# ============================================================

def can_send_new_signal(
    inst_id: str
) -> bool:

    current = now_ts()

    if inst_id in ready_setups:
        return False

    if signals_today >= (
        MAX_SIGNALS_PER_DAY
    ):
        return False

    cutoff = (
        current
        - 3600
    )

    signals_hour[:] = [
        x
        for x in signals_hour
        if x >= cutoff
    ]

    if len(
        signals_hour
    ) >= MAX_SIGNALS_PER_HOUR:
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

    return True


# ============================================================
# DATABASE
# ============================================================

def save_signal(
    setup: Setup,
    status: str
) -> int:

    created = now_ts()

    cursor = db.execute(
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
            setup_score,
            momentum_score,
            trade_score,
            expected_move_pct,
            risk_reward,
            status,
            created_at,
            expires_at
        )
        VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?
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
            setup.setup_score,
            setup.momentum_score,
            setup.trade_score,
            setup.expected_move_pct,
            setup.risk_reward,
            status,
            created,
            created
            + READY_TTL_MINUTES * 60
        )
    )

    db.commit()

    return int(
        cursor.lastrowid
    )


# ============================================================
# CHART
# ============================================================

def make_chart(
    setup: Setup
) -> str:

    candles = setup.candles_5m[-70:]

    if len(candles) < 10:
        raise RuntimeError(
            "Not enough candles."
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
            linewidth=1
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

        ax.add_patch(
            Rectangle(
                (
                    i - width / 2,
                    body_low
                ),
                width,
                body_height,
                facecolor=color,
                edgecolor=color
            )
        )

    # LEVEL
    ax.axhline(
        setup.level,
        color="#f5c542",
        linewidth=2.2,
        linestyle="--"
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
        linestyle="-."
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

    labels = [
        (
            setup.level,
            "LEVEL",
            "#f5c542"
        ),
        (
            setup.sl,
            "SL",
            "#ff3b30"
        ),
        (
            setup.tp1,
            "TP1",
            "#ffd166"
        ),
        (
            setup.tp2,
            "TP2",
            "#ffd166"
        ),
        (
            setup.tp3,
            "TP3",
            "#ffd166"
        )
    ]

    for price, text, color in labels:

        ax.text(
            last_x,
            price,
            f" {text}",
            color=color,
            va="bottom",
            fontsize=9,
            fontweight="bold"
        )

    ax.set_title(
        (
            f"{setup.coin}USDT | "
            f"{setup.direction} | "
            f"{setup.strategy}\n"
            f"Quantum {setup.score}/100 | "
            f"Expected {setup.expected_move_pct:.2f}%"
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
# TELEGRAM TEXT
# ============================================================

def build_signal_text(
    setup: Setup,
    state: str = "READY"
) -> str:

    label = score_label(
        setup.score
    )

    if state == "READY":

        state_text = (
            "🟡 *SETUP READY*\n"
            "Цена находится в рабочей зоне. "
            "Ждём подтверждение."
        )

    elif state == "ACTIVE":

        state_text = (
            "🟢 *ENTRY ACTIVE*\n"
            "Подтверждение получено."
        )

    else:

        state_text = state

    volume_m = (
        setup.volume_24h
        / 1_000_000
    )

    return (
        f"🔥 *{setup.coin}USDT — "
        f"{setup.direction}*\n\n"

        f"{label} · `{setup.strategy}`\n\n"

        f"💰 *Цена:* "
        f"`{fmt_price(setup.current_price)}`\n"

        f"📊 *24H оборот:* "
        f"${volume_m:,.1f}M\n"

        f"📈 *Volume:* "
        f"`{setup.breakout_volume_ratio:.2f}x`\n\n"

        f"{state_text}\n\n"

        f"🧠 *ЛОГИКА*\n"
        f"{setup.reason}\n\n"

        f"📍 *LEVEL*\n"
        f"`{fmt_price(setup.level)}`\n"
        f"TF: `{setup.level_tf}`\n"
        f"Touches: `{setup.level_touches}`\n"
        f"Age: `{setup.level_age_hours:.1f}h`\n"
        f"Freshness: `{setup.level_freshness:.0f}/100`\n\n"

        f"🎯 *ENTRY*\n"
        f"`{fmt_price(setup.entry_low)}` – "
        f"`{fmt_price(setup.entry_high)}`\n\n"

        f"🛑 *STOP LOSS*\n"
        f"`{fmt_price(setup.sl)}`\n"
        f"Risk: `−{setup.risk_pct:.2f}%`\n\n"

        f"🪜 *TAKE PROFITS*\n\n"

        f"TP1 — 30%\n"
        f"`{fmt_price(setup.tp1)}`\n\n"

        f"TP2 — 30%\n"
        f"`{fmt_price(setup.tp2)}`\n\n"

        f"TP3 — 40%\n"
        f"`{fmt_price(setup.tp3)}`\n\n"

        f"🔒 После TP1 → SL в BE\n\n"

        f"📐 *Expected Move:* "
        f"`{setup.expected_move_pct:.2f}%`\n"

        f"⚖️ *R:R:* "
        f"`{setup.risk_reward:.1f}`\n\n"

        f"🧠 *SCORES*\n"
        f"Setup: `{setup.setup_score}/100`\n"
        f"Momentum: `{setup.momentum_score}/100`\n"
        f"Trade: `{setup.trade_score}/100`\n\n"

        f"⭐ *QUANTUM SCORE:* "
        f"`{setup.score}/100` {label}\n\n"

        f"🌍 *Market:* "
        f"`{setup.market_regime}`\n"

        f"BTC/ETH context: "
        f"`{setup.market_context}`\n"

        f"Relative strength: "
        f"`{setup.relative_strength:+.2f}%`\n\n"

        f"💧 *Liquidity:* "
        f"`{setup.liquidity}`\n"

        f"📦 *Volume grade:* "
        f"`{setup.volume_grade}`\n"

        f"⚡ *OI:* "
        f"`{setup.oi_status}` "
        f"({setup.oi_change_pct:+.2f}%)\n"

        f"🌡 *ATR:* "
        f"`{setup.atr_pct:.2f}%`\n\n"

        f"🔄 Retest: "
        f"`{setup.retest_quality}/20`\n"

        f"⚠️ Fakeout risk: "
        f"`{setup.fakeout_risk}/40`\n\n"

        f"⏱ READY: "
        f"`{READY_TTL_MINUTES} мин`\n\n"

        f"*Не догоняем движение. "
        f"Качество важнее количества.*"
    )


# ============================================================
# TELEGRAM SEND
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
            f"{setup.strategy}\n"
            f"Expected move: "
            f"`{setup.expected_move_pct:.2f}%`"
        )

        with open(
            chart_path,
            "rb"
        ) as photo:

            sent_photo = bot.send_photo(
                CHANNEL_ID,
                photo,
                caption=caption,
                parse_mode="Markdown"
            )

        sent_text = bot.send_message(
            CHANNEL_ID,
            build_signal_text(
                setup,
                state
            ),
            parse_mode="Markdown"
        )

        return (
            sent_photo.message_id,
            sent_text.message_id
        )

    except Exception as exc:

        log.exception(
            "TELEGRAM FAILED | %s | %s",
            setup.inst_id,
            exc
        )

        return (
            None,
            None
        )

    finally:

        if chart_path:

            try:
                os.remove(
                    chart_path
                )
            except OSError:
                pass


# ============================================================
# READY EXPIRE
# ============================================================

def expire_old_ready():

    current = now_ts()

    for inst_id, ready in list(
        ready_setups.items()
    ):

        if current < ready.expires_at:
            continue

        setup = ready.setup

        ready_setups.pop(
            inst_id,
            None
        )

        db.execute(
            """
            UPDATE signals
            SET status = ?
            WHERE inst_id = ?
            AND status = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (
                "EXPIRED",
                inst_id,
                "READY"
            )
        )

        db.commit()

        try:

            bot.send_message(
                CHANNEL_ID,
                (
                    f"🔴 *SETUP EXPIRED — "
                    f"{setup.coin}USDT*\n\n"
                    f"Подтверждение не пришло "
                    f"в течение {READY_TTL_MINUTES} минут.\n"
                    f"Рынок не догоняем."
                ),
                parse_mode="Markdown"
            )

        except Exception:

            log.exception(
                "EXPIRE MESSAGE FAILED"
            )


# ============================================================
# MORNING
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

    try:

        bot.send_message(
            CHANNEL_ID,
            (
                "🌅 *ДОБРОЕ УТРО, РЕБЯТА!*\n\n"
                "QUANTUM SCALPER V5 начинает "
                "новый торговый день.\n\n"
                "🔎 Сканируем весь рынок.\n"
                "📍 Ищем свежие intraday уровни.\n"
                "📐 Ловим горизонтальные и "
                "диагональные пробои.\n"
                "⚡ Ищем compression expansion.\n"
                "💧 Контролируем liquidity sweeps.\n"
                "🧠 Проверяем BTC/ETH context.\n"
                "🎯 Отбираем только лучшие "
                "risk/reward возможности.\n\n"
                "*Качество важнее количества.* 🚀"
            ),
            parse_mode="Markdown"
        )

        last_morning_date = today

    except Exception:

        log.exception(
            "MORNING FAILED"
        )


# ============================================================
# DAY RESET
# ============================================================

def reset_daily_counter():

    global signals_today
    global last_day

    today = local_now().date()

    if last_day != today:

        signals_today = 0
        last_day = today

        log.info(
            "DAILY COUNTER RESET"
        )


# ============================================================
# SIGNAL PUBLISH
# ============================================================

def publish_setup(
    setup: Setup
) -> bool:

    global signals_today

    if not can_send_new_signal(
        setup.inst_id
    ):
        return False

    status = (
        "READY"
        if setup.setup_state == "READY"
        else "ACTIVE"
    )

    signal_id = save_signal(
        setup,
        status
    )

    photo_id, text_id = (
        send_photo_and_text(
            setup,
            status
        )
    )

    if (
        photo_id is None
        or text_id is None
    ):

        db.execute(
            """
            UPDATE signals
            SET status = ?
            WHERE id = ?
            """,
            (
                "SEND_FAILED",
                signal_id
            )
        )

        db.commit()

        return False

    created = now_ts()

    ready_setups[
        setup.inst_id
    ] = ActiveReady(
        setup=setup,
        created_at=created,
        expires_at=(
            created
            + READY_TTL_MINUTES * 60
        ),
        telegram_photo_id=photo_id,
        telegram_text_id=text_id
    )

    signals_hour.append(
        created
    )

    signals_today += 1

    log.info(
        "SIGNAL PUBLISHED | %s | %s | "
        "strategy=%s score=%s expected=%.2f",
        setup.coin,
        setup.direction,
        setup.strategy,
        setup.score,
        setup.expected_move_pct
    )

    return True


# ============================================================
# FETCH CANDLES FOR SYMBOL
# ============================================================

def fetch_symbol_candles(
    inst_id: str
) -> Optional[
    Dict[str, List[Candle]]
]:

    bars = {
        "1D": 100,
        "4H": 100,
        "1H": 120,
        "30m": 120,
        "15m": 150,
        "5m": 180
    }

    result = {}

    for bar, limit in bars.items():

        try:

            result[bar] = get_candles(
                inst_id,
                bar,
                limit
            )

        except Exception as exc:

            log.warning(
                "CANDLES FAILED | %s | %s | %s",
                inst_id,
                bar,
                exc
            )

            result[bar] = []

    return result


# ============================================================
# MAIN SCAN
# ============================================================

def scan_market(
    instruments: Dict[str, dict],
    tickers: Dict[str, dict]
):

    global scan_count

    scan_count += 1

    # --------------------------------------------------------
    # FAST FILTER
    # --------------------------------------------------------

    ranked_market = []

    for inst_id, ticker in tickers.items():

        if inst_id not in instruments:
            continue

        volume = float(
            ticker.get(
                "vol24h_usd",
                0
            )
            or 0
        )

        if volume < MIN_24H_VOLUME_USD:
            continue

        fast_score = fast_market_score(
            ticker
        )

        if fast_score <= 0:
            continue

        ranked_market.append(
            (
                fast_score,
                volume,
                inst_id
            )
        )

    ranked_market.sort(
        reverse=True
    )

    ranked_market = ranked_market[
        :MAX_SYMBOLS
    ]

    candidates = ranked_market[
        :MAX_CANDIDATES
    ]

    log.info(
        "SCAN #%s | market=%s candidates=%s",
        scan_count,
        len(ranked_market),
        len(candidates)
    )

    # --------------------------------------------------------
    # FIRST PASS
    # --------------------------------------------------------

    setups = []

    for _, _, inst_id in candidates:

        ticker = tickers.get(
            inst_id
        )

        if not ticker:
            continue

        try:

            candles = fetch_symbol_candles(
                inst_id
            )

            setup = analyze_symbol(
                inst_id,
                ticker,
                candles,
                tickers,
                None
            )

            if setup:

                setups.append(
                    setup
                )

        except Exception as exc:

            log.exception(
                "ANALYZE FAILED | %s | %s",
                inst_id,
                exc
            )

    if not setups:

        return

    # --------------------------------------------------------
    # OI ENRICHMENT ONLY FOR BEST FIRST-PASS SETUPS
    # --------------------------------------------------------

    setups.sort(
        key=lambda x: (
            x.score,
            x.expected_move_pct
        ),
        reverse=True
    )

    oi_targets = setups[
        :OI_MAX_REQUESTS_PER_SCAN
    ]

    enriched = []

    for setup in oi_targets:

        oi_value = get_open_interest(
            setup.inst_id
        )

        try:

            candles = fetch_symbol_candles(
                setup.inst_id
            )

            updated = analyze_symbol(
                setup.inst_id,
                tickers[
                    setup.inst_id
                ],
                candles,
                tickers,
                oi_value
            )

            if updated:

                enriched.append(
                    updated
                )

        except Exception as exc:

            log.exception(
                "OI REANALYZE FAILED | %s | %s",
                setup.inst_id,
                exc
            )

    # If enrichment failed for some,
    # retain first-pass setups.
    enriched_ids = {
        x.inst_id
        for x in enriched
    }

    for setup in setups:

        if (
            setup.inst_id
            not in enriched_ids
            and len(enriched) < MAX_CANDIDATES
        ):

            enriched.append(
                setup
            )

    # --------------------------------------------------------
    # FINAL RANKING
    # --------------------------------------------------------

    final_setups = rank_setups(
        enriched
    )

    log.info(
        "FINAL RANKING | %s setups",
        len(final_setups)
    )

    # --------------------------------------------------------
    # PUBLISH
    # --------------------------------------------------------

    for setup in final_setups:

        if signals_today >= (
            MAX_SIGNALS_PER_DAY
        ):
            break

        if not can_send_new_signal(
            setup.inst_id
        ):
            continue

        publish_setup(
            setup
        )


# ============================================================
# STARTUP MESSAGE
# ============================================================

def send_startup_message():

    try:

        bot.send_message(
            CHANNEL_ID,
            (
                "🟢 *QUANTUM SCALPER V5 ONLINE*\n\n"
                "TOP SIGNAL ENGINE активирован.\n\n"
                "Стратегии:\n"
                "• Horizontal Breakout\n"
                "• Diagonal Breakout\n"
                "• Compression Expansion\n"
                "• Liquidity Sweep Reversal\n\n"
                "Дополнительно:\n"
                "• Fresh intraday levels\n"
                "• Market regime\n"
                "• BTC/ETH context\n"
                "• Relative strength\n"
                "• Expected move\n"
                "• R:R\n"
                "• Retest quality\n"
                "• Fakeout protection\n"
                "• Signal ranking\n\n"
                "*Система работает в режиме "
                "анализа рынка. Торговые операции "
                "не выполняются.*"
            ),
            parse_mode="Markdown"
        )

    except Exception:

        log.exception(
            "STARTUP MESSAGE FAILED"
        )


# ============================================================
# HEALTH LOG
# ============================================================

def health_log():

    if scan_count % 20 != 0:
        return

    log.info(
        "HEALTH | scans=%s ready=%s "
        "signals_today=%s cache=%s",
        scan_count,
        len(ready_setups),
        signals_today,
        len(candle_cache)
    )


# ============================================================
# MAIN
# ============================================================

def main():

    global signals_hour

    reset_daily_counter()

    log.info(
        "QUANTUM SCALPER V5 STARTING"
    )

    instruments = get_instruments()

    log.info(
        "LIVE USDT SWAPS: %s",
        len(instruments)
    )

    send_startup_message()

    last_instruments_refresh = now_ts()

    while True:

        try:

            reset_daily_counter()

            send_morning_message()

            expire_old_ready()

            # Refresh instruments periodically.
            if (
                now_ts()
                - last_instruments_refresh
                > 3600
            ):

                try:

                    instruments = (
                        get_instruments()
                    )

                    last_instruments_refresh = (
                        now_ts()
                    )

                except Exception:

                    log.exception(
                        "INSTRUMENT REFRESH FAILED"
                    )

            # Remove old hourly timestamps.
            cutoff = (
                now_ts()
                - 3600
            )

            signals_hour[:] = [
                x
                for x in signals_hour
                if x >= cutoff
            ]

            # Market tickers.
            tickers = get_tickers()

            if not tickers:

                log.warning(
                    "NO TICKERS"
                )

                time.sleep(
                    SCAN_INTERVAL_SECONDS
                )

                continue

            scan_market(
                instruments,
                tickers
            )

            health_log()

        except KeyboardInterrupt:

            log.info(
                "SHUTDOWN"
            )

            break

        except Exception:

            log.exception(
                "MAIN LOOP ERROR"
            )

        time.sleep(
            SCAN_INTERVAL_SECONDS
        )


# ============================================================
# ENTRY
# ============================================================

if __name__ == "__main__":

    main()
