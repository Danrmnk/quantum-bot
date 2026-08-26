# ============================================================
# QUANTUM SCALPER V3
# BINANCE USDⓈ-M FUTURES
# MARKET ANALYSIS -> SIGNALS -> TELEGRAM -> STATISTICS
#
# Бот НЕ торгует и НЕ использует Binance API Key.
# Он только получает публичные данные рынка,
# анализирует их и публикует сигналы.
#
# Основные возможности:
# - Binance USDⓈ-M Futures
# - все USDT perpetual-контракты
# - фильтр 24h quote volume >= $100,000,000
# - 1H trend
# - 15M structure
# - 5M confirmation
# - breakout / compression / momentum
# - volume confirmation
# - funding rate
# - score 0..100
# - entry zone
# - stop
# - TP1 / TP2 / TP3
# - автоматическое сопровождение сигналов
# - честная статистика в R
# - утреннее сообщение
# - дневная статистика
# - недельная статистика
# - SQLite
# - восстановление после перезапуска
# - retry / timeout / защита от падения цикла
# ============================================================

import os
import time
import math
import uuid
import sqlite3
import logging
import threading

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, List, Dict, Tuple

import requests
import telebot

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle


# ============================================================
# CONFIG
# ============================================================

TELEGRAM_TOKEN = os.getenv(
    "TELEGRAM_TOKEN",
    ""
).strip()

CHANNEL_ID = os.getenv(
    "CHANNEL_ID",
    ""
).strip()

# Binance USDⓈ-M Futures public API
BINANCE_BASE_URL = os.getenv(
    "BINANCE_BASE_URL",
    "https://fapi.binance.com"
).rstrip("/")

TIMEZONE = os.getenv(
    "BOT_TIMEZONE",
    "Europe/Kyiv"
)

# Главное условие пользователя:
# НЕ ограничиваем количество монет.
# Фильтруем все подходящие USDT perpetual.
MIN_24H_VOLUME_USD = float(
    os.getenv(
        "MIN_24H_VOLUME_USD",
        "100000000"
    )
)

MIN_SCORE = int(
    os.getenv(
        "MIN_SCORE",
        "82"
    )
)

READY_TTL_MINUTES = int(
    os.getenv(
        "READY_TTL_MINUTES",
        "10"
    )
)

COOLDOWN_MINUTES = int(
    os.getenv(
        "COOLDOWN_MINUTES",
        "60"
    )
)

SCAN_INTERVAL_SECONDS = int(
    os.getenv(
        "SCAN_INTERVAL_SECONDS",
        "60"
    )
)

MAX_CHASE_PCT = float(
    os.getenv(
        "MAX_CHASE_PCT",
        "0.35"
    )
)

MAX_RISK_PCT = float(
    os.getenv(
        "MAX_RISK_PCT",
        "1.50"
    )
)

MIN_RISK_PCT = float(
    os.getenv(
        "MIN_RISK_PCT",
        "0.15"
    )
)

# Час утреннего сообщения
MORNING_ENABLED = (
    os.getenv(
        "MORNING_ENABLED",
        "true"
    ).lower() == "true"
)

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

# Время дневной статистики
DAILY_REPORT_ENABLED = (
    os.getenv(
        "DAILY_REPORT_ENABLED",
        "true"
    ).lower() == "true"
)

DAILY_REPORT_HOUR = int(
    os.getenv(
        "DAILY_REPORT_HOUR",
        "23"
    )
)

DAILY_REPORT_MINUTE = int(
    os.getenv(
        "DAILY_REPORT_MINUTE",
        "50"
    )
)

# Недельный отчёт:
# 0 = понедельник
WEEKLY_REPORT_ENABLED = (
    os.getenv(
        "WEEKLY_REPORT_ENABLED",
        "true"
    ).lower() == "true"
)

WEEKLY_REPORT_DAY = int(
    os.getenv(
        "WEEKLY_REPORT_DAY",
        "6"
    )
)

WEEKLY_REPORT_HOUR = int(
    os.getenv(
        "WEEKLY_REPORT_HOUR",
        "23"
    )
)

WEEKLY_REPORT_MINUTE = int(
    os.getenv(
        "WEEKLY_REPORT_MINUTE",
        "55"
    )
)

DB_PATH = os.getenv(
    "DB_PATH",
    "quantum_scalper.db"
)

HTTP_TIMEOUT = int(
    os.getenv(
        "HTTP_TIMEOUT",
        "10"
    )
)

HTTP_RETRIES = int(
    os.getenv(
        "HTTP_RETRIES",
        "3"
    )
)


# ============================================================
# VALIDATION
# ============================================================

if not TELEGRAM_TOKEN:
    raise RuntimeError(
        "TELEGRAM_TOKEN не найден в Environment Variables."
    )

if not CHANNEL_ID:
    raise RuntimeError(
        "CHANNEL_ID не найден в Environment Variables."
    )

if MIN_24H_VOLUME_USD <= 0:
    raise RuntimeError(
        "MIN_24H_VOLUME_USD должен быть > 0."
    )

if MIN_SCORE < 1 or MIN_SCORE > 100:
    raise RuntimeError(
        "MIN_SCORE должен быть от 1 до 100."
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

log = logging.getLogger(
    "QUANTUM_SCALPER"
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

db_lock = threading.RLock()

db = sqlite3.connect(
    DB_PATH,
    check_same_thread=False
)

db.execute(
    "PRAGMA journal_mode=WAL"
)

db.execute(
    "PRAGMA synchronous=NORMAL"
)

db.execute("""
CREATE TABLE IF NOT EXISTS signals (

    id INTEGER PRIMARY KEY AUTOINCREMENT,

    signal_id TEXT UNIQUE NOT NULL,

    symbol TEXT NOT NULL,

    direction TEXT NOT NULL,

    strategy TEXT NOT NULL,

    level REAL NOT NULL,

    entry_low REAL NOT NULL,

    entry_high REAL NOT NULL,

    planned_entry REAL NOT NULL,

    actual_entry REAL,

    sl REAL NOT NULL,

    tp1 REAL NOT NULL,

    tp2 REAL NOT NULL,

    tp3 REAL NOT NULL,

    score INTEGER NOT NULL,

    volume_24h REAL NOT NULL,

    volume_ratio REAL NOT NULL,

    funding_rate REAL NOT NULL,

    atr_pct REAL NOT NULL,

    level_tf TEXT NOT NULL,

    status TEXT NOT NULL,

    created_at REAL NOT NULL,

    expires_at REAL NOT NULL,

    activated_at REAL,

    tp1_hit_at REAL,

    tp2_hit_at REAL,

    tp3_hit_at REAL,

    closed_at REAL,

    exit_price REAL,

    result_r REAL,

    realized_r REAL DEFAULT 0,

    photo_message_id INTEGER,

    text_message_id INTEGER
)
""")

db.execute("""
CREATE INDEX IF NOT EXISTS idx_signals_symbol
ON signals(symbol)
""")

db.execute("""
CREATE INDEX IF NOT EXISTS idx_signals_created
ON signals(created_at)
""")

db.execute("""
CREATE INDEX IF NOT EXISTS idx_signals_status
ON signals(status)
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

    signal_id: str

    symbol: str

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

    level_tf: str

    reason: str

    volume_24h: float

    volume_ratio: float

    funding_rate: float

    atr_pct: float

    candles_5m: List[Candle]


@dataclass
class ActiveSignal:

    setup: Setup

    created_at: float

    expires_at: float

    photo_message_id: Optional[int] = None

    text_message_id: Optional[int] = None

    activated: bool = False

    actual_entry: Optional[float] = None

    tp1_hit: bool = False
    tp2_hit: bool = False
    tp3_hit: bool = False

    realized_r: float = 0.0


# ============================================================
# MEMORY
# ============================================================

active_signals: Dict[
    str,
    ActiveSignal
] = {}

signals_hour: List[float] = []

scan_count = 0

last_morning_date = None
last_daily_report_date = None
last_weekly_report_key = None


# ============================================================
# TIME HELPERS
# ============================================================

def now_ts() -> float:
    return time.time()


def local_now() -> datetime:

    return datetime.now(
        ZoneInfo(TIMEZONE)
    )


def date_key() -> str:

    return str(
        local_now().date()
    )


def week_key() -> str:

    current = local_now()

    monday = (
        current.date()
        - timedelta(
            days=current.weekday()
        )
    )

    return str(monday)


# ============================================================
# GENERAL HELPERS
# ============================================================

def fmt_price(
    price: float
) -> str:

    if price >= 1000:

        return (
            f"{price:,.2f}"
            .replace(",", " ")
        )

    if price >= 100:

        return (
            f"{price:,.3f}"
            .replace(",", " ")
        )

    if price >= 1:

        return (
            f"{price:,.4f}"
            .replace(",", " ")
        )

    if price >= 0.01:

        return (
            f"{price:.6f}"
            .rstrip("0")
            .rstrip(".")
        )

    return (
        f"{price:.10f}"
        .rstrip("0")
        .rstrip(".")
    )


def fmt_usd(
    value: float
) -> str:

    if value >= 1_000_000_000:

        return (
            f"${value / 1_000_000_000:.2f}B"
        )

    if value >= 1_000_000:

        return (
            f"${value / 1_000_000:.1f}M"
        )

    if value >= 1_000:

        return (
            f"${value / 1_000:.1f}K"
        )

    return f"${value:.0f}"


def percentage(
    current: float,
    reference: float
) -> float:

    if reference == 0:
        return 0.0

    return (
        (current - reference)
        / reference
        * 100.0
    )


def clamp(
    value: float,
    low: float,
    high: float
) -> float:

    return max(
        low,
        min(high, value)
    )


def direction_ru(
    direction: str
) -> str:

    if direction == "LONG":
        return "ЛОНГ"

    return "ШОРТ"


def strategy_ru(
    strategy: str
) -> str:

    mapping = {

        "LEVEL_BREAKOUT":
            "Пробой ключевого уровня",

        "COMPRESSION_BREAKOUT":
            "Сжатие → пробой",

        "MOMENTUM_BREAKOUT":
            "Импульсный пробой",

    }

    return mapping.get(
        strategy,
        strategy
    )


def score_label(
    score: int
) -> str:

    if score >= 95:
        return "💎 ЭЛИТНЫЙ"

    if score >= 90:
        return "🔥 ПРЕМИУМ"

    if score >= 85:
        return "⚡ СИЛЬНЫЙ"

    return "🎯 СИГНАЛ"


def funding_label(
    funding: float
) -> str:

    pct = funding * 100

    return f"{pct:+.4f}%"


# ============================================================
# BINANCE PUBLIC API
# ============================================================

def binance_get(
    path: str,
    params: Optional[dict] = None,
    retries: int = HTTP_RETRIES
):

    url = (
        f"{BINANCE_BASE_URL}"
        f"{path}"
    )

    last_error = None

    for attempt in range(
        1,
        retries + 1
    ):

        try:

            response = session.get(
                url,
                params=params or {},
                timeout=HTTP_TIMEOUT
            )

            # Rate limit / server errors
            if response.status_code in (
                418,
                429,
                500,
                502,
                503,
                504
            ):

                raise RuntimeError(
                    f"HTTP {response.status_code}"
                )

            response.raise_for_status()

            return response.json()

        except Exception as exc:

            last_error = exc

            log.warning(
                "Binance ошибка "
                "%s/%s %s: %s",
                attempt,
                retries,
                path,
                exc
            )

            if attempt < retries:

                time.sleep(
                    min(
                        2 ** attempt,
                        8
                    )
                )

    raise RuntimeError(
        f"Binance request failed: "
        f"{last_error}"
    )


# ============================================================
# EXCHANGE INFO
# ============================================================

def get_exchange_symbols() -> set:

    payload = binance_get(
        "/fapi/v1/exchangeInfo"
    )

    symbols = set()

    for item in payload.get(
        "symbols",
        []
    ):

        try:

            symbol = item["symbol"]

            contract_type = item.get(
                "contractType"
            )

            status = item.get(
                "status"
            )

            quote_asset = item.get(
                "quoteAsset"
            )

            if (
                status == "TRADING"
                and quote_asset == "USDT"
                and contract_type == "PERPETUAL"
            ):

                symbols.add(
                    symbol
                )

        except Exception:
            continue

    return symbols


# ============================================================
# 24H TICKERS
# ============================================================

def get_tickers() -> Dict[str, dict]:

    payload = binance_get(
        "/fapi/v1/ticker/24hr"
    )

    result = {}

    for item in payload:

        try:

            symbol = item.get(
                "symbol",
                ""
            )

            if not symbol:
                continue

            last = float(
                item.get(
                    "lastPrice",
                    0
                )
            )

            volume = float(
                item.get(
                    "quoteVolume",
                    0
                )
            )

            high = float(
                item.get(
                    "highPrice",
                    0
                )
            )

            low = float(
                item.get(
                    "lowPrice",
                    0
                )
            )

            if (
                last <= 0
                or volume <= 0
            ):
                continue

            result[symbol] = {

                "last": last,

                "high24h": high,

                "low24h": low,

                "volume_24h": volume,

                "price_change_pct": float(
                    item.get(
                        "priceChangePercent",
                        0
                    )
                ),

                "weighted_price": float(
                    item.get(
                        "weightedAvgPrice",
                        0
                    )
                    or 0
                ),
            }

        except (
            TypeError,
            ValueError
        ):
            continue

    return result


# ============================================================
# FUNDING
# ============================================================

def get_funding_map(
    symbols: set
) -> Dict[str, float]:

    result = {}

    try:

        payload = binance_get(
            "/fapi/v1/premiumIndex"
        )

        for item in payload:

            symbol = item.get(
                "symbol"
            )

            if symbol not in symbols:
                continue

            try:

                result[symbol] = float(
                    item.get(
                        "lastFundingRate",
                        0
                    )
                    or 0
                )

            except (
                TypeError,
                ValueError
            ):
                result[symbol] = 0.0

    except Exception:

        log.exception(
            "Не удалось получить funding."
        )

    return result


# ============================================================
# KLINES
# ============================================================

def get_candles(
    symbol: str,
    interval: str,
    limit: int = 120
) -> List[Candle]:

    payload = binance_get(
        "/fapi/v1/klines",
        {
            "symbol": symbol,
            "interval": interval,
            "limit": min(
                limit,
                1000
            )
        }
    )

    result = []

    now_ms = int(
        time.time() * 1000
    )

    for row in payload:

        try:

            open_time = int(
                row[0]
            )

            close_time = int(
                row[6]
            )

            result.append(
                Candle(

                    ts=open_time,

                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),

                    volume=float(
                        row[5]
                    ),

                    quote_volume=float(
                        row[7]
                    ),

                    confirmed=(
                        close_time
                        < now_ms
                    )
                )
            )

        except (
            IndexError,
            TypeError,
            ValueError
        ):

            continue

    return result


# ============================================================
# INDICATORS
# ============================================================

def ema(
    values: List[float],
    period: int
) -> List[float]:

    if not values:
        return []

    alpha = (
        2.0
        / (period + 1.0)
    )

    result = [
        values[0]
    ]

    for value in values[1:]:

        result.append(
            value * alpha
            + result[-1]
            * (1 - alpha)
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

        trs.append(tr)

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

    e20 = ema(
        closes,
        20
    )[-1]

    e50 = ema(
        closes,
        50
    )[-1]

    recent = candles[-12:]

    first = recent[:6]
    second = recent[6:]

    first_high = max(
        c.high for c in first
    )

    second_high = max(
        c.high for c in second
    )

    first_low = min(
        c.low for c in first
    )

    second_low = min(
        c.low for c in second
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


# ============================================================
# PIVOTS
# ============================================================

def pivot_highs(
    candles: List[Candle],
    left: int = 2,
    right: int = 2
) -> List[Tuple[int, float]]:

    result = []

    if len(candles) < (
        left + right + 1
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
                (i, value)
            )

    return result


def pivot_lows(
    candles: List[Candle],
    left: int = 2,
    right: int = 2
) -> List[Tuple[int, float]]:

    result = []

    if len(candles) < (
        left + right + 1
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
                (i, value)
            )

    return result


# ============================================================
# KEY LEVEL
# ============================================================

def find_key_level(
    candles_15m: List[Candle],
    candles_1h: List[Candle],
    current: float,
    direction: str
) -> Tuple[
    Optional[float],
    str
]:

    candidates = []

    highs = pivot_highs(
        candles_15m
    )

    lows = pivot_lows(
        candles_15m
    )

    if direction == "LONG":

        for _, level in highs[-25:]:

            distance = abs(
                percentage(
                    level,
                    current
                )
            )

            if (
                level > current
                and distance <= 1.5
            ):

                candidates.append(
                    (
                        level,
                        "15M",
                        distance
                    )
                )

        if len(candles_1h) >= 30:

            recent = candles_1h[
                -25:-1
            ]

            level = max(
                c.high
                for c in recent
            )

            distance = abs(
                percentage(
                    level,
                    current
                )
            )

            if (
                level > current
                and distance <= 2.5
            ):

                candidates.append(
                    (
                        level,
                        "1H",
                        distance
                    )
                )

    else:

        for _, level in lows[-25:]:

            distance = abs(
                percentage(
                    level,
                    current
                )
            )

            if (
                level < current
                and distance <= 1.5
            ):

                candidates.append(
                    (
                        level,
                        "15M",
                        distance
                    )
                )

        if len(candles_1h) >= 30:

            recent = candles_1h[
                -25:-1
            ]

            level = min(
                c.low
                for c in recent
            )

            distance = abs(
                percentage(
                    level,
                    current
                )
            )

            if (
                level < current
                and distance <= 2.5
            ):

                candidates.append(
                    (
                        level,
                        "1H",
                        distance
                    )
                )

    if not candidates:

        return None, ""

    candidates.sort(
        key=lambda x: (
            0
            if x[1] == "1H"
            else 1,
            x[2]
        )
    )

    return (
        candidates[0][0],
        candidates[0][1]
    )


# ============================================================
# COMPRESSION
# ============================================================

def compression_score(
    candles: List[Candle],
    direction: str
) -> Tuple[int, bool]:

    if len(candles) < 30:

        return 0, False

    recent = candles[-20:]

    ranges = [
        c.high - c.low
        for c in recent
        if c.high > c.low
    ]

    if len(ranges) < 15:

        return 0, False

    first_avg = (
        sum(ranges[:8])
        / 8
    )

    last_avg = (
        sum(ranges[-8:])
        / 8
    )

    if first_avg <= 0:

        return 0, False

    reduction = (
        1
        - last_avg / first_avg
    )

    score = 0
    valid = False

    highs = pivot_highs(
        recent
    )

    lows = pivot_lows(
        recent
    )

    if direction == "LONG":

        values = [
            x[1]
            for x in lows[-3:]
        ]

        if len(values) >= 2:

            if all(
                values[i]
                <= values[i + 1]
                for i in range(
                    len(values) - 1
                )
            ):

                score += 12
                valid = True

    else:

        values = [
            x[1]
            for x in highs[-3:]
        ]

        if len(values) >= 2:

            if all(
                values[i]
                >= values[i + 1]
                for i in range(
                    len(values) - 1
                )
            ):

                score += 12
                valid = True

    if reduction >= 0.15:
        score += 8

    if reduction >= 0.25:
        score += 5

    return (
        min(score, 25),
        valid
    )


# ============================================================
# SIGNAL ANALYSIS
# ============================================================

def analyze_symbol(
    symbol: str,
    ticker: dict,
    funding_rate: float,
    candles_1h: List[Candle],
    candles_15m: List[Candle],
    candles_5m: List[Candle]
) -> Optional[Setup]:

    c1h = [
        c
        for c in candles_1h
        if c.confirmed
    ]

    c15 = [
        c
        for c in candles_15m
        if c.confirmed
    ]

    c5 = [
        c
        for c in candles_5m
        if c.confirmed
    ]

    if len(c1h) < 60:
        return None

    if len(c15) < 60:
        return None

    if len(c5) < 60:
        return None

    current = ticker["last"]

    trend = market_structure(
        c1h
    )

    if trend == "NEUTRAL":
        return None

    direction = trend

    level, level_tf = find_key_level(
        c15,
        c1h,
        current,
        direction
    )

    if level is None:
        return None

    atr_value = atr(
        c5,
        14
    )

    if atr_value <= 0:
        return None

    atr_pct = (
        atr_value
        / current
        * 100
    )

    if atr_pct < 0.03:
        return None

    if atr_pct > 2.0:
        return None

    v_ratio = volume_ratio(
        c5,
        20
    )

    current_candle = c5[-1]
    previous_candle = c5[-2]

    score = 0
    reasons = []

    # ========================================================
    # TREND
    # ========================================================

    score += 15

    reasons.append(
        "тренд 1H подтверждает направление"
    )

    # ========================================================
    # LEVEL
    # ========================================================

    if level_tf == "1H":

        score += 20

        reasons.append(
            "ключевой уровень 1H"
        )

    else:

        score += 12

        reasons.append(
            "локальный уровень 15M"
        )

    # ========================================================
    # COMPRESSION
    # ========================================================

    compression_points, compression_valid = (
        compression_score(
            c15,
            direction
        )
    )

    score += compression_points

    if compression_valid:

        reasons.append(
            "структура сжатия 15M"
        )

    # ========================================================
    # BREAKOUT
    # ========================================================

    breakout = False

    if direction == "LONG":

        breakout = (
            previous_candle.close
            <= level
            and current_candle.close
            > level
        )

    else:

        breakout = (
            previous_candle.close
            >= level
            and current_candle.close
            < level
        )

    # ========================================================
    # MOMENTUM
    # ========================================================

    closes = [
        c.close
        for c in c5
    ]

    ema9 = ema(
        closes,
        9
    )[-1]

    ema21 = ema(
        closes,
        21
    )[-1]

    momentum = False

    if direction == "LONG":

        previous_high = max(
            c.high
            for c in c5[-13:-1]
        )

        momentum = (
            ema9 > ema21
            and current_candle.close
            > previous_high
        )

    else:

        previous_low = min(
            c.low
            for c in c5[-13:-1]
        )

        momentum = (
            ema9 < ema21
            and current_candle.close
            < previous_low
        )

    # ========================================================
    # STRATEGY
    # ========================================================

    if breakout:

        strategy = (
            "LEVEL_BREAKOUT"
        )

        score += 25

        reasons.append(
            "5M подтвердил пробой уровня"
        )

    elif (
        momentum
        and compression_valid
    ):

        strategy = (
            "COMPRESSION_BREAKOUT"
        )

        score += 20

        reasons.append(
            "импульс после сжатия"
        )

    elif momentum:

        strategy = (
            "MOMENTUM_BREAKOUT"
        )

        score += 15

        reasons.append(
            "сильный импульс 5M"
        )

    else:

        return None

    # ========================================================
    # VOLUME
    # ========================================================

    if v_ratio >= 1.25:

        score += 8

        reasons.append(
            f"объём выше среднего ({v_ratio:.2f}x)"
        )

    if v_ratio >= 1.50:
        score += 4

    if v_ratio >= 2.00:
        score += 3

    # ========================================================
    # LIQUIDITY
    # ========================================================

    volume_24h = ticker[
        "volume_24h"
    ]

    if volume_24h >= 1_000_000_000:

        score += 5

        reasons.append(
            "24H ликвидность > $1B"
        )

    elif volume_24h >= 250_000_000:

        score += 3

        reasons.append(
            "24H высокая ликвидность"
        )

    else:

        reasons.append(
            "24H объём проходит фильтр"
        )

    # ========================================================
    # FUNDING
    # ========================================================

    funding_abs = abs(
        funding_rate
    )

    if funding_abs <= 0.0005:

        score += 4

        reasons.append(
            "funding нейтральный"
        )

    elif funding_abs <= 0.001:

        score += 2

        reasons.append(
            "funding умеренный"
        )

    else:

        reasons.append(
            "funding повышенный"
        )

    # ========================================================
    # DISTANCE FROM LEVEL
    # ========================================================

    distance = abs(
        percentage(
            current,
            level
        )
    )

    if distance > 1.0:

        return None

    if breakout:

        if distance > (
            MAX_CHASE_PCT + 0.20
        ):

            return None

    else:

        if distance > 0.40:

            return None

    # ========================================================
    # ENTRY ZONE
    # ========================================================

    zone_pct = clamp(
        atr_pct * 0.35,
        0.08,
        0.22
    )

    if direction == "LONG":

        entry_low = (
            level
            * (1 - zone_pct / 100)
        )

        entry_high = (
            level
            * (1 + zone_pct / 100)
        )

    else:

        entry_low = (
            level
            * (1 - zone_pct / 100)
        )

        entry_high = (
            level
            * (1 + zone_pct / 100)
        )

    # ========================================================
    # STOP
    # ========================================================

    recent = c15[-18:]

    if direction == "LONG":

        structural_low = min(
            c.low
            for c in recent
        )

        sl = (
            structural_low
            - atr_value * 0.25
        )

        if sl >= current:
            return None

        risk = (
            current - sl
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

        if sl <= current:
            return None

        risk = (
            sl - current
        )

    risk_pct = (
        risk
        / current
        * 100
    )

    if risk_pct < MIN_RISK_PCT:
        return None

    if risk_pct > MAX_RISK_PCT:
        return None

    # ========================================================
    # PLANNED ENTRY
    # ========================================================

    planned_entry = clamp(
        level,
        entry_low,
        entry_high
    )

    # ========================================================
    # TP
    # ========================================================

    # Расстояние TP считается от planned entry,
    # а не от случайной цены публикации.

    entry_risk = abs(
        planned_entry - sl
    )

    if entry_risk <= 0:
        return None

    if direction == "LONG":

        tp1 = (
            planned_entry
            + entry_risk * 1.0
        )

        tp2 = (
            planned_entry
            + entry_risk * 2.0
        )

        tp3 = (
            planned_entry
            + entry_risk * 3.0
        )

    else:

        tp1 = (
            planned_entry
            - entry_risk * 1.0
        )

        tp2 = (
            planned_entry
            - entry_risk * 2.0
        )

        tp3 = (
            planned_entry
            - entry_risk * 3.0
        )

    # ========================================================
    # FINAL SCORE
    # ========================================================

    score = int(
        clamp(
            score,
            0,
            100
        )
    )

    if score < MIN_SCORE:
        return None

    signal_id = (
        "Q-"
        + local_now().strftime(
            "%y%m%d"
        )
        + "-"
        + symbol
        + "-"
        + uuid.uuid4().hex[
            :6
        ].upper()
    )

    reason = (
        "• "
        + "\n• ".join(
            reasons
        )
    )

    return Setup(

        signal_id=signal_id,

        symbol=symbol,

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

        level_tf=level_tf,

        reason=reason,

        volume_24h=volume_24h,

        volume_ratio=v_ratio,

        funding_rate=funding_rate,

        atr_pct=atr_pct,

        candles_5m=c5[-80:]
    )


# ============================================================
# DATABASE HELPERS
# ============================================================

def db_execute(
    query: str,
    params: tuple = (),
    commit: bool = False
):

    with db_lock:

        cursor = db.execute(
            query,
            params
        )

        if commit:
            db.commit()

        return cursor


def save_signal(
    active: ActiveSignal
):

    setup = active.setup

    db_execute(
        """
        INSERT INTO signals (

            signal_id,
            symbol,
            direction,
            strategy,

            level,

            entry_low,
            entry_high,
            planned_entry,

            sl,

            tp1,
            tp2,
            tp3,

            score,

            volume_24h,
            volume_ratio,
            funding_rate,
            atr_pct,

            level_tf,

            status,

            created_at,
            expires_at,

            photo_message_id,
            text_message_id

        )
        VALUES (
            ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
        )
        """,
        (

            setup.signal_id,

            setup.symbol,

            setup.direction,

            setup.strategy,

            setup.level,

            setup.entry_low,

            setup.entry_high,

            setup.level,

            setup.sl,

            setup.tp1,
            setup.tp2,
            setup.tp3,

            setup.score,

            setup.volume_24h,

            setup.volume_ratio,

            setup.funding_rate,

            setup.atr_pct,

            setup.level_tf,

            "READY",

            active.created_at,

            active.expires_at,

            active.photo_message_id,

            active.text_message_id
        ),
        commit=True
    )


def update_signal(
    signal_id: str,
    **fields
):

    if not fields:
        return

    columns = []
    values = []

    for key, value in fields.items():

        columns.append(
            f"{key} = ?"
        )

        values.append(value)

    values.append(
        signal_id
    )

    query = (
        "UPDATE signals SET "
        + ", ".join(columns)
        + " WHERE signal_id = ?"
    )

    db_execute(
        query,
        tuple(values),
        commit=True
    )


# ============================================================
# RESTORE
# ============================================================

def restore_active_signals():

    rows = db_execute(
        """
        SELECT

            signal_id,
            symbol,
            direction,
            strategy,

            level,

            entry_low,
            entry_high,

            planned_entry,

            actual_entry,

            sl,

            tp1,
            tp2,
            tp3,

            score,

            volume_24h,
            volume_ratio,
            funding_rate,
            atr_pct,

            level_tf,

            status,

            created_at,
            expires_at,

            activated_at,

            tp1_hit_at,
            tp2_hit_at,
            tp3_hit_at,

            realized_r,

            photo_message_id,
            text_message_id

        FROM signals

        WHERE status IN (
            'READY',
            'ACTIVE',
            'TP1',
            'TP2'
        )

        ORDER BY created_at ASC
        """
    ).fetchall()

    restored = 0

    for row in rows:

        try:

            (
                signal_id,
                symbol,
                direction,
                strategy,

                level,

                entry_low,
                entry_high,

                planned_entry,

                actual_entry,

                sl,

                tp1,
                tp2,
                tp3,

                score,

                volume_24h,
                volume_ratio,
                funding_rate,
                atr_pct,

                level_tf,

                status,

                created_at,
                expires_at,

                activated_at,

                tp1_hit_at,
                tp2_hit_at,
                tp3_hit_at,

                realized_r,

                photo_message_id,
                text_message_id

            ) = row

            # READY истёк
            if (
                status == "READY"
                and expires_at < now_ts()
            ):

                update_signal(
                    signal_id,
                    status="EXPIRED",
                    closed_at=now_ts()
                )

                continue

            setup = Setup(

                signal_id=signal_id,

                symbol=symbol,

                direction=direction,

                strategy=strategy,

                level=float(level),

                current_price=(
                    float(actual_entry)
                    if actual_entry
                    else float(planned_entry)
                ),

                entry_low=float(
                    entry_low
                ),

                entry_high=float(
                    entry_high
                ),

                sl=float(sl),

                tp1=float(tp1),
                tp2=float(tp2),
                tp3=float(tp3),

                score=int(score),

                level_tf=level_tf,

                reason=(
                    "Сигнал восстановлен "
                    "после перезапуска."
                ),

                volume_24h=float(
                    volume_24h
                ),

                volume_ratio=float(
                    volume_ratio
                ),

                funding_rate=float(
                    funding_rate
                ),

                atr_pct=float(
                    atr_pct
                ),

                candles_5m=[]
            )

            active = ActiveSignal(

                setup=setup,

                created_at=float(
                    created_at
                ),

                expires_at=float(
                    expires_at
                ),

                photo_message_id=(
                    photo_message_id
                ),

                text_message_id=(
                    text_message_id
                ),

                activated=(
                    status != "READY"
                ),

                actual_entry=(
                    float(actual_entry)
                    if actual_entry
                    else None
                ),

                tp1_hit=(
                    tp1_hit_at
                    is not None
                ),

                tp2_hit=(
                    tp2_hit_at
                    is not None
                ),

                tp3_hit=(
                    tp3_hit_at
                    is not None
                ),

                realized_r=float(
                    realized_r or 0
                )
            )

            active_signals[
                symbol
            ] = active

            restored += 1

        except Exception:

            log.exception(
                "Ошибка восстановления сигнала."
            )

    log.info(
        "Восстановлено активных сигналов: %s",
        restored
    )


# ============================================================
# COOLDOWN
# ============================================================

def can_create_signal(
    symbol: str
) -> bool:

    if symbol in active_signals:
        return False

    row = db_execute(
        """
        SELECT created_at
        FROM signals
        WHERE symbol = ?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (symbol,)
    ).fetchone()

    if row:

        last_created = float(
            row[0]
        )

        if (
            now_ts()
            - last_created
            < COOLDOWN_MINUTES * 60
        ):

            return False

    return True


# ============================================================
# HOURLY SIGNAL LIMIT
# ============================================================

def clean_hour_history():

    cutoff = (
        now_ts()
        - 3600
    )

    signals_hour[:] = [
        value
        for value in signals_hour
        if value >= cutoff
    ]


# ============================================================
# CHART
# ============================================================

def make_chart(
    setup: Setup
) -> str:

    candles = setup.candles_5m[-70:]

    if len(candles) < 10:

        raise RuntimeError(
            "Недостаточно свечей для графика."
        )

    path = (
        "/tmp/"
        + setup.symbol
        + "_"
        + str(int(time.time()))
        + "_"
        + setup.signal_id
        + ".png"
    )

    fig, ax = plt.subplots(
        figsize=(12, 7),
        dpi=140
    )

    fig.patch.set_facecolor(
        "#080d18"
    )

    ax.set_facecolor(
        "#080d18"
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
        linewidth=2,
        linestyle="--"
    )

    # ENTRY ZONE
    ax.axhspan(
        setup.entry_low,
        setup.entry_high,
        color="#00aaff",
        alpha=0.10
    )

    # STOP
    ax.axhline(
        setup.sl,
        color="#ff3b30",
        linewidth=1.7,
        linestyle="-."
    )

    # TARGETS
    for target in (
        setup.tp1,
        setup.tp2,
        setup.tp3
    ):

        ax.axhline(
            target,
            color="#ffd166",
            linewidth=1.1
        )

    ax.text(
        len(candles) - 1,
        setup.level,
        "  УРОВЕНЬ",
        color="#f5c542",
        fontsize=10,
        fontweight="bold"
    )

    ax.text(
        len(candles) - 1,
        setup.sl,
        "  СТОП",
        color="#ff3b30",
        fontsize=10,
        fontweight="bold"
    )

    ax.set_title(
        (
            "BINANCE ФЬЮЧЕРСЫ\n"
            f"{setup.symbol} · "
            f"{direction_ru(setup.direction)} · "
            f"{setup.score}/100"
        ),
        color="white",
        fontsize=15,
        fontweight="bold"
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

    plt.close(fig)

    return path


# ============================================================
# SIGNAL TEXT
# ============================================================

def build_signal_text(
    setup: Setup
) -> str:

    planned_risk = abs(
        setup.level
        - setup.sl
    )

    if setup.level > 0:

        risk_pct = (
            planned_risk
            / setup.level
            * 100
        )

    else:

        risk_pct = 0

    return (

        "🔥 *BINANCE ФЬЮЧЕРСЫ*\n"
        "━━━━━━━━━━━━━━━━━━\n"

        f"*{setup.symbol} · "
        f"{direction_ru(setup.direction)}*\n"

        f"{score_label(setup.score)} · "
        f"`{setup.score}/100`\n"

        "━━━━━━━━━━━━━━━━━━\n\n"

        "📈 *НАПРАВЛЕНИЕ*\n"
        f"`{direction_ru(setup.direction)}`\n\n"

        "💰 *ТЕКУЩАЯ ЦЕНА*\n"
        f"`{fmt_price(setup.current_price)}`\n\n"

        "🎯 *ЗОНА ВХОДА*\n"
        f"`{fmt_price(setup.entry_low)}`"
        " — "
        f"`{fmt_price(setup.entry_high)}`\n\n"

        "🛑 *СТОП*\n"
        f"`{fmt_price(setup.sl)}`\n"
        f"Риск: `-{risk_pct:.2f}%`\n\n"

        "🎯 *TP1 · 30%*\n"
        f"`{fmt_price(setup.tp1)}`\n"
        "R/R `1 : 1`\n\n"

        "🎯 *TP2 · 30%*\n"
        f"`{fmt_price(setup.tp2)}`\n"
        "R/R `1 : 2`\n\n"

        "🏆 *TP3 · 40%*\n"
        f"`{fmt_price(setup.tp3)}`\n"
        "R/R `1 : 3`\n\n"

        "🧠 *СЕТАП*\n"
        f"`{strategy_ru(setup.strategy)}`\n\n"

        "📊 *ПОДТВЕРЖДЕНИЯ*\n"
        f"{setup.reason}\n\n"

        f"📍 Уровень: `{fmt_price(setup.level)}` "
        f"({setup.level_tf})\n"

        f"📦 Объём 5M: "
        f"`{setup.volume_ratio:.2f}x`\n"

        f"💧 24H объём: "
        f"`{fmt_usd(setup.volume_24h)}`\n"

        f"💵 Funding: "
        f"`{funding_label(setup.funding_rate)}`\n"

        f"📐 ATR 5M: "
        f"`{setup.atr_pct:.2f}%`\n\n"

        "⏱ *Сетап действителен:* "
        f"`{READY_TTL_MINUTES} мин`\n"

        "🔒 *Пауза по паре:* "
        f"`{COOLDOWN_MINUTES} мин`\n\n"

        "🔥 *Движение подтверждено.*\n"
        "🎯 Работаем строго по плану.\n"
        "⚡ Не догоняем цену."
    )


# ============================================================
# PUBLISH
# ============================================================

def publish_signal(
    setup: Setup
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

            "🔥 *BINANCE ФЬЮЧЕРСЫ*\n"

            f"*{setup.symbol} · "
            f"{direction_ru(setup.direction)}*\n"

            f"{score_label(setup.score)} · "
            f"`{setup.score}/100`\n"

            f"`{strategy_ru(setup.strategy)}`"
        )

        with open(
            chart_path,
            "rb"
        ) as photo:

            photo_message = (
                bot.send_photo(
                    CHANNEL_ID,
                    photo,
                    caption=caption,
                    parse_mode="Markdown"
                )
            )

        text_message = (
            bot.send_message(
                CHANNEL_ID,
                build_signal_text(
                    setup
                ),
                parse_mode="Markdown"
            )
        )

        return (
            photo_message.message_id,
            text_message.message_id
        )

    except Exception:

        log.exception(
            "Ошибка публикации %s",
            setup.signal_id
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
# ENTRY DETECTION
# ============================================================

def price_in_entry_zone(
    setup: Setup,
    price: float
) -> bool:

    return (
        setup.entry_low
        <= price
        <= setup.entry_high
    )


def activate_signal(
    active: ActiveSignal,
    price: float
):

    if active.activated:
        return

    setup = active.setup

    active.activated = True
    active.actual_entry = price

    # Реальный риск считается от фактической активации.
    risk = abs(
        price - setup.sl
    )

    if risk <= 0:
        risk = abs(
            setup.planned_entry
            - setup.sl
        )

    # Пересчитываем TP от реального entry.
    if setup.direction == "LONG":

        setup.tp1 = (
            price + risk * 1.0
        )

        setup.tp2 = (
            price + risk * 2.0
        )

        setup.tp3 = (
            price + risk * 3.0
        )

    else:

        setup.tp1 = (
            price - risk * 1.0
        )

        setup.tp2 = (
            price - risk * 2.0
        )

        setup.tp3 = (
            price - risk * 3.0
        )

    update_signal(
        setup.signal_id,

        status="ACTIVE",

        activated_at=now_ts(),

        actual_entry=price,

        tp1=setup.tp1,

        tp2=setup.tp2,

        tp3=setup.tp3
    )

    text = (

        "🟢 *ВХОД АКТИВИРОВАН*\n\n"

        "🔥 *BINANCE ФЬЮЧЕРСЫ*\n"

        f"*{setup.symbol} · "
        f"{direction_ru(setup.direction)}*\n\n"

        f"🆔 `{setup.signal_id}`\n\n"

        f"💰 Цена входа: "
        f"`{fmt_price(price)}`\n\n"

        f"🛑 Стоп: "
        f"`{fmt_price(setup.sl)}`\n"

        f"🎯 TP1: "
        f"`{fmt_price(setup.tp1)}`\n"

        f"🎯 TP2: "
        f"`{fmt_price(setup.tp2)}`\n"

        f"🏆 TP3: "
        f"`{fmt_price(setup.tp3)}`\n\n"

        "🎯 Вход подтверждён.\n"
        "🔥 Работаем по плану."
    )

    try:

        bot.send_message(
            CHANNEL_ID,
            text,
            parse_mode="Markdown"
        )

    except Exception:

        log.exception(
            "Ошибка ENTRY message."
        )


# ============================================================
# TP / STOP HELPERS
# ============================================================

def stop_price_hit(
    setup: Setup,
    price: float
) -> bool:

    if setup.direction == "LONG":

        return price <= setup.sl

    return price >= setup.sl


def target_hit(
    setup: Setup,
    price: float,
    target: float
) -> bool:

    if setup.direction == "LONG":

        return price >= target

    return price <= target


def send_tp_message(
    active: ActiveSignal,
    number: int,
    price: float
):

    setup = active.setup

    if number == 1:

        text = (

            "🎯 *TP1 ДОСТИГНУТ*\n\n"

            "🔥 *BINANCE ФЬЮЧЕРСЫ*\n"

            f"*{setup.symbol} · "
            f"{direction_ru(setup.direction)}*\n\n"

            f"🆔 `{setup.signal_id}`\n\n"

            f"Цена: `{fmt_price(price)}`\n\n"

            "Закрываем *30%*.\n"
            "🛡 Стоп переводится в *БЕЗУБЫТОК*.\n\n"

            "🔥 Сделка развивается по плану."
        )

    elif number == 2:

        text = (

            "🎯 *TP2 ДОСТИГНУТ*\n\n"

            "🔥 *BINANCE ФЬЮЧЕРСЫ*\n"

            f"*{setup.symbol} · "
            f"{direction_ru(setup.direction)}*\n\n"

            f"🆔 `{setup.signal_id}`\n\n"

            f"Цена: `{fmt_price(price)}`\n\n"

            "Закрываем ещё *30%*.\n"
            "Оставшиеся *40%* направляем к TP3.\n\n"

            "🏆 Держим план."
        )

    else:

        text = (

            "🏆 *TP3 ДОСТИГНУТ*\n\n"

            "🔥 *BINANCE ФЬЮЧЕРСЫ*\n"

            f"*{setup.symbol} · "
            f"{direction_ru(setup.direction)}*\n\n"

            f"🆔 `{setup.signal_id}`\n\n"

            f"Цена: `{fmt_price(price)}`\n\n"

            "Все 100% позиции по плану закрыты.\n"
            "🔥 Сетап завершён."
        )

    try:

        bot.send_message(
            CHANNEL_ID,
            text,
            parse_mode="Markdown"
        )

    except Exception:

        log.exception(
            "Ошибка TP message."
        )


def send_stop_message(
    active: ActiveSignal,
    price: float
):

    setup = active.setup

    entry = (
        active.actual_entry
        or setup.planned_entry
    )

    risk = abs(
        entry - setup.sl
    )

    if risk <= 0:
        risk = 1.0

    # Если TP1 был достигнут, часть позиции уже закрыта.
    # Стоп после TP1 находится на BE, поэтому результат
    # рассчитывается по уже реализованным частям.

    if not active.tp1_hit:

        remaining_r = -1.0

    elif not active.tp2_hit:

        # TP1: 30% * +1R = +0.30R
        # Остальные 70% закрылись на BE.
        remaining_r = 0.30

    else:

        # TP1 + TP2:
        # 0.30*1 + 0.30*2 = 0.90R
        # Остальные 40% закрылись на BE.
        remaining_r = 0.90

    result_r = remaining_r

    text = (

        "🛑 *СТОП / ЗАВЕРШЕНИЕ*\n\n"

        "🔥 *BINANCE ФЬЮЧЕРСЫ*\n"

        f"*{setup.symbol} · "
        f"{direction_ru(setup.direction)}*\n\n"

        f"🆔 `{setup.signal_id}`\n\n"

        f"Цена выхода: "
        f"`{fmt_price(price)}`\n\n"

        f"Результат: "
        f"`{result_r:+.2f}R`\n\n"

        "Сделка завершена.\n"
        "🧠 Риск не увеличиваем.\n"
        "🔥 Следующий сетап — только по плану."
    )

    try:

        bot.send_message(
            CHANNEL_ID,
            text,
            parse_mode="Markdown"
        )

    except Exception:

        log.exception(
            "Ошибка STOP message."
        )

    update_signal(

        setup.signal_id,

        status="SL",

        closed_at=now_ts(),

        exit_price=price,

        result_r=result_r,

        realized_r=result_r
    )


# ============================================================
# ACTIVE SIGNAL MANAGEMENT
# ============================================================

def manage_active_signal(
    symbol: str,
    price: float
):

    active = active_signals.get(
        symbol
    )

    if not active:
        return

    setup = active.setup

    # ========================================================
    # READY
    # ========================================================

    if not active.activated:

        # Если READY истёк
        if now_ts() >= active.expires_at:

            signal_id = (
                setup.signal_id
            )

            update_signal(
                signal_id,
                status="EXPIRED",
                closed_at=now_ts()
            )

            try:

                bot.send_message(
                    CHANNEL_ID,
                    (
                        "🔴 *СЕТАП ОТМЕНЁН*\n\n"

                        "🔥 *BINANCE ФЬЮЧЕРСЫ*\n"

                        f"*{symbol} · "
                        f"{direction_ru(setup.direction)}*\n\n"

                        f"🆔 `{signal_id}`\n\n"

                        "Цена не дала своевременной "
                        "активации.\n\n"

                        "🎯 Рынок не догоняем."
                    ),
                    parse_mode="Markdown"
                )

            except Exception:

                log.exception(
                    "Ошибка EXPIRED."
                )

            active_signals.pop(
                symbol,
                None
            )

            return

        # Вход только внутри зоны.
        if price_in_entry_zone(
            setup,
            price
        ):

            activate_signal(
                active,
                price
            )

        return

    # ========================================================
    # ACTIVE -> STOP
    # ========================================================

    # После TP1/TP2 стоп становится безубытком.
    stop = setup.sl

    if active.tp1_hit:

        stop = (
            active.actual_entry
            or setup.planned_entry
        )

    setup.sl = stop

    if stop_price_hit(
        setup,
        price
    ):

        send_stop_message(
            active,
            price
        )

        active_signals.pop(
            symbol,
            None
        )

        return

    # ========================================================
    # TP1
    # ========================================================

    if not active.tp1_hit:

        if target_hit(
            setup,
            price,
            setup.tp1
        ):

            active.tp1_hit = True

            # +0.30R реализовано
            active.realized_r = 0.30

            update_signal(

                setup.signal_id,

                status="TP1",

                tp1_hit_at=now_ts(),

                realized_r=0.30
            )

            send_tp_message(
                active,
                1,
                price
            )

            return

    # ========================================================
    # TP2
    # ========================================================

    if (
        active.tp1_hit
        and not active.tp2_hit
    ):

        if target_hit(
            setup,
            price,
            setup.tp2
        ):

            active.tp2_hit = True

            # 0.30*1R + 0.30*2R = 0.90R
            active.realized_r = 0.90

            update_signal(

                setup.signal_id,

                status="TP2",

                tp2_hit_at=now_ts(),

                realized_r=0.90
            )

            send_tp_message(
                active,
                2,
                price
            )

            return

    # ========================================================
    # TP3
    # ========================================================

    if (
        active.tp2_hit
        and not active.tp3_hit
    ):

        if target_hit(
            setup,
            price,
            setup.tp3
        ):

            active.tp3_hit = True

            # 0.30*1 + 0.30*2 + 0.40*3 = 2.10R
            active.realized_r = 2.10

            update_signal(

                setup.signal_id,

                status="TP3",

                tp3_hit_at=now_ts(),

                closed_at=now_ts(),

                exit_price=price,

                result_r=2.10,

                realized_r=2.10
            )

            send_tp_message(
                active,
                3,
                price
            )

            active_signals.pop(
                symbol,
                None
            )


# ============================================================
# MORNING MESSAGE
# ============================================================

def send_morning_message():

    global last_morning_date

    if not MORNING_ENABLED:
        return

    current = local_now()

    if (
        current.hour
        != MORNING_HOUR
        or current.minute
        != MORNING_MINUTE
    ):

        return

    today = current.date()

    if last_morning_date == today:
        return

    text = (

        "🌅 *ДОБРОЕ УТРО*\n\n"

        "🔥 *BINANCE ФЬЮЧЕРСЫ*\n\n"

        "Новый торговый день начинается.\n\n"

        "🎯 Ждём только подтверждённые сетапы.\n"
        "🛑 Риск держим под контролем.\n"
        "⚡ Не догоняем движение.\n"
        "⏳ Слабые входы пропускаем.\n\n"

        "*Качество важнее количества.*\n\n"

        "Холодная голова. Чёткий план. 🔥"
    )

    try:

        bot.send_message(
            CHANNEL_ID,
            text,
            parse_mode="Markdown"
        )

        last_morning_date = today

        save_state(
            "last_morning_date",
            str(today)
        )

    except Exception:

        log.exception(
            "Ошибка утреннего сообщения."
        )


# ============================================================
# STATISTICS
# ============================================================

def statistics_between(
    start_ts: float,
    end_ts: float
) -> dict:

    rows = db_execute(
        """
        SELECT
            status,
            result_r,
            realized_r,
            actual_entry,
            exit_price
        FROM signals
        WHERE created_at >= ?
        AND created_at < ?
        """,
        (
            start_ts,
            end_ts
        )
    ).fetchall()

    total = len(rows)

    activated = 0

    expired = 0

    tp1 = 0
    tp2 = 0
    tp3 = 0
    stop = 0

    total_r = 0.0

    wins = 0
    losses = 0

    for row in rows:

        (
            status,
            result_r,
            realized_r,
            actual_entry,
            exit_price
        ) = row

        if status in (
            "ACTIVE",
            "TP1",
            "TP2",
            "TP3",
            "SL"
        ):

            activated += 1

        if status == "EXPIRED":
            expired += 1

        if status == "TP1":
            tp1 += 1

        elif status == "TP2":
            tp2 += 1

        elif status == "TP3":
            tp3 += 1

        elif status == "SL":
            stop += 1

        # Только закрытые результаты.
        if status in (
            "SL",
            "TP3"
        ):

            value = (
                result_r
                if result_r is not None
                else realized_r
            )

            if value is not None:

                total_r += float(
                    value
                )

                if value > 0:
                    wins += 1

                elif value < 0:
                    losses += 1

    completed = (
        wins
        + losses
    )

    win_rate = (
        wins / completed * 100
        if completed > 0
        else 0.0
    )

    return {

        "total": total,

        "activated": activated,

        "expired": expired,

        "tp1": tp1,

        "tp2": tp2,

        "tp3": tp3,

        "stop": stop,

        "wins": wins,

        "losses": losses,

        "completed": completed,

        "win_rate": win_rate,

        "total_r": total_r
    }


def start_of_day(
    dt: datetime
) -> float:

    beginning = datetime(
        dt.year,
        dt.month,
        dt.day,
        tzinfo=dt.tzinfo
    )

    return beginning.timestamp()


# ============================================================
# DAILY REPORT
# ============================================================

def send_daily_report():

    global last_daily_report_date

    if not DAILY_REPORT_ENABLED:
        return

    current = local_now()

    if (
        current.hour
        != DAILY_REPORT_HOUR
        or current.minute
        != DAILY_REPORT_MINUTE
    ):

        return

    today = current.date()

    if last_daily_report_date == today:
        return

    start = start_of_day(
        current
    )

    end = (
        start
        + 86400
    )

    stats = statistics_between(
        start,
        end
    )

    text = (

        "🌙 *ИТОГИ ТОРГОВОГО ДНЯ*\n\n"

        "🔥 *BINANCE ФЬЮЧЕРСЫ*\n"
        "━━━━━━━━━━━━━━━━━━\n\n"

        f"Сигналов: `{stats['total']}`\n"

        f"Активировано: "
        f"`{stats['activated']}`\n"

        f"Не активировано: "
        f"`{stats['expired']}`\n\n"

        f"🎯 TP1: `{stats['tp1']}`\n"
        f"🎯 TP2: `{stats['tp2']}`\n"
        f"🏆 TP3: `{stats['tp3']}`\n"
        f"🛑 STOP: `{stats['stop']}`\n\n"

        f"📊 Закрытых: "
        f"`{stats['completed']}`\n"

        f"✅ Положительных: "
        f"`{stats['wins']}`\n"

        f"❌ Отрицательных: "
        f"`{stats['losses']}`\n\n"

        f"🎯 Win Rate: "
        f"`{stats['win_rate']:.1f}%`\n"

        f"⚖️ Результат: "
        f"`{stats['total_r']:+.2f}R`\n\n"

        "ℹ️ Статистика рассчитана только "
        "по фактически зафиксированным "
        "результатам сигналов.\n\n"

        "*Без выдуманной прибыли. "
        "Только данные системы.*"
    )

    try:

        bot.send_message(
            CHANNEL_ID,
            text,
            parse_mode="Markdown"
        )

        last_daily_report_date = today

        save_state(
            "last_daily_report_date",
            str(today)
        )

    except Exception:

        log.exception(
            "Ошибка дневной статистики."
        )


# ============================================================
# WEEKLY REPORT
# ============================================================

def send_weekly_report():

    global last_weekly_report_key

    if not WEEKLY_REPORT_ENABLED:
        return

    current = local_now()

    if current.weekday() != WEEKLY_REPORT_DAY:
        return

    if (
        current.hour
        != WEEKLY_REPORT_HOUR
        or current.minute
        != WEEKLY_REPORT_MINUTE
    ):

        return

    key = week_key()

    if last_weekly_report_key == key:
        return

    monday = (
        current
        - timedelta(
            days=current.weekday()
        )
    )

    monday = datetime(
        monday.year,
        monday.month,
        monday.day,
        tzinfo=current.tzinfo
    )

    start = monday.timestamp()

    end = (
        start
        + 7 * 86400
    )

    stats = statistics_between(
        start,
        end
    )

    text = (

        "📅 *ИТОГИ НЕДЕЛИ*\n\n"

        "🔥 *BINANCE ФЬЮЧЕРСЫ*\n"
        "━━━━━━━━━━━━━━━━━━\n\n"

        f"Сигналов: `{stats['total']}`\n"

        f"Активировано: "
        f"`{stats['activated']}`\n"

        f"Не активировано: "
        f"`{stats['expired']}`\n\n"

        f"🎯 TP1: `{stats['tp1']}`\n"
        f"🎯 TP2: `{stats['tp2']}`\n"
        f"🏆 TP3: `{stats['tp3']}`\n"
        f"🛑 STOP: `{stats['stop']}`\n\n"

        f"📊 Закрытых: "
        f"`{stats['completed']}`\n"

        f"✅ Положительных: "
        f"`{stats['wins']}`\n"

        f"❌ Отрицательных: "
        f"`{stats['losses']}`\n\n"

        f"🎯 Win Rate: "
        f"`{stats['win_rate']:.1f}%`\n"

        f"⚖️ Результат недели: "
        f"`{stats['total_r']:+.2f}R`\n\n"

        "ℹ️ R рассчитывается по системе "
        "частичного выхода:\n"
        "30% / 30% / 40%.\n\n"

        "*Статистика основана только "
        "на реально зафиксированных "
        "результатах сигналов.*"
    )

    try:

        bot.send_message(
            CHANNEL_ID,
            text,
            parse_mode="Markdown"
        )

        last_weekly_report_key = key

        save_state(
            "last_weekly_report_key",
            key
        )

    except Exception:

        log.exception(
            "Ошибка недельной статистики."
        )


# ============================================================
# STATE
# ============================================================

def save_state(
    key: str,
    value: str
):

    db_execute(
        """
        INSERT INTO bot_state (
            key,
            value
        )
        VALUES (?, ?)

        ON CONFLICT(key)
        DO UPDATE SET value = excluded.value
        """,
        (
            key,
            value
        ),
        commit=True
    )


def load_state(
    key: str
) -> Optional[str]:

    row = db_execute(
        """
        SELECT value
        FROM bot_state
        WHERE key = ?
        """,
        (key,)
    ).fetchone()

    if not row:
        return None

    return row[0]


def restore_report_state():

    global last_morning_date
    global last_daily_report_date
    global last_weekly_report_key

    morning = load_state(
        "last_morning_date"
    )

    if morning:

        try:

            last_morning_date = (
                datetime
                .fromisoformat(morning)
                .date()
            )

        except Exception:
            pass

    daily = load_state(
        "last_daily_report_date"
    )

    if daily:

        try:

            last_daily_report_date = (
                datetime
                .fromisoformat(daily)
                .date()
            )

        except Exception:
            pass

    last_weekly_report_key = load_state(
        "last_weekly_report_key"
    )


# ============================================================
# MARKET SCAN
# ============================================================

def scan_market():

    global scan_count

    scan_count += 1

    log.info(
        "========== СКАН #%s ==========",
        scan_count
    )

    # --------------------------------------------------------
    # Получаем 24H тикеры одним запросом.
    # --------------------------------------------------------

    tickers = get_tickers()

    # --------------------------------------------------------
    # Получаем список реально торгующихся
    # USDT perpetual.
    # --------------------------------------------------------

    exchange_symbols = (
        get_exchange_symbols()
    )

    # --------------------------------------------------------
    # Все монеты, удовлетворяющие $100M.
    # Никакого MAX_SYMBOLS.
    # --------------------------------------------------------

    candidates = [

        (
            symbol,
            ticker
        )

        for symbol, ticker
        in tickers.items()

        if (
            symbol
            in exchange_symbols
            and ticker[
                "volume_24h"
            ]
            >= MIN_24H_VOLUME_USD
        )
    ]

    candidates.sort(
        key=lambda item:
            item[1]["volume_24h"],
        reverse=True
    )

    log.info(
        "Тикеры=%s | "
        "USDT perpetual=%s | "
        "Объём >= $100M=%s",
        len(tickers),
        len(exchange_symbols),
        len(candidates)
    )

    if not candidates:
        return

    # Funding одним запросом для всех.
    funding_map = get_funding_map(
        {
            symbol
            for symbol, _
            in candidates
        }
    )

    analyzed = 0
    signals_created = 0

    for symbol, ticker in candidates:

        try:

            # ------------------------------------------------
            # Сначала сопровождаем существующий сигнал.
            # ------------------------------------------------

            manage_active_signal(
                symbol,
                ticker["last"]
            )

            # ------------------------------------------------
            # Новый сигнал по той же паре не создаём.
            # ------------------------------------------------

            if symbol in active_signals:
                continue

            if not can_create_signal(
                symbol
            ):
                continue

            # ------------------------------------------------
            # Свечи.
            # ------------------------------------------------

            candles_1h = get_candles(
                symbol,
                "1h",
                100
            )

            candles_15m = get_candles(
                symbol,
                "15m",
                100
            )

            candles_5m = get_candles(
                symbol,
                "5m",
                100
            )

            analyzed += 1

            funding = funding_map.get(
                symbol,
                0.0
            )

            setup = analyze_symbol(
                symbol,

                ticker,

                funding,

                candles_1h,

                candles_15m,

                candles_5m
            )

            if not setup:
                continue

            log.info(
                "КАНДИДАТ | %s | %s | "
                "%s | score=%s | volume=%s",
                symbol,
                setup.direction,
                setup.strategy,
                setup.score,
                fmt_usd(
                    setup.volume_24h
                )
            )

            # ------------------------------------------------
            # Публикуем.
            # ------------------------------------------------

            photo_id, text_id = (
                publish_signal(
                    setup
                )
            )

            if not text_id:

                log.warning(
                    "Сигнал %s не сохранён: "
                    "Telegram публикация не удалась.",
                    setup.signal_id
                )

                continue

            created = now_ts()

            active = ActiveSignal(

                setup=setup,

                created_at=created,

                expires_at=(
                    created
                    + READY_TTL_MINUTES * 60
                ),

                photo_message_id=photo_id,

                text_message_id=text_id
            )

            active_signals[
                symbol
            ] = active

            save_signal(
                active
            )

            signals_hour.append(
                created
            )

            signals_created += 1

            log.info(
                "READY СОЗДАН | %s | %s",
                setup.signal_id,
                setup.score
            )

            # Небольшая пауза.
            # Это снижает вероятность лишнего
            # давления на API/Telegram.
            time.sleep(0.5)

        except Exception as exc:

            log.exception(
                "Ошибка анализа %s: %s",
                symbol,
                exc
            )

            continue

    log.info(
        "СКАН #%s ЗАВЕРШЁН | "
        "анализ=%s | новых сигналов=%s",
        scan_count,
        analyzed,
        signals_created
    )


# ============================================================
# BINANCE CONNECTIVITY TEST
# ============================================================

def test_binance():

    try:

        payload = binance_get(
            "/fapi/v1/time"
        )

        server_time = payload.get(
            "serverTime"
        )

        if server_time:

            log.info(
                "BINANCE FUTURES ONLINE | "
                "server=%s",
                server_time
            )

        else:

            log.info(
                "BINANCE FUTURES ONLINE"
            )

        return True

    except Exception:

        log.exception(
            "BINANCE FUTURES недоступен."
        )

        return False


# ============================================================
# MAIN
# ============================================================

def main():

    log.info(
        "======================================"
    )

    log.info(
        "QUANTUM SCALPER V3 START"
    )

    log.info(
        "Market: BINANCE USDⓈ-M FUTURES"
    )

    log.info(
        "Min 24H volume: $%s",
        f"{MIN_24H_VOLUME_USD:,.0f}"
    )

    log.info(
        "Min score: %s",
        MIN_SCORE
    )

    log.info(
        "Timezone: %s",
        TIMEZONE
    )

    log.info(
        "======================================"
    )

    # --------------------------------------------------------
    # Восстановление.
    # --------------------------------------------------------

    restore_report_state()

    restore_active_signals()

    # --------------------------------------------------------
    # Проверка Binance.
    # --------------------------------------------------------

    if not test_binance():

        log.warning(
            "Binance не отвечает. "
            "Основной цикл продолжит попытки."
        )

    # --------------------------------------------------------
    # Основной цикл.
    # --------------------------------------------------------

    while True:

        cycle_started = now_ts()

        try:

            # -----------------------------------------------
            # Служебные сообщения.
            # -----------------------------------------------

            send_morning_message()

            send_daily_report()

            send_weekly_report()

            # -----------------------------------------------
            # Убираем старые записи hourly memory.
            # -----------------------------------------------

            clean_hour_history()

            # -----------------------------------------------
            # Рынок.
            # -----------------------------------------------

            scan_market()

            elapsed = (
                now_ts()
                - cycle_started
            )

            sleep_for = max(
                5,
                SCAN_INTERVAL_SECONDS
                - int(elapsed)
            )

            log.info(
                "Следующий скан через %s сек.",
                sleep_for
            )

            time.sleep(
                sleep_for
            )

        except KeyboardInterrupt:

            log.info(
                "QUANTUM SCALPER остановлен."
            )

            break

        except Exception as exc:

            log.exception(
                "КРИТИЧЕСКАЯ ОШИБКА ЦИКЛА: %s",
                exc
            )

            log.warning(
                "Продолжение работы через 15 секунд."
            )

            time.sleep(15)


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()
