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
# QUANTUM SCALPER V4
#
# OKX PUBLIC MARKET DATA
#
# FULL MARKET SCANNER
# -> LIQUIDITY
# -> MULTI TIMEFRAME LEVEL ENGINE
# -> LEVEL CLUSTERS
# -> 1D / 4H / 1H STRUCTURE
# -> 30M / 15M SETUP
# -> 5M TRIGGER
# -> VOLUME
# -> ATR
# -> OPEN INTEREST
# -> EARLY READY
# -> ACTIVE RECHECK
# -> TELEGRAM
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

# Минимальный 24H оборот.
MIN_24H_VOLUME_USD = float(
    os.getenv(
        "MIN_24H_VOLUME_USD",
        "30000000"
    )
)

# V4 больше не анализирует только первые 35.
# Это быстрый лимит кандидатов после первичного screening.
MAX_CANDIDATES = int(
    os.getenv(
        "MAX_CANDIDATES",
        "80"
    )
)

# Сколько монет реально глубоко анализировать за цикл.
MAX_DEEP_ANALYSIS = int(
    os.getenv(
        "MAX_DEEP_ANALYSIS",
        "45"
    )
)


# ============================================================
# SCORING
# ============================================================

MIN_SCORE = int(
    os.getenv(
        "MIN_SCORE",
        "78"
    )
)

PREMIUM_SCORE = int(
    os.getenv(
        "PREMIUM_SCORE",
        "88"
    )
)


# ============================================================
# SIGNAL CONTROL
# ============================================================

COOLDOWN_MINUTES = int(
    os.getenv(
        "COOLDOWN_MINUTES",
        "60"
    )
)

READY_TTL_MINUTES = int(
    os.getenv(
        "READY_TTL_MINUTES",
        "15"
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
        "8"
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
# LEVEL ENGINE
# ============================================================

# Расстояние, в пределах которого два уровня считаются
# частью одной зоны.
LEVEL_CLUSTER_PCT = float(
    os.getenv(
        "LEVEL_CLUSTER_PCT",
        "0.18"
    )
)

# Максимальное расстояние текущей цены до интересующего
# уровня для формирования WATCH/READY.
MAX_LEVEL_DISTANCE_PCT = float(
    os.getenv(
        "MAX_LEVEL_DISTANCE_PCT",
        "1.20"
    )
)

# Сильный кластер получает дополнительный вес.
STRONG_CLUSTER_SCORE = int(
    os.getenv(
        "STRONG_CLUSTER_SCORE",
        "8"
    )
)


# ============================================================
# VOLATILITY
# ============================================================

MIN_ATR_PCT = float(
    os.getenv(
        "MIN_ATR_PCT",
        "0.025"
    )
)

MAX_ATR_PCT = float(
    os.getenv(
        "MAX_ATR_PCT",
        "3.00"
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
    format="%(asctime)s | %(levelname)s | %(message)s",
)

log = logging.getLogger(
    "QUANTUM_V4"
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
    touches: int
    age_bars: int


@dataclass
class LevelCluster:
    price: float
    levels: List[Level]
    strength: int
    timeframes: List[str]
    touches: int
    distance_pct: float


@dataclass
class Setup:
    inst_id: str
    coin: str

    direction: str
    strategy: str

    level: float
    level_tf: str
    level_strength: int

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
    volume_acceleration: float

    atr_pct: float

    structure_1d: str
    structure_4h: str
    structure_1h: str

    level_timeframes: str

    state_hint: str

    candles_5m: List[Candle]


@dataclass
class ActiveReady:
    setup: Setup
    created_at: float
    expires_at: float

    telegram_message_id: Optional[int] = None
    photo_message_id: Optional[int] = None


# ============================================================
# MEMORY
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


def fmt_price(
    price: float
) -> str:

    if price >= 1000:
        return f"{price:,.2f}".replace(",", " ")

    if price >= 100:
        return f"{price:,.2f}".replace(",", " ")

    if price >= 1:
        return f"{price:,.4f}".replace(",", " ")

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
        return "🟢 HIGH QUALITY"

    return "⚡ SIGNAL"


def grade_volume(
    ratio: float
) -> str:

    if ratio >= 2.50:
        return "EXTREME"

    if ratio >= 2.00:
        return "VERY HIGH"

    if ratio >= 1.50:
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
                        attempt * 2,
                        6
                    )
                )

    raise RuntimeError(
        f"OKX request failed: {last_error}"
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

            # В актуальном OKX API volCcy24h для market ticker
            # используется как объём в котируемой валюте.
            volume_usd = float(
                item.get(
                    "volCcy24h",
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

            bid = float(
                item.get(
                    "bidPx",
                    0
                ) or 0
            )

            ask = float(
                item.get(
                    "askPx",
                    0
                ) or 0
            )

            result[inst_id] = {
                "last": last,
                "high24h": high24h,
                "low24h": low24h,
                "vol24h_usd": volume_usd,
                "bid": bid,
                "ask": ask,
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
# OI
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

        log.debug(
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

        tr = max(
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

        trs.append(tr)

    if len(trs) < period:
        return 0.0

    return (
        sum(trs[-period:])
        / period
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

    return (
        current
        / average
    )


def volume_acceleration(
    candles: List[Candle]
) -> float:

    if len(candles) < 30:
        return 1.0

    recent = [
        c.quote_volume
        for c in candles[-5:]
        if c.quote_volume > 0
    ]

    previous = [
        c.quote_volume
        for c in candles[-25:-5]
        if c.quote_volume > 0
    ]

    if not recent or not previous:
        return 1.0

    recent_avg = (
        sum(recent)
        / len(recent)
    )

    previous_avg = (
        sum(previous)
        / len(previous)
    )

    if previous_avg <= 0:
        return 1.0

    return (
        recent_avg
        / previous_avg
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

    if len(candles) <= left + right:
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

    if len(candles) <= left + right:
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
# STRUCTURE
# ============================================================

def market_structure(
    candles: List[Candle]
) -> str:

    if len(candles) < 50:
        return "NEUTRAL"

    confirmed = [
        c
        for c in candles
        if c.confirmed
    ]

    if len(confirmed) < 50:
        return "NEUTRAL"

    closes = [
        c.close
        for c in confirmed
    ]

    ema20 = ema(
        closes,
        20
    )[-1]

    ema50 = ema(
        closes,
        50
    )[-1]

    recent = confirmed[-16:]

    first = recent[:8]
    second = recent[8:]

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
# LEVEL CREATION
# ============================================================

TIMEFRAME_WEIGHTS = {
    "1D": 30,
    "4H": 26,
    "1H": 21,
    "30M": 16,
    "15M": 12,
    "5M": 8,
}


def make_levels(
    candles: List[Candle],
    timeframe: str
) -> List[Level]:

    levels = []

    if len(candles) < 30:
        return levels

    confirmed = [
        c
        for c in candles
        if c.confirmed
    ]

    if len(confirmed) < 30:
        return levels

    weight = TIMEFRAME_WEIGHTS.get(
        timeframe,
        5
    )

    highs = pivot_highs(
        confirmed,
        2,
        2
    )

    lows = pivot_lows(
        confirmed,
        2,
        2
    )

    total = len(confirmed)

    for index, price in highs[-20:]:

        age = (
            total
            - 1
            - index
        )

        touches = 1

        tolerance = (
            price
            * 0.0015
        )

        for candle in confirmed[
            max(0, index - 20):
            min(total, index + 21)
        ]:

            if (
                abs(
                    candle.high
                    - price
                )
                <= tolerance
            ):
                touches += 1

        freshness_bonus = max(
            0,
            6 - age // 20
        )

        strength = (
            weight
            + min(
                touches * 2,
                10
            )
            + freshness_bonus
        )

        levels.append(
            Level(
                price=price,
                timeframe=timeframe,
                strength=strength,
                touches=touches,
                age_bars=age
            )
        )

    for index, price in lows[-20:]:

        age = (
            total
            - 1
            - index
        )

        touches = 1

        tolerance = (
            price
            * 0.0015
        )

        for candle in confirmed[
            max(0, index - 20):
            min(total, index + 21)
        ]:

            if (
                abs(
                    candle.low
                    - price
                )
                <= tolerance
            ):
                touches += 1

        freshness_bonus = max(
            0,
            6 - age // 20
        )

        strength = (
            weight
            + min(
                touches * 2,
                10
            )
            + freshness_bonus
        )

        levels.append(
            Level(
                price=price,
                timeframe=timeframe,
                strength=strength,
                touches=touches,
                age_bars=age
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

    if not levels or current <= 0:
        return []

    levels = sorted(
        levels,
        key=lambda x: x.price
    )

    clusters: List[
        List[Level]
    ] = []

    for level in levels:

        added = False

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

            if distance <= LEVEL_CLUSTER_PCT:

                cluster.append(
                    level
                )

                added = True
                break

        if not added:

            clusters.append(
                [level]
            )

    result = []

    for cluster in clusters:

        total_strength = sum(
            x.strength
            for x in cluster
        )

        weighted_price = (
            sum(
                x.price * x.strength
                for x in cluster
            )
            / max(
                total_strength,
                1
            )
        )

        timeframes = sorted(
            set(
                x.timeframe
                for x in cluster
            ),
            key=lambda tf:
                TIMEFRAME_WEIGHTS.get(
                    tf,
                    0
                ),
            reverse=True
        )

        touches = sum(
            x.touches
            for x in cluster
        )

        distance_pct = abs(
            pct(
                current,
                weighted_price
            )
        )

        result.append(
            LevelCluster(
                price=weighted_price,
                levels=cluster,
                strength=min(
                    total_strength,
                    100
                ),
                timeframes=timeframes,
                touches=touches,
                distance_pct=distance_pct
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
# FIND BEST LEVEL
# ============================================================

def find_best_level(
    clusters: List[LevelCluster],
    current: float,
    direction: str
) -> Optional[
    LevelCluster
]:

    candidates = []

    for cluster in clusters:

        if cluster.distance_pct > (
            MAX_LEVEL_DISTANCE_PCT
        ):
            continue

        if direction == "LONG":

            # Для LONG ищем сопротивление выше цены.
            if cluster.price <= current:
                continue

        else:

            # Для SHORT ищем поддержку ниже цены.
            if cluster.price >= current:
                continue

        candidates.append(
            cluster
        )

    if not candidates:
        return None

    # Приоритет:
    # 1. сильный уровень
    # 2. близкий уровень
    candidates.sort(
        key=lambda x: (
            -x.strength,
            x.distance_pct
        )
    )

    return candidates[0]


# ============================================================
# BREAKOUT / APPROACH
# ============================================================

def level_state(
    current: float,
    level: float,
    direction: str
) -> str:

    distance = abs(
        pct(
            current,
            level
        )
    )

    if distance <= 0.12:
        return "AT_LEVEL"

    if distance <= 0.35:
        return "NEAR_LEVEL"

    if distance <= 0.75:
        return "APPROACHING"

    return "FAR"


# ============================================================
# COMPRESSION
# ============================================================

def compression_score(
    candles: List[Candle],
    direction: str
) -> Tuple[int, bool]:

    if len(candles) < 25:
        return 0, False

    recent = [
        c
        for c in candles[-24:]
        if c.confirmed
    ]

    if len(recent) < 18:
        return 0, False

    ranges = [
        c.high - c.low
        for c in recent
        if c.high > c.low
    ]

    if len(ranges) < 15:
        return 0, False

    first = ranges[:8]
    last = ranges[-8:]

    first_avg = (
        sum(first)
        / len(first)
    )

    last_avg = (
        sum(last)
        / len(last)
    )

    if first_avg <= 0:
        return 0, False

    compression = (
        1
        - last_avg
        / first_avg
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

            values = [
                x
                for _, x
                in lows[-3:]
            ]

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

        if len(highs) >= 2:

            values = [
                x
                for _, x
                in highs[-3:]
            ]

            if all(
                values[i]
                >= values[i + 1]
                for i in range(
                    len(values) - 1
                )
            ):

                score += 12
                valid = True

    if compression >= 0.10:
        score += 5

    if compression >= 0.18:
        score += 5

    if compression >= 0.25:
        score += 5

    return min(
        score,
        25
    ), valid


# ============================================================
# MOMENTUM
# ============================================================

def momentum_score(
    candles: List[Candle],
    direction: str
) -> Tuple[int, bool]:

    if len(candles) < 35:
        return 0, False

    confirmed = [
        c
        for c in candles
        if c.confirmed
    ]

    if len(confirmed) < 30:
        return 0, False

    closes = [
        c.close
        for c in confirmed
    ]

    e9 = ema(
        closes,
        9
    )[-1]

    e21 = ema(
        closes,
        21
    )[-1]

    current = confirmed[-1]

    previous = confirmed[
        -13:-1
    ]

    if len(previous) < 8:
        return 0, False

    score = 0

    if direction == "LONG":

        high = max(
            c.high
            for c in previous
        )

        if e9 > e21:
            score += 5

        if current.close > high:
            score += 12

        if (
            current.close > current.open
            and current.close
            >= current.high
            - (
                current.high
                - current.low
            ) * 0.25
        ):
            score += 5

    else:

        low = min(
            c.low
            for c in previous
        )

        if e9 < e21:
            score += 5

        if current.close < low:
            score += 12

        if (
            current.close < current.open
            and current.close
            <= current.low
            + (
                current.high
                - current.low
            ) * 0.25
        ):
            score += 5

    return min(
        score,
        22
    ), score >= 15


# ============================================================
# VOLUME SIGNAL
# ============================================================

def volume_signal(
    candles: List[Candle]
) -> Tuple[
    int,
    float,
    float
]:

    ratio = volume_ratio(
        candles,
        20
    )

    acceleration = volume_acceleration(
        candles
    )

    score = 0

    if ratio >= 1.15:
        score += 4

    if ratio >= 1.25:
        score += 5

    if ratio >= 1.50:
        score += 5

    if ratio >= 2.00:
        score += 5

    if acceleration >= 1.15:
        score += 3

    if acceleration >= 1.35:
        score += 3

    return (
        min(score, 25),
        ratio,
        acceleration
    )


# ============================================================
# DETECT SETUP
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
    )

    if current <= 0:
        return None

    c1d = candles["1D"]
    c4h = candles["4H"]
    c1h = candles["1H"]
    c30 = candles["30m"]
    c15 = candles["15m"]
    c5 = candles["5m"]

    if len(c1h) < 60:
        return None

    if len(c15) < 60:
        return None

    if len(c5) < 50:
        return None

    s1d = market_structure(
        c1d
    )

    s4h = market_structure(
        c4h
    )

    s1h = market_structure(
        c1h
    )

    # --------------------------------------------------------
    # DETERMINE DIRECTION
    # --------------------------------------------------------

    long_votes = 0
    short_votes = 0

    for structure in (
        s1d,
        s4h,
        s1h
    ):

        if structure == "LONG":
            long_votes += 1

        elif structure == "SHORT":
            short_votes += 1

    if long_votes > short_votes:
        direction = "LONG"

    elif short_votes > long_votes:
        direction = "SHORT"

    else:
        return None

    # --------------------------------------------------------
    # LEVEL ENGINE
    # --------------------------------------------------------

    all_levels = []

    for tf, data in (
        ("1D", c1d),
        ("4H", c4h),
        ("1H", c1h),
        ("30M", c30),
        ("15M", c15),
        ("5M", c5)
    ):

        all_levels.extend(
            make_levels(
                data,
                tf
            )
        )

    if not all_levels:
        return None

    clusters = cluster_levels(
        all_levels,
        current
    )

    level_cluster = find_best_level(
        clusters,
        current,
        direction
    )

    if level_cluster is None:
        return None

    level = level_cluster.price

    distance = abs(
        pct(
            current,
            level
        )
    )

    if distance > MAX_LEVEL_DISTANCE_PCT:
        return None

    state = level_state(
        current,
        level,
        direction
    )

    # --------------------------------------------------------
    # ATR
    # --------------------------------------------------------

    confirmed_5m = [
        c
        for c in c5
        if c.confirmed
    ]

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

    if atr_pct < MIN_ATR_PCT:
        return None

    if atr_pct > MAX_ATR_PCT:
        return None

    # --------------------------------------------------------
    # VOLUME
    # --------------------------------------------------------

    volume_points, v_ratio, v_accel = (
        volume_signal(
            confirmed_5m
        )
    )

    # --------------------------------------------------------
    # SCORE
    # --------------------------------------------------------

    score = 0

    reasons = []

    # --------------------------------------------------------
    # HIGHER TIMEFRAME STRUCTURE
    # --------------------------------------------------------

    if s1d == direction:
        score += 10
        reasons.append(
            f"1D structure {direction}"
        )

    if s4h == direction:
        score += 13
        reasons.append(
            f"4H structure {direction}"
        )

    if s1h == direction:
        score += 15
        reasons.append(
            f"1H structure {direction}"
        )

    # --------------------------------------------------------
    # LEVEL STRENGTH
    # --------------------------------------------------------

    level_score = int(
        clamp(
            level_cluster.strength * 0.35,
            8,
            22
        )
    )

    score += level_score

    if len(
        level_cluster.timeframes
    ) >= 2:

        score += 5

        reasons.append(
            "Multi-timeframe level cluster"
        )

    if len(
        level_cluster.timeframes
    ) >= 3:

        score += 5

        reasons.append(
            "Strong multi-timeframe confluence"
        )

    if level_cluster.touches >= 4:

        score += 4

        reasons.append(
            "Multiple level reactions"
        )

    # --------------------------------------------------------
    # DISTANCE
    # --------------------------------------------------------

    if distance <= 0.15:
        score += 8
        reasons.append(
            "Price at key level"
        )

    elif distance <= 0.30:
        score += 7
        reasons.append(
            "Price very close to key level"
        )

    elif distance <= 0.60:
        score += 5

    elif distance <= 1.00:
        score += 2

    # --------------------------------------------------------
    # COMPRESSION
    # --------------------------------------------------------

    compression_points, compression_ok = (
        compression_score(
            c15,
            direction
        )
    )

    score += compression_points

    if compression_ok:

        reasons.append(
            "15M compression"
        )

    # --------------------------------------------------------
    # MOMENTUM
    # --------------------------------------------------------

    momentum_points, momentum_ok = (
        momentum_score(
            confirmed_5m,
            direction
        )
    )

    score += momentum_points

    if momentum_ok:

        reasons.append(
            "5M momentum"
        )

    # --------------------------------------------------------
    # VOLUME
    # --------------------------------------------------------

    score += volume_points

    if v_ratio >= 1.25:

        reasons.append(
            f"Volume {v_ratio:.2f}x"
        )

    if v_accel >= 1.25:

        reasons.append(
            f"Volume acceleration {v_accel:.2f}x"
        )

    # --------------------------------------------------------
    # LIQUIDITY
    # --------------------------------------------------------

    volume_24h = float(
        ticker.get(
            "vol24h_usd",
            0
        )
    )

    if volume_24h < MIN_24H_VOLUME_USD:
        return None

    liquidity = grade_liquidity(
        volume_24h
    )

    if volume_24h >= 1_000_000_000:

        score += 5

    elif volume_24h >= 250_000_000:

        score += 3

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
    # PRE-TRIGGER / EARLY READY
    # --------------------------------------------------------

    # Мы не требуем обязательного уже случившегося пробоя.
    #
    # Если цена рядом с сильным кластером и есть
    # структура/compression/volume — можно дать READY заранее.

    if (
        state == "FAR"
        and not momentum_ok
        and not compression_ok
    ):
        return None

    # --------------------------------------------------------
    # STRATEGY
    # --------------------------------------------------------

    if momentum_ok and distance <= 0.35:

        strategy = (
            "Momentum Level Breakout"
        )

    elif compression_ok:

        strategy = (
            "Compression Level Breakout"
        )

    elif len(
        level_cluster.timeframes
    ) >= 3:

        strategy = (
            "Multi-Timeframe Level Breakout"
        )

    else:

        strategy = (
            "Horizontal Level Breakout"
        )

    # --------------------------------------------------------
    # CHASE PROTECTION
    # --------------------------------------------------------

    if direction == "LONG":

        if current > level * (
            1
            + MAX_CHASE_PCT / 100
        ):

            return None

    else:

        if current < level * (
            1
            - MAX_CHASE_PCT / 100
        ):

            return None

    # --------------------------------------------------------
    # ENTRY ZONE
    # --------------------------------------------------------

    zone_pct = clamp(
        atr_pct * 0.40,
        0.08,
        0.30
    )

    entry_low = (
        level
        * (
            1
            - zone_pct / 100
        )
    )

    entry_high = (
        level
        * (
            1
            + zone_pct / 100
        )
    )

    # --------------------------------------------------------
    # STRUCTURAL STOP
    # --------------------------------------------------------

    recent_15 = [
        c
        for c in c15[-24:]
        if c.confirmed
    ]

    if len(recent_15) < 12:
        return None

    if direction == "LONG":

        structural_low = min(
            c.low
            for c in recent_15
        )

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
        * 100
    )

    if risk_pct < 0.15:
        return None

    if risk_pct > 2.20:
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

    reason = (
        "; ".join(
            reasons[:8]
        )
        if reasons
        else
        "Multi-factor market setup."
    )

    return Setup(
        inst_id=inst_id,
        coin=get_coin(inst_id),
        direction=direction,
        strategy=strategy,
        level=level,
        level_tf=(
            level_cluster.timeframes[0]
            if level_cluster.timeframes
            else "MULTI"
        ),
        level_strength=level_cluster.strength,
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
        reason=reason,
        volume_24h=volume_24h,
        breakout_volume_ratio=v_ratio,
        volume_acceleration=v_accel,
        atr_pct=atr_pct,
        structure_1d=s1d,
        structure_4h=s4h,
        structure_1h=s1h,
        level_timeframes=(
            " / ".join(
                level_cluster.timeframes
            )
        ),
        state_hint=state,
        candles_5m=confirmed_5m[-80:]
    )


# ============================================================
# FAST MARKET SCREEN
# ============================================================

def fast_screen(
    tickers: Dict[str, dict]
) -> List[Tuple[str, dict]]:

    candidates = []

    for inst_id, ticker in tickers.items():

        volume = float(
            ticker.get(
                "vol24h_usd",
                0
            )
        )

        if volume < MIN_24H_VOLUME_USD:
            continue

        last = float(
            ticker.get(
                "last",
                0
            )
        )

        high24 = float(
            ticker.get(
                "high24h",
                0
            )
        )

        low24 = float(
            ticker.get(
                "low24h",
                0
            )
        )

        if last <= 0:
            continue

        # 24H range.
        if low24 > 0:

            range_pct = (
                high24
                - low24
            ) / low24 * 100

        else:

            range_pct = 0

        # Чем больше ликвидность и разумная волатильность,
        # тем выше приоритет глубокого анализа.
        priority = (
            min(
                volume / 100_000_000,
                15
            )
            + min(
                range_pct * 2,
                15
            )
        )

        candidates.append(
            (
                priority,
                inst_id,
                ticker
            )
        )

    candidates.sort(
        key=lambda x: x[0],
        reverse=True
    )

    return [
        (
            inst_id,
            ticker
        )
        for _, inst_id, ticker
        in candidates[:MAX_CANDIDATES]
    ]


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
            current - last_time
            < COOLDOWN_MINUTES * 60
        ):
            return False

    cutoff = current - 3600

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

    candles = setup.candles_5m[-70:]

    if len(candles) < 10:
        raise RuntimeError(
            "Not enough candles for chart."
        )

    safe_coin = (
        setup.coin
        .replace("/", "_")
        .replace("\\", "_")
    )

    path = (
        f"/tmp/quantum_v4_"
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

    for i, candle in enumerate(candles):

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

        ax.add_patch(rect)

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

    labels = [
        (
            setup.level,
            " LEVEL",
            "#f5c542"
        ),
        (
            setup.sl,
            " SL",
            "#ff3b30"
        ),
        (
            setup.tp1,
            " TP1",
            "#ffd166"
        ),
        (
            setup.tp2,
            " TP2",
            "#ffd166"
        ),
        (
            setup.tp3,
            " TP3",
            "#ffd166"
        ),
    ]

    for price, text, color in labels:

        ax.text(
            last_x,
            price,
            text,
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
            f"Score {setup.score}/100 | "
            f"Level {setup.level_tf}"
        ),
        color="white",
        fontsize=14,
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

    plt.close(fig)

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

        state_line = (
            "🟡 *SETUP READY*\n"
            "Цена находится в рабочей зоне. "
            "Ждём подтверждение и не догоняем рынок."
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
        f"`{setup.breakout_volume_ratio:.2f}x`\n"

        f"⚡ *Volume acceleration:* "
        f"`{setup.volume_acceleration:.2f}x`\n\n"

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

        f"📍 *Основной уровень:*\n"
        f"`{setup.level_tf}` — "
        f"`{fmt_price(setup.level)}`\n\n"

        f"🧲 *LEVEL CLUSTER:*\n"
        f"`{setup.level_timeframes}`\n"
        f"Strength: `{setup.level_strength}/100`\n\n"

        f"🧭 *СТРУКТУРА*\n"
        f"1D: `{setup.structure_1d}`\n"
        f"4H: `{setup.structure_4h}`\n"
        f"1H: `{setup.structure_1h}`\n\n"

        f"📐 *ATR:* "
        f"`{setup.atr_pct:.2f}%`\n\n"

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
# SEND SIGNAL
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

    message = (
        "🌅 *ДОБРОЕ УТРО, РЕБЯТА!*\n\n"

        "Начинаем новый торговый день.\n"
        "Quantum снова сканирует рынок.\n\n"

        "🎯 Ищем сильные уровни.\n"
        "🧲 Проверяем зоны на нескольких ТФ.\n"
        "📊 Следим за объёмом.\n"
        "⚡ Проверяем импульс.\n"
        "🚫 Не догоняем движение.\n"
        "🛑 Не увеличиваем риск.\n\n"

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
# SAVE SIGNAL
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
# ACTIVE
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
                + MAX_CHASE_PCT / 100
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
                - MAX_CHASE_PCT / 100
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

    # --------------------------------------------------------
    # SECOND PRICE CHECK
    # --------------------------------------------------------

    distance_from_level = abs(
        pct(
            current_price,
            setup.level
        )
    )

    if distance_from_level > (
        MAX_CHASE_PCT
    ):

        ready_setups.pop(
            inst_id,
            None
        )

        log.info(
            "ACTIVE BLOCKED CHASE | %s",
            inst_id
        )

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
                f"`{fmt_price(setup.tp3)}`\n\n"

                f"⭐ Score: "
                f"`{setup.score}/100`"
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
# STARTUP
# ============================================================

def startup_message():

    message = (
        "🚀 *QUANTUM SCALPER V4 ONLINE*\n\n"

        "OKX: 🟢\n"
        "Telegram: 🟢\n"
        "Scanner: 🟢\n\n"

        "🧠 *NEW MARKET ENGINE*\n"
        "• Full market screening\n"
        "• Multi-timeframe levels\n"
        "• Level clusters\n"
        "• 1D / 4H / 1H structure\n"
        "• 30M / 15M compression\n"
        "• 5M momentum\n"
        "• Volume acceleration\n"
        "• ATR volatility\n"
        "• Open Interest\n"
        "• Early READY\n\n"

        f"💧 Minimum 24H turnover: "
        f"`${MIN_24H_VOLUME_USD / 1_000_000:.0f}M`\n"

        f"⭐ Minimum Score: "
        f"`{MIN_SCORE}/100`\n"

        f"🎯 Premium Score: "
        f"`{PREMIUM_SCORE}/100`\n"

        f"⏱ READY TTL: "
        f"`{READY_TTL_MINUTES} min`\n"

        f"🔒 Cooldown: "
        f"`{COOLDOWN_MINUTES} min`\n"

        f"🔎 Max candidates: "
        f"`{MAX_CANDIDATES}`\n\n"

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
        "=================================================="
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
    # FAST SCREEN
    # --------------------------------------------------------

    selected = fast_screen(
        tickers
    )

    log.info(
        "MARKET | tickers=%s | candidates=%s",
        len(tickers),
        len(selected)
    )

    if not selected:
        return

    # --------------------------------------------------------
    # EXISTING READY
    # --------------------------------------------------------

    for inst_id, ticker in selected:

        try:

            check_activation(
                inst_id,
                float(
                    ticker["last"]
                )
            )

        except Exception:

            log.exception(
                "READY CHECK ERROR | %s",
                inst_id
            )

    # --------------------------------------------------------
    # DEEP ANALYSIS
    # --------------------------------------------------------

    analyzed = 0

    for inst_id, ticker in selected:

        if analyzed >= MAX_DEEP_ANALYSIS:
            break

        if inst_id in ready_setups:
            continue

        if not can_send_new_signal(
            inst_id
        ):
            continue

        analyzed += 1

        try:

            log.info(
                "DEEP ANALYSIS | %s",
                get_coin(inst_id)
            )

            # ------------------------------------------------
            # MULTI TIMEFRAME DATA
            # ------------------------------------------------

            candles = {}

            candles["1D"] = get_candles(
                inst_id,
                "1D",
                100
            )

            candles["4H"] = get_candles(
                inst_id,
                "4H",
                120
            )

            candles["1H"] = get_candles(
                inst_id,
                "1H",
                120
            )

            candles["30m"] = get_candles(
                inst_id,
                "30m",
                120
            )

            candles["15m"] = get_candles(
                inst_id,
                "15m",
                120
            )

            candles["5m"] = get_candles(
                inst_id,
                "5m",
                120
            )

            setup = analyze_symbol(
                inst_id,
                ticker,
                candles
            )

            if setup is None:
                continue

            log.info(
                "CANDIDATE | %s | %s | "
                "%s | score=%s | "
                "level=%s | dist=%.3f%%",
                setup.coin,
                setup.direction,
                setup.strategy,
                setup.score,
                setup.level_tf,
                abs(
                    pct(
                        setup.current_price,
                        setup.level
                    )
                )
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
                    "SIGNAL NOT SAVED | Telegram failed | %s",
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
                "READY CREATED | %s | score=%s | state=%s",
                setup.coin,
                setup.score,
                setup.state_hint
            )

            time.sleep(
                1.2
            )

        except Exception as exc:

            # Ошибка одной монеты не останавливает сканер.
            log.exception(
                "SYMBOL ERROR | %s | %s",
                inst_id,
                exc
            )

            continue

    log.info(
        "SCAN #%s COMPLETE | deep=%s",
        scan_count,
        analyzed
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
            "WATCHDOG | scanner has not completed "
            "a scan for %.0f seconds",
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
        "QUANTUM SCALPER V4 STARTING"
    )

    log.info(
        "Timezone=%s",
        TIMEZONE
    )

    log.info(
        "MIN_24H_VOLUME=$%s",
        f"{MIN_24H_VOLUME_USD:,.0f}"
    )

    log.info(
        "MAX_CANDIDATES=%s",
        MAX_CANDIDATES
    )

    log.info(
        "MAX_DEEP_ANALYSIS=%s",
        MAX_DEEP_ANALYSIS
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
    # TELEGRAM
    # --------------------------------------------------------

    startup_message()

    # --------------------------------------------------------
    # OKX TEST
    # --------------------------------------------------------

    try:

        tickers = get_tickers()

        liquid_count = sum(
            1
            for data in tickers.values()
            if data.get(
                "vol24h_usd",
                0
            ) >= MIN_24H_VOLUME_USD
        )

        log.info(
            "OKX TICKERS: %s",
            len(tickers)
        )

        log.info(
            "OKX LIQUID: %s",
            liquid_count
        )

        top = sorted(
            tickers.items(),
            key=lambda item:
                item[1].get(
                    "vol24h_usd",
                    0
                ),
            reverse=True
        )[:10]

        for inst_id, data in top:

            log.info(
                "TOP LIQUID | %s | $%.1fM",
                get_coin(inst_id),
                data.get(
                    "vol24h_usd",
                    0
                ) / 1_000_000
            )

    except Exception:

        log.exception(
            "INITIAL OKX CONNECTION FAILED"
        )

    # --------------------------------------------------------
    # LOOP
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
