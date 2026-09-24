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
# QUANTUM LEVEL ENGINE V8
# Replaces the old signal-generation loop with a pure level scanner.
# The original OKX market/candle infrastructure and indicator helpers are
# intentionally reused; the output is a ranked list of technical zones.
# ============================================================

from dataclasses import dataclass, field
from collections import defaultdict
from pathlib import Path

LEVEL_SCAN_INTERVAL = int(os.getenv("LEVEL_SCAN_INTERVAL", "30"))
LEVEL_DEEP_CANDIDATES = int(os.getenv("LEVEL_DEEP_CANDIDATES", "50"))
LEVEL_FINAL_COUNT = int(os.getenv("LEVEL_FINAL_COUNT", "5"))
LEVEL_MIN_SCORE = int(os.getenv("LEVEL_MIN_SCORE", "78"))
LEVEL_MIN_VOLUME = float(os.getenv("LEVEL_MIN_VOLUME", "60000000"))
LEVEL_MAX_DISTANCE = float(os.getenv("LEVEL_MAX_DISTANCE", "4.0"))
LEVEL_NEAR_DISTANCE = float(os.getenv("LEVEL_NEAR_DISTANCE", "1.0"))
LEVEL_COOLDOWN_MINUTES = int(os.getenv("LEVEL_COOLDOWN_MINUTES", "120"))
LEVEL_CHART_CANDLES = int(os.getenv("LEVEL_CHART_CANDLES", "90"))
LEVEL_MAX_MESSAGES_PER_SCAN = int(os.getenv("LEVEL_MAX_MESSAGES_PER_SCAN", "5"))

LEVEL_TF_WEIGHTS = {
    "1D": 34,
    "4H": 28,
    "1H": 22,
    "30m": 15,
    "15m": 10,
    "5m": 5,
}

@dataclass
class LevelCandidate:
    inst_id: str
    coin: str
    kind: str
    lower: float
    upper: float
    price: float
    score: int
    distance_pct: float
    timeframes: List[str] = field(default_factory=list)
    touches: int = 0
    reactions: int = 0
    volume_score: int = 0
    structure_score: int = 0
    confluence_score: int = 0
    freshness_score: int = 0
    rejection_score: int = 0
    displacement_score: int = 0
    liquidity_score: int = 0
    role_score: int = 0
    reason: str = ""
    status: str = "WATCH"
    current_price: float = 0.0
    volume_24h: float = 0.0
    candles_1h: List[Candle] = field(default_factory=list)


def _level_tf_tolerance(tf: str, candles: List[Candle], price: float) -> float:
    if price <= 0:
        return 0.15
    ap = atr_pct(candles[-80:]) if candles else 0.0
    base = {"1D": 0.55, "4H": 0.40, "1H": 0.28, "30m": 0.20, "15m": 0.14, "5m": 0.09}.get(tf, 0.20)
    return max(base, min(base * 1.8, ap * 0.65 if ap > 0 else base))


def _pivot_candidates(candles: List[Candle], tf: str) -> List[Tuple[float, str, int]]:
    if len(candles) < 25:
        return []
    out = []
    ph = pivot_highs(candles, 2 if tf not in ("1D", "4H") else 2, 2)
    pl = pivot_lows(candles, 2 if tf not in ("1D", "4H") else 2, 2)
    # Keep enough history to discover repeated institutional zones.
    for idx, price in ph[-45:]:
        age = len(candles) - 1 - idx
        out.append((price, "RESISTANCE", max(1, 100 - min(age, 100))))
    for idx, price in pl[-45:]:
        age = len(candles) - 1 - idx
        out.append((price, "SUPPORT", max(1, 100 - min(age, 100))))
    return out


def _extreme_candidates(candles: List[Candle], tf: str) -> List[Tuple[float, str, int]]:
    if len(candles) < 30:
        return []
    windows = [20, 50, 100]
    out = []
    for w in windows:
        if len(candles) < w:
            continue
        part = candles[-w:]
        hi = max(part, key=lambda c: c.high)
        lo = min(part, key=lambda c: c.low)
        out.append((hi.high, "RESISTANCE", 70))
        out.append((lo.low, "SUPPORT", 70))
    return out


def _round_level_candidates(current: float) -> List[Tuple[float, str, int]]:
    if current <= 0:
        return []
    # Psychological levels are weak evidence by themselves; they only help
    # when another source already places a real technical level nearby.
    step = 10 ** max(0, int(__import__('math').floor(__import__('math').log10(current))) - 1)
    if step <= 0:
        return []
    out = []
    center = round(current / step) * step
    for k in range(-4, 5):
        p = center + k * step
        if p > 0:
            out.append((p, "PSYCHOLOGICAL", 20))
    return out


def _cluster_raw_levels(raw: List[Tuple[float, str, str, int]], current: float) -> List[dict]:
    if not raw or current <= 0:
        return []
    raw = sorted(raw, key=lambda x: x[0])
    clusters = []
    for price, kind, tf, recency in raw:
        tolerance = 0.18
        if kind == "PSYCHOLOGICAL":
            tolerance = 0.12
        placed = False
        for cl in clusters:
            mid = cl["price"]
            width = max(tolerance, cl["width"])
            if abs(price - mid) / mid * 100 <= width:
                cl["items"].append((price, kind, tf, recency))
                weights = [LEVEL_TF_WEIGHTS.get(x[2], 5) for x in cl["items"]]
                cl["price"] = sum(x[0] * w for x, w in zip(cl["items"], weights)) / sum(weights)
                cl["width"] = max(cl["width"], tolerance)
                placed = True
                break
        if not placed:
            clusters.append({"price": price, "width": tolerance, "items": [(price, kind, tf, recency)]})
    return clusters


def _zone_from_cluster(cluster: dict, current: float, candles_by_tf: Dict[str, List[Candle]]) -> Tuple[float, float]:
    price = cluster["price"]
    spreads = []
    for _, _, tf, _ in cluster["items"]:
        cs = candles_by_tf.get(tf, [])
        if cs:
            a = atr_pct(cs[-80:])
            if a > 0:
                spreads.append(a * 0.32)
    width_pct = max(0.08, min(0.45, (sum(spreads) / len(spreads)) if spreads else 0.15))
    return price * (1 - width_pct / 100), price * (1 + width_pct / 100)


def _level_touch_metrics(candles: List[Candle], lower: float, upper: float) -> Tuple[int, int, int, int]:
    if len(candles) < 10:
        return 0, 0, 0, 0
    touches = reactions = rejection = displacement = 0
    lo = min(lower, upper)
    hi = max(lower, upper)
    zone = max(hi - lo, 1e-12)
    start = max(1, len(candles) - 100)
    for i in range(start, len(candles)):
        c = candles[i]
        intersects = c.high >= lo and c.low <= hi
        if not intersects:
            continue
        touches += 1
        body = candle_body_ratio(c)
        upper_wick = c.high - max(c.open, c.close)
        lower_wick = min(c.open, c.close) - c.low
        # Rejection: price trades through the zone but closes back away.
        if c.close < lo and upper_wick > zone * 0.25 and upper_wick > abs(c.close - c.open):
            reactions += 1
            rejection += int(6 + min(10, body * 6))
        elif c.close > hi and lower_wick > zone * 0.25 and lower_wick > abs(c.close - c.open):
            reactions += 1
            rejection += int(6 + min(10, body * 6))
        elif body >= 0.55:
            reactions += 1
        # Displacement away from the zone within the next few candles.
        future = candles[i + 1:min(i + 5, len(candles))]
        if future:
            if c.close <= hi:
                move = max(x.high for x in future) - c.close
            else:
                move = c.close - min(x.low for x in future)
            if move / max(abs(c.close), 1e-12) * 100 > 0.35:
                displacement += 1
    return touches, reactions, min(30, rejection), min(20, displacement * 5)


def _volume_at_level(candles: List[Candle], lower: float, upper: float) -> int:
    if len(candles) < 25:
        return 0
    vals = [c.quote_volume for c in candles[-100:] if c.quote_volume > 0]
    if len(vals) < 20:
        return 0
    avg = sum(vals[:-1]) / max(1, len(vals) - 1)
    if avg <= 0:
        return 0
    best = 0.0
    for c in candles[-100:]:
        if c.high >= lower and c.low <= upper:
            best = max(best, c.quote_volume / avg)
    if best >= 3.0: return 15
    if best >= 2.0: return 12
    if best >= 1.5: return 9
    if best >= 1.2: return 6
    return 2 if best > 0 else 0


def _structure_context(candles_by_tf: Dict[str, List[Candle]], kind: str, price: float) -> int:
    points = 0
    for tf, weight in (("1D", 15), ("4H", 15), ("1H", 10)):
        cs = candles_by_tf.get(tf, [])
        if len(cs) < 60:
            continue
        closes = [c.close for c in cs]
        e20 = ema(closes, 20)[-1]
        e50 = ema(closes, 50)[-1]
        if kind == "SUPPORT" and e20 >= e50:
            points += weight
        elif kind == "RESISTANCE" and e20 <= e50:
            points += weight
    return min(40, points)


def _liquidity_context(candles_by_tf: Dict[str, List[Candle]], lower: float, upper: float) -> int:
    points = 0
    for tf in ("4H", "1H", "15m"):
        cs = candles_by_tf.get(tf, [])
        if len(cs) < 30:
            continue
        hits = 0
        for c in cs[-80:]:
            if c.high >= lower and c.low <= upper:
                hits += 1
        if hits >= 2:
            points += 4
        if hits >= 4:
            points += 3
    return min(12, points)


def _status_for_level(current: float, lower: float, upper: float) -> str:
    if lower <= current <= upper:
        return "TESTING"
    distance = (lower - current) / current * 100 if current < lower else (current - upper) / current * 100
    if distance <= 0.20:
        return "VERY NEAR"
    if distance <= LEVEL_NEAR_DISTANCE:
        return "APPROACHING"
    return "WATCH"


def _build_level_candidates(inst_id: str, ticker: dict, candles_by_tf: Dict[str, List[Candle]]) -> List[LevelCandidate]:
    current = float(ticker.get("last", 0) or 0)
    volume = float(ticker.get("vol24h_usd", 0) or 0)
    if current <= 0 or volume < LEVEL_MIN_VOLUME:
        return []

    raw = []
    for tf, cs in candles_by_tf.items():
        if len(cs) < 30:
            continue
        for price, kind, recency in _pivot_candidates(cs, tf):
            raw.append((price, kind, tf, recency))
        for price, kind, recency in _extreme_candidates(cs, tf):
            raw.append((price, kind, tf, recency))
        if tf in ("1D", "4H", "1H"):
            for price, kind, recency in _round_level_candidates(current):
                if abs(price - current) / current * 100 <= LEVEL_MAX_DISTANCE:
                    raw.append((price, kind, tf, recency))

    clusters = _cluster_raw_levels(raw, current)
    candidates = []
    for cl in clusters:
        p = cl["price"]
        distance = abs(current - p) / current * 100
        if distance > LEVEL_MAX_DISTANCE:
            continue
        items = cl["items"]
        real_items = [x for x in items if x[1] != "PSYCHOLOGICAL"]
        if not real_items:
            continue
        kinds = [x[1] for x in real_items]
        support = sum(k == "SUPPORT" for k in kinds)
        resistance = sum(k == "RESISTANCE" for k in kinds)
        kind = "SUPPORT" if support >= resistance else "RESISTANCE"
        lower, upper = _zone_from_cluster(cl, current, candles_by_tf)
        tfs = list(dict.fromkeys(x[2] for x in real_items))
        touches = reactions = rejection = displacement = 0
        volume_score = 0
        for tf in tfs:
            cs = candles_by_tf.get(tf, [])
            t, r, rej, disp = _level_touch_metrics(cs, lower, upper)
            touches += t
            reactions += r
            rejection += rej
            displacement += disp
            volume_score = max(volume_score, _volume_at_level(cs, lower, upper))
        confluence = min(30, sum(LEVEL_TF_WEIGHTS.get(tf, 5) for tf in tfs) // 2)
        confluence += min(10, max(0, len(tfs) - 1) * 4)
        confluence = min(40, confluence)
        touch_score = min(18, touches * 2)
        reaction_score = min(20, reactions * 4)
        freshness = max(0, 12 - max(0, min(12, int(distance * 2))))
        structure = _structure_context(candles_by_tf, kind, p)
        liquidity = _liquidity_context(candles_by_tf, lower, upper)
        role_score = 5 if len(set(kinds)) > 1 else 0
        raw_score = confluence + touch_score + reaction_score + min(30, rejection) + min(20, displacement) + volume_score + structure + liquidity + freshness + role_score
        # Distance is a discovery factor, not a quality factor. Very far levels
        # are still allowed inside LEVEL_MAX_DISTANCE, but get a modest penalty.
        distance_penalty = min(12, distance * 2.0)
        score = int(clamp(raw_score - distance_penalty, 0, 100))
        if score < LEVEL_MIN_SCORE:
            continue
        reason_parts = []
        if len(tfs) >= 3: reason_parts.append("3+ TF confluence")
        elif len(tfs) == 2: reason_parts.append("multi-TF confluence")
        if touches >= 4: reason_parts.append(f"{touches} touches")
        if reactions >= 2: reason_parts.append(f"{reactions} reactions")
        if volume_score >= 9: reason_parts.append("volume confirmation")
        if structure >= 20: reason_parts.append("HTF structure aligned")
        if liquidity >= 8: reason_parts.append("liquidity concentration")
        if not reason_parts: reason_parts.append("clustered technical level")
        candidates.append(LevelCandidate(
            inst_id=inst_id,
            coin=get_coin(inst_id),
            kind=kind,
            lower=lower,
            upper=upper,
            price=p,
            score=score,
            distance_pct=distance,
            timeframes=tfs,
            touches=touches,
            reactions=reactions,
            volume_score=volume_score,
            structure_score=structure,
            confluence_score=confluence,
            freshness_score=freshness,
            rejection_score=min(30, rejection),
            displacement_score=min(20, displacement),
            liquidity_score=liquidity,
            role_score=role_score,
            reason="; ".join(reason_parts),
            status=_status_for_level(current, lower, upper),
            current_price=current,
            volume_24h=volume,
            candles_1h=candles_by_tf.get("1H", [])[-LEVEL_CHART_CANDLES:],
        ))
    candidates.sort(key=lambda x: (x.score, -x.distance_pct), reverse=True)
    # Deduplicate overlapping candidates of the same coin/type.
    selected = []
    for c in candidates:
        if any(c.kind == s.kind and abs(c.price - s.price) / c.price * 100 < 0.20 for s in selected):
            continue
        selected.append(c)
    return selected[:8]


def _level_key(c: LevelCandidate) -> str:
    return f"{c.inst_id}:{c.kind}:{c.lower:.12g}:{c.upper:.12g}"


def _init_level_db():
    db.execute("""
        CREATE TABLE IF NOT EXISTS levels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            level_key TEXT UNIQUE NOT NULL,
            inst_id TEXT NOT NULL,
            coin TEXT NOT NULL,
            kind TEXT NOT NULL,
            lower REAL NOT NULL,
            upper REAL NOT NULL,
            price REAL NOT NULL,
            score INTEGER NOT NULL,
            distance_pct REAL NOT NULL,
            timeframes TEXT NOT NULL,
            touches INTEGER NOT NULL,
            reactions INTEGER NOT NULL,
            status TEXT NOT NULL,
            reason TEXT NOT NULL,
            created_at REAL NOT NULL,
            last_sent_at REAL
        )
    """)
    db.commit()

_init_level_db()


def _level_recently_sent(candidate: LevelCandidate) -> bool:
    row = db.execute("SELECT last_sent_at FROM levels WHERE level_key=?", (_level_key(candidate),)).fetchone()
    return bool(row and row[0] and now_ts() - float(row[0]) < LEVEL_COOLDOWN_MINUTES * 60)


def _save_level(candidate: LevelCandidate, sent: bool):
    key = _level_key(candidate)
    ts = now_ts()
    db.execute("""
        INSERT INTO levels(level_key,inst_id,coin,kind,lower,upper,price,score,distance_pct,timeframes,touches,reactions,status,reason,created_at,last_sent_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(level_key) DO UPDATE SET
            score=excluded.score,
            distance_pct=excluded.distance_pct,
            timeframes=excluded.timeframes,
            touches=excluded.touches,
            reactions=excluded.reactions,
            status=excluded.status,
            reason=excluded.reason,
            last_sent_at=CASE WHEN ? THEN excluded.last_sent_at ELSE levels.last_sent_at END
    """, (key,candidate.inst_id,candidate.coin,candidate.kind,candidate.lower,candidate.upper,candidate.price,candidate.score,candidate.distance_pct,",".join(candidate.timeframes),candidate.touches,candidate.reactions,candidate.status,candidate.reason,ts,ts if sent else None,1 if sent else 0))
    db.commit()


def _draw_candles(ax, candles: List[Candle]):
    if not candles:
        return
    width = 0.62
    for i, c in enumerate(candles):
        ax.vlines(i, c.low, c.high, linewidth=1.0)
        body_low = min(c.open, c.close)
        body_h = max(abs(c.close - c.open), max(c.high - c.low, 1e-12) * 0.008)
        from matplotlib.patches import Rectangle
        ax.add_patch(Rectangle((i - width / 2, body_low), width, body_h, fill=False, linewidth=1.0))


def make_level_chart(level: LevelCandidate) -> str:
    path = f"level_{level.coin}_{int(time.time()*1000)}.png"
    candles = level.candles_1h[-LEVEL_CHART_CANDLES:]
    if not candles:
        return ""
    fig, ax = plt.subplots(figsize=(13, 7), facecolor="#0b1020")
    ax.set_facecolor("#0b1020")
    _draw_candles(ax, candles)
    ax.axhspan(level.lower, level.upper, alpha=0.20, label=f"{level.kind} zone")
    ax.axhline(level.price, linewidth=1.8, label="Level")
    ax.axhline(level.current_price, linewidth=1.0, linestyle="--", label="Current")
    ax.set_title(f"{level.coin}/USDT | 1H | {level.kind} | Score {level.score}/100", color="white", fontsize=15, fontweight="bold")
    ax.grid(alpha=0.12)
    ax.tick_params(colors="#9ca3af")
    for spine in ax.spines.values():
        spine.set_color("#29334d")
    ax.legend(facecolor="#111827", labelcolor="white", loc="upper left")
    plt.tight_layout()
    fig.savefig(path, facecolor=fig.get_facecolor(), bbox_inches="tight", dpi=140)
    plt.close(fig)
    return path


def build_level_text(level: LevelCandidate) -> str:
    tf = ", ".join(level.timeframes)
    distance = f"{level.distance_pct:.2f}%"
    return (
        f"<b>{'🟢 SUPPORT' if level.kind == 'SUPPORT' else '🔴 RESISTANCE'} — {level.coin}/USDT</b>\n\n"
        f"<b>Зона:</b> {fmt_price(level.lower)} — {fmt_price(level.upper)}\n"
        f"<b>Центр:</b> {fmt_price(level.price)}\n"
        f"<b>Цена:</b> {fmt_price(level.current_price)}\n"
        f"<b>Дистанция:</b> {distance}\n"
        f"<b>Level Score:</b> {level.score}/100\n"
        f"<b>Статус:</b> {level.status}\n\n"
        f"<b>Подтверждение:</b> {tf}\n"
        f"Касания: {level.touches} | Реакции: {level.reactions}\n"
        f"Объём: {level.volume_score}/15 | Структура: {level.structure_score}/40\n\n"
        f"<b>Почему уровень:</b> {html.escape(level.reason)}\n\n"
        f"⚠️ Уровень — зона интереса, а не гарантия разворота. Ждём реакцию цены."
    )


def send_level(candidate: LevelCandidate) -> bool:
    try:
        path = make_level_chart(candidate)
        text = build_level_text(candidate)
        if path and os.path.exists(path):
            with open(path, "rb") as photo:
                bot.send_photo(CHANNEL_ID, photo, caption=text, parse_mode="HTML")
            try:
                os.remove(path)
            except OSError:
                pass
        else:
            bot.send_message(CHANNEL_ID, text, parse_mode="HTML")
        return True
    except Exception:
        log.exception("LEVEL SEND FAILED | %s", candidate.inst_id)
        return False


def load_level_candles(inst_id: str) -> Dict[str, List[Candle]]:
    return {
        "1D": get_candles(inst_id, "1D", 140),
        "4H": get_candles(inst_id, "4H", 160),
        "1H": get_candles(inst_id, "1H", 180),
        "30m": get_candles(inst_id, "30m", 160),
        "15m": get_candles(inst_id, "15m", 160),
        "5m": get_candles(inst_id, "5m", 160),
    }


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
    all_levels = []
    for _, _, inst_id, ticker in deep:
        try:
            candles = load_level_candles(inst_id)
            levels = _build_level_candidates(inst_id, ticker, candles)
            all_levels.extend(levels)
        except Exception as exc:
            log.warning("LEVEL ANALYZE FAILED | %s | %s", inst_id, exc)
    # Diversify: don't let one coin occupy the whole top list.
    all_levels.sort(key=lambda x: (x.score, -x.distance_pct), reverse=True)
    final = []
    per_coin = defaultdict(int)
    for level in all_levels:
        if per_coin[level.coin] >= 2:
            continue
        final.append(level)
        per_coin[level.coin] += 1
        if len(final) >= LEVEL_FINAL_COUNT:
            break
    log.info("LEVEL SCAN | liquid=%s | deep=%s | levels=%s", len(universe), len(deep), len(final))
    for level in final:
        sent = False
        if level.score >= LEVEL_MIN_SCORE and not _level_recently_sent(level):
            sent = send_level(level)
        _save_level(level, sent)


def level_report():
    rows = db.execute("""
        SELECT coin, kind, score, distance_pct, status, touches, reactions, timeframes
        FROM levels ORDER BY created_at DESC LIMIT 20
    """).fetchall()
    if not rows:
        return "<b>LEVEL ENGINE</b>\nПока нет сохранённых уровней."
    lines = ["<b>LEVEL ENGINE — последние уровни</b>"]
    for coin, kind, score, dist, status, touches, reactions, tfs in rows:
        icon = "🟢" if kind == "SUPPORT" else "🔴"
        lines.append(f"{icon} {coin} {kind} | {score}/100 | {dist:.2f}% | {status} | {tfs}")
    return "\n".join(lines)


def main():
    log.info("QUANTUM LEVEL ENGINE V8 STARTED")
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
