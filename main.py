# ============================================================
# QUANTUM SCALPER V2
# ============================================================
#
# OKX PUBLIC MARKET DATA
# ->
# MARKET ANALYSIS
# ->
# SCALPING SIGNAL ENGINE
# ->
# TELEGRAM
#
# ВАЖНО:
# БОТ НЕ ТОРГУЕТ.
# Только анализирует рынок и публикует сигналы.
#
# ============================================================

import os
import time
import math
import logging
import sqlite3
import uuid

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
# CONFIG
# ============================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()

OKX_BASE_URL = os.getenv(
    "OKX_BASE_URL",
    "https://www.okx.com"
).rstrip("/")

TIMEZONE = os.getenv(
    "BOT_TIMEZONE",
    "Europe/Kyiv"
)

MAX_SYMBOLS = int(
    os.getenv("MAX_SYMBOLS", "25")
)

MIN_24H_VOLUME_USD = float(
    os.getenv(
        "MIN_24H_VOLUME_USD",
        "20000000"
    )
)

MIN_SCORE = int(
    os.getenv("MIN_SCORE", "82")
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

MAX_SIGNALS_PER_HOUR = int(
    os.getenv(
        "MAX_SIGNALS_PER_HOUR",
        "6"
    )
)

SCAN_INTERVAL_SECONDS = int(
    os.getenv(
        "SCAN_INTERVAL_SECONDS",
        "30"
    )
)

MAX_CHASE_PCT = float(
    os.getenv(
        "MAX_CHASE_PCT",
        "0.35"
    )
)

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

DB_PATH = os.getenv(
    "DB_PATH",
    "quantum_scalper.db"
)


if not TELEGRAM_TOKEN:
    raise RuntimeError(
        "TELEGRAM_TOKEN не найден."
    )

if not CHANNEL_ID:
    raise RuntimeError(
        "CHANNEL_ID не найден."
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
    "User-Agent": "QuantumScalper/2.0",
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

    signal_id TEXT UNIQUE NOT NULL,

    inst_id TEXT NOT NULL,
    coin TEXT NOT NULL,

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

    tp1_hit_at REAL,
    tp2_hit_at REAL,
    tp3_hit_at REAL,

    closed_at REAL,

    exit_price REAL,

    result_r REAL,

    expires_at REAL NOT NULL,

    photo_message_id INTEGER,
    text_message_id INTEGER
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

    signal_id: str

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

    level_tf: str

    reason: str

    volume_24h: float
    volume_ratio: float

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

    tp1_hit: bool = False
    tp2_hit: bool = False
    tp3_hit: bool = False


# ============================================================
# MEMORY
# ============================================================

active_signals: Dict[
    str,
    ActiveSignal
] = {}

signals_hour: List[float] = []

last_scan_ts = 0.0
scan_count = 0

signals_today = 0

last_morning_date = None


# ============================================================
# GENERAL HELPERS
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
        return (
            f"{price:,.2f}"
            .replace(",", " ")
        )

    if price >= 100:
        return (
            f"{price:,.2f}"
            .replace(",", " ")
        )

    if price >= 1:
        return (
            f"{price:,.4f}"
            .replace(",", " ")
        )

    return (
        f"{price:.8f}"
        .rstrip("0")
        .rstrip(".")
    )


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


def coin_name(
    inst_id: str
) -> str:

    return inst_id.replace(
        "-USDT-SWAP",
        ""
    )


def direction_ru(
    direction: str
) -> str:

    return (
        "ЛОНГ"
        if direction == "LONG"
        else "ШОРТ"
    )


def strategy_ru(
    strategy: str
) -> str:

    mapping = {

        "LEVEL_BREAKOUT":
            "Пробой ключевого уровня",

        "COMPRESSION_BREAKOUT":
            "Пробой после сжатия",

        "MOMENTUM_BREAKOUT":
            "Импульсный пробой",

        "LEVEL_REJECTION":
            "Отбой от ключевого уровня",

        "FALSE_BREAKOUT":
            "Ложный пробой",

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


# ============================================================
# OKX
# ============================================================

def okx_get(
    path: str,
    params: dict,
    retries: int = 3
):

    url = (
        f"{OKX_BASE_URL}"
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
                params=params,
                timeout=10
            )

            response.raise_for_status()

            payload = response.json()

            if payload.get("code") != "0":

                raise RuntimeError(
                    "OKX error: "
                    f"{payload.get('code')} "
                    f"{payload.get('msg')}"
                )

            return payload

        except Exception as exc:

            last_error = exc

            log.warning(
                "OKX ошибка "
                "%s/%s %s: %s",
                attempt,
                retries,
                path,
                exc
            )

            if attempt < retries:
                time.sleep(
                    attempt * 2
                )

    raise RuntimeError(
        f"OKX request failed: "
        f"{last_error}"
    )


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

        inst_id = item.get(
            "instId",
            ""
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
                )
            )

            volume = float(
                item.get(
                    "volCcyQuote24h",
                    0
                ) or 0
            )

            if last <= 0:
                continue

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

                "volume_24h": volume,

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

    result = []

    for row in reversed(
        payload.get(
            "data",
            []
        )
    ):

        try:

            result.append(
                Candle(
                    ts=int(row[0]),

                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),

                    volume=float(
                        row[5] or 0
                    ),

                    quote_volume=float(
                        row[7] or 0
                    ),

                    confirmed=(
                        row[8] == "1"
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

    alpha = 2.0 / (
        period + 1.0
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

        true_range = max(

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
            true_range
        )

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
        current / average
    )


# ============================================================
# STRUCTURE
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
# LEVEL ENGINE
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

    # ------------------------------------------
    # LONG -> resistance above price
    # ------------------------------------------

    if direction == "LONG":

        for _, level in highs[-20:]:

            distance = abs(
                percentage(
                    level,
                    current
                )
            )

            if (
                level > current
                and distance <= 1.2
            ):

                candidates.append(
                    (
                        level,
                        "15M",
                        distance
                    )
                )

        # 1H resistance

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
                and distance <= 2.0
            ):

                candidates.append(
                    (
                        level,
                        "1H",
                        distance
                    )
                )

    # ------------------------------------------
    # SHORT -> support below price
    # ------------------------------------------

    else:

        for _, level in lows[-20:]:

            distance = abs(
                percentage(
                    level,
                    current
                )
            )

            if (
                level < current
                and distance <= 1.2
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
                and distance <= 2.0
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
            0 if x[1] == "1H"
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

def compression(
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

    return min(
        score,
        25
    ), valid


# ============================================================
# SIGNAL ENGINE
# ============================================================

def analyze_symbol(
    inst_id: str,
    ticker: dict,
    candles_1h: List[Candle],
    candles_15m: List[Candle],
    candles_5m: List[Candle]
) -> Optional[Setup]:

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

    if len(confirmed_5m) < 60:
        return None

    if len(confirmed_15m) < 60:
        return None

    if len(confirmed_1h) < 60:
        return None

    current = ticker["last"]

    trend = market_structure(
        confirmed_1h
    )

    if trend == "NEUTRAL":
        return None

    direction = trend

    level, level_tf = find_key_level(
        confirmed_15m,
        confirmed_1h,
        current,
        direction
    )

    if level is None:
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
        * 100
    )

    # Фильтр слишком спокойного/безумного рынка.

    if atr_pct < 0.03:
        return None

    if atr_pct > 2.0:
        return None

    v_ratio = volume_ratio(
        confirmed_5m,
        20
    )

    current_candle = (
        confirmed_5m[-1]
    )

    previous_candle = (
        confirmed_5m[-2]
    )

    # ========================================================
    # SCORE
    # ========================================================

    score = 0

    reasons = []

    # 1H trend

    score += 15

    reasons.append(
        "тренд 1H подтверждает направление"
    )

    # Strong level

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

    # Compression

    compression_points, compression_valid = (
        compression(
            confirmed_15m,
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
        for c in confirmed_5m
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
            for c in confirmed_5m[
                -13:-1
            ]
        )

        momentum = (
            ema9 > ema21
            and current_candle.close
            > previous_high
        )

    else:

        previous_low = min(
            c.low
            for c in confirmed_5m[
                -13:-1
            ]
        )

        momentum = (
            ema9 < ema21
            and current_candle.close
            < previous_low
        )

    # ========================================================
    # STRATEGY
    # ========================================================

    strategy = None

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

        # Пока нет подтверждения.
        # Не отправляем READY.

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

    if v_ratio >= 2.0:
        score += 3

    # ========================================================
    # LIQUIDITY
    # ========================================================

    volume_24h = ticker[
        "volume_24h"
    ]

    if volume_24h >= 1_000_000_000:

        score += 5

        liquidity = "ВЫСОКАЯ"

    elif volume_24h >= 250_000_000:

        score += 3

        liquidity = "ХОРОШАЯ"

    else:

        liquidity = "СРЕДНЯЯ"

    # ========================================================
    # DISTANCE
    # ========================================================

    distance = abs(
        percentage(
            current,
            level
        )
    )

    if distance > 1.0:
        return None

    # После полноценного пробоя цена может быть немного
    # выше/ниже уровня, но не должна убегать.

    if breakout:

        if distance > (
            MAX_CHASE_PCT
            + 0.20
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

    recent = confirmed_15m[-18:]

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

    if risk_pct < 0.15:
        return None

    if risk_pct > 1.50:
        return None

    # ========================================================
    # TP
    # ========================================================

    if direction == "LONG":

        tp1 = current + risk * 1.0
        tp2 = current + risk * 2.0
        tp3 = current + risk * 3.0

    else:

        tp1 = current - risk * 1.0
        tp2 = current - risk * 2.0
        tp3 = current - risk * 3.0

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
        f"Q-"
        f"{local_now().strftime('%y%m%d')}-"
        f"{coin_name(inst_id)}-"
        f"{uuid.uuid4().hex[:6].upper()}"
    )

    reason = (
        "• "
        + "\n• ".join(reasons)
    )

    return Setup(

        signal_id=signal_id,

        inst_id=inst_id,
        coin=coin_name(inst_id),

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

        volume_grade=(
            "ОЧЕНЬ ВЫСОКИЙ"
            if v_ratio >= 2.0
            else
            "ВЫСОКИЙ"
            if v_ratio >= 1.5
            else
            "ХОРОШИЙ"
            if v_ratio >= 1.25
            else
            "ОБЫЧНЫЙ"
        ),

        level_tf=level_tf,

        reason=reason,

        volume_24h=volume_24h,

        volume_ratio=v_ratio,

        atr_pct=atr_pct,

        candles_5m=(
            confirmed_5m[-80:]
        )
    )


# ============================================================
# DATABASE HELPERS
# ============================================================

def save_signal(
    setup: Setup,
    active: ActiveSignal
):

    created = active.created_at

    expires = active.expires_at

    db.execute(
        """
        INSERT INTO signals (
            signal_id,
            inst_id,
            coin,
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
            expires_at,
            photo_message_id,
            text_message_id
        )
        VALUES (
            ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
        )
        """,
        (
            setup.signal_id,

            setup.inst_id,
            setup.coin,

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

            "READY",

            created,

            expires,

            active.photo_message_id,
            active.text_message_id,
        )
    )

    db.commit()


def update_status(
    signal_id: str,
    status: str
):

    db.execute(
        """
        UPDATE signals
        SET status = ?
        WHERE signal_id = ?
        """,
        (
            status,
            signal_id
        )
    )

    db.commit()


def restore_active_signals():

    rows = db.execute(
        """
        SELECT
            signal_id,
            inst_id,
            coin,
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
            created_at,
            expires_at,
            photo_message_id,
            text_message_id
        FROM signals
        WHERE status IN (
            'READY',
            'ACTIVE'
        )
        """
    ).fetchall()

    restored = 0

    for row in rows:

        (
            signal_id,
            inst_id,
            coin,
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
            created_at,
            expires_at,
            photo_message_id,
            text_message_id
        ) = row

        if (
            float(expires_at)
            < now_ts()
            and (
                # ACTIVE не должен исчезать только
                # из-за READY TTL.
                # READY после TTL исчезает.
                db.execute(
                    """
                    SELECT status
                    FROM signals
                    WHERE signal_id = ?
                    """,
                    (signal_id,)
                ).fetchone()[0]
                == "READY"
            )
        ):
            update_status(
                signal_id,
                "EXPIRED"
            )
            continue

        # После рестарта нам нужны свежие свечи.
        # Поэтому Setup временно создаём без chart data.
        setup = Setup(

            signal_id=signal_id,

            inst_id=inst_id,
            coin=coin,

            direction=direction,
            strategy=strategy,

            level=float(level),
            current_price=0.0,

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

            liquidity="—",
            volume_grade="—",

            level_tf="—",

            reason="Восстановлено после перезапуска.",

            volume_24h=0.0,
            volume_ratio=0.0,

            atr_pct=0.0,

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
                db.execute(
                    """
                    SELECT status
                    FROM signals
                    WHERE signal_id = ?
                    """,
                    (signal_id,)
                ).fetchone()[0]
                == "ACTIVE"
            )
        )

        active_signals[
            inst_id
        ] = active

        restored += 1

    log.info(
        "Восстановлено активных сигналов: %s",
        restored
    )


# ============================================================
# COOLDOWN
# ============================================================

def can_create_signal(
    inst_id: str
) -> bool:

    if inst_id in active_signals:
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

        last = float(
            row[0]
        )

        if (
            now_ts() - last
            < COOLDOWN_MINUTES * 60
        ):
            return False

    cutoff = (
        now_ts() - 3600
    )

    signals_hour[:] = [
        ts
        for ts in signals_hour
        if ts >= cutoff
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
            "Недостаточно свечей для графика."
        )

    path = (
        f"/tmp/"
        f"{setup.coin}_"
        f"{int(time.time())}_"
        f"{setup.signal_id}.png"
    )

    fig, ax = plt.subplots(
        figsize=(12, 7),
        dpi=140
    )

    fig.patch.set_facecolor(
        "#090e1a"
    )

    ax.set_facecolor(
        "#090e1a"
    )

    width = 0.65

    for i, candle in enumerate(
        candles
    ):

        color = (
            "#16c784"
            if candle.close
            >= candle.open
            else
            "#ea3943"
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

    ax.axhline(
        setup.level,
        color="#f5c542",
        linewidth=2,
        linestyle="--"
    )

    ax.axhspan(
        setup.entry_low,
        setup.entry_high,
        color="#00aaff",
        alpha=0.10
    )

    ax.axhline(
        setup.sl,
        color="#ff3b30",
        linewidth=1.7,
        linestyle="-."
    )

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
            f"{setup.coin}USDT | "
            f"{direction_ru(setup.direction)}\n"
            f"{strategy_ru(setup.strategy)} | "
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
# TELEGRAM TEXT
# ============================================================

def build_signal_text(
    setup: Setup
) -> str:

    risk = (
        abs(
            setup.current_price
            - setup.sl
        )
        / setup.current_price
        * 100
    )

    return (

        f"🔥 *{setup.coin}USDT — "
        f"{direction_ru(setup.direction)}*\n\n"

        f"🟡 *ГОТОВ К ВХОДУ*\n"
        f"Сетап подтверждён. "
        f"Не догоняем цену.\n\n"

        f"🆔 *ID:* `{setup.signal_id}`\n\n"

        f"💰 *Текущая цена:*\n"
        f"`{fmt_price(setup.current_price)}`\n\n"

        f"🎯 *ЗОНА ВХОДА:*\n"
        f"`{fmt_price(setup.entry_low)}` — "
        f"`{fmt_price(setup.entry_high)}`\n\n"

        f"🛑 *СТОП:*\n"
        f"`{fmt_price(setup.sl)}`\n"
        f"Риск: `-{risk:.2f}%`\n\n"

        f"🎯 *TP1 — 30%*\n"
        f"`{fmt_price(setup.tp1)}`\n\n"

        f"🎯 *TP2 — 30%*\n"
        f"`{fmt_price(setup.tp2)}`\n\n"

        f"🏆 *TP3 — 40%*\n"
        f"`{fmt_price(setup.tp3)}`\n\n"

        f"🧠 *ПОЧЕМУ СИГНАЛ:*\n"
        f"{setup.reason}\n\n"

        f"📊 *СТРАТЕГИЯ:*\n"
        f"`{strategy_ru(setup.strategy)}`\n\n"

        f"📍 *УРОВЕНЬ:*\n"
        f"{setup.level_tf} — "
        f"`{fmt_price(setup.level)}`\n\n"

        f"📦 *ОБЪЁМ 5M:*\n"
        f"`{setup.volume_ratio:.2f}x`\n\n"

        f"💧 *ЛИКВИДНОСТЬ:*\n"
        f"`{setup.liquidity}`\n\n"

        f"⭐ *ОЦЕНКА СИГНАЛА:*\n"
        f"`{setup.score}/100` "
        f"{score_label(setup.score)}\n\n"

        f"⏱ Сетап действует "
        f"`{READY_TTL_MINUTES} мин`.\n\n"

        f"⚠️ *Не увеличиваем риск после убытка.*\n"
        f"*Качество важнее количества.*"
    )


# ============================================================
# TELEGRAM SEND
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
            f"🔥 *{setup.coin}USDT — "
            f"{direction_ru(setup.direction)}*\n"
            f"{score_label(setup.score)} · "
            f"{strategy_ru(setup.strategy)}"
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
# ACTIVE SIGNAL MANAGEMENT
# ============================================================

def send_entry_message(
    active: ActiveSignal,
    price: float
):

    setup = active.setup

    text = (

        f"🟢 *ВХОД АКТИВИРОВАН*\n\n"

        f"{setup.coin}USDT — "
        f"{direction_ru(setup.direction)}\n\n"

        f"🆔 `{setup.signal_id}`\n\n"

        f"Цена: `{fmt_price(price)}`\n"
        f"Уровень: `{fmt_price(setup.level)}`\n\n"

        f"🛑 Стоп: `{fmt_price(setup.sl)}`\n"
        f"🎯 TP1: `{fmt_price(setup.tp1)}`\n"
        f"🎯 TP2: `{fmt_price(setup.tp2)}`\n"
        f"🏆 TP3: `{fmt_price(setup.tp3)}`\n\n"

        f"Работаем по плану. "
        f"Не увеличиваем риск."
    )

    bot.send_message(
        CHANNEL_ID,
        text,
        parse_mode="Markdown"
    )


def send_tp_message(
    active: ActiveSignal,
    number: int,
    price: float
):

    setup = active.setup

    if number == 1:

        text = (

            f"🎯 *{setup.coin}USDT — TP1*\n\n"

            f"🆔 `{setup.signal_id}`\n\n"

            f"TP1 достигнут:\n"
            f"`{fmt_price(price)}`\n\n"

            f"Закрытие: *30%*\n"
            f"Стоп → *БЕЗУБЫТОК*\n\n"

            f"Оставляем сделку по плану."
        )

    elif number == 2:

        text = (

            f"🎯 *{setup.coin}USDT — TP2*\n\n"

            f"🆔 `{setup.signal_id}`\n\n"

            f"Цена: `{fmt_price(price)}`\n\n"

            f"Закрытие: *ещё 30%*\n\n"

            f"Оставшиеся 40% "
            f"держим к TP3."
        )

    else:

        text = (

            f"🏆 *{setup.coin}USDT — TP3*\n\n"

            f"🆔 `{setup.signal_id}`\n\n"

            f"TP3 достигнут:\n"
            f"`{fmt_price(price)}`\n\n"

            f"Сделка полностью завершена. "
            f"Результат зафиксирован."
        )

    bot.send_message(
        CHANNEL_ID,
        text,
        parse_mode="Markdown"
    )


def send_stop_message(
    active: ActiveSignal,
    price: float
):

    setup = active.setup

    # Упрощённый расчёт R.
    if setup.direction == "LONG":

        risk = (
            setup.entry_high
            - setup.sl
        )

        result = (
            price
            - setup.entry_high
        )

    else:

        risk = (
            setup.sl
            - setup.entry_low
        )

        result = (
            setup.entry_low
            - price
        )

    if risk > 0:

        result_r = (
            result / risk
        )

    else:

        result_r = -1.0

    text = (

        f"🛑 *{setup.coin}USDT — СТОП*\n\n"

        f"🆔 `{setup.signal_id}`\n\n"

        f"Цена выхода:\n"
        f"`{fmt_price(price)}`\n\n"

        f"Результат:\n"
        f"`{result_r:+.2f}R`\n\n"

        f"Сделка завершена.\n"
        f"*Убыток не увеличиваем.*"
    )

    bot.send_message(
        CHANNEL_ID,
        text,
        parse_mode="Markdown"
    )

    db.execute(
        """
        UPDATE signals
        SET
            status = 'SL',
            closed_at = ?,
            exit_price = ?,
            result_r = ?
        WHERE signal_id = ?
        """,
        (
            now_ts(),
            price,
            result_r,
            setup.signal_id
        )
    )

    db.commit()


def manage_active_signal(
    inst_id: str,
    price: float
):

    active = active_signals.get(
        inst_id
    )

    if not active:
        return

    setup = active.setup

    # После рестарта current_price может быть 0.
    setup.current_price = price

    # ========================================================
    # READY
    # ========================================================

    if not active.activated:

        if setup.direction == "LONG":

            if price >= setup.level:

                active.activated = True

                update_status(
                    setup.signal_id,
                    "ACTIVE"
                )

                db.execute(
                    """
                    UPDATE signals
                    SET activated_at = ?
                    WHERE signal_id = ?
                    """,
                    (
                        now_ts(),
                        setup.signal_id
                    )
                )

                db.commit()

                send_entry_message(
                    active,
                    price
                )

        else:

            if price <= setup.level:

                active.activated = True

                update_status(
                    setup.signal_id,
                    "ACTIVE"
                )

                db.execute(
                    """
                    UPDATE signals
                    SET activated_at = ?
                    WHERE signal_id = ?
                    """,
                    (
                        now_ts(),
                        setup.signal_id
                    )
                )

                db.commit()

                send_entry_message(
                    active,
                    price
                )

        return

    # ========================================================
    # ACTIVE -> STOP
    # ========================================================

    if setup.direction == "LONG":

        if price <= setup.sl:

            send_stop_message(
                active,
                price
            )

            active_signals.pop(
                inst_id,
                None
            )

            return

    else:

        if price >= setup.sl:

            send_stop_message(
                active,
                price
            )

            active_signals.pop(
                inst_id,
                None
            )

            return

    # ========================================================
    # TP1
    # ========================================================

    if not active.tp1_hit:

        hit = (
            price >= setup.tp1
            if setup.direction == "LONG"
            else price <= setup.tp1
        )

        if hit:

            active.tp1_hit = True

            send_tp_message(
                active,
                1,
                price
            )

            db.execute(
                """
                UPDATE signals
                SET tp1_hit_at = ?
                WHERE signal_id = ?
                """,
                (
                    now_ts(),
                    setup.signal_id
                )
            )

            db.commit()

            # Стоп переводим в безубыток.
            if setup.direction == "LONG":

                setup.sl = (
                    setup.entry_high
                )

            else:

                setup.sl = (
                    setup.entry_low
                )

    # ========================================================
    # TP2
    # ========================================================

    if (
        active.tp1_hit
        and not active.tp2_hit
    ):

        hit = (
            price >= setup.tp2
            if setup.direction == "LONG"
            else price <= setup.tp2
        )

        if hit:

            active.tp2_hit = True

            send_tp_message(
                active,
                2,
                price
            )

            db.execute(
                """
                UPDATE signals
                SET tp2_hit_at = ?
                WHERE signal_id = ?
                """,
                (
                    now_ts(),
                    setup.signal_id
                )
            )

            db.commit()

    # ========================================================
    # TP3
    # ========================================================

    if (
        active.tp2_hit
        and not active.tp3_hit
    ):

        hit = (
            price >= setup.tp3
            if setup.direction == "LONG"
            else price <= setup.tp3
        )

        if hit:

            active.tp3_hit = True

            send_tp_message(
                active,
                3,
                price
            )

            db.execute(
                """
                UPDATE signals
                SET
                    status = 'TP3',
                    tp3_hit_at = ?,
                    closed_at = ?,
                    exit_price = ?,
                    result_r = 3.0
                WHERE signal_id = ?
                """,
                (
                    now_ts(),
                    now_ts(),
                    price,
                    setup.signal_id
                )
            )

            db.commit()

            active_signals.pop(
                inst_id,
                None
            )


# ============================================================
# EXPIRE READY
# ============================================================

def expire_ready():

    current = now_ts()

    for inst_id, active in list(
        active_signals.items()
    ):

        if active.activated:
            continue

        if current < active.expires_at:
            continue

        signal_id = (
            active.setup.signal_id
        )

        update_status(
            signal_id,
            "EXPIRED"
        )

        try:

            bot.send_message(
                CHANNEL_ID,
                (
                    f"🔴 *СЕТАП ОТМЕНЁН*\n\n"
                    f"{active.setup.coin}USDT — "
                    f"{direction_ru(active.setup.direction)}\n\n"
                    f"🆔 `{signal_id}`\n\n"
                    f"Цена не дала своевременного "
                    f"входа.\n\n"
                    f"*Рынок не догоняем.*"
                ),
                parse_mode="Markdown"
            )

        except Exception:

            log.exception(
                "Ошибка сообщения EXPIRATION"
            )

        active_signals.pop(
            inst_id,
            None
        )


# ============================================================
# MORNING MESSAGE
# ============================================================

def morning_message():

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

    text = (

        "🌅 *ДОБРОЕ УТРО, РЕБЯТА!*\n\n"

        "QUANTUM SCALPER начинает новый "
        "торговый день.\n\n"

        "🎯 Ждём только подтверждённые сетапы.\n"
        "🚫 Не догоняем рынок.\n"
        "🛑 Не увеличиваем риск после убытка.\n"
        "💰 Не используем весь депозит в одной сделке.\n"
        "⏳ Если хорошего входа нет — ждём.\n\n"

        "*Качество важнее количества.*\n\n"

        "Удачного торгового дня! 🚀"
    )

    try:

        bot.send_message(
            CHANNEL_ID,
            text,
            parse_mode="Markdown"
        )

        last_morning_date = today

    except Exception:

        log.exception(
            "Ошибка утреннего сообщения"
        )


# ============================================================
# STARTUP
# ============================================================

def startup():

    text = (

        "🚀 *QUANTUM SCALPER V2 ONLINE*\n\n"

        "OKX: 🟢\n"
        "Telegram: 🟢\n"
        "Сканер: 🟢\n"
        "Режим: *АНАЛИЗ*\n\n"

        "🧠 Модели:\n"
        "• Пробой уровня\n"
        "• Сжатие → пробой\n"
        "• Импульсный пробой\n\n"

        f"⭐ Минимальная оценка: "
        f"`{MIN_SCORE}/100`\n"

        f"⏱ Время сетапа: "
        f"`{READY_TTL_MINUTES} мин`\n"

        f"🔒 Пауза пары: "
        f"`{COOLDOWN_MINUTES} мин`\n\n"

        "*Бот не торгует. "
        "Он только анализирует рынок.*"
    )

    try:

        bot.send_message(
            CHANNEL_ID,
            text,
            parse_mode="Markdown"
        )

    except Exception:

        log.exception(
            "Startup Telegram error"
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
        "========== СКАН #%s ==========",
        scan_count
    )

    tickers = get_tickers()

    liquid = [

        (inst_id, data)

        for inst_id, data
        in tickers.items()

        if (
            data["volume_24h"]
            >= MIN_24H_VOLUME_USD
        )
    ]

    liquid.sort(
        key=lambda item:
            item[1]["volume_24h"],
        reverse=True
    )

    selected = liquid[
        :MAX_SYMBOLS
    ]

    log.info(
        "Тикеры=%s | "
        "Ликвидных=%s | "
        "Анализ=%s",
        len(tickers),
        len(liquid),
        len(selected)
    )

    for inst_id, ticker in selected:

        try:

            # ------------------------------------------------
            # Сначала сопровождаем уже существующий сигнал.
            # ------------------------------------------------

            manage_active_signal(
                inst_id,
                ticker["last"]
            )

            # ------------------------------------------------
            # Если уже есть сигнал — новый не создаём.
            # ------------------------------------------------

            if inst_id in active_signals:
                continue

            if not can_create_signal(
                inst_id
            ):
                continue

            # ------------------------------------------------
            # Получаем свечи.
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

            if not setup:
                continue

            log.info(
                "КАНДИДАТ | %s | %s | "
                "%s | score=%s",
                setup.coin,
                setup.direction,
                setup.strategy,
                setup.score
            )

            # ------------------------------------------------
            # Публикация.
            # ------------------------------------------------

            photo_id, text_id = (
                publish_signal(
                    setup
                )
            )

            if not text_id:

                log.warning(
                    "Сигнал не сохранён: "
                    "Telegram не ответил."
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
                inst_id
            ] = active

            save_signal(
                setup,
                active
            )

            signals_hour.append(
                created
            )

            signals_today += 1

            log.info(
                "READY СОЗДАН | %s | %s",
                setup.signal_id,
                setup.score
            )

            time.sleep(1)

        except Exception as exc:

            log.exception(
                "Ошибка пары %s: %s",
                inst_id,
                exc
            )

            # Одна плохая монета
            # не должна ломать весь сканер.

            continue


# ============================================================
# DAILY COUNTER
# ============================================================

def reset_daily_counter():

    global signals_today

    today = str(
        local_now().date()
    )

    row = db.execute(
        """
        SELECT value
        FROM bot_state
        WHERE key = 'counter_date'
        """
    ).fetchone()

    if not row:

        db.execute(
            """
            INSERT INTO bot_state
            (key, value)
            VALUES (?, ?)
            """,
            (
                "counter_date",
                today
            )
        )

        db.commit()

        signals_today = 0

        return

    if row[0] != today:

        db.execute(
            """
            UPDATE bot_state
            SET value = ?
            WHERE key = ?
            """,
            (
                today,
                "counter_date"
            )
        )

        db.commit()

        signals_today = 0


# ============================================================
# STATUS
# ============================================================

def send_status():

    try:

        bot.send_message(
            CHANNEL_ID,
            (
                "🟢 *QUANTUM STATUS*\n\n"

                "OKX: 🟢 ONLINE\n"
                "Telegram: 🟢 ONLINE\n"
                "Сканер: 🟢 RUNNING\n\n"

                f"Активных сетапов: "
                f"`{len(active_signals)}`\n"

                f"Сигналов сегодня: "
                f"`{signals_today}`\n"

                f"Сканов: "
                f"`{scan_count}`"
            ),
            parse_mode="Markdown"
        )

    except Exception:

        log.exception(
            "Ошибка STATUS"
        )


# ============================================================
# MAIN
# ============================================================

def main():

    log.info(
        "======================================"
    )

    log.info(
        "QUANTUM SCALPER V2 START"
    )

    log.info(
        "Timezone: %s",
        TIMEZONE
    )

    log.info(
        "Symbols: %s",
        MAX_SYMBOLS
    )

    log.info(
        "Min score: %s",
        MIN_SCORE
    )

    log.info(
        "Min volume: $%s",
        f"{MIN_24H_VOLUME_USD:,.0f}"
    )

    log.info(
        "======================================"
    )

    # --------------------------------------------------------
    # Восстановление состояния.
    # --------------------------------------------------------

    restore_active_signals()

    # --------------------------------------------------------
    # Telegram.
    # --------------------------------------------------------

    startup()

    # --------------------------------------------------------
    # OKX test.
    # --------------------------------------------------------

    try:

        tickers = get_tickers()

        log.info(
            "OKX ONLINE | тикеров: %s",
            len(tickers)
        )

    except Exception:

        log.exception(
            "OKX не отвечает при запуске"
        )

    # --------------------------------------------------------
    # MAIN LOOP.
    # --------------------------------------------------------

    while True:

        try:

            reset_daily_counter()

            morning_message()

            expire_ready()

            scan_market()

            log.info(
                "Скан завершён. "
                "Следующий через %s сек.",
                SCAN_INTERVAL_SECONDS
            )

            time.sleep(
                SCAN_INTERVAL_SECONDS
            )

        except KeyboardInterrupt:

            log.info(
                "Остановка."
            )

            break

        except Exception as exc:

            log.exception(
                "КРИТИЧЕСКАЯ ОШИБКА: %s",
                exc
            )

            log.warning(
                "Перезапуск цикла через 15 секунд."
            )

            time.sleep(15)


# ============================================================
# ENTRY
# ============================================================

if __name__ == "__main__":

    main()
