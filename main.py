import os
import html
import re
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
# QUANTUM SCALPER V4
#
# OKX PUBLIC MARKET DATA
#
# V4 ARCHITECTURE:
#
# ALL USDT SWAPS
#       ↓
# FAST MARKET FILTER
#       ↓
# ACTIVITY / VOLUME / VOLATILITY
#       ↓
# MULTI-TIMEFRAME LEVEL ENGINE
#       ↓
# LEVEL CLUSTERS
#       ↓
# 1H / 4H STRUCTURE
#       ↓
# 15M / 30M SETUP
#       ↓
# 5M TRIGGER / PRE-TRIGGER
#       ↓
# VOLUME / OI / ATR
#       ↓
# QUANTUM SCORE
#       ↓
# READY
#       ↓
# ACTIVE
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

# Минимальный приблизительный 24H оборот.
MIN_24H_VOLUME_USD = float(
    os.getenv(
        "MIN_24H_VOLUME_USD",
        "60000000"
    )
)

# Ограничение глубокой обработки.
# 0 = без искусственного лимита: после фильтра $60M+
# бот анализирует ВСЕ доступные ликвидные USDT-инструменты.
# Переменные оставлены для совместимости с окружением.
MAX_SYMBOLS = int(
    os.getenv(
        "MAX_SYMBOLS",
        "0"
    )
)

# Ограничение кандидатов после быстрого фильтра.
# 0 = без ограничения.
MAX_CANDIDATES = int(
    os.getenv(
        "MAX_CANDIDATES",
        "0"
    )
)

# Если объём ниже этого уровня — монета не рассматривается.
MIN_CANDIDATE_VOLUME_USD = float(
    os.getenv(
        "MIN_CANDIDATE_VOLUME_USD",
        "60000000"
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

# Для особо сильных сетапов можно отправлять независимо
# от небольшого недостатка одного из вторичных факторов.
ELITE_SCORE = int(
    os.getenv(
        "ELITE_SCORE",
        "92"
    )
)


# ============================================================
# READY
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

# Расстояние, на котором начинаем искать PRE-BREAKOUT.
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

SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "20"))
FAST_PASS_SECONDS = int(os.getenv("FAST_PASS_SECONDS", "60"))
DEEP_CANDIDATES = int(os.getenv("DEEP_CANDIDATES", "40"))
FINAL_SIGNAL_CANDIDATES = int(os.getenv("FINAL_SIGNAL_CANDIDATES", "10"))

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

REPORT_HOUR = int(os.getenv("REPORT_HOUR", "23"))
REPORT_MINUTE = int(os.getenv("REPORT_MINUTE", "55"))


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
    parse_mode="HTML"
)


# ============================================================
# HTTP
# ============================================================

session = requests.Session()

session.headers.update({
    "User-Agent": "QuantumScalper/4.0",
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

db.execute("""
CREATE TABLE IF NOT EXISTS market_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    inst_id TEXT NOT NULL,
    direction TEXT NOT NULL,
    strategy TEXT NOT NULL,
    pattern TEXT NOT NULL DEFAULT '',
    volume_ratio REAL DEFAULT 0,
    oi_change_pct REAL,
    funding_rate REAL,
    result TEXT DEFAULT '',
    r_multiple REAL DEFAULT 0,
    created_at REAL NOT NULL
)
""")

db.commit()


# ============================================================
# DATABASE MIGRATIONS
# ============================================================

def ensure_column(table: str, column: str, definition: str):
    cols = {
        row[1]
        for row in db.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in cols:
        db.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
        )
        db.commit()


# Result tracking / exactly-once public notification state.
for _column, _definition in (
    ("tp1_hit", "INTEGER NOT NULL DEFAULT 0"),
    ("tp2_hit", "INTEGER NOT NULL DEFAULT 0"),
    ("tp3_hit", "INTEGER NOT NULL DEFAULT 0"),
    ("result", "TEXT"),
    ("result_r", "REAL"),
    ("closed_at", "REAL"),
    ("be_hit", "INTEGER NOT NULL DEFAULT 0"),
    ("result_notified", "INTEGER NOT NULL DEFAULT 0"),
):
    ensure_column("signals", _column, _definition)


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
    distance_pct: float = 0.0


@dataclass
class LevelCluster:
    price: float
    strength: int
    timeframes: List[str]
    kinds: List[str]
    distance_pct: float


@dataclass
class Setup:
    inst_id: str
    coin: str

    direction: str
    strategy: str

    level: float
    level_strength: int
    level_tf: str

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

    reason: str

    volume_24h: float
    breakout_volume_ratio: float

    atr_pct: float

    setup_state: str

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
last_fast_pass_ts = 0.0
watchlist = []

scan_count = 0

signals_today = 0

last_morning_date = None


# ============================================================
# CANDLE CACHE
# ============================================================

candle_cache: Dict[
    Tuple[str, str],
    Tuple[float, List[Candle]]
] = {}

CANDLE_CACHE_SECONDS = int(
    os.getenv(
        "CANDLE_CACHE_SECONDS",
        "12"
    )
)


# ============================================================
# HELPERS
# ============================================================

def now_ts() -> float:
    return time.time()


def local_now() -> datetime:
    return datetime.now(
        ZoneInfo(TIMEZONE)
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

    if ratio >= 2.5:
        return "EXTREME"

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

    if score >= 80:
        return "⚡ HIGH QUALITY"

    return "🟡 SIGNAL"


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
                    "OKX returned invalid JSON."
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
                "%s | attempt=%s/%s | %s",
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
                ),
            }

        except (
            TypeError,
            ValueError
        ):

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

            vol_24h = float(
                item.get(
                    "vol24h",
                    0
                ) or 0
            )

            vol_ccy_24h = float(
                item.get(
                    "volCcy24h",
                    0
                ) or 0
            )

            open24h = float(
                item.get(
                    "open24h",
                    0
                ) or 0
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

            # Для SWAP используем несколько способов
            # оценки оборота и выбираем разумное значение.
            #
            # Основной вариант:
            # volCcy24h * last.
            #
            # Если поле отсутствует/нулевое,
            # используем vol24h * last.

            turnover = (
                vol_ccy_24h
                * last
            )

            fallback_turnover = (
                vol_24h
                * last
            )

            if turnover <= 0:
                turnover = fallback_turnover

            # Нормализуем экстремально большие значения.
            # Это дополнительная защита от неверного формата.
            if (
                fallback_turnover > 0
                and turnover > fallback_turnover * 1000
            ):
                turnover = fallback_turnover

            result[inst_id] = {
                "last": last,
                "open24h": open24h,
                "high24h": high24h,
                "low24h": low24h,
                "vol24h": vol_24h,
                "vol_ccy_24h": vol_ccy_24h,
                "vol24h_usd": turnover,
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

    cache_key = (
        inst_id,
        bar
    )

    cached = candle_cache.get(
        cache_key
    )

    if cached:

        cached_ts, cached_data = cached

        if (
            now_ts()
            - cached_ts
            < CANDLE_CACHE_SECONDS
        ):
            return cached_data

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

    candle_cache[
        cache_key
    ] = (
        now_ts(),
        candles
    )

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
# DERIVATIVES CONTEXT
# ============================================================

oi_cache: Dict[str, Tuple[float, float]] = {}


def get_open_interest_context(inst_id: str) -> Tuple[Optional[float], Optional[float]]:
    current = get_open_interest(inst_id)
    if current is None:
        return None, None
    ts = now_ts()
    previous = oi_cache.get(inst_id)
    oi_cache[inst_id] = (ts, current)
    if previous is None:
        return current, None
    prev_ts, prev_value = previous
    if ts - prev_ts < 120 or prev_value <= 0:
        return current, None
    return current, (current - prev_value) / prev_value * 100.0


def get_funding_rate(inst_id: str) -> Optional[float]:
    try:
        payload = okx_get("/api/v5/public/funding-rate", {"instId": inst_id})
        data = payload.get("data", [])
        if not data:
            return None
        value = data[0].get("fundingRate")
        return float(value) if value not in (None, "") else None
    except Exception as exc:
        log.warning("FUNDING FAILED | %s | %s", inst_id, exc)
        return None


def derivatives_context_score(direction, price_change_pct, oi_change_pct, funding_rate):
    points = 0
    parts = []
    if oi_change_pct is not None:
        if direction == "LONG" and price_change_pct > 0 and oi_change_pct > 0:
            points += 5
            parts.append("цена↑ + OI↑")
        elif direction == "SHORT" and price_change_pct < 0 and oi_change_pct > 0:
            points += 5
            parts.append("цена↓ + OI↑")
        elif oi_change_pct < 0:
            points += 1
            parts.append("OI снижается")
    if funding_rate is not None:
        if abs(funding_rate) >= 0.0008:
            points -= 3
            parts.append("экстремальный funding")
        elif abs(funding_rate) <= 0.0002:
            points += 2
            parts.append("умеренный funding")
    return int(clamp(points, -3, 7)), ", ".join(parts)


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
        candles[-1].quote_volume
    )

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


# ============================================================
# VOLATILITY
# ============================================================

def atr_pct(
    candles: List[Candle]
) -> float:

    if not candles:
        return 0.0

    price = candles[-1].close

    if price <= 0:
        return 0.0

    value = atr(
        candles,
        14
    )

    return (
        value
        / price
        * 100.0
    )


def candle_body_ratio(
    candle: Candle
) -> float:

    full_range = (
        candle.high
        - candle.low
    )

    if full_range <= 0:
        return 0.0

    body = abs(
        candle.close
        - candle.open
    )

    return (
        body
        / full_range
    )


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

    ema20 = ema(
        closes,
        20
    )[-1]

    ema50 = ema(
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
# LEVEL ENGINE
# ============================================================

def add_pivot_levels(
    levels: List[Level],
    candles: List[Candle],
    timeframe: str,
    direction: str,
    current: float,
    strength: int
):

    if direction == "LONG":

        for _, price in pivot_highs(
            candles
        )[-30:]:

            if price > current:

                levels.append(
                    Level(
                        price=price,
                        timeframe=timeframe,
                        strength=strength,
                        kind="SWING_HIGH",
                        distance_pct=abs(
                            pct(
                                current,
                                price
                            )
                        )
                    )
                )

    else:

        for _, price in pivot_lows(
            candles
        )[-30:]:

            if price < current:

                levels.append(
                    Level(
                        price=price,
                        timeframe=timeframe,
                        strength=strength,
                        kind="SWING_LOW",
                        distance_pct=abs(
                            pct(
                                current,
                                price
                            )
                        )
                    )
                )


def add_range_levels(
    levels: List[Level],
    candles: List[Candle],
    timeframe: str,
    direction: str,
    current: float,
    strength: int
):

    if len(candles) < 20:
        return

    recent = candles[-80:]

    highs = [
        c.high
        for c in recent
    ]

    lows = [
        c.low
        for c in recent
    ]

    high = max(
        highs
    )

    low = min(
        lows
    )

    if direction == "LONG":

        if high > current:

            levels.append(
                Level(
                    price=high,
                    timeframe=timeframe,
                    strength=strength,
                    kind="RANGE_HIGH",
                    distance_pct=abs(
                        pct(
                            current,
                            high
                        )
                    )
                )
            )

    else:

        if low < current:

            levels.append(
                Level(
                    price=low,
                    timeframe=timeframe,
                    strength=strength,
                    kind="RANGE_LOW",
                    distance_pct=abs(
                        pct(
                            current,
                            low
                        )
                    )
                )
            )


def build_levels(
    current: float,
    direction: str,
    candles: Dict[str, List[Candle]]
) -> List[Level]:

    levels = []

    # --------------------------------------------------------
    # WEIGHT BY TIMEFRAME
    # --------------------------------------------------------

    weights = {
        "1D": 30,
        "4H": 25,
        "1H": 20,
        "30m": 15,
        "15m": 12,
        "5m": 8,
    }

    for tf, strength in weights.items():

        data = candles.get(
            tf,
            []
        )

        if len(data) < 25:
            continue

        add_pivot_levels(
            levels,
            data,
            tf,
            direction,
            current,
            strength
        )

        add_range_levels(
            levels,
            data,
            tf,
            direction,
            current,
            max(
                strength - 5,
                5
            )
        )

    return levels


# ============================================================
# LEVEL CLUSTERING
# ============================================================

def cluster_levels(
    levels: List[Level],
    current: float
) -> List[LevelCluster]:

    if not levels:
        return []

    # Чем волатильнее рынок,
    # тем шире допустимая зона объединения.
    base_cluster_pct = 0.18

    sorted_levels = sorted(
        levels,
        key=lambda x: x.price
    )

    clusters: List[List[Level]] = []

    for level in sorted_levels:

        placed = False

        for cluster in clusters:

            average_price = (
                sum(
                    x.price
                    for x in cluster
                )
                / len(cluster)
            )

            distance = abs(
                pct(
                    level.price,
                    average_price
                )
            )

            dynamic_width = (
                base_cluster_pct
                + min(
                    level.distance_pct * 0.08,
                    0.18
                )
            )

            if distance <= dynamic_width:

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

        # Повторное подтверждение разными TF
        # значительно усиливает кластер.
        unique_tfs = list(
            dict.fromkeys(
                x.timeframe
                for x in cluster
            )
        )

        unique_kinds = list(
            dict.fromkeys(
                x.kind
                for x in cluster
            )
        )

        strength = min(
            100,
            total_weight
            + max(
                0,
                len(unique_tfs) - 1
            ) * 8
        )

        result.append(
            LevelCluster(
                price=weighted_price,
                strength=strength,
                timeframes=unique_tfs,
                kinds=unique_kinds,
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


# ============================================================
# BEST LEVEL
# ============================================================

def best_level(
    clusters: List[LevelCluster],
    current: float
) -> Optional[LevelCluster]:

    if not clusters:
        return None

    # Берём не просто ближайший,
    # а баланс силы + расстояния.
    candidates = []

    for cluster in clusters:

        if cluster.distance_pct > 1.50:
            continue

        distance_penalty = (
            cluster.distance_pct
            * 18.0
        )

        quality = (
            cluster.strength
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
# COMPRESSION
# ============================================================

def compression_analysis(
    candles: List[Candle],
    direction: str
) -> Tuple[int, bool, str]:

    if len(candles) < 30:
        return 0, False, ""

    recent = candles[-24:]

    ranges = [
        c.high - c.low
        for c in recent
        if c.high > c.low
    ]

    if len(ranges) < 18:
        return 0, False, ""

    first = ranges[:10]
    last = ranges[-10:]

    first_avg = (
        sum(first)
        / len(first)
    )

    last_avg = (
        sum(last)
        / len(last)
    )

    if first_avg <= 0:
        return 0, False, ""

    compression = (
        1.0
        - last_avg
        / first_avg
    )

    score = 0

    valid = False

    if direction == "LONG":

        lows = [
            value
            for _, value
            in pivot_lows(
                recent
            )[-4:]
        ]

        if len(lows) >= 2:

            rising = all(
                lows[i]
                <= lows[i + 1]
                for i in range(
                    len(lows) - 1
                )
            )

            if rising:

                score += 15
                valid = True

    else:

        highs = [
            value
            for _, value
            in pivot_highs(
                recent
            )[-4:]
        ]

        if len(highs) >= 2:

            falling = all(
                highs[i]
                >= highs[i + 1]
                for i in range(
                    len(highs) - 1
                )
            )

            if falling:

                score += 15
                valid = True

    if compression >= 0.10:
        score += 5

    if compression >= 0.18:
        score += 5

    if compression >= 0.28:
        score += 5

    if valid:

        reason = (
            "На 15M/30M сформировалось "
            "сжатие перед ключевой зоной."
        )

    else:

        reason = ""

    return (
        min(
            score,
            30
        ),
        valid,
        reason
    )


# ============================================================
# MOMENTUM
# ============================================================

def momentum_analysis(
    candles: List[Candle],
    direction: str
) -> Tuple[int, bool, str]:

    if len(candles) < 35:
        return 0, False, ""

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

    previous = candles[
        -13:-1
    ]

    if len(previous) < 8:
        return 0, False, ""

    score = 0

    if direction == "LONG":

        previous_high = max(
            c.high
            for c in previous
        )

        if e9 > e21:
            score += 7

        if current.close > previous_high:
            score += 15

        body = candle_body_ratio(
            current
        )

        if body >= 0.55:
            score += 5

        valid = (
            e9 > e21
            and current.close > previous_high
        )

    else:

        previous_low = min(
            c.low
            for c in previous
        )

        if e9 < e21:
            score += 7

        if current.close < previous_low:
            score += 15

        body = candle_body_ratio(
            current
        )

        if body >= 0.55:
            score += 5

        valid = (
            e9 < e21
            and current.close < previous_low
        )

    if not valid:

        return (
            0,
            False,
            ""
        )

    return (
        min(
            score,
            27
        ),
        True,
        "5M подтверждает импульсное движение "
        "в сторону ключевого уровня."
    )


# ============================================================
# PRE-TRIGGER
# ============================================================

def pre_trigger_analysis(
    current: float,
    level: LevelCluster,
    candles_5m: List[Candle],
    direction: str
) -> Tuple[int, bool, str]:

    if not candles_5m:
        return 0, False, ""

    distance = abs(
        pct(
            current,
            level.price
        )
    )

    if distance > PRE_TRIGGER_DISTANCE_PCT:
        return 0, False, ""

    recent = candles_5m[-8:]

    if direction == "LONG":

        # Цена должна подходить к сопротивлению,
        # а не удаляться от него.
        closes = [
            c.close
            for c in recent
        ]

        approaching = (
            closes[-1]
            >= closes[0]
        )

        if not approaching:
            return 0, False, ""

        score = 12

        if distance <= 0.20:
            score += 5

        if distance <= 0.10:
            score += 3

        return (
            score,
            True,
            "Цена заранее подошла к сильной зоне "
            "и формирует подготовку к пробою."
        )

    else:

        closes = [
            c.close
            for c in recent
        ]

        approaching = (
            closes[-1]
            <= closes[0]
        )

        if not approaching:
            return 0, False, ""

        score = 12

        if distance <= 0.20:
            score += 5

        if distance <= 0.10:
            score += 3

        return (
            score,
            True,
            "Цена заранее подошла к сильной зоне "
            "и формирует подготовку к пробою."
        )


# ============================================================
# HORIZONTAL BREAKOUT
# ============================================================

def breakout_analysis(
    current: float,
    level: LevelCluster,
    previous: Candle,
    current_candle: Candle,
    direction: str
) -> Tuple[int, bool, str]:

    price = level.price

    if direction == "LONG":

        crossed = (
            current_candle.close > price
            and previous.close <= price
        )

        if crossed:

            body = candle_body_ratio(
                current_candle
            )

            points = 22

            if body >= 0.55:
                points += 4

            return (
                points,
                True,
                "Подтверждённая 5M свеча закрылась "
                "выше сильной зоны сопротивления."
            )

    else:

        crossed = (
            current_candle.close < price
            and previous.close >= price
        )

        if crossed:

            body = candle_body_ratio(
                current_candle
            )

            points = 22

            if body >= 0.55:
                points += 4

            return (
                points,
                True,
                "Подтверждённая 5M свеча закрылась "
                "ниже сильной зоны поддержки."
            )

    return (
        0,
        False,
        ""
    )


# ============================================================
# LEVEL REACTION
# ============================================================

def level_reaction(
    candles_5m: List[Candle],
    level: float,
    direction: str
) -> Tuple[int, bool]:

    if len(candles_5m) < 8:
        return 0, False

    recent = candles_5m[-12:]

    touches = 0

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

    if touches == 0:
        return 0, False

    if touches >= 3:
        return 12, True

    if touches == 2:
        return 8, True

    return 4, True


# ============================================================
# MARKET ACTIVITY
# ============================================================

def activity_score(
    candles_5m: List[Candle],
    ticker: dict
) -> Tuple[int, float]:

    if len(candles_5m) < 25:
        return 0, 0.0

    ratio = volume_ratio(
        candles_5m,
        20
    )

    score = 0

    if ratio >= 1.20:
        score += 4

    if ratio >= 1.50:
        score += 5

    if ratio >= 2.00:
        score += 6

    if ratio >= 2.50:
        score += 5

    # 24H range activity.
    high24h = float(
        ticker.get(
            "high24h",
            0
        )
        or 0
    )

    low24h = float(
        ticker.get(
            "low24h",
            0
        )
        or 0
    )

    current = float(
        ticker.get(
            "last",
            0
        )
        or 0
    )

    if (
        current > 0
        and high24h > low24h
    ):

        range_pct = (
            (
                high24h
                - low24h
            )
            / current
            * 100.0
        )

        if range_pct >= 1.0:
            score += 2

        if range_pct >= 2.0:
            score += 3

        if range_pct >= 4.0:
            score += 3

    return (
        min(
            score,
            25
        ),
        ratio
    )


# ============================================================
# FAST MARKET RANKING
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
        return 0.0

    score = 0.0

    # Liquidity.
    if volume >= 1_000_000_000:
        score += 30

    elif volume >= 500_000_000:
        score += 25

    elif volume >= 250_000_000:
        score += 20

    elif volume >= 100_000_000:
        score += 15

    elif volume >= 60_000_000:
        score += 10

    else:
        score += 5

    # 24H volatility.
    if high > low:

        range_pct = (
            high
            - low
        ) / price * 100.0

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
# ANALYZE SYMBOL
# ============================================================

def analyze_symbol(
    inst_id: str,
    ticker: dict,
    candles: Dict[str, List[Candle]]
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

    c1h = candles.get(
        "1H",
        []
    )

    c4h = candles.get(
        "4H",
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

    c5 = candles.get(
        "5m",
        []
    )

    confirmed_5m = [
        c
        for c in c5
        if c.confirmed
    ]

    if len(c1h) < 60:
        return None

    if len(c4h) < 50:
        return None

    if len(c15) < 50:
        return None

    if len(confirmed_5m) < 35:
        return None

    # --------------------------------------------------------
    # 1H + 4H STRUCTURE
    # --------------------------------------------------------

    structure_1h = structure_direction(
        c1h
    )

    structure_4h = structure_direction(
        c4h
    )

    if structure_1h == "NEUTRAL":
        return None

    direction = structure_1h

    # 4H против 1H не запрещает сетап полностью,
    # но сильно снижает качество.
    higher_tf_alignment = (
        structure_4h == direction
    )

    score = 0

    score += 15

    if higher_tf_alignment:
        score += 12

    else:
        score -= 5

    score += structure_strength(
        c1h,
        direction
    )

    score += structure_strength(
        c4h,
        direction
    ) // 2

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
        "5m": confirmed_5m,
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

    # Не берём слишком далёкие уровни.
    if level.distance_pct > 1.20:
        return None

    # Сильный кластер.
    score += int(
        clamp(
            level.strength * 0.35,
            5,
            25
        )
    )

    # Несколько старших TF.
    higher_tfs = sum(
        1
        for tf in level.timeframes
        if tf in (
            "1D",
            "4H",
            "1H"
        )
    )

    if higher_tfs >= 2:
        score += 8

    if higher_tfs >= 3:
        score += 5

    # --------------------------------------------------------
    # ATR
    # --------------------------------------------------------

    atr_value = atr(
        confirmed_5m,
        14
    )

    if atr_value <= 0:
        return None

    volatility_pct = (
        atr_value
        / current
        * 100.0
    )

    if volatility_pct < 0.025:
        return None

    if volatility_pct > 3.0:
        return None

    # --------------------------------------------------------
    # VOLUME
    # --------------------------------------------------------

    activity_points, v_ratio = (
        activity_score(
            confirmed_5m,
            ticker
        )
    )

    score += activity_points

    # --------------------------------------------------------
    # LEVEL REACTION
    # --------------------------------------------------------

    reaction_points, reaction_ok = (
        level_reaction(
            confirmed_5m,
            level.price,
            direction
        )
    )

    if reaction_ok:
        score += reaction_points

    # --------------------------------------------------------
    # STRATEGIES
    # --------------------------------------------------------

    current_candle = confirmed_5m[-1]
    previous = confirmed_5m[-2]

    strategies = []

    # 1. Actual breakout.
    breakout_points, breakout_ok, breakout_reason = (
        breakout_analysis(
            current,
            level,
            previous,
            current_candle,
            direction
        )
    )

    if breakout_ok:

        strategies.append(
            (
                "Horizontal Level Breakout",
                breakout_points,
                breakout_reason,
                "ACTIVE"
            )
        )

    # 2. Compression.
    compression_points, compression_ok, compression_reason = (
        compression_analysis(
            c15,
            direction
        )
    )

    if compression_ok:

        strategies.append(
            (
                "Trendline Compression Breakout",
                compression_points,
                compression_reason,
                "READY"
            )
        )

    # 3. Momentum.
    momentum_points, momentum_ok, momentum_reason = (
        momentum_analysis(
            confirmed_5m,
            direction
        )
    )

    if momentum_ok:

        strategies.append(
            (
                "Momentum Breakout",
                momentum_points,
                momentum_reason,
                "ACTIVE"
            )
        )

    # 4. Pre-trigger.
    pre_points, pre_ok, pre_reason = (
        pre_trigger_analysis(
            current,
            level,
            confirmed_5m,
            direction
        )
    )

    if pre_ok:

        strategies.append(
            (
                "Pre-Breakout Level Setup",
                pre_points,
                pre_reason,
                "READY"
            )
        )

    if not strategies:
        return None

    strategies.sort(
        key=lambda item: item[1],
        reverse=True
    )

    strategy, strategy_points, reason, setup_state = (
        strategies[0]
    )

    score += strategy_points

    # --------------------------------------------------------
    # MULTI-STRATEGY BONUS
    # --------------------------------------------------------

    if len(strategies) >= 2:
        score += 5

    if len(strategies) >= 3:
        score += 5

    # --------------------------------------------------------
    # LIQUIDITY
    # --------------------------------------------------------

    volume_24h = float(
        ticker.get(
            "vol24h_usd",
            0
        )
        or 0
    )

    if volume_24h < MIN_CANDIDATE_VOLUME_USD:
        return None

    if volume_24h >= 1_000_000_000:

        liquidity = "HIGH"
        score += 6

    elif volume_24h >= 500_000_000:

        liquidity = "HIGH"
        score += 5

    elif volume_24h >= 250_000_000:

        liquidity = "GOOD"
        score += 4

    elif volume_24h >= 100_000_000:

        liquidity = "GOOD"
        score += 3

    else:

        liquidity = "MEDIUM"
        score += 1

    # --------------------------------------------------------
    # OI
    # --------------------------------------------------------

    oi_value = get_open_interest(
        inst_id
    )

    if oi_value is not None:

        oi_status = "AVAILABLE"
        score += 3

    else:

        oi_status = "N/A"

    # --------------------------------------------------------
    # DISTANCE / CHASE
    # --------------------------------------------------------

    if level.distance_pct > 0.75:
        return None

    # После пробоя не берём сильно убежавшую цену.
    if direction == "LONG":

        if current > (
            level.price
            * (
                1
                + MAX_CHASE_PCT / 100.0
            )
        ):

            return None

    else:

        if current < (
            level.price
            * (
                1
                - MAX_CHASE_PCT / 100.0
            )
        ):

            return None

    # --------------------------------------------------------
    # ENTRY ZONE
    # --------------------------------------------------------

    zone_pct = clamp(
        volatility_pct * 0.40,
        0.08,
        0.30
    )

    if direction == "LONG":

        entry_low = (
            level.price
            * (
                1
                - zone_pct / 100.0
            )
        )

        entry_high = (
            level.price
            * (
                1
                + zone_pct / 100.0
            )
        )

    else:

        entry_low = (
            level.price
            * (
                1
                - zone_pct / 100.0
            )
        )

        entry_high = (
            level.price
            * (
                1
                + zone_pct / 100.0
            )
        )

    # --------------------------------------------------------
    # STRUCTURAL STOP
    # --------------------------------------------------------

    recent_15 = c15[-24:]

    if len(recent_15) < 12:
        return None

    if direction == "LONG":

        structural_low = min(
            c.low
            for c in recent_15
        )

        # Для long SL ниже структуры.
        sl = (
            structural_low
            - atr_value * 0.30
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
            + atr_value * 0.30
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

    if risk_pct > 2.20:
        return None

    # --------------------------------------------------------
    # TAKE PROFITS
    # --------------------------------------------------------

    # TP строим от текущего риска.
    # Это сохраняет знакомую нам лестницу.
    if direction == "LONG":

        tp1 = current + risk * 1.0
        tp2 = current + risk * 2.0
        tp3 = current + risk * 3.0

    else:

        tp1 = current - risk * 1.0
        tp2 = current - risk * 2.0
        tp3 = current - risk * 3.0

    # --------------------------------------------------------
    # FINAL SCORE
    # --------------------------------------------------------

    score = int(
        clamp(
            score,
            0,
            100
        )
    )

    if score < MIN_SCORE:
        return None

    # --------------------------------------------------------
    # REASON
    # --------------------------------------------------------

    tf_text = ", ".join(
        level.timeframes
    )

    cluster_reason = (
        f"Ключевой кластер уровня "
        f"подтверждён таймфреймами: {tf_text}. "
        f"Сила зоны: {level.strength}/100."
    )

    full_reason = (
        f"{reason} "
        f"{cluster_reason}"
    )

    return Setup(
        inst_id=inst_id,
        coin=get_coin(inst_id),
        direction=direction,
        strategy=strategy,
        level=level.price,
        level_strength=level.strength,
        level_tf=tf_text,
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
        reason=full_reason,
        volume_24h=volume_24h,
        breakout_volume_ratio=v_ratio,
        atr_pct=volatility_pct,
        setup_state=setup_state,
        candles_5m=confirmed_5m[-80:]
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

    # --------------------------------------------------------
    # DAILY LIMIT
    # --------------------------------------------------------

    if signals_today >= (
        MAX_SIGNALS_PER_DAY
    ):
        return False

    # --------------------------------------------------------
    # COOLDOWN
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # HOURLY LIMIT
    # --------------------------------------------------------

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
        linewidth=2.2,
        linestyle="--",
        label="LEVEL CLUSTER"
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
            f"5M trigger"
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
# TELEGRAM HTML SAFETY
# ============================================================

def telegram_html(message: str) -> str:
    """
    Convert the bot's legacy Markdown-like markup to Telegram HTML.
    Dynamic market values are escaped so coin/strategy names cannot
    break Telegram entity parsing.
    """
    if not message:
        return ""

    # Escape first, then restore only our intentional formatting tokens.
    s = html.escape(str(message), quote=False)

    # Bold
    s = re.sub(r"\*([^*\n]+)\*", r"<b>\1</b>", s)

    # Inline code
    s = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", s)

    # Telegram/HTML accepts ordinary emoji and text.
    return s

# ============================================================
# TELEGRAM SIGNAL
# ============================================================

def build_signal_text(
    setup: Setup,
    state: str = "READY"
) -> str:

    if state == "READY":

        state_line = (
            "🟡 *SETUP READY*\n"
            "Рынок находится возле рабочей зоны. "
            "Ждём подтверждение и не догоняем цену."
        )

    elif state == "ACTIVE":

        state_line = (
            "🟡 *SETUP READY*\n"
            "Подтверждение контролируется системой."
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

        f"🪜 *ЗАКРЫТИЕ ЛЕСЕНКОЙ*\n\n"

        f"TP1 — 30%\n"
        f"`{fmt_price(setup.tp1)}`\n\n"

        f"TP2 — 30%\n"
        f"`{fmt_price(setup.tp2)}`\n\n"

        f"TP3 — 40%\n"
        f"`{fmt_price(setup.tp3)}`\n\n"

        f"🔒 После TP1 → SL в BE\n\n"

        f"📊 *Стратегия:*\n"
        f"`{setup.strategy}`\n\n"

        f"📍 *LEVEL CLUSTER:*\n"
        f"`{setup.level_tf}`\n"
        f"`{fmt_price(setup.level)}`\n"
        f"Сила зоны: `{setup.level_strength}/100`\n\n"

        f"💧 *Ликвидность:* "
        f"`{setup.liquidity}`\n"

        f"📦 *Volume grade:* "
        f"`{setup.volume_grade}`\n"

        f"⚡ *OI:* "
        f"`{setup.oi_status}`\n"

        f"🌡 *ATR:* "
        f"`{setup.atr_pct:.2f}%`\n\n"

        f"⏱ *READY действует:* "
        f"`{READY_TTL_MINUTES} мин`\n\n"

        f"💎 *Дисциплина. Терпение. Чёткое исполнение.*\n"
        f"🔥 Следим за движением и работаем строго по плану.\n"
        f"Не догоняем цену."
    )


# ============================================================
# TELEGRAM REACTION
# ============================================================

def add_signal_reactions(message_id: Optional[int]):
    if not message_id:
        return
    try:
        reaction_type = getattr(telebot.types, "ReactionTypeEmoji", None)
        if reaction_type is None:
            return
        bot.set_message_reaction(
            CHANNEL_ID,
            message_id,
            reaction=[reaction_type("🔥")]
        )
    except Exception as exc:
        log.debug("REACTION NOT SET | %s", exc)


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
                    caption=telegram_html(caption),
                    parse_mode="HTML",
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
                parse_mode="HTML"
            )
        )

        add_signal_reactions(sent_text.message_id)

        log.info(
            "TELEGRAM SENT | %s | %s | score=%s | state=%s",
            setup.coin,
            setup.direction,
            setup.score,
            state
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

        "QUANTUM SCALPER V4 начинает новый "
        "торговый день.\n\n"

        "🔎 Ищем рынок широко.\n"
        "📍 Проверяем уровни на нескольких ТФ.\n"
        "🧠 Ищем сильные зоны.\n"
        "🟡 Сигнал даём заранее.\n"
        "🚫 Не догоняем движение.\n"
        "🛑 Не увеличиваем риск.\n\n"

        "*Качество важнее количества.*\n\n"

        "Всем продуктивного торгового дня! 🚀"
    )

    try:

        bot.send_message(
            CHANNEL_ID,
            message,
            parse_mode="HTML"
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

        # Expiration is tracked internally, but no public Telegram message is sent.
        # Public channel updates are reserved for TP1/TP2/TP3 or SL.



# ============================================================
# PATTERN FILTER / SETUP SELECTION


def _pivot_series(candles, highs=True):
    vals = pivot_highs(candles) if highs else pivot_lows(candles)
    return [v for _, v in vals[-6:]]


def _avg_range(candles):
    if not candles:
        return 0.0
    return sum(max(c.high - c.low, 0.0) for c in candles) / len(candles)


def _avg_volume(candles):
    if not candles:
        return 0.0
    return sum(max(c.volume, 0.0) for c in candles) / len(candles)


def _trend_slope(values):
    if len(values) < 2:
        return 0.0
    return (values[-1] - values[0]) / max(abs(values[0]), 1e-12)


def _pattern_breakout_state(candles, direction, lookback=24):
    if len(candles) < lookback + 2:
        return False, False, False
    prior = candles[-lookback-1:-1]
    last = candles[-1]
    prev = candles[-2]
    avg_vol = _avg_volume(prior[-20:])
    if direction == "LONG":
        boundary = max(c.high for c in prior)
        breakout = last.close > boundary and prev.close <= boundary
        retest = (
            (last.low <= boundary * 1.0015 and last.close > boundary)
            or (prev.low <= boundary * 1.0015 and last.close > boundary)
        )
    else:
        boundary = min(c.low for c in prior)
        breakout = last.close < boundary and prev.close >= boundary
        retest = (
            (last.high >= boundary * 0.9985 and last.close < boundary)
            or (prev.high >= boundary * 0.9985 and last.close < boundary)
        )
    volume_confirmed = avg_vol > 0 and last.volume >= avg_vol * 1.35
    return breakout, retest, volume_confirmed


def chart_pattern_analysis(candles_15m, candles_5m, direction):
    """Rule-based chart pattern/confluence engine.

    Pattern names are backed by price/volume geometry; the names themselves are
    never used as a trigger. This is an additional quality layer over V4.
    """
    if len(candles_15m) < 60:
        return 0, False, ""

    recent = candles_15m[-48:]
    highs = _pivot_series(recent, True)
    lows = _pivot_series(recent, False)
    ref = max(recent[-1].close, 1e-12)
    points = 0
    patterns = []

    # Ascending / descending / symmetrical triangles.
    if len(highs) >= 3 and len(lows) >= 3:
        high_span = (max(highs[-3:]) - min(highs[-3:])) / ref
        low_span = (max(lows[-3:]) - min(lows[-3:])) / ref
        hs = _trend_slope(highs[-3:])
        ls = _trend_slope(lows[-3:])

        if direction == "LONG" and high_span <= 0.012 and ls > 0.002:
            points += 14
            patterns.append("ascending triangle")
        elif direction == "SHORT" and low_span <= 0.012 and hs < -0.002:
            points += 14
            patterns.append("descending triangle")
        elif high_span <= 0.020 and low_span <= 0.020 and hs < -0.001 and ls > 0.001:
            points += 12
            patterns.append("symmetrical triangle")

        if high_span <= 0.018 and low_span <= 0.018:
            points += 5
            patterns.append("compression")

        # Falling/rising wedge.
        if hs < -0.001 and ls < -0.001 and high_span < 0.025 and low_span < 0.025 and direction == "LONG":
            points += 10
            patterns.append("falling wedge")
        elif hs > 0.001 and ls > 0.001 and high_span < 0.025 and low_span < 0.025 and direction == "SHORT":
            points += 10
            patterns.append("rising wedge")

        # Double top / bottom with a real middle reaction.
        if direction == "SHORT":
            h1, h2 = highs[-3], highs[-1]
            middle = min(lows[-3:]) if lows else 0.0
            if h1 > 0 and abs(h2-h1)/h1 <= 0.009 and middle < min(h1, h2) * 0.985:
                points += 12
                patterns.append("double top")
        else:
            l1, l2 = lows[-3], lows[-1]
            middle = max(highs[-3:]) if highs else 0.0
            if l1 > 0 and abs(l2-l1)/l1 <= 0.009 and middle > max(l1, l2) * 1.015:
                points += 12
                patterns.append("double bottom")

    # Bull/bear flag or pennant: impulse followed by contraction and quieter volume.
    if len(recent) >= 30:
        impulse = abs(recent[-30].close - recent[-18].close) / max(abs(recent[-30].close), 1e-12) * 100
        old_range = _avg_range(recent[-30:-15])
        new_range = _avg_range(recent[-15:])
        old_vol = _avg_volume(recent[-30:-15])
        new_vol = _avg_volume(recent[-15:])
        contraction = new_range / old_range if old_range > 0 else 1.0
        vol_contract = new_vol / old_vol if old_vol > 0 else 1.0
        if impulse >= 1.0 and contraction <= 0.72:
            points += 11
            patterns.append("flag/pennant")
            if vol_contract <= 0.85:
                points += 3
                patterns.append("volume contraction")

    # Rectangle / range breakout preparation.
    if len(recent) >= 24:
        box = recent[-24:]
        box_high = max(c.high for c in box)
        box_low = min(c.low for c in box)
        box_width = (box_high - box_low) / ref
        near_top = sum(1 for c in box if abs(c.high-box_high)/ref <= 0.003)
        near_bottom = sum(1 for c in box if abs(c.low-box_low)/ref <= 0.003)
        if box_width <= 0.045 and near_top >= 2 and near_bottom >= 2:
            points += 8
            patterns.append("rectangle/range")

    # Live breakout + retest + volume confirmation on 5M.
    breakout, retest, volume_confirmed = _pattern_breakout_state(candles_5m, direction)
    if breakout:
        points += 12
        patterns.append("breakout")
    if volume_confirmed and breakout:
        points += 7
        patterns.append("breakout volume confirmation")
    if retest:
        points += 10
        patterns.append("breakout + retest")

    if not patterns:
        return 0, False, ""

    unique_patterns = list(dict.fromkeys(patterns))
    if len(unique_patterns) >= 2:
        points += 4
    if len(unique_patterns) >= 3:
        points += 4

    return min(points, 40), True, "Паттерн/структура: " + ", ".join(unique_patterns) + "."


def pattern_quality_gate(setup: Setup, candles: Dict[str, List[Candle]]) -> Tuple[bool, str]:
    c5 = [c for c in candles.get("5m", []) if c.confirmed]
    c15 = [c for c in candles.get("15m", []) if c.confirmed]
    c1 = [c for c in candles.get("1H", []) if c.confirmed]
    c4 = [c for c in candles.get("4H", []) if c.confirmed]

    if min(len(c5), len(c15), len(c1), len(c4)) < 30:
        return False, "insufficient confirmed candles"

    last = c5[-1]
    avg_vol = _avg_volume(c5[-21:-1])
    vol_ratio = last.volume / avg_vol if avg_vol > 0 else 0.0
    body = candle_body_ratio(last)

    if structure_direction(c1) != setup.direction:
        return False, "1H structure mismatch"
    if structure_direction(c4) != setup.direction:
        return False, "4H structure mismatch"

    if setup.setup_state == "ACTIVE":
        if setup.direction == "LONG" and last.close <= setup.level:
            return False, "breakout not closed above level"
        if setup.direction == "SHORT" and last.close >= setup.level:
            return False, "breakout not closed below level"
        if body < 0.45:
            return False, "weak breakout body"
        if vol_ratio < 1.20:
            return False, "weak breakout volume"

    if setup.setup_state == "READY":
        distance = abs(pct(setup.current_price, setup.level))
        if distance > PRE_TRIGGER_DISTANCE_PCT:
            return False, "READY too far from level"
        p_score, p_ok, _ = chart_pattern_analysis(c15, c5, setup.direction)
        if not p_ok or p_score < 12:
            return False, "pattern evidence too weak"

    return True, "passed"


def analyze_symbol_with_patterns(inst_id, ticker, candles):
    setup = analyze_symbol(inst_id, ticker, candles)
    if setup is None:
        return None

    p5, ok, reason = chart_pattern_analysis(
        candles.get("15m", []), candles.get("5m", []), setup.direction
    )

    if ok:
        setup.score = int(clamp(setup.score + p5, 0, 100))
        setup.reason = setup.reason + " " + reason
    elif setup.setup_state == "READY":
        return None

    c5 = [c for c in candles.get("5m", []) if c.confirmed]
    price_change = 0.0
    if len(c5) >= 13 and c5[-13].close > 0:
        price_change = (c5[-1].close - c5[-13].close) / c5[-13].close * 100.0

    oi_value, oi_change = get_open_interest_context(inst_id)
    funding = get_funding_rate(inst_id)
    d_points, d_reason = derivatives_context_score(
        setup.direction, price_change, oi_change, funding
    )
    setup.score = int(clamp(setup.score + d_points, 0, 100))

    if oi_value is not None:
        setup.oi_status = (
            f"OI {oi_value:,.0f} USD"
            + (f" | Δ {oi_change:+.2f}%" if oi_change is not None else "")
        )

    if d_reason:
        setup.reason += " Деривативный контекст: " + d_reason + "."

    passed, gate_reason = pattern_quality_gate(setup, candles)
    if not passed:
        log.info("QUALITY GATE REJECT | %s | %s", inst_id, gate_reason)
        return None

    return setup


# RESULT TRACKING
# ============================================================

def ensure_result_columns():
    cols = {
        "tp1_hit": "INTEGER DEFAULT 0",
        "tp2_hit": "INTEGER DEFAULT 0",
        "tp3_hit": "INTEGER DEFAULT 0",
        "result": "TEXT DEFAULT ''",
        "closed_at": "REAL",
        "r_multiple": "REAL DEFAULT 0",
    }
    existing = {row[1] for row in db.execute("PRAGMA table_info(signals)").fetchall()}
    for name, definition in cols.items():
        if name not in existing:
            db.execute(f"ALTER TABLE signals ADD COLUMN {name} {definition}")
    db.commit()

ensure_result_columns()


def _row_risk(row):
    entry = float(row[4])
    sl = float(row[7])
    return abs(entry - sl)


def _send_result_update(inst_id, direction, result):
    """Send one public result update. Only this function publishes trade results."""
    coin = get_coin(inst_id)
    if result == "TP1":
        message = f"🟢 *TP1 — {coin}USDT*\n\nПервая цель достигнута. Стоп переносится в безубыток по логике сигнала."
    elif result == "TP2":
        message = f"🟢 *TP2 — {coin}USDT*\n\nВторая цель достигнута. Движение развивается по плану."
    elif result == "TP3":
        message = f"💎 *TP3 — {coin}USDT*\n\nФинальная цель достигнута. Сделка завершена."
    elif result == "SL":
        message = f"🔴 *STOP LOSS — {coin}USDT*\n\nСделка закрыта по стопу. Дисциплина прежде всего."
    elif result == "BE":
        message = f"🟡 *BREAK EVEN — {coin}USDT*\n\nСделка закрыта в безубытке."
    else:
        return
    try:
        bot.send_message(CHANNEL_ID, telegram_html(message), parse_mode="HTML")
    except Exception:
        log.exception("RESULT TELEGRAM ERROR | %s | %s", inst_id, result)


def update_signal_results():
    rows = db.execute("""
        SELECT id, inst_id, direction, strategy, level, entry_low, entry_high,
               sl, tp1, tp2, tp3, score, status, created_at, expires_at,
               tp1_hit, tp2_hit, tp3_hit, result
        FROM signals
        WHERE status IN ('READY','ACTIVE')
        ORDER BY id ASC
    """).fetchall()

    for row in rows:
        sid, inst_id, direction = row[0], row[1], row[2]
        try:
            cs = get_candles(inst_id, "5m", 80)
            if len(cs) < 2:
                continue
            c = cs[-1]
            price = c.close
        except Exception:
            continue

        status = row[12]
        entry_low, entry_high = float(row[5]), float(row[6])
        sl, tp1, tp2, tp3 = map(float, (row[7], row[8], row[9], row[10]))
        tp1_hit, tp2_hit, tp3_hit = map(int, (row[15] or 0, row[16] or 0, row[17] or 0))

        # READY -> ACTIVE when price actually reaches the published entry zone.
        if status == "READY":
            touched = entry_low <= price <= entry_high
            if touched:
                db.execute("UPDATE signals SET status='ACTIVE', activated_at=? WHERE id=?", (now_ts(), sid))
                status = "ACTIVE"
            elif now_ts() > float(row[14]):
                db.execute("UPDATE signals SET status='EXPIRED', result='EXPIRED', closed_at=? WHERE id=?", (now_ts(), sid))
                continue

        if status != "ACTIVE":
            continue

        # Conservative intrabar rule: if SL and TP are both touched by the same
        # candle and the order is ambiguous, SL is counted first.
        sl_hit = (c.low <= sl) if direction == "LONG" else (c.high >= sl)
        tp1_now = (c.high >= tp1) if direction == "LONG" else (c.low <= tp1)
        tp2_now = (c.high >= tp2) if direction == "LONG" else (c.low <= tp2)
        tp3_now = (c.high >= tp3) if direction == "LONG" else (c.low <= tp3)

        # A result flag is also the de-duplication key. Once a target is marked
        # hit in SQLite, later scan cycles cannot publish it again.
        new_tp1 = bool(tp1_now and not tp1_hit)
        new_tp2 = bool(tp2_now and not tp2_hit)
        new_tp3 = bool(tp3_now and not tp3_hit)

        if sl_hit and not tp1_hit and not tp2_hit and not tp3_hit and not (new_tp1 or new_tp2 or new_tp3):
            db.execute("UPDATE signals SET status='CLOSED', result='SL', closed_at=?, r_multiple=-1 WHERE id=? AND status='ACTIVE'", (now_ts(), sid))
            if db.rowcount:
                db.commit()
                _send_result_update(inst_id, direction, "SL")
            continue

        if new_tp1:
            db.execute("UPDATE signals SET tp1_hit=1 WHERE id=? AND tp1_hit=0", (sid,))
            if db.rowcount:
                tp1_hit = 1
                db.commit()
                _send_result_update(inst_id, direction, "TP1")
        if new_tp2:
            db.execute("UPDATE signals SET tp2_hit=1 WHERE id=? AND tp2_hit=0", (sid,))
            if db.rowcount:
                tp2_hit = 1
                db.commit()
                _send_result_update(inst_id, direction, "TP2")
        if new_tp3:
            db.execute("UPDATE signals SET tp3_hit=1 WHERE id=? AND tp3_hit=0", (sid,))
            if db.rowcount:
                tp3_hit = 1
                db.commit()
                _send_result_update(inst_id, direction, "TP3")

        if tp3_hit:
            db.execute("UPDATE signals SET status='CLOSED', result='TP3', closed_at=?, r_multiple=3 WHERE id=? AND status='ACTIVE'", (now_ts(), sid))
        elif tp2_hit:
            db.execute("UPDATE signals SET status='ACTIVE', result='TP2', r_multiple=2 WHERE id=? AND status='ACTIVE'", (sid,))
        elif tp1_hit:
            # Do not evaluate BE on the same candle that first hit TP1.
            # The stop can move to BE only on a later scan/candle.
            if not new_tp1:
                be_hit = (c.low <= entry_high) if direction == "LONG" else (c.high >= entry_low)
                if be_hit:
                    db.execute("UPDATE signals SET status='CLOSED', result='BE', closed_at=?, r_multiple=0 WHERE id=? AND status='ACTIVE'", (now_ts(), sid))
                    if db.rowcount:
                        db.commit()
                        _send_result_update(inst_id, direction, "BE")
                        continue
            db.execute("UPDATE signals SET status='ACTIVE', result='TP1', r_multiple=1 WHERE id=? AND status='ACTIVE'", (sid,))

    db.commit()


# ============================================================
# REPORTS
# ============================================================

def report_period(start_ts, end_ts, title):
    rows = db.execute("""
        SELECT strategy, result, r_multiple, score, status
        FROM signals
        WHERE created_at >= ? AND created_at < ?
    """, (start_ts, end_ts)).fetchall()

    total = len(rows)
    closed = [r for r in rows if r[1] in ("SL", "BE", "TP3")]
    sl = sum(1 for r in rows if r[1] == "SL")
    be = sum(1 for r in rows if r[1] == "BE")
    tp3 = sum(1 for r in rows if r[1] == "TP3")
    tp1 = sum(1 for r in rows if r[1] in ("TP1", "TP2", "TP3"))
    tp2 = sum(1 for r in rows if r[1] in ("TP2", "TP3"))
    r_sum = sum(float(r[2] or 0) for r in rows)
    avg_r = (r_sum / len(closed)) if closed else 0.0
    outcome_rate = ((tp3 + be) / len(closed) * 100.0) if closed else 0.0
    tp1_rate = (tp1 / total * 100.0) if total else 0.0
    tp2_rate = (tp2 / total * 100.0) if total else 0.0
    tp3_rate = (tp3 / total * 100.0) if total else 0.0

    lines = [
        f"📊 *{title}*",
        "",
        f"Сигналов: *{total}*",
        f"Закрыто: *{len(closed)}*",
        f"TP1: *{tp1}* | TP2: *{tp2}* | TP3: *{tp3}*",
        f"SL: *{sl}* | BE: *{be}*",
        f"TP1 hit rate: *{tp1_rate:.1f}%* | TP2: *{tp2_rate:.1f}%* | TP3: *{tp3_rate:.1f}%*",
        f"Закрытие без SL (TP3/BE): *{outcome_rate:.1f}%*",
        f"Суммарно R: *{r_sum:+.2f}R*",
        f"Средний R закрытых: *{avg_r:+.2f}R*",
    ]

    strategies = {}
    for strategy, result, r, score, status in rows:
        d = strategies.setdefault(strategy, {"n":0,"closed":0,"r":0.0,"tp3":0,"sl":0})
        d["n"] += 1
        if result in ("SL", "BE", "TP3"):
            d["closed"] += 1
        d["r"] += float(r or 0)
        d["tp3"] += int(result == "TP3")
        d["sl"] += int(result == "SL")

    if strategies:
        lines += ["", "*По стратегиям:"]
        for strategy, d in sorted(strategies.items(), key=lambda x: x[0]):
            lines.append(f"• {strategy}: {d['n']} сигналов | {d['tp3']} TP3 | {d['sl']} SL | {d['r']:+.2f}R")

    if not rows:
        lines += ["", "Нет сигналов за период."]

    return "\n".join(lines)


def report_boundaries():
    n = local_now()
    day_start = datetime(n.year, n.month, n.day, tzinfo=ZoneInfo(TIMEZONE))
    next_day = day_start + __import__('datetime').timedelta(days=1)
    # Calendar week: Monday-Sunday.
    week_start = day_start - __import__('datetime').timedelta(days=day_start.weekday())
    next_week = week_start + __import__('datetime').timedelta(days=7)
    month_start = datetime(n.year, n.month, 1, tzinfo=ZoneInfo(TIMEZONE))
    if n.month == 12:
        next_month = datetime(n.year + 1, 1, 1, tzinfo=ZoneInfo(TIMEZONE))
    else:
        next_month = datetime(n.year, n.month + 1, 1, tzinfo=ZoneInfo(TIMEZONE))
    return (
        day_start.timestamp(), next_day.timestamp(),
        week_start.timestamp(), next_week.timestamp(),
        month_start.timestamp(), next_month.timestamp()
    )


def send_reports_once_per_day():
    n = local_now()
    if (n.hour, n.minute) < (REPORT_HOUR, REPORT_MINUTE):
        return
    key = f"reports_{n.date().isoformat()}"
    if db.execute("SELECT 1 FROM bot_state WHERE key=?", (key,)).fetchone():
        return

    ds, de, ws, we, ms, me = report_boundaries()
    messages = [
        report_period(ds, de, "ОТЧЁТ ЗА СЕГОДНЯ"),
        report_period(ws, we, "ОТЧЁТ ЗА НЕДЕЛЮ"),
        report_period(ms, me, "ОТЧЁТ ЗА МЕСЯЦ"),
    ]
    try:
        for message in messages:
            bot.send_message(CHANNEL_ID, message)
        db.execute("INSERT OR REPLACE INTO bot_state(key,value) VALUES(?,?)", (key, "1"))
        db.commit()
        log.info("REPORTS SENT | %s", n.date())
    except Exception:
        log.exception("REPORTS SEND FAILED")


# ============================================================
# MAIN SCANNER
# ============================================================

def load_candles_for_symbol(inst_id):
    return {
        "1D": get_candles(inst_id, "1D", 120),
        "4H": get_candles(inst_id, "4H", 120),
        "1H": get_candles(inst_id, "1H", 120),
        "30m": get_candles(inst_id, "30m", 120),
        "15m": get_candles(inst_id, "15m", 120),
        "5m": get_candles(inst_id, "5m", 120),
    }


def scan_market():
    global signals_today, last_fast_pass_ts, watchlist

    n = local_now()
    day_key = n.date().isoformat()
    state_key = "signals_day"
    saved_day = db.execute("SELECT value FROM bot_state WHERE key=?", (state_key,)).fetchone()
    if not saved_day or saved_day[0] != day_key:
        signals_today = 0
        db.execute("INSERT OR REPLACE INTO bot_state(key,value) VALUES(?,?)", (state_key, day_key))
        db.commit()

    if signals_today >= MAX_SIGNALS_PER_DAY:
        return

    instruments = get_instruments()
    tickers = get_tickers()
    candidates = []

    for inst_id, ticker in tickers.items():
        if inst_id not in instruments:
            continue
        volume = float(ticker.get("vol24h_usd", 0) or 0)
        if volume < MIN_24H_VOLUME_USD:
            continue
        candidates.append((fast_market_score(ticker), volume, inst_id, ticker))

    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)

    now = now_ts()
    if now - last_fast_pass_ts >= FAST_PASS_SECONDS or not watchlist:
        watchlist = candidates[:DEEP_CANDIDATES] if DEEP_CANDIDATES > 0 else candidates
        last_fast_pass_ts = now
        log.info("WATCHLIST UPDATED | liquid=%s | deep=%s", len(candidates), len(watchlist))

    setups = []
    for _, _, inst_id, ticker in watchlist:
        if not can_send_new_signal(inst_id):
            continue
        try:
            candles = load_candles_for_symbol(inst_id)
            setup = analyze_symbol_with_patterns(inst_id, ticker, candles)
            if setup is not None and setup.score >= MIN_SCORE:
                setups.append(setup)
        except Exception as exc:
            log.warning("ANALYZE FAILED | %s | %s", inst_id, exc)

    setups.sort(key=lambda x: x.score, reverse=True)
    final_setups = setups[:FINAL_SIGNAL_CANDIDATES] if FINAL_SIGNAL_CANDIDATES > 0 else setups

    for setup in final_setups:
        if not can_send_new_signal(setup.inst_id):
            continue
        photo_id, text_id = send_photo_and_text(setup, setup.setup_state)
        if photo_id or text_id:
            save_signal(setup, setup.setup_state)
            if setup.setup_state == "READY":
                ready_setups[setup.inst_id] = ActiveReady(
                    setup=setup,
                    created_at=now_ts(),
                    expires_at=now_ts() + READY_TTL_MINUTES * 60,
                    telegram_message_id=text_id,
                    photo_message_id=photo_id,
                )
            signals_hour.append(now_ts())
            signals_today += 1
            if signals_today >= MAX_SIGNALS_PER_DAY:
                break


def main():
    log.info("QUANTUM SCALPER V7 STARTED")
    while True:
        try:
            send_morning_message()
            expire_old_ready()
            update_signal_results()
            send_reports_once_per_day()
            scan_market()
        except KeyboardInterrupt:
            log.info("STOPPED")
            break
        except Exception:
            log.exception("MAIN LOOP ERROR")
        time.sleep(SCAN_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
