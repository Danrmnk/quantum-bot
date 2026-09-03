import os
import time
import logging
import sqlite3
import math

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
# BROAD MARKET DISCOVERY
# -> MULTI TIMEFRAME STRUCTURE
# -> DEEP LEVEL ENGINE
# -> LEVEL CONFLUENCE
# -> LIQUIDITY SWEEP
# -> REJECTION
# -> RETEST
# -> BREAKOUT
# -> COMPRESSION
# -> MOMENTUM
# -> EARLY READY
# -> ENTRY ACTIVE
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
# MARKET DISCOVERY
# ============================================================

# Было $60M.
# Теперь ниже порог, чтобы не терять интересные скальп-сетапы.
MIN_24H_VOLUME_USD = float(
    os.getenv(
        "MIN_24H_VOLUME_USD",
        "20000000"
    )
)

# Сколько монет максимально проходит глубокий анализ.
MAX_SYMBOLS = int(
    os.getenv(
        "MAX_SYMBOLS",
        "80"
    )
)

# Максимальное количество монет, которые допускаем
# даже если они чуть ниже основного фильтра.
SECONDARY_MIN_VOLUME_USD = float(
    os.getenv(
        "SECONDARY_MIN_VOLUME_USD",
        "10000000"
    )
)


# ============================================================
# SIGNAL
# ============================================================

MIN_SCORE = int(
    os.getenv(
        "MIN_SCORE",
        "76"
    )
)

COOLDOWN_MINUTES = int(
    os.getenv(
        "COOLDOWN_MINUTES",
        "45"
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
        "0.55"
    )
)

# 0 = без часового ограничения.
MAX_SIGNALS_PER_HOUR = int(
    os.getenv(
        "MAX_SIGNALS_PER_HOUR",
        "0"
    )
)

SCAN_INTERVAL_SECONDS = int(
    os.getenv(
        "SCAN_INTERVAL_SECONDS",
        "30"
    )
)

HTTP_TIMEOUT = int(
    os.getenv(
        "HTTP_TIMEOUT",
        "10"
    )
)


# ============================================================
# TIMEFRAMES
# ============================================================

TIMEFRAMES = (
    "1m",
    "3m",
    "5m",
    "15m",
    "1H",
    "4H",
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
    tf: str
    kind: str
    touches: int
    strength: float


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
    level_strength: float
    level_confluence: int

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
        return "VERY HIGH"

    if ratio >= 1.8:
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
        return "⚡ HIGH"

    return "🟡 SIGNAL"


def tf_weight(
    tf: str
) -> float:

    weights = {
        "1m": 1.0,
        "3m": 1.1,
        "5m": 1.2,
        "15m": 1.5,
        "1H": 2.0,
        "4H": 2.6,
    }

    return weights.get(
        tf,
        1.0
    )


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

            result[inst_id] = {
                "last": last,
                "high24h": high24h,
                "low24h": low24h,
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
        sum(
            trs[-period:]
        )
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


# ============================================================
# SWING STRUCTURE
# ============================================================

def structure_score(
    candles: List[Candle]
) -> Tuple[str, int]:

    if len(candles) < 40:
        return "NEUTRAL", 0

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

    long_points = 0
    short_points = 0

    if e20 > e50:
        long_points += 2

    if e20 < e50:
        short_points += 2

    if second_high > first_high:
        long_points += 2

    if second_high < first_high:
        short_points += 2

    if second_low > first_low:
        long_points += 2

    if second_low < first_low:
        short_points += 2

    if long_points >= short_points + 2:
        return "LONG", long_points

    if short_points >= long_points + 2:
        return "SHORT", short_points

    return "NEUTRAL", max(
        long_points,
        short_points
    )


def multi_tf_structure(
    candles: Dict[str, List[Candle]]
) -> Tuple[str, int, Dict[str, str]]:

    states = {}

    for tf in (
        "3m",
        "5m",
        "15m",
        "1H",
        "4H"
    ):

        data = candles.get(
            tf,
            []
        )

        state, points = structure_score(
            data
        )

        states[tf] = state

    long_votes = 0
    short_votes = 0

    weights = {
        "3m": 1,
        "5m": 1,
        "15m": 2,
        "1H": 3,
        "4H": 4,
    }

    for tf, state in states.items():

        if state == "LONG":
            long_votes += weights[tf]

        elif state == "SHORT":
            short_votes += weights[tf]

    if long_votes > short_votes:
        return (
            "LONG",
            long_votes,
            states
        )

    if short_votes > long_votes:
        return (
            "SHORT",
            short_votes,
            states
        )

    return (
        "NEUTRAL",
        max(
            long_votes,
            short_votes
        ),
        states
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
# LEVEL ENGINE
# ============================================================

def collect_levels(
    candles: Dict[str, List[Candle]]
) -> List[Level]:

    levels = []

    for tf in TIMEFRAMES:

        data = candles.get(
            tf,
            []
        )

        if len(data) < 20:
            continue

        highs = pivot_highs(
            data,
            2,
            2
        )

        lows = pivot_lows(
            data,
            2,
            2
        )

        # Старшие ТФ берём глубже.
        if tf == "4H":
            limit = 25
        elif tf == "1H":
            limit = 30
        elif tf == "15m":
            limit = 35
        else:
            limit = 20

        for _, price in highs[-limit:]:

            levels.append(
                Level(
                    price=price,
                    tf=tf,
                    kind="RESISTANCE",
                    touches=1,
                    strength=tf_weight(tf)
                )
            )

        for _, price in lows[-limit:]:

            levels.append(
                Level(
                    price=price,
                    tf=tf,
                    kind="SUPPORT",
                    touches=1,
                    strength=tf_weight(tf)
                )
            )

    return levels


def cluster_levels(
    levels: List[Level],
    current: float
) -> List[Level]:

    if not levels:
        return []

    # Допуск адаптируется к волатильности цены.
    tolerance_pct = 0.20

    sorted_levels = sorted(
        levels,
        key=lambda x: x.price
    )

    clusters = []

    for level in sorted_levels:

        placed = False

        for cluster in clusters:

            distance = abs(
                pct(
                    level.price,
                    cluster.price
                )
            )

            if distance <= tolerance_pct:

                total_weight = (
                    cluster.strength
                    + level.strength
                )

                cluster.price = (
                    cluster.price
                    * cluster.strength
                    + level.price
                    * level.strength
                ) / total_weight

                cluster.strength = (
                    total_weight
                )

                cluster.touches += 1

                if level.tf != cluster.tf:

                    # Храним старший TF как основной.
                    if tf_weight(level.tf) > tf_weight(
                        cluster.tf
                    ):
                        cluster.tf = level.tf

                placed = True
                break

        if not placed:

            clusters.append(
                Level(
                    price=level.price,
                    tf=level.tf,
                    kind=level.kind,
                    touches=1,
                    strength=level.strength
                )
            )

    # Пересчёт силы с учётом количества ТФ.
    for cluster in clusters:

        cluster.strength += (
            max(
                0,
                cluster.touches - 1
            )
            * 1.5
        )

    return clusters


def find_best_level(
    levels: List[Level],
    current: float,
    direction: str
) -> Tuple[
    Optional[Level],
    float
]:

    candidates = []

    for level in levels:

        distance = abs(
            pct(
                current,
                level.price
            )
        )

        # Слишком далёкие уровни не подходят
        # для текущего скальп-сетапа.
        if distance > 3.5:
            continue

        if direction == "LONG":

            # Для LONG основной уровень —
            # сопротивление сверху.
            if level.price >= current * 0.995:

                score = (
                    level.strength * 10
                    - distance * 2
                )

                candidates.append(
                    (
                        score,
                        level,
                        distance
                    )
                )

        else:

            # Для SHORT основной уровень —
            # поддержка снизу.
            if level.price <= current * 1.005:

                score = (
                    level.strength * 10
                    - distance * 2
                )

                candidates.append(
                    (
                        score,
                        level,
                        distance
                    )
                )

    if not candidates:
        return None, 0.0

    candidates.sort(
        key=lambda x: x[0],
        reverse=True
    )

    _, best, distance = candidates[0]

    return best, distance


# ============================================================
# DEEP LEVELS
# ============================================================

def nearest_opposite_level(
    levels: List[Level],
    price: float,
    direction: str
) -> Optional[float]:

    candidates = []

    for level in levels:

        if direction == "LONG":

            if level.price > price:
                candidates.append(
                    level.price
                )

        else:

            if level.price < price:
                candidates.append(
                    level.price
                )

    if not candidates:
        return None

    if direction == "LONG":
        return min(candidates)

    return max(candidates)


# ============================================================
# CANDLE PATTERNS
# ============================================================

def bullish_rejection(
    candles: List[Candle]
) -> bool:

    if len(candles) < 3:
        return False

    c = candles[-1]

    body = abs(
        c.close - c.open
    )

    lower_wick = (
        min(c.open, c.close)
        - c.low
    )

    if body <= 0:
        body = c.high - c.low

    return (
        lower_wick > body * 1.5
        and c.close > c.open
    )


def bearish_rejection(
    candles: List[Candle]
) -> bool:

    if len(candles) < 3:
        return False

    c = candles[-1]

    body = abs(
        c.close - c.open
    )

    upper_wick = (
        c.high
        - max(c.open, c.close)
    )

    if body <= 0:
        body = c.high - c.low

    return (
        upper_wick > body * 1.5
        and c.close < c.open
    )


# ============================================================
# LIQUIDITY SWEEP
# ============================================================

def detect_liquidity_sweep(
    candles: List[Candle],
    level: float,
    direction: str
) -> Tuple[
    bool,
    int,
    str
]:

    if len(candles) < 8:
        return False, 0, ""

    current = candles[-1]

    tolerance = (
        abs(level)
        * 0.0015
    )

    if direction == "LONG":

        swept = (
            current.low
            < level - tolerance
            and current.close > level
        )

        if swept:

            return (
                True,
                22,
                "Liquidity sweep: цена забрала "
                "ликвидность под уровнем и вернулась выше."
            )

    else:

        swept = (
            current.high
            > level + tolerance
            and current.close < level
        )

        if swept:

            return (
                True,
                22,
                "Liquidity sweep: цена забрала "
                "ликвидность над уровнем и вернулась ниже."
            )

    return False, 0, ""


# ============================================================
# REJECTION
# ============================================================

def detect_rejection(
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

    tolerance = (
        abs(level)
        * 0.0025
    )

    if direction == "LONG":

        touched = (
            current.low
            <= level + tolerance
            and current.high
            >= level - tolerance
        )

        if (
            touched
            and bullish_rejection(candles)
            and current.close > level
        ):

            return (
                True,
                18,
                "Сформировалась бычья rejection-свеча "
                "от ключевой зоны."
            )

    else:

        touched = (
            current.low
            <= level + tolerance
            and current.high
            >= level - tolerance
        )

        if (
            touched
            and bearish_rejection(candles)
            and current.close < level
        ):

            return (
                True,
                18,
                "Сформировалась медвежья rejection-свеча "
                "от ключевой зоны."
            )

    return False, 0, ""


# ============================================================
# RETEST
# ============================================================

def detect_retest(
    candles: List[Candle],
    level: float,
    direction: str
) -> Tuple[
    bool,
    int,
    str
]:

    if len(candles) < 6:
        return False, 0, ""

    previous = candles[-2]
    current = candles[-1]

    tolerance = (
        abs(level)
        * 0.0025
    )

    if direction == "LONG":

        breakout_before = (
            previous.close > level
        )

        retest = (
            current.low
            <= level + tolerance
            and current.close > level
        )

        if breakout_before and retest:

            return (
                True,
                20,
                "Цена пробила уровень и выполняет "
                "повторный тест сверху."
            )

    else:

        breakout_before = (
            previous.close < level
        )

        retest = (
            current.high
            >= level - tolerance
            and current.close < level
        )

        if breakout_before and retest:

            return (
                True,
                20,
                "Цена пробила уровень и выполняет "
                "повторный тест снизу."
            )

    return False, 0, ""


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

    first_avg = (
        sum(ranges[:8])
        / len(ranges[:8])
    )

    last_avg = (
        sum(ranges[-8:])
        / len(ranges[-8:])
    )

    if first_avg <= 0:
        return 0, False

    compression = (
        1.0
        - last_avg / first_avg
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
                value
                for _, value
                in lows[-3:]
            ]

            if all(
                values[i]
                <= values[i + 1]
                for i in range(
                    len(values) - 1
                )
            ):

                score += 10
                valid = True

    else:

        if len(highs) >= 2:

            values = [
                value
                for _, value
                in highs[-3:]
            ]

            if all(
                values[i]
                >= values[i + 1]
                for i in range(
                    len(values) - 1
                )
            ):

                score += 10
                valid = True

    if compression >= 0.12:
        score += 7

    if compression >= 0.20:
        score += 5

    if compression >= 0.30:
        score += 3

    return (
        min(score, 25),
        valid
    )


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

    e9 = ema(
        closes,
        9
    )[-1]

    e21 = ema(
        closes,
        21
    )[-1]

    current = candles[-1]

    previous_window = candles[
        -13:-1
    ]

    if direction == "LONG":

        previous_high = max(
            c.high
            for c in previous_window
        )

        valid = (
            e9 > e21
            and current.close > previous_high
        )

    else:

        previous_low = min(
            c.low
            for c in previous_window
        )

        valid = (
            e9 < e21
            and current.close < previous_low
        )

    if not valid:
        return False, 0, ""

    return (
        True,
        15,
        "5M показывает подтверждённый "
        "импульсный выход из локального диапазона."
    )


# ============================================================
# BREAKOUT
# ============================================================

def detect_breakout(
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

    previous = candles[-2]
    current = candles[-1]

    if direction == "LONG":

        valid = (
            current.close > level
            and previous.close <= level
        )

        if valid:

            return (
                True,
                22,
                "Подтверждённый breakout выше ключевой зоны."
            )

    else:

        valid = (
            current.close < level
            and previous.close >= level
        )

        if valid:

            return (
                True,
                22,
                "Подтверждённый breakout ниже ключевой зоны."
            )

    return False, 0, ""


# ============================================================
# EARLY APPROACH
# ============================================================

def detect_early_approach(
    current: float,
    level: float,
    direction: str,
    atr_value: float
) -> Tuple[
    bool,
    int,
    str
]:

    if level <= 0 or atr_value <= 0:
        return False, 0, ""

    distance = abs(
        current - level
    )

    atr_distance = (
        distance
        / atr_value
    )

    # Цена в пределах примерно 1 ATR от уровня.
    if atr_distance > 1.2:
        return False, 0, ""

    if direction == "LONG":

        if current < level:

            return (
                True,
                12,
                "Цена заранее подходит к сильному сопротивлению. "
                "Сетап готовится к возможному breakout."
            )

    else:

        if current > level:

            return (
                True,
                12,
                "Цена заранее подходит к сильной поддержке. "
                "Сетап готовится к возможному breakdown."
            )

    return False, 0, ""


# ============================================================
# SIGNAL DIRECTION
# ============================================================

def infer_direction(
    current: float,
    level: Level,
    structures: Dict[str, str]
) -> Optional[str]:

    long_score = 0
    short_score = 0

    for tf, state in structures.items():

        weight = tf_weight(tf)

        if state == "LONG":
            long_score += weight

        elif state == "SHORT":
            short_score += weight

    # Цена относительно уровня.
    if current < level.price:
        long_score += 2

    if current > level.price:
        short_score += 2

    if long_score >= short_score + 1.5:
        return "LONG"

    if short_score >= long_score + 1.5:
        return "SHORT"

    return None


# ============================================================
# ANALYZE SYMBOL
# ============================================================

def analyze_symbol(
    inst_id: str,
    ticker: dict,
    candles: Dict[str, List[Candle]]
) -> Optional[Setup]:

    # --------------------------------------------------------
    # DATA CHECK
    # --------------------------------------------------------

    required = (
        "3m",
        "5m",
        "15m",
        "1H",
        "4H"
    )

    for tf in required:

        if len(
            candles.get(tf, [])
        ) < 45:

            return None

    confirmed = {}

    for tf, data in candles.items():

        confirmed[tf] = [
            c
            for c in data
            if c.confirmed
        ]

    if len(
        confirmed["5m"]
    ) < 30:

        return None

    current = float(
        ticker["last"]
    )

    if current <= 0:
        return None

    # --------------------------------------------------------
    # LEVEL ENGINE
    # --------------------------------------------------------

    raw_levels = collect_levels(
        confirmed
    )

    if not raw_levels:
        return None

    levels = cluster_levels(
        raw_levels,
        current
    )

    if not levels:
        return None

    # --------------------------------------------------------
    # STRUCTURE
    # --------------------------------------------------------

    structure_direction, structure_points, structures = (
        multi_tf_structure(
            confirmed
        )
    )

    # --------------------------------------------------------
    # ATR
    # --------------------------------------------------------

    atr_value = atr(
        confirmed["5m"],
        14
    )

    if atr_value <= 0:
        return None

    atr_pct = (
        atr_value
        / current
        * 100
    )

    # Не берём мёртвый рынок.
    if atr_pct < 0.025:
        return None

    # И не берём абсолютно безумную волатильность.
    if atr_pct > 4.0:
        return None

    # --------------------------------------------------------
    # FIND BOTH SIDES
    # --------------------------------------------------------

    candidates = []

    for direction in (
        "LONG",
        "SHORT"
    ):

        level, distance = find_best_level(
            levels,
            current,
            direction
        )

        if level is None:
            continue

        # Если структура явно противоположная,
        # не запрещаем сетап полностью,
        # но сильно уменьшаем его score.
        structure_penalty = 0

        if (
            structure_direction
            not in (
                "NEUTRAL",
                direction
            )
        ):

            structure_penalty = 10

        # ----------------------------------------------------
        # STRATEGIES
        # ----------------------------------------------------

        strategy_hits = []

        c5 = confirmed["5m"]

        ok, points, reason = detect_breakout(
            c5,
            level.price,
            direction
        )

        if ok:

            strategy_hits.append(
                (
                    "Horizontal Level Breakout",
                    points,
                    reason
                )
            )

        ok, points, reason = detect_retest(
            c5,
            level.price,
            direction
        )

        if ok:

            strategy_hits.append(
                (
                    "Level Retest",
                    points,
                    reason
                )
            )

        ok, points, reason = detect_rejection(
            c5,
            level.price,
            direction
        )

        if ok:

            strategy_hits.append(
                (
                    "Level Rejection",
                    points,
                    reason
                )
            )

        ok, points, reason = detect_liquidity_sweep(
            c5,
            level.price,
            direction
        )

        if ok:

            strategy_hits.append(
                (
                    "Liquidity Sweep",
                    points,
                    reason
                )
            )

        ok, points, reason = detect_compression(
            confirmed["15m"],
            direction
        )

        if ok:

            strategy_hits.append(
                (
                    "Trendline Compression",
                    points,
                    reason
                )
            )

        ok, points, reason = detect_momentum(
            c5,
            direction
        )

        if ok:

            strategy_hits.append(
                (
                    "Momentum Breakout",
                    points,
                    reason
                )
            )

        # ----------------------------------------------------
        # EARLY READY
        # ----------------------------------------------------

        ok, points, reason = detect_early_approach(
            current,
            level.price,
            direction,
            atr_value
        )

        if ok:

            strategy_hits.append(
                (
                    "Early Level Approach",
                    points,
                    reason
                )
            )

        if not strategy_hits:
            continue

        strategy_hits.sort(
            key=lambda x: x[1],
            reverse=True
        )

        primary_strategy = strategy_hits[0]

        score = 0

        # ----------------------------------------------------
        # STRUCTURE
        # ----------------------------------------------------

        score += min(
            structure_points * 3,
            22
        )

        # ----------------------------------------------------
        # LEVEL STRENGTH
        # ----------------------------------------------------

        score += int(
            clamp(
                level.strength * 4,
                6,
                24
            )
        )

        # ----------------------------------------------------
        # CONFLUENCE
        # ----------------------------------------------------

        confluence = level.touches

        if confluence >= 2:
            score += 7

        if confluence >= 3:
            score += 5

        if confluence >= 4:
            score += 4

        # ----------------------------------------------------
        # STRATEGY
        # ----------------------------------------------------

        score += primary_strategy[1]

        # Второй независимый сигнал.
        if len(strategy_hits) >= 2:
            score += 5

        if len(strategy_hits) >= 3:
            score += 4

        # ----------------------------------------------------
        # VOLUME
        # ----------------------------------------------------

        v_ratio = volume_ratio(
            confirmed["5m"],
            20
        )

        if v_ratio >= 1.15:
            score += 5

        if v_ratio >= 1.50:
            score += 4

        if v_ratio >= 2.0:
            score += 4

        # ----------------------------------------------------
        # LIQUIDITY
        # ----------------------------------------------------

        volume_24h = float(
            ticker["vol24h_usd"]
        )

        if volume_24h < SECONDARY_MIN_VOLUME_USD:
            continue

        if volume_24h >= 1_000_000_000:

            score += 7
            liquidity = "HIGH"

        elif volume_24h >= 250_000_000:

            score += 5
            liquidity = "GOOD"

        elif volume_24h >= MIN_24H_VOLUME_USD:

            score += 3
            liquidity = "MEDIUM"

        else:

            liquidity = "LOW"

        # ----------------------------------------------------
        # STRUCTURE PENALTY
        # ----------------------------------------------------

        score -= structure_penalty

        # ----------------------------------------------------
        # OI
        # ----------------------------------------------------

        oi_value = get_open_interest(
            inst_id
        )

        if oi_value is not None:

            oi_status = "AVAILABLE"
            score += 2

        else:

            oi_status = "N/A"

        # ----------------------------------------------------
        # DISTANCE
        # ----------------------------------------------------

        if distance <= 0.10:
            score += 8

        elif distance <= 0.20:
            score += 6

        elif distance <= 0.40:
            score += 4

        elif distance <= 0.80:
            score += 2

        else:
            score -= 3

        # ----------------------------------------------------
        # CHASE
        # ----------------------------------------------------

        if direction == "LONG":

            if current > (
                level.price
                * (
                    1
                    + MAX_CHASE_PCT / 100
                )
            ):
                continue

        else:

            if current < (
                level.price
                * (
                    1
                    - MAX_CHASE_PCT / 100
                )
            ):
                continue

        # ----------------------------------------------------
        # ENTRY ZONE
        # ----------------------------------------------------

        zone_pct = clamp(
            atr_pct * 0.40,
            0.08,
            0.35
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

        # ----------------------------------------------------
        # STRUCTURAL STOP
        # ----------------------------------------------------

        recent_15 = confirmed["15m"][
            -24:
        ]

        if len(recent_15) < 12:
            continue

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
                continue

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
                continue

            risk = (
                sl
                - current
            )

        if risk <= 0:
            continue

        risk_pct = (
            risk
            / current
            * 100
        )

        if risk_pct < 0.12:
            continue

        if risk_pct > 2.2:
            continue

        # ----------------------------------------------------
        # TARGET ENGINE
        # ----------------------------------------------------

        opposite = nearest_opposite_level(
            levels,
            current,
            direction
        )

        if direction == "LONG":

            base_tp1 = current + risk * 1.0
            base_tp2 = current + risk * 2.0
            base_tp3 = current + risk * 3.0

            if opposite:

                if opposite > base_tp1:
                    base_tp1 = min(
                        base_tp1,
                        opposite * 0.995
                    )

                if opposite > base_tp2:
                    base_tp2 = min(
                        base_tp2,
                        opposite * 0.995
                    )

            tp1 = base_tp1
            tp2 = max(
                base_tp2,
                tp1 + risk * 0.5
            )
            tp3 = max(
                base_tp3,
                tp2 + risk * 0.5
            )

        else:

            base_tp1 = current - risk * 1.0
            base_tp2 = current - risk * 2.0
            base_tp3 = current - risk * 3.0

            if opposite:

                if opposite < base_tp1:
                    base_tp1 = max(
                        base_tp1,
                        opposite * 1.005
                    )

                if opposite < base_tp2:
                    base_tp2 = max(
                        base_tp2,
                        opposite * 1.005
                    )

            tp1 = base_tp1
            tp2 = min(
                base_tp2,
                tp1 - risk * 0.5
            )
            tp3 = min(
                base_tp3,
                tp2 - risk * 0.5
            )

        # ----------------------------------------------------
        # FINAL SCORE
        # ----------------------------------------------------

        score = int(
            clamp(
                score,
                0,
                100
            )
        )

        if score < MIN_SCORE:
            continue

        # ----------------------------------------------------
        # REASON
        # ----------------------------------------------------

        strategy_names = [
            item[0]
            for item in strategy_hits[:3]
        ]

        reason = (
            primary_strategy[2]
            + "\n\n"
            + "Подтверждения: "
            + ", ".join(
                strategy_names
            )
            + "."
        )

        if confluence >= 2:

            reason += (
                f"\nЗона подтверждена "
                f"{confluence} ценовыми реакциями "
                f"на нескольких таймфреймах."
            )

        if structure_direction == direction:

            reason += (
                f"\nMulti-TF структура поддерживает "
                f"{direction}."
            )

        candidates.append(
            (
                score,
                Setup(
                    inst_id=inst_id,
                    coin=get_coin(inst_id),
                    direction=direction,
                    strategy=primary_strategy[0],
                    level=level.price,
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
                    level_tf=level.tf,
                    level_strength=level.strength,
                    level_confluence=confluence,
                    reason=reason,
                    volume_24h=volume_24h,
                    breakout_volume_ratio=v_ratio,
                    atr_pct=atr_pct,
                    candles_5m=confirmed["5m"][
                        -80:
                    ]
                )
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

    if MAX_SIGNALS_PER_HOUR > 0:

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
        .replace("/", "_")
        .replace("\\", "_")
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

    ax.axhline(
        setup.level,
        color="#f5c542",
        linewidth=2.0,
        linestyle="--",
        label="LEVEL"
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
        linestyle="-.",
        label="SL"
    )

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
            f"Score {setup.score}/100 | "
            f"Multi-TF"
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

    plt.close(fig)

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
        f"`{fmt_price(setup.level)}`\n"

        f"Сила зоны: "
        f"`{setup.level_strength:.1f}`\n"

        f"Confluence: "
        f"`{setup.level_confluence}`\n\n"

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

        "Quantum Scalper V4 начинает новый день.\n\n"

        "🔎 Ищем сетапы по всему ликвидному рынку.\n"
        "🧠 Используем несколько таймфреймов.\n"
        "📍 Ищем глубокие уровни и зоны ликвидности.\n"
        "🎯 READY приходит заранее.\n"
        "🟢 ENTRY появляется после подтверждения.\n"
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
# STARTUP
# ============================================================

def startup_message():

    message = (
        "🚀 *QUANTUM SCALPER V4 ONLINE*\n\n"

        "OKX: 🟢\n"
        "Telegram: 🟢\n"
        "Scanner: 🟢\n\n"

        "🧠 *MULTI-TF ENGINE*\n"
        "• 1m\n"
        "• 3m\n"
        "• 5m\n"
        "• 15m\n"
        "• 1H\n"
        "• 4H\n\n"

        "📍 *LEVEL ENGINE*\n"
        "• Deep levels\n"
        "• Level clustering\n"
        "• Multi-TF confluence\n"
        "• Liquidity zones\n\n"

        "🎯 *SETUPS*\n"
        "• Early Approach\n"
        "• Breakout\n"
        "• Retest\n"
        "• Rejection\n"
        "• Liquidity Sweep\n"
        "• Compression\n"
        "• Momentum\n\n"

        f"💧 Minimum turnover: "
        f"`${MIN_24H_VOLUME_USD / 1_000_000:.0f}M`\n"

        f"🔎 Max symbols: "
        f"`{MAX_SYMBOLS}`\n"

        f"⭐ Minimum Score: "
        f"`{MIN_SCORE}/100`\n"

        f"⏱ READY TTL: "
        f"`{READY_TTL_MINUTES} min`\n"

        f"🔒 Cooldown: "
        f"`{COOLDOWN_MINUTES} min`\n\n"

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
    # MARKET DISCOVERY
    # --------------------------------------------------------

    liquid = []
    secondary = []

    for inst_id, data in tickers.items():

        volume_usd = float(
            data.get(
                "vol24h_usd",
                0
            )
        )

        if volume_usd >= MIN_24H_VOLUME_USD:

            liquid.append(
                (
                    inst_id,
                    data
                )
            )

        elif volume_usd >= SECONDARY_MIN_VOLUME_USD:

            secondary.append(
                (
                    inst_id,
                    data
                )
            )

    liquid.sort(
        key=lambda item: item[1]["vol24h_usd"],
        reverse=True
    )

    secondary.sort(
        key=lambda item: item[1]["vol24h_usd"],
        reverse=True
    )

    selected = (
        liquid[:MAX_SYMBOLS]
    )

    # Если ликвидных мало —
    # добираем из secondary.
    if len(selected) < MAX_SYMBOLS:

        remaining = (
            MAX_SYMBOLS
            - len(selected)
        )

        selected.extend(
            secondary[:remaining]
        )

    log.info(
        "MARKET | tickers=%s | "
        "primary=%s | secondary=%s | "
        "selected=%s",
        len(tickers),
        len(liquid),
        len(secondary),
        len(selected)
    )

    # --------------------------------------------------------
    # TOP LIQUID
    # --------------------------------------------------------

    if selected:

        top_names = []

        for inst_id, data in selected[:10]:

            top_names.append(
                (
                    f"{get_coin(inst_id)}:"
                    f"${data['vol24h_usd'] / 1_000_000:.0f}M"
                )
            )

        log.info(
            "MARKET WATCH | %s",
            " | ".join(top_names)
        )

    # --------------------------------------------------------
    # ANALYSIS
    # --------------------------------------------------------

    for index, (
        inst_id,
        ticker
    ) in enumerate(selected, 1):

        try:

            current_price = float(
                ticker["last"]
            )

            # Сначала обслуживаем существующие READY.
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

            log.debug(
                "ANALYZE | %s/%s | %s",
                index,
                len(selected),
                inst_id
            )

            # ------------------------------------------------
            # MULTI-TF DATA
            # ------------------------------------------------

            candles = {}

            for tf in TIMEFRAMES:

                try:

                    # На младших ТФ достаточно 100.
                    # Старшим даём больше истории.
                    if tf == "4H":
                        limit = 100

                    elif tf == "1H":
                        limit = 120

                    else:
                        limit = 100

                    candles[tf] = get_candles(
                        inst_id,
                        tf,
                        limit
                    )

                except Exception as exc:

                    log.debug(
                        "CANDLES FAILED | %s | %s | %s",
                        inst_id,
                        tf,
                        exc
                    )

                    candles[tf] = []

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
                "level=%s/%s | "
                "confluence=%s | "
                "volume=$%.1fM",
                setup.coin,
                setup.direction,
                setup.strategy,
                setup.score,
                fmt_price(setup.level),
                setup.level_tf,
                setup.level_confluence,
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
                "direction=%s | score=%s",
                setup.coin,
                setup.direction,
                setup.score
            )

            time.sleep(
                1.0
            )

        except Exception as exc:

            log.exception(
                "SYMBOL ERROR | %s | %s",
                inst_id,
                exc
            )

            continue

    log.info(
        "SCAN #%s COMPLETE | "
        "READY=%s | today=%s",
        scan_count,
        len(ready_setups),
        signals_today
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
        "QUANTUM SCALPER V4 STARTING"
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
        "SECONDARY_MIN_VOLUME_USD=$%s",
        f"{SECONDARY_MIN_VOLUME_USD:,.0f}"
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
        "MAX_SIGNALS_PER_HOUR=%s",
        MAX_SIGNALS_PER_HOUR
    )

    log.info(
        "SCAN_INTERVAL=%ss",
        SCAN_INTERVAL_SECONDS
    )

    log.info(
        "TIMEFRAMES=%s",
        ",".join(TIMEFRAMES)
    )

    log.info(
        "=================================================="
    )

    # --------------------------------------------------------
    # TELEGRAM
    # --------------------------------------------------------

    startup_message()

    # --------------------------------------------------------
    # OKX
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
            "OKX LIQUID >= $%.0fM: %s",
            MIN_24H_VOLUME_USD / 1_000_000,
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
