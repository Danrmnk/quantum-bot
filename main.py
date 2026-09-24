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
# QUANTUM LEVEL ENGINE V10
#
# Product principle:
#   rare, selective technical situations;
#   no guaranteed direction;
#   alert BEFORE the level is tested;
#   silence when the structure is weak.
#
# Main setups:
#   1) APPROACHING SUPPORT
#   2) APPROACHING RESISTANCE
#   3) APPROACHING CLEAN TRENDLINE
#   4) BREAKOUT + RETEST
# ============================================================

from dataclasses import dataclass, field
from collections import defaultdict
from pathlib import Path
import math

LEVEL_SCAN_INTERVAL = int(os.getenv("LEVEL_SCAN_INTERVAL", "30"))
# Scalping: 15M = structural setup, 5M = entry/approach timing.
LEVEL_SCALP_CHART_5M = int(os.getenv("LEVEL_SCALP_CHART_5M", "96"))
LEVEL_SCALP_CHART_15M = int(os.getenv("LEVEL_SCALP_CHART_15M", "96"))
LEVEL_DEEP_CANDIDATES = int(os.getenv("LEVEL_DEEP_CANDIDATES", "40"))
LEVEL_FINAL_COUNT = int(os.getenv("LEVEL_FINAL_COUNT", "3"))
LEVEL_MIN_VOLUME = float(os.getenv("LEVEL_MIN_VOLUME", "60000000"))
LEVEL_COOLDOWN_MINUTES = int(os.getenv("LEVEL_COOLDOWN_MINUTES", "45"))
LEVEL_CHART_CANDLES = int(os.getenv("LEVEL_CHART_CANDLES", "96"))
LEVEL_MAX_MESSAGES_PER_SCAN = int(os.getenv("LEVEL_MAX_MESSAGES_PER_SCAN", "1"))
LEVEL_MIN_TESTS = int(os.getenv("LEVEL_MIN_TESTS", "3"))
LEVEL_MAX_DISTINCT_TESTS = int(os.getenv("LEVEL_MAX_DISTINCT_TESTS", "7"))
LEVEL_MIN_REACTIONS = int(os.getenv("LEVEL_MIN_REACTIONS", "2"))
LEVEL_MAX_DISTANCE_PCT = float(os.getenv("LEVEL_MAX_DISTANCE_PCT", "1.20"))
LEVEL_APPROACH_MIN_PCT = float(os.getenv("LEVEL_APPROACH_MIN_PCT", "0.15"))
LEVEL_APPROACH_MAX_ATR = float(os.getenv("LEVEL_APPROACH_MAX_ATR", "1.10"))
LEVEL_ZONE_ATR_MULT = float(os.getenv("LEVEL_ZONE_ATR_MULT", "0.10"))
LEVEL_MAX_ZONE_PCT = float(os.getenv("LEVEL_MAX_ZONE_PCT", "0.30"))
LEVEL_TRENDLINE_MIN_TOUCHES = int(os.getenv("LEVEL_TRENDLINE_MIN_TOUCHES", "3"))
LEVEL_MIN_QUALITY = int(os.getenv("LEVEL_MIN_QUALITY", "82"))
LEVEL_MAX_SAME_COIN = int(os.getenv("LEVEL_MAX_SAME_COIN", "1"))
LEVEL_STATE_DB = os.getenv("LEVEL_STATE_DB", DB_PATH)

LEVEL_TF_ORDER = ("1D", "4H", "1H", "30m", "15m", "5m")
LEVEL_TF_WEIGHTS = {"1D": 34, "4H": 30, "1H": 24, "30m": 16, "15m": 10, "5m": 5}


@dataclass
class LevelCandidate:
    inst_id: str
    coin: str
    kind: str
    lower: float
    upper: float
    price: float
    quality: int
    distance_pct: float
    timeframes: List[str] = field(default_factory=list)
    anchor_tf: str = "1H"
    touches: int = 0
    reactions: int = 0
    consolidation: float = 0.0
    rejection: float = 0.0
    displacement: float = 0.0
    confluence: float = 0.0
    freshness: float = 0.0
    status: str = "WATCH"
    current_price: float = 0.0
    volume_24h: float = 0.0
    chart_tf: str = "15m"
    chart_candles: List[Candle] = field(default_factory=list)
    chart_candles_5m: List[Candle] = field(default_factory=list)
    chart_candles_15m: List[Candle] = field(default_factory=list)
    reason: str = ""
    setup: str = ""


@dataclass
class TrendlineCandidate:
    inst_id: str
    coin: str
    kind: str
    p1: float
    p2: float
    slope: float
    touches: int
    quality: int
    distance_pct: float
    status: str
    current_price: float
    chart_tf: str
    chart_candles: List[Candle]
    chart_candles_5m: List[Candle] = field(default_factory=list)
    chart_candles_15m: List[Candle] = field(default_factory=list)
    timeframes: List[str] = field(default_factory=list)
    reason: str = ""
    setup: str = "TRENDLINE"


def _safe_pct_distance(a: float, b: float) -> float:
    return abs(a - b) / max(abs(b), 1e-12) * 100.0


def _tf_atr_pct(cs: List[Candle]) -> float:
    return atr_pct(cs[-80:]) if len(cs) >= 20 else 0.0


def _zone_for_price(price: float, cs: List[Candle]) -> Tuple[float, float]:
    ap = _tf_atr_pct(cs)
    width = max(0.08, min(LEVEL_MAX_ZONE_PCT, ap * LEVEL_ZONE_ATR_MULT))
    return price * (1 - width / 100), price * (1 + width / 100)


def _is_zone_touch(c: Candle, lower: float, upper: float) -> bool:
    return c.high >= lower and c.low <= upper


def _distinct_touch_events(candles: List[Candle], lower: float, upper: float) -> List[Tuple[int, int]]:
    """Count a test only after price has LEFT the zone.

    A test is an interaction episode, not a candle count:
      approach -> enter/touch -> rejection/dwell -> leave -> later re-approach.
    Consecutive candles inside/near the zone therefore remain one test.
    """
    if len(candles) < 30:
        return []
    start = max(0, len(candles) - 180)
    events = []
    i = start
    # Outside buffer prevents tiny one-candle wiggles from creating new tests.
    width = max(upper - lower, 1e-12)
    # A new test requires a meaningful excursion away from the zone.
    # For scalping, tiny oscillations around the boundary are still the same test.
    avg_ranges = [max(c.high - c.low, 1e-12) for c in candles[max(start, 0):]]
    avg_range = sum(avg_ranges[-60:]) / max(1, min(60, len(avg_ranges)))
    outside = max(width * 1.25, avg_range * 0.80)
    min_gap = 3
    while i < len(candles):
        if not _is_zone_touch(candles[i], lower, upper):
            i += 1
            continue
        first = i
        last = i
        # Stay in the same episode while price overlaps the zone.
        while last + 1 < len(candles) and _is_zone_touch(candles[last + 1], lower, upper):
            last += 1
        # Require a real departure before another test can start.
        j = last + 1
        left = False
        while j < len(candles) and j <= last + 8:
            c = candles[j]
            if c.close > upper + outside or c.close < lower - outside:
                left = True
                break
            j += 1
        events.append((first, last))
        if left:
            i = max(j + 1, last + min_gap + 1)
        else:
            # Current/unfinished interaction: do not manufacture another test.
            break
    return events


def _event_reaction(candles: List[Candle], event: Tuple[int, int], kind: str, lower: float, upper: float) -> Tuple[bool, float, float]:
    first, last = event
    segment = candles[first:last + 1]
    zone = max(upper - lower, 1e-12)
    ranges = [max(c.high - c.low, 1e-12) for c in candles[max(0, first - 20):first]]
    avg_range = sum(ranges) / len(ranges) if ranges else zone
    if kind == "SUPPORT":
        test = min(segment, key=lambda c: c.low)
        wick = min(test.open, test.close) - test.low
        move = max((candles[j].high - test.close for j in range(last + 1, min(last + 9, len(candles)))), default=0.0)
        rejected = (test.close >= upper) or (wick >= zone * 0.35 and test.close >= test.open)
    else:
        test = max(segment, key=lambda c: c.high)
        wick = test.high - max(test.open, test.close)
        move = max((test.close - candles[j].low for j in range(last + 1, min(last + 9, len(candles)))), default=0.0)
        rejected = (test.close <= lower) or (wick >= zone * 0.35 and test.close <= test.open)
    displacement = move / max(avg_range, 1e-12)
    # A reaction is real only when rejection is followed by meaningful displacement.
    reacted = rejected and displacement >= 0.85
    return reacted, displacement, abs(wick) / max(zone, 1e-12)


def _touch_reaction_metrics(candles: List[Candle], lower: float, upper: float, kind: str) -> Tuple[int, int, float, float]:
    events = _distinct_touch_events(candles, lower, upper)
    reactions = 0
    displacement_hits = 0
    rejection_sum = 0.0
    for event in events:
        reacted, displacement, rejection = _event_reaction(candles, event, kind, lower, upper)
        if reacted:
            reactions += 1
        if displacement >= 1.15:
            displacement_hits += 1
        rejection_sum += min(rejection, 2.0)
    rejection_quality = min(1.0, rejection_sum / max(1, len(events)))
    displacement_quality = min(1.0, displacement_hits / max(1, len(events)))
    return len(events), reactions, rejection_quality, displacement_quality


def _consolidation_score(candles: List[Candle], lower: float, upper: float) -> float:
    if len(candles) < 30:
        return 0.0
    events = _distinct_touch_events(candles, lower, upper)
    if not events:
        return 0.0
    zone = upper - lower
    total = 0
    inside = 0
    for c in candles[-120:]:
        total += 1
        if c.high >= lower and c.low <= upper:
            inside += 1
    # Repeated tests + meaningful dwell around the area.
    event_component = min(0.6, len(events) / 5.0 * 0.6)
    dwell_component = min(0.4, inside / max(total, 1) * 2.0)
    return min(1.0, event_component + dwell_component)


def _anchor_tf_for_level(tfs: List[str]) -> Optional[str]:
    for tf in ("1D", "4H", "1H"):
        if tf in tfs:
            return tf
    return None


def _candidate_raw_levels(candles_by_tf: Dict[str, List[Candle]]) -> List[Tuple[float, str, str, float]]:
    raw = []
    for tf in ("1D", "4H", "1H", "30m"):
        cs = candles_by_tf.get(tf, [])
        if len(cs) < 40:
            continue
        # Recent pivots only; extremes are deliberately not enough by themselves.
        for idx, p in pivot_highs(cs, 2, 2)[-35:]:
            age = len(cs) - 1 - idx
            raw.append((p, "RESISTANCE", tf, max(0.0, 1.0 - age / 160.0)))
        for idx, p in pivot_lows(cs, 2, 2)[-35:]:
            age = len(cs) - 1 - idx
            raw.append((p, "SUPPORT", tf, max(0.0, 1.0 - age / 160.0)))
    return raw


def _cluster_price_sources(raw: List[Tuple[float, str, str, float]]) -> List[dict]:
    clusters = []
    for price, kind, tf, recency in sorted(raw, key=lambda x: x[0]):
        tol = {"1D": 0.45, "4H": 0.32, "1H": 0.24, "30m": 0.18}.get(tf, 0.2)
        placed = False
        for cl in clusters:
            if abs(price - cl["price"]) / max(cl["price"], 1e-12) * 100 <= max(tol, cl["tol"]):
                cl["items"].append((price, kind, tf, recency))
                weights = [LEVEL_TF_WEIGHTS.get(x[2], 5) for x in cl["items"]]
                cl["price"] = sum(x[0] * w for x, w in zip(cl["items"], weights)) / sum(weights)
                cl["tol"] = max(cl["tol"], tol)
                placed = True
                break
        if not placed:
            clusters.append({"price": price, "tol": tol, "items": [(price, kind, tf, recency)]})
    return clusters


def _build_horizontal_candidates(inst_id: str, ticker: dict, candles_by_tf: Dict[str, List[Candle]]) -> List[LevelCandidate]:
    current = float(ticker.get("last", 0) or 0)
    volume = float(ticker.get("vol24h_usd", 0) or 0)
    if current <= 0 or volume < LEVEL_MIN_VOLUME:
        return []
    candidates = []
    for cl in _cluster_price_sources(_candidate_raw_levels(candles_by_tf)):
        p = cl["price"]
        distance = _safe_pct_distance(current, p)
        if distance > LEVEL_MAX_DISTANCE_PCT:
            continue
        items = cl["items"]
        support_votes = sum(x[1] == "SUPPORT" for x in items)
        resistance_votes = sum(x[1] == "RESISTANCE" for x in items)
        kind = "SUPPORT" if support_votes >= resistance_votes else "RESISTANCE"
        real_tfs = list(dict.fromkeys(x[2] for x in items))
        anchor_tf = _anchor_tf_for_level(real_tfs)
        if not anchor_tf:
            continue
        if anchor_tf == "1D" and not any(tf in real_tfs for tf in ("4H", "1H")):
            continue
        anchor = candles_by_tf.get(anchor_tf, [])
        # Scalping level lifecycle is measured on 15M; HTF only establishes structure.
        measure = candles_by_tf.get("15m", []) if len(candles_by_tf.get("15m", [])) >= 50 else anchor
        lower, upper = _zone_for_price(p, measure)
        # Do not allow giant zones.
        if _safe_pct_distance(lower, upper) > LEVEL_MAX_ZONE_PCT * 2.2:
            continue
        touches, reactions, rejection_q, displacement_q = _touch_reaction_metrics(measure, lower, upper, kind)
        consolidation = _consolidation_score(measure, lower, upper)
        if touches < LEVEL_MIN_TESTS or reactions < LEVEL_MIN_REACTIONS:
            continue
        # A decisive break after the latest reaction invalidates the level.
        latest_event = _distinct_touch_events(measure, lower, upper)
        if latest_event:
            _, last = latest_event[-1]
            post = measure[last + 1:]
            if post:
                if kind == "SUPPORT" and min(c.close for c in post) < lower - (upper - lower) * 0.65:
                    continue
                if kind == "RESISTANCE" and max(c.close for c in post) > upper + (upper - lower) * 0.65:
                    continue
        htf_count = len([tf for tf in real_tfs if tf in ("1D", "4H", "1H")])
        confluence = min(1.0, (htf_count / 3.0) + (0.15 if len(real_tfs) >= 3 else 0.0))
        freshness = max(x[3] for x in items)
        # Before reaching the zone we want a useful buffer, not a chase.
        atr_now = _tf_atr_pct(candles_by_tf.get("5m", []) if len(candles_by_tf.get("5m", [])) >= 30 else measure)
        distance_atr = distance / max(atr_now, 0.01)
        if distance < LEVEL_APPROACH_MIN_PCT:
            status = "TESTING" if lower <= current <= upper else "TOO_LATE"
        elif distance_atr <= LEVEL_APPROACH_MAX_ATR and distance <= 1.8:
            status = "APPROACHING"
        elif distance <= LEVEL_MAX_DISTANCE_PCT:
            status = "WATCH"
        else:
            continue
        if status == "TOO_LATE":
            continue
        # Internal quality for ranking only. It is NOT displayed as a score.
        test_quality = 1.0 if 3 <= touches <= 5 else (0.82 if touches == 6 else 0.68)
        quality = round(30 * test_quality +
                        25 * min(1, reactions / 4) +
                        18 * consolidation +
                        10 * rejection_q +
                        8 * displacement_q +
                        6 * confluence +
                        3 * freshness)
        if quality < LEVEL_MIN_QUALITY:
            continue
        reason = f"{touches} отдельных теста • {reactions} выраженные реакции"
        if consolidation >= 0.55:
            reason += " • хорошая проторговка"
        if htf_count >= 2:
            reason += " • HTF подтверждение"
        setup = "LONG" if kind == "SUPPORT" else "SHORT"
        # Always show scalping execution context: 15M structure + 5M trigger.
        chart_tf = "15m"
        chart = candles_by_tf.get("15m", [])[-LEVEL_SCALP_CHART_15M:]
        chart5 = candles_by_tf.get("5m", [])[-LEVEL_SCALP_CHART_5M:]
        candidates.append(LevelCandidate(
            inst_id=inst_id,
            coin=get_coin(inst_id),
            kind=kind,
            lower=lower,
            upper=upper,
            price=p,
            quality=quality,
            distance_pct=distance,
            timeframes=real_tfs,
            anchor_tf=anchor_tf,
            touches=touches,
            reactions=reactions,
            consolidation=consolidation,
            rejection=rejection_q,
            displacement=displacement_q,
            confluence=confluence,
            freshness=freshness,
            status=status,
            current_price=current,
            volume_24h=volume,
            chart_tf=chart_tf,
            chart_candles=chart,
            chart_candles_5m=chart5,
            chart_candles_15m=chart,
            reason=reason,
            setup=setup,
        ))
    # Merge overlapping/nearby zones so one market area produces one candidate.
    candidates.sort(key=lambda x: (x.quality, -x.distance_pct), reverse=True)
    selected = []
    for c in candidates:
        merged = False
        for s0 in selected:
            if c.kind != s0.kind:
                continue
            gap = max(s0.lower - c.upper, c.lower - s0.upper, 0.0)
            near = gap / max(c.price, 1e-12) * 100 <= 0.12
            overlap = c.lower <= s0.upper and c.upper >= s0.lower
            center_near = abs(c.price - s0.price) / max(c.price, 1e-12) * 100 <= 0.22
            if overlap or near or center_near:
                # Keep the cleaner/narrower structural zone rather than creating a giant union.
                if c.quality > s0.quality + 2 and (c.upper-c.lower) <= (s0.upper-s0.lower)*1.15:
                    selected[selected.index(s0)] = c
                merged = True
                break
        if not merged:
            selected.append(c)
    return selected[:4]


def _line_value(p1: Tuple[int, float], p2: Tuple[int, float], x: int) -> float:
    x1, y1 = p1
    x2, y2 = p2
    if x2 == x1:
        return y1
    return y1 + (y2 - y1) * (x - x1) / (x2 - x1)


def _trendline_candidates(inst_id: str, ticker: dict, candles_by_tf: Dict[str, List[Candle]]) -> List[TrendlineCandidate]:
    current = float(ticker.get("last", 0) or 0)
    volume = float(ticker.get("vol24h_usd", 0) or 0)
    if current <= 0 or volume < LEVEL_MIN_VOLUME:
        return []
    out = []
    for tf in ("4H", "1H"):
        cs = candles_by_tf.get(tf, [])
        if len(cs) < 70:
            continue
        piv_hi = pivot_highs(cs, 2, 2)[-25:]
        piv_lo = pivot_lows(cs, 2, 2)[-25:]
        atrv = max(atr(cs[-80:]), current * 0.0005)
        tol = atrv * 0.35
        for pivots, kind in ((piv_lo, "TRENDLINE_SUPPORT"), (piv_hi, "TRENDLINE_RESISTANCE")):
            if len(pivots) < LEVEL_TRENDLINE_MIN_TOUCHES:
                continue
            best = None
            for a in range(max(0, len(pivots) - 10), len(pivots) - 2):
                for b in range(a + 1, len(pivots) - 1):
                    p1 = pivots[a]
                    p2 = pivots[b]
                    if p2[0] - p1[0] < 8:
                        continue
                    slope = (p2[1] - p1[1]) / (p2[0] - p1[0])
                    if kind == "TRENDLINE_SUPPORT" and slope <= 0:
                        continue
                    if kind == "TRENDLINE_RESISTANCE" and slope >= 0:
                        continue
                    touches = 0
                    errors = []
                    for idx, price in pivots:
                        if idx < p1[0] or idx > len(cs) - 1:
                            continue
                        line = _line_value(p1, p2, idx)
                        err = abs(price - line)
                        if err <= tol:
                            touches += 1
                            errors.append(err)
                    if touches < LEVEL_TRENDLINE_MIN_TOUCHES:
                        continue
                    line_now = _line_value(p1, p2, len(cs) - 1)
                    dist = _safe_pct_distance(current, line_now)
                    if dist > 2.5:
                        continue
                    distance_atr = abs(current - line_now) / atrv
                    if distance_atr > 1.25:
                        continue
                    spacing = p2[0] - p1[0]
                    quality = round(min(50, touches / 5 * 50) + min(25, spacing / max(1, len(cs)) * 30) + max(0, 25 - distance_atr * 12))
                    if quality < 72:
                        continue
                    status = "APPROACHING" if dist >= LEVEL_APPROACH_MIN_PCT else "VERY_NEAR"
                    candidate = TrendlineCandidate(
                        inst_id=inst_id,
                        coin=get_coin(inst_id),
                        kind=kind,
                        p1=p1[1],
                        p2=p2[1],
                        slope=slope,
                        touches=touches,
                        quality=quality,
                        distance_pct=dist,
                        status=status,
                        current_price=current,
                        chart_tf=tf,
                        chart_candles=cs[-LEVEL_SCALP_CHART_15M:],
                        chart_candles_5m=candles_by_tf.get("5m", [])[-LEVEL_SCALP_CHART_5M:],
                        chart_candles_15m=candles_by_tf.get("15m", [])[-LEVEL_SCALP_CHART_15M:],
                        timeframes=[tf],
                        reason=f"{touches} касания трендовой • чистый наклон • цена подходит к линии",
                    )
                    if best is None or candidate.quality > best.quality:
                        best = candidate
            if best:
                out.append(best)
    out.sort(key=lambda x: (x.quality, -x.distance_pct), reverse=True)
    return out[:2]


def _detect_breakout_retest(inst_id: str, ticker: dict, candles_by_tf: Dict[str, List[Candle]]) -> List[LevelCandidate]:
    current = float(ticker.get("last", 0) or 0)
    volume = float(ticker.get("vol24h_usd", 0) or 0)
    if current <= 0 or volume < LEVEL_MIN_VOLUME:
        return []
    out = []
    for tf in ("4H", "1H"):
        cs = candles_by_tf.get(tf, [])
        if len(cs) < 80:
            continue
        recent = cs[-35:]
        highs = [c.high for c in cs[-70:-8]]
        lows = [c.low for c in cs[-70:-8]]
        resistance = max(highs)
        support = min(lows)
        avg_vol = sum(c.quote_volume for c in cs[-25:-3]) / max(1, len(cs[-25:-3]))
        for kind, level in (("BREAKOUT_RETEST_SUPPORT", resistance), ("BREAKOUT_RETEST_RESISTANCE", support)):
            # Need a close beyond the level followed by a return to it.
            breakout_idx = None
            for i in range(len(recent) - 8, len(recent) - 2):
                c = recent[i]
                if kind.endswith("SUPPORT") and c.close > level:
                    breakout_idx = i
                if kind.endswith("RESISTANCE") and c.close < level:
                    breakout_idx = i
            if breakout_idx is None:
                continue
            after = recent[breakout_idx + 1:]
            if not after:
                continue
            retest = any(c.low <= level * 1.002 and c.high >= level * 0.998 for c in after)
            if not retest:
                continue
            if kind.endswith("SUPPORT"):
                holding = current >= level * 0.998
            else:
                holding = current <= level * 1.002
            if not holding:
                continue
            breakout_candle = recent[breakout_idx]
            vol_ratio = breakout_candle.quote_volume / max(avg_vol, 1e-12)
            if vol_ratio < 1.25:
                continue
            lower, upper = _zone_for_price(level, cs)
            dist = _safe_pct_distance(current, level)
            if dist > 1.2:
                continue
            quality = round(45 + min(20, vol_ratio * 7) + min(20, (1.2 - min(dist, 1.2)) * 12))
            if quality < 70:
                continue
            out.append(LevelCandidate(
                inst_id=inst_id,
                coin=get_coin(inst_id),
                kind="SUPPORT" if kind.endswith("SUPPORT") else "RESISTANCE",
                lower=lower,
                upper=upper,
                price=level,
                quality=quality,
                distance_pct=dist,
                timeframes=[tf],
                anchor_tf=tf,
                touches=1,
                reactions=1,
                consolidation=0.0,
                status="RETEST",
                current_price=current,
                volume_24h=volume,
                chart_tf=tf,
                chart_candles=cs[-LEVEL_CHART_CANDLES:],
                reason=f"пробой + ретест • объём пробоя {vol_ratio:.1f}x среднего",
                setup="LONG" if kind.endswith("SUPPORT") else "SHORT",
            ))
    return out


def _status_key(c) -> str:
    if isinstance(c, LevelCandidate):
        return f"H:{c.inst_id}:{c.kind}:{round(c.price, 8)}"
    return f"T:{c.inst_id}:{c.kind}:{round(c.p1, 8)}:{round(c.p2, 8)}"


def _init_level_db_v10():
    db.execute("""
        CREATE TABLE IF NOT EXISTS level_events (
            event_key TEXT PRIMARY KEY,
            inst_id TEXT NOT NULL,
            coin TEXT NOT NULL,
            setup TEXT NOT NULL,
            kind TEXT NOT NULL,
            status TEXT NOT NULL,
            price REAL NOT NULL,
            lower REAL,
            upper REAL,
            quality INTEGER NOT NULL,
            distance_pct REAL NOT NULL,
            touches INTEGER DEFAULT 0,
            reactions INTEGER DEFAULT 0,
            created_at REAL NOT NULL,
            last_sent_at REAL,
            last_status TEXT,
            first_seen_at REAL,
            broken_at REAL
        )
    """)
    db.commit()


_init_level_db_v10()


def _recently_sent(event_key: str) -> bool:
    row = db.execute("SELECT last_sent_at FROM level_events WHERE event_key=?", (event_key,)).fetchone()
    return bool(row and row[0] and now_ts() - float(row[0]) < LEVEL_COOLDOWN_MINUTES * 60)


def _save_event(c, sent: bool):
    key = _status_key(c)
    ts = now_ts()
    if isinstance(c, LevelCandidate):
        values = (key, c.inst_id, c.coin, c.setup, c.kind, c.status, c.price, c.lower, c.upper, c.quality, c.distance_pct, c.touches, c.reactions)
    else:
        values = (key, c.inst_id, c.coin, "LONG" if "SUPPORT" in c.kind else "SHORT", c.kind, c.status, c.p1, None, None, c.quality, c.distance_pct, c.touches, 0)
    db.execute("""
        INSERT INTO level_events(event_key,inst_id,coin,setup,kind,status,price,lower,upper,quality,distance_pct,touches,reactions,created_at,last_sent_at,last_status,first_seen_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, ?,?)
        ON CONFLICT(event_key) DO UPDATE SET
            status=excluded.status,
            quality=excluded.quality,
            distance_pct=excluded.distance_pct,
            touches=excluded.touches,
            reactions=excluded.reactions,
            last_sent_at=CASE WHEN ? THEN excluded.last_sent_at ELSE level_events.last_sent_at END,
            last_status=excluded.status
    """, (*values, ts, ts if sent else None, getattr(c, "status", ""), ts, 1 if sent else 0))
    db.commit()


def _price_decimals(price: float) -> int:
    if price >= 1000: return 1
    if price >= 100: return 2
    if price >= 1: return 3
    if price >= 0.1: return 4
    if price >= 0.01: return 5
    return 7


def _draw_candles(ax, candles: List[Candle]):
    if not candles:
        return
    width = 0.62
    for i, c in enumerate(candles):
        up = c.close >= c.open
        body_low = min(c.open, c.close)
        body_high = max(c.open, c.close)
        body_h = max(body_high - body_low, max((c.high - c.low) * 0.006, 1e-12))
        body_color = "#22c55e" if up else "#ef4444"
        ax.vlines(i, c.low, c.high, color=body_color, linewidth=1.0, zorder=3)
        ax.add_patch(Rectangle((i - width / 2, body_low), width, body_h,
                               facecolor=body_color, edgecolor=body_color, linewidth=0.7, zorder=4))


def _draw_chart_panel(ax, candles: List[Candle], level: LevelCandidate, title_tf: str):
    _draw_candles(ax, candles)
    zone_color = "#22c55e" if level.kind == "SUPPORT" else "#ef4444"
    ax.axhspan(level.lower, level.upper, facecolor=zone_color, alpha=0.16, zorder=1)
    ax.axhline(level.lower, color=zone_color, linewidth=1.0, alpha=0.55)
    ax.axhline(level.upper, color=zone_color, linewidth=1.0, alpha=0.55)
    ax.axhline(level.price, color=zone_color, linewidth=2.2, zorder=6)
    ax.axhline(level.current_price, color="#f8fafc", linewidth=1.0, linestyle=(0, (5, 4)), zorder=6)
    d = _price_decimals(level.price)
    ax.text(len(candles)-0.2, level.price, f" {level.kind} {level.price:.{d}f} ", color="#fff", fontsize=9, fontweight="bold", va="center", ha="right",
            bbox=dict(boxstyle="round,pad=0.22", facecolor=zone_color, edgecolor="none"))
    ax.text(0.01, 0.96, f"{title_tf} • {level.status}", transform=ax.transAxes, color="#e5e7eb", fontsize=11, fontweight="bold", va="top")
    ticks = list(range(0, len(candles), max(1, len(candles)//7)))[:8]
    labels = [datetime.fromtimestamp(candles[i].ts/1000, tz=ZoneInfo("UTC")).strftime("%d %b\n%H:%M") for i in ticks]
    ax.set_xticks(ticks); ax.set_xticklabels(labels, fontsize=7, color="#94a3b8")
    ax.yaxis.tick_right(); ax.tick_params(axis="y", colors="#cbd5e1", labelsize=8, length=0)
    ax.grid(axis="y", color="#334155", alpha=0.25, linewidth=0.7)
    lows = [c.low for c in candles]; highs = [c.high for c in candles]
    span = max(max(highs)-min(lows), level.current_price*0.003)
    ax.set_ylim(min(min(lows), level.lower)-span*0.10, max(max(highs), level.upper)+span*0.10)
    ax.set_xlim(-1.5, len(candles)+1.5)
    for spine in ax.spines.values(): spine.set_color("#1e293b")


def make_level_chart(level: LevelCandidate) -> str:
    path = f"level_{level.coin}_{int(time.time()*1000)}.png"
    c15 = level.chart_candles_15m or level.chart_candles
    c5 = level.chart_candles_5m
    if len(c15) < 20 or len(c5) < 20:
        return ""
    fig = plt.figure(figsize=(15, 11), facecolor="#070b12")
    gs = fig.add_gridspec(2, 1, height_ratios=[1, 1], hspace=0.12)
    ax15 = fig.add_subplot(gs[0])
    ax5 = fig.add_subplot(gs[1])
    for ax in (ax15, ax5): ax.set_facecolor("#070b12")
    _draw_chart_panel(ax15, c15, level, "15M STRUCTURE")
    _draw_chart_panel(ax5, c5, level, "5M SCALP TRIGGER")
    d = _price_decimals(level.price)
    fig.suptitle(f"{level.coin}/USDT • SCALPING • {level.kind}", x=0.055, ha="left", color="#f8fafc", fontsize=18, fontweight="bold")
    fig.text(0.055, 0.965, f"ZONE {fmt_price(level.lower)} — {fmt_price(level.upper)} • NOW {fmt_price(level.current_price)} • {level.touches} tests / {level.reactions} reactions", color="#94a3b8", fontsize=9.5)
    plt.subplots_adjust(left=0.055, right=0.94, top=0.91, bottom=0.06)
    fig.savefig(path, facecolor=fig.get_facecolor(), dpi=170)
    plt.close(fig)
    return path


def build_level_text(level: LevelCandidate) -> str:
    if level.kind == "SUPPORT":
        title = f"🟢 {level.coin}/USDT"
        action = "Можно рассматривать LONG при реакции."
    else:
        title = f"🔴 {level.coin}/USDT"
        action = "Можно рассматривать SHORT при отказе от зоны."
    return (
        f"<b>{title}</b>\n"
        f"Подходит к {'поддержке' if level.kind == 'SUPPORT' else 'сопротивлению'} {fmt_price(level.lower)}–{fmt_price(level.upper)}\n"
        f"15M / 5M • HTF: {level.anchor_tf}\n"
        f"{level.touches} теста • {level.reactions} реакции"
        + (" • хорошая проторговка" if level.consolidation >= 0.55 else "") + "\n"
        f"<b>{action}</b>"
    )


def send_level(candidate: LevelCandidate) -> bool:
    try:
        path = make_level_chart(candidate)
        text = build_level_text(candidate)
        if path and os.path.exists(path):
            with open(path, "rb") as photo:
                bot.send_photo(CHANNEL_ID, photo, caption=text, parse_mode="HTML")
            try: os.remove(path)
            except OSError: pass
        else:
            bot.send_message(CHANNEL_ID, text, parse_mode="HTML")
        return True
    except Exception:
        log.exception("LEVEL SEND FAILED | %s", candidate.inst_id)
        return False


def send_trendline(candidate: TrendlineCandidate) -> bool:
    try:
        text = (
            f"<b>🔵 {candidate.coin}/USDT</b>\n"
            f"Подходит к {'восходящей поддержке' if candidate.kind == 'TRENDLINE_SUPPORT' else 'нисходящему сопротивлению'}\n"
            f"15M / 5M • {candidate.touches} касания трендовой\n"
            f"Можно рассматривать реакцию / пробой с подтверждением."
        )
        path = f"trend_{candidate.coin}_{int(time.time()*1000)}.png"
        c15 = candidate.chart_candles_15m or candidate.chart_candles
        c5 = candidate.chart_candles_5m
        if len(c15) < 20 or len(c5) < 20:
            return False
        fig = plt.figure(figsize=(15, 11), facecolor="#070b12")
        gs = fig.add_gridspec(2, 1, height_ratios=[1, 1], hspace=0.12)
        for ax, candles, label in ((fig.add_subplot(gs[0]), c15, "15M STRUCTURE"), (fig.add_subplot(gs[1]), c5, "5M SCALP TRIGGER")):
            ax.set_facecolor("#070b12")
            _draw_candles(ax, candles)
            piv = pivot_lows(candles,2,2) if candidate.kind == "TRENDLINE_SUPPORT" else pivot_highs(candles,2,2)
            pts = piv[-25:]
            if len(pts) >= 2:
                a,b = pts[-2], pts[-1]
                xs = list(range(a[0], len(candles)))
                ys = [_line_value(a,b,x) for x in xs]
                ax.plot(xs, ys, linewidth=2.5)
            ax.axhline(candidate.current_price, linewidth=1, linestyle=(0,(5,4)))
            ax.yaxis.tick_right(); ax.set_title(label, loc="left", fontsize=11, fontweight="bold", color="#e5e7eb")
            ax.grid(axis="y", alpha=0.2)
            lows=[c.low for c in candles]; highs=[c.high for c in candles]
            ax.set_ylim(min(lows), max(highs)); ax.set_xlim(-1.5,len(candles)+1.5)
            for spine in ax.spines.values(): spine.set_color("#1e293b")
        fig.suptitle(f"{candidate.coin}/USDT • SCALPING • TRENDLINE", x=0.055, ha="left", color="#f8fafc", fontsize=18, fontweight="bold")
        plt.subplots_adjust(left=0.055, right=0.94, top=0.91, bottom=0.06)
        fig.savefig(path, facecolor=fig.get_facecolor(), dpi=170, bbox_inches="tight"); plt.close(fig)
        with open(path,"rb") as photo: bot.send_photo(CHANNEL_ID, photo, caption=text, parse_mode="HTML")
        try: os.remove(path)
        except OSError: pass
        return True
    except Exception:
        log.exception("TRENDLINE SEND FAILED | %s", candidate.inst_id)
        return False


def load_level_candles(inst_id: str) -> Dict[str, List[Candle]]:
    return {tf: get_candles(inst_id, tf, 180 if tf in ("1H","4H") else 160) for tf in ("1D","4H","1H","30m","15m","5m")}


def scan_levels():
    instruments = get_instruments()
    tickers = get_tickers()
    universe = []
    for inst_id, ticker in tickers.items():
        if inst_id not in instruments:
            continue
        vol = float(ticker.get("vol24h_usd", 0) or 0)
        if vol < LEVEL_MIN_VOLUME:
            continue
        universe.append((fast_market_score(ticker), vol, inst_id, ticker))
    universe.sort(key=lambda x: (x[0], x[1]), reverse=True)
    deep = universe[:LEVEL_DEEP_CANDIDATES] if LEVEL_DEEP_CANDIDATES > 0 else universe
    all_candidates = []
    for _, _, inst_id, ticker in deep:
        try:
            candles = load_level_candles(inst_id)
            all_candidates.extend(_build_horizontal_candidates(inst_id, ticker, candles))
            all_candidates.extend(_detect_breakout_retest(inst_id, ticker, candles))
            all_candidates.extend(_trendline_candidates(inst_id, ticker, candles))
        except Exception as exc:
            log.warning("LEVEL ANALYZE FAILED | %s | %s", inst_id, exc)
    all_candidates.sort(key=lambda x: (getattr(x, "quality", 0), -getattr(x, "distance_pct", 999)), reverse=True)
    sent = 0
    per_coin = defaultdict(int)
    for candidate in all_candidates:
        if sent >= LEVEL_MAX_MESSAGES_PER_SCAN:
            break
        if per_coin[candidate.coin] >= LEVEL_MAX_SAME_COIN:
            continue
        key = _status_key(candidate)
        if _recently_sent(key):
            _save_event(candidate, False)
            continue
        # Only send actionable state transitions. WATCH is intentionally silent.
        if isinstance(candidate, LevelCandidate) and candidate.status not in ("APPROACHING", "RETEST"):
            _save_event(candidate, False)
            continue
        if isinstance(candidate, TrendlineCandidate) and candidate.status not in ("APPROACHING", "VERY_NEAR"):
            _save_event(candidate, False)
            continue
        ok = send_level(candidate) if isinstance(candidate, LevelCandidate) else send_trendline(candidate)
        _save_event(candidate, ok)
        if ok:
            sent += 1
            per_coin[candidate.coin] += 1
    log.info("LEVEL SCAN | liquid=%s | deep=%s | candidates=%s | sent=%s", len(universe), len(deep), len(all_candidates), sent)


def level_report():
    rows = db.execute("""
        SELECT coin, kind, quality, distance_pct, status, touches, reactions
        FROM level_events ORDER BY COALESCE(last_sent_at,created_at) DESC LIMIT 20
    """).fetchall()
    if not rows:
        return "<b>LEVEL ENGINE</b>\nПока нет сохранённых ситуаций."
    lines = ["<b>LEVEL ENGINE — последние ситуации</b>"]
    for coin, kind, quality, dist, status, touches, reactions in rows:
        icon = "🟢" if kind == "SUPPORT" else "🔴" if kind == "RESISTANCE" else "🔵"
        lines.append(f"{icon} {coin} | {kind} | {dist:.2f}% | {status} | {touches} теста | {reactions} реакции")
    return "\n".join(lines)


def main():
    log.info("QUANTUM LEVEL ENGINE V10 STARTED")
    while True:
        try:
            scan_levels()
        except KeyboardInterrupt:
            log.info("STOPPED")
            break
        except Exception:
            log.exception("LEVEL ENGINE LOOP ERROR")
        time.sleep(LEVEL_SCAN_INTERVAL)


if __name__ == "__main__":
    main()
