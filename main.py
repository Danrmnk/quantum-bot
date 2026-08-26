ю
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
# ОСНОВНОЙ ФИЛЬТР ЛИКВИДНОСТИ
# ------------------------------------------------------------

MIN_24H_VOLUME_USD = float(
    os.getenv(
        "MIN_24H_VOLUME_USD",
        "100000000"
    )
)

# ------------------------------------------------------------
# КАЧЕСТВО СИГНАЛА
# ------------------------------------------------------------

MIN_SCORE = int(
    os.getenv(
        "MIN_SCORE",
        "84"
    )
)

# ------------------------------------------------------------
# READY TTL
# ------------------------------------------------------------

READY_TTL_MINUTES = int(
    os.getenv(
        "READY_TTL_MINUTES",
        "10"
    )
)

# ------------------------------------------------------------
# COOLDOWN ОДНОЙ МОНЕТЫ
# ------------------------------------------------------------

COOLDOWN_MINUTES = int(
    os.getenv(
        "COOLDOWN_MINUTES",
        "45"
    )
)

# ------------------------------------------------------------
# ГЛОБАЛЬНЫЙ ЛИМИТ СИГНАЛОВ
#
# 0 = без искусственного лимита.
# ------------------------------------------------------------

MAX_SIGNALS_PER_HOUR = int(
    os.getenv(
        "MAX_SIGNALS_PER_HOUR",
        "0"
    )
)

# ------------------------------------------------------------
# ИНТЕРВАЛ СКАНИРОВАНИЯ
# ------------------------------------------------------------

SCAN_INTERVAL_SECONDS = int(
    os.getenv(
        "SCAN_INTERVAL_SECONDS",
        "30"
    )
)

# ------------------------------------------------------------
# MAX CHASE
# ------------------------------------------------------------

MAX_CHASE_PCT = float(
    os.getenv(
        "MAX_CHASE_PCT",
        "0.35"
    )
)

# ------------------------------------------------------------
# УТРЕННЕЕ СООБЩЕНИЕ
# ------------------------------------------------------------

MORNING_ENABLED = (
    os.getenv(
        "MORNING_ENABLED",
        "true"
    ).lower()
    == "true"
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

# ------------------------------------------------------------
# ДНЕВНАЯ СТАТИСТИКА
# ------------------------------------------------------------

DAILY_STATS_ENABLED = (
    os.getenv(
        "DAILY_STATS_ENABLED",
        "true"
    ).lower()
    == "true"
)

DAILY_STATS_HOUR = int(
    os.getenv(
        "DAILY_STATS_HOUR",
        "23"
    )
)

DAILY_STATS_MINUTE = int(
    os.getenv(
        "DAILY_STATS_MINUTE",
        "55"
    )
)

# ------------------------------------------------------------
# НЕДЕЛЬНАЯ СТАТИСТИКА
# ------------------------------------------------------------

WEEKLY_STATS_ENABLED = (
    os.getenv(
        "WEEKLY_STATS_ENABLED",
        "true"
    ).lower()
    == "true"
)

WEEKLY_STATS_WEEKDAY = int(
    os.getenv(
        "WEEKLY_STATS_WEEKDAY",
        "6"
    )
)

WEEKLY_STATS_HOUR = int(
    os.getenv(
        "WEEKLY_STATS_HOUR",
        "23"
    )
)

WEEKLY_STATS_MINUTE = int(
    os.getenv(
        "WEEKLY_STATS_MINUTE",
        "58"
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

db_lock = threading.Lock()


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


db_execute("""
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
""", commit=True)


db_execute("""
CREATE TABLE IF NOT EXISTS bot_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
""", commit=True)


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

    level_tf: str

    reason: str

    volume_24h: float
    volume_ratio: float

    atr_pct: float

    candles_5m: List[Candle]


@dataclass
class ActiveSignal:

    setup: Setup

    signal_id: str

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

scan_count = 0

signals_today = 0

last_morning_date = None
last_daily_stats_date = None
last_weekly_stats_key = None


# ============================================================
# GENERAL
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


def direction_emoji(
    direction: str
) -> str:

    return (
        "🟢"
        if direction == "LONG"
        else "🔴"
    )


def strategy_ru(
    strategy: str
) -> str:

    mapping = {

        "BREAKOUT_VOLUME":
            "Пробой уровня + объём",

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

    if score >= 87:
        return "⚡ СИЛЬНЫЙ"

    return "🎯 КАЧЕСТВЕННЫЙ"


def volume_label(
    volume: float
) -> str:

    if volume >= 5_000_000_000:
        return "💎 ЭКСТРЕМАЛЬНАЯ"

    if volume >= 1_000_000_000:
        return "🔥 ОЧЕНЬ ВЫСОКАЯ"

    if volume >= 500_000_000:
        return "🟢 ВЫСОКАЯ"

    if volume >= 250_000_000:
        return "🟢 ХОРОШАЯ"

    return "🟡 ПРОХОДНАЯ"


def fmt_usd(
    value: float
) -> str:

    if value >= 1_000_000_000:

        return (
            f"${value / 1_000_000_000:.2f}B"
        )

    return (
        f"${value / 1_000_000:.0f}M"
    )


# ============================================================
# OKX REQUEST
# ============================================================

def okx_get(
    path: str,
    params: dict,
    retries: int = 3
):

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
def get_tickers() -> Dict[str, dict]:
    """Получение USDT-SWAP тикеров OKX с диагностикой."""

    log.info(
        "OKX DEBUG | requesting SWAP tickers | base=%s",
        OKX_BASE_URL
    )

    try:
        payload = okx_get(
            "/api/v5/market/tickers",
            {"instType": "SWAP"}
        )

        raw_data = payload.get("data", [])

     def get_tickers() -> Dict[str, dict]:
    """Получение USDT-SWAP тикеров OKX."""

    payload = okx_get(
        "/api/v5/market/tickers",
        {"instType": "SWAP"}
    )

    raw_data = payload.get("data", [])

    log.info(
        "OKX DEBUG | code=%s | msg=%s | raw_items=%s",
        payload.get("code"),
        payload.get("msg", ""),
        len(raw_data)
    )

    result = {}

    total = 0
    usdt_swap = 0
    bad_price = 0
    bad_volume = 0
    invalid = 0

    for item in raw_data:

        if not isinstance(item, dict):
            invalid += 1
            continue

        total += 1

        inst_id = str(
            item.get("instId", "")
        ).strip()

        if not inst_id.endswith("-USDT-SWAP"):
            continue

        usdt_swap += 1

        try:
            last = float(
                item.get("last") or 0
            )

            high24h = float(
                item.get("high24h") or 0
            )

            low24h = float(
                item.get("low24h") or 0
            )

            # Для USDT-SWAP OKX уже отдаёт
            # оборот в котируемой валюте.
            volume = float(
                item.get("volCcyQuote24h") or 0
            )

            # Fallback
            if volume <= 0:
                volume = float(
                    item.get("volCcy24h") or 0
                ) * last

            ts = int(
                float(
                    item.get("ts") or 0
                )
            )

        except (
            TypeError,
            ValueError,
            OverflowError
        ):
            invalid += 1
            continue

        if last <= 0:
            bad_price += 1
            continue

        if volume <= 0:
            bad_volume += 1
            continue

        result[inst_id] = {
            "last": last,
            "high24h": high24h,
            "low24h": low24h,
            "volume_24h": volume,
            "ts": ts
        }

    log.info(
        "OKX DEBUG | total=%s | USDT-SWAP=%s | "
        "valid=%s | bad_price=%s | "
        "bad_volume=%s | invalid=%s",
        total,
        usdt_swap,
        len(result),
        bad_price,
        bad_volume,
        invalid
    )

    if result:
        log.info(
            "OKX DEBUG | sample=%s",
            ", ".join(
                list(result.keys())[:5]
            )
        )
    else:
        log.warning(
            "OKX DEBUG | NO VALID USDT-SWAP"
        )

    return result

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
                        row[5]
                        or 0
                    ),

                    quote_volume=float(
                        row[7]
                        or 0
                    ),

                    confirmed=(
                        str(row[8])
                        == "1"
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
            * (1.0 - alpha)
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

        previous = candles[
            i - 1
        ]

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

    current = candles[
        -1
    ].quote_volume

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

    recent = candles[
        -20:
    ]

    first = recent[
        :10
    ]

    second = recent[
        10:
    ]

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

        value = candles[
            i
        ].high

        valid = True

        for j in range(
            i - left,
            i + right + 1
        ):

            if j == i:
                continue

            if candles[
                j
            ].high > value:

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

    if len(candles) < (
        left + right + 1
    ):
        return result

    for i in range(
        left,
        len(candles) - right
    ):

        value = candles[
            i
        ].low

        valid = True

        for j in range(
            i - left,
            i + right + 1
        ):

            if j == i:
                continue

            if candles[
                j
            ].low < value:

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

        for _, level in highs[-30:]:

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
                        "15M",
                        distance
                    )
                )

        if len(candles_1h) >= 30:

            recent = candles_1h[
                -30:-1
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
                and distance <= 3.0
            ):

                candidates.append(
                    (
                        level,
                        "1H",
                        distance
                    )
                )

    else:

        for _, level in lows[-30:]:

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
                        "15M",
                        distance
                    )
                )

        if len(candles_1h) >= 30:

            recent = candles_1h[
                -30:-1
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
                and distance <= 3.0
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

    # Сначала предпочитаем 1H,
    # затем ближайший уровень.

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
    candles: List[Candle]
) -> Tuple[int, bool]:

    if len(candles) < 30:

        return 0, False

    recent = candles[
        -24:
    ]

    ranges = [

        c.high - c.low

        for c in recent

        if c.high > c.low
    ]

    if len(ranges) < 18:

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
        1.0
        - last_avg / first_avg
    )

    score = 0

    if reduction >= 0.15:

        score += 8

    if reduction >= 0.25:

        score += 7

    if reduction >= 0.35:

        score += 5

    # Сужение последних свечей.

    highs = [
        c.high
        for c in recent[-8:]
    ]

    lows = [
        c.low
        for c in recent[-8:]
    ]

    width = (
        max(highs)
        - min(lows)
    )

    avg_price = (
        sum(
            c.close
            for c in recent[-8:]
        )
        / 8
    )

    width_pct = (
        width
        / avg_price
        * 100
    )

    if width_pct <= 1.2:

        score += 5

    valid = (
        reduction >= 0.15
    )

    return (
        min(score, 25),
        valid
    )


# ============================================================
# 5M PRE-FILTER
# ============================================================

def fast_5m_candidate(
    candles: List[Candle]
) -> bool:

    if len(candles) < 60:
        return False

    confirmed = [
        c
        for c in candles
        if c.confirmed
    ]

    if len(confirmed) < 50:
        return False

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

    previous = confirmed[-2]

    vr = volume_ratio(
        confirmed,
        20
    )

    compression_points, compressed = (
        compression_score(
            confirmed
        )
    )

    bullish_impulse = (
        e9 > e21
        and current.close
        > previous.close
    )

    bearish_impulse = (
        e9 < e21
        and current.close
        < previous.close
    )

    volume_ok = (
        vr >= 1.05
    )

    return (
        (
            bullish_impulse
            or bearish_impulse
        )
        and (
            volume_ok
            or compressed
        )
    )


# ============================================================
# SIGNAL ANALYSIS
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

    current = ticker[
        "last"
    ]

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

    # --------------------------------------------------------
    # 1H TREND
    # --------------------------------------------------------

    score += 15

    reasons.append(
        "тренд 1H подтверждает направление"
    )

    # --------------------------------------------------------
    # LEVEL
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # 15M COMPRESSION
    # --------------------------------------------------------

    compression_points, compressed = (
        compression_score(
            confirmed_15m
        )
    )

    score += compression_points

    if compressed:

        reasons.append(
            "рынок сжимается перед движением"
        )

    # ========================================================
    # BREAKOUT
    # ========================================================

    breakout = False

    if direction == "LONG":

        breakout = (

            previous_candle.close
            <= level

            and

            current_candle.close
            > level
        )

    else:

        breakout = (

            previous_candle.close
            >= level

            and

            current_candle.close
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

            and

            current_candle.close
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

            and

            current_candle.close
            < previous_low
        )

    # ========================================================
    # STRATEGY 1
    # BREAKOUT + VOLUME
    # ========================================================

    if (
        breakout
        and v_ratio >= 1.25
    ):

        strategy = (
            "BREAKOUT_VOLUME"
        )

        score += 28

        reasons.append(
            "пробой уровня подтверждён объёмом"
        )

    # ========================================================
    # STRATEGY 2
    # COMPRESSION BREAKOUT
    # ========================================================

    elif (
        breakout
        and compressed
    ):

        strategy = (
            "COMPRESSION_BREAKOUT"
        )

        score += 25

        reasons.append(
            "сжатие 15M завершилось пробоем"
        )

    # ========================================================
    # STRATEGY 3
    # MOMENTUM
    # ========================================================

    elif momentum:

        strategy = (
            "MOMENTUM_BREAKOUT"
        )

        score += 22

        reasons.append(
            "сильный импульс 5M подтверждён EMA"
        )

    else:

        return None

    # ========================================================
    # VOLUME
    # ========================================================

    if v_ratio >= 1.25:

        score += 7

        reasons.append(
            f"объём {v_ratio:.2f}x от среднего"
        )

    if v_ratio >= 1.50:

        score += 4

    if v_ratio >= 2.0:

        score += 4

        reasons.append(
            "аномальный всплеск объёма"
        )

    # ========================================================
    # LIQUIDITY
    # ========================================================

    volume_24h = ticker[
        "volume_24h"
    ]

    if volume_24h >= 1_000_000_000:

        score += 6

        liquidity = "ОЧЕНЬ ВЫСОКАЯ"

    elif volume_24h >= 500_000_000:

        score += 5

        liquidity = "ВЫСОКАЯ"

    elif volume_24h >= 250_000_000:

        score += 4

        liquidity = "ХОРОШАЯ"

    else:

        score += 2

        liquidity = "ПРОХОДНАЯ"

    # ========================================================
    # DISTANCE FROM LEVEL
    # ========================================================

    distance = abs(
        percentage(
            current,
            level
        )
    )

    if distance > 1.20:
        return None

    if breakout:

        if distance > (
            MAX_CHASE_PCT + 0.25
        ):

            return None

    else:

        if distance > 0.50:

            return None

    # ========================================================
    # CANDLE QUALITY
    # ========================================================

    candle_range = (
        current_candle.high
        - current_candle.low
    )

    if candle_range <= 0:
        return None

    body = abs(
        current_candle.close
        - current_candle.open
    )

    body_ratio = (
        body
        / candle_range
    )

    if body_ratio >= 0.55:

        score += 5

        reasons.append(
            "сильная импульсная свеча"
        )

    elif body_ratio < 0.25:

        score -= 5

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

    recent = confirmed_15m[
        -18:
    ]

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
    # TARGETS
    # ========================================================

    if direction == "LONG":

        tp1 = (
            current
            + risk * 1.0
        )

        tp2 = (
            current
            + risk * 2.0
        )

        tp3 = (
            current
            + risk * 3.0
        )

    else:

        tp1 = (
            current
            - risk * 1.0
        )

        tp2 = (
            current
            - risk * 2.0
        )

        tp3 = (
            current
            - risk * 3.0
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

    reason = (
        "• "
        + "\n• ".join(
            reasons
        )
    )

    return Setup(

        inst_id=inst_id,

        coin=coin_name(
            inst_id
        ),

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
# SIGNAL DATABASE
# ============================================================

def save_signal(
    setup: Setup,
    active: ActiveSignal
):

    db_execute(
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
            active.signal_id,

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

            active.created_at,

            active.expires_at,

            active.photo_message_id,

            active.text_message_id
        ),
        commit=True
    )


def update_status(
    signal_id: str,
    status: str
):

    db_execute(
        """
        UPDATE signals
        SET status = ?
        WHERE signal_id = ?
        """,
        (
            status,
            signal_id
        ),
        commit=True
    )


# ============================================================
# COOLDOWN
# ============================================================

def can_create_signal(
    inst_id: str
) -> bool:

    if inst_id in active_signals:

        return False

    row = db_execute(
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

    if MAX_SIGNALS_PER_HOUR > 0:

        cutoff = (
            now_ts()
            - 3600
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
            "Недостаточно свечей."
        )

    path = (
        f"/tmp/"
        f"quantum_"
        f"{setup.coin}_"
        f"{int(time.time())}.png"
    )

    fig, ax = plt.subplots(
        figsize=(13, 7.5),
        dpi=150
    )

    fig.patch.set_facecolor(
        "#080d18"
    )

    ax.set_facecolor(
        "#080d18"
    )

    width = 0.62

    for i, candle in enumerate(
        candles
    ):

        bullish = (
            candle.close
            >= candle.open
        )

        color = (
            "#00d084"
            if bullish
            else "#ff4757"
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
                    i
                    - width / 2,
                    body_low
                ),

                width,

                body_height,

                facecolor=color,

                edgecolor=color
            )
        )

    # --------------------------------------------------------
    # LEVEL
    # --------------------------------------------------------

    ax.axhline(
        setup.level,
        color="#ffd166",
        linewidth=2.2,
        linestyle="--"
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
    # STOP
    # --------------------------------------------------------

    ax.axhline(
        setup.sl,
        color="#ff3b30",
        linewidth=1.8,
        linestyle="-."
    )

    # --------------------------------------------------------
    # TARGETS
    # --------------------------------------------------------

    target_colors = [
        "#5ee7df",
        "#ffd166",
        "#00d084"
    ]

    for target, color in zip(
        [
            setup.tp1,
            setup.tp2,
            setup.tp3
        ],
        target_colors
    ):

        ax.axhline(
            target,
            color=color,
            linewidth=1.3,
            alpha=0.9
        )

    last_index = (
        len(candles) - 1
    )

    # --------------------------------------------------------
    # LABELS
    # --------------------------------------------------------

    ax.text(
        last_index,
        setup.level,
        "  УРОВЕНЬ",
        color="#ffd166",
        fontsize=10,
        fontweight="bold",
        va="bottom"
    )

    ax.text(
        last_index,
        setup.sl,
        "  STOP",
        color="#ff4757",
        fontsize=10,
        fontweight="bold",
        va="bottom"
    )

    ax.text(
        last_index,
        setup.tp1,
        "  TP1",
        color="#5ee7df",
        fontsize=9,
        fontweight="bold",
        va="bottom"
    )

    ax.text(
        last_index,
        setup.tp2,
        "  TP2",
        color="#ffd166",
        fontsize=9,
        fontweight="bold",
        va="bottom"
    )

    ax.text(
        last_index,
        setup.tp3,
        "  TP3",
        color="#00d084",
        fontsize=9,
        fontweight="bold",
        va="bottom"
    )

    # --------------------------------------------------------
    # TITLE
    # --------------------------------------------------------

    title = (
        f"{setup.coin} / USDT\n"
        f"{direction_emoji(setup.direction)} "
        f"{direction_ru(setup.direction)}  •  "
        f"{strategy_ru(setup.strategy)}"
    )

    ax.set_title(
        title,
        color="white",
        fontsize=16,
        fontweight="bold",
        pad=14
    )

    ax.grid(
        alpha=0.10,
        color="white"
    )

    ax.tick_params(
        colors="#9ca3af"
    )

    for spine in ax.spines.values():

        spine.set_color(
            "#26334d"
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

    risk = (
        abs(
            setup.current_price
            - setup.sl
        )
        / setup.current_price
        * 100
    )

    direction = direction_ru(
        setup.direction
    )

    emoji = direction_emoji(
        setup.direction
    )

    return (

        f"{emoji} *{setup.coin}USDT — "
        f"{direction}*\n\n"

        f"🔥 *ТОПОВЫЙ СЕТАП*\n"
        f"*{score_label(setup.score)}*\n\n"

        f"━━━━━━━━━━━━━━━━━━\n"

        f"🎯 *ВХОД*\n"
        f"`{fmt_price(setup.entry_low)}` "
        f"— "
        f"`{fmt_price(setup.entry_high)}`\n\n"

        f"💰 Цена сейчас: "
        f"`{fmt_price(setup.current_price)}`\n\n"

        f"🟡 Уровень пробоя: "
        f"`{fmt_price(setup.level)}`\n\n"

        f"🛑 *STOP*\n"
        f"`{fmt_price(setup.sl)}`\n"
        f"Риск: `-{risk:.2f}%`\n\n"

        f"🎯 *TP1 — 30%*\n"
        f"`{fmt_price(setup.tp1)}`\n\n"

        f"🎯 *TP2 — 30%*\n"
        f"`{fmt_price(setup.tp2)}`\n\n"

        f"🏆 *TP3 — 40%*\n"
        f"`{fmt_price(setup.tp3)}`\n\n"

        f"━━━━━━━━━━━━━━━━━━\n"

        f"🧠 *ПОЧЕМУ ВХОД:*\n"
        f"{setup.reason}\n\n"

        f"📊 *СТРАТЕГИЯ:*\n"
        f"`{strategy_ru(setup.strategy)}`\n\n"

        f"📍 *УРОВЕНЬ:*\n"
        f"`{setup.level_tf}` • "
        f"`{fmt_price(setup.level)}`\n\n"

        f"📈 *ОБЪЁМ 5M:*\n"
        f"`{setup.volume_ratio:.2f}x` "
        f"({setup.volume_grade})\n\n"

        f"💧 *24H ОБОРОТ:*\n"
        f"`{fmt_usd(setup.volume_24h)}`\n\n"

        f"⭐ *SCORE:* "
        f"`{setup.score}/100`\n\n"

        f"⏱ Сетап действителен "
        f"`{READY_TTL_MINUTES} мин`.\n\n"

        f"🔥 *Не догоняем цену.*\n"
        f"🧠 *Качество важнее количества.*\n\n"

        f"💚 Лайк • 🔥 Огонь • 🚀 Вперёд"
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

            f"{direction_emoji(setup.direction)} "
            f"*{setup.coin}USDT — "
            f"{direction_ru(setup.direction)}*\n"

            f"{score_label(setup.score)}\n"

            f"📊 "
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
            "Ошибка публикации сигнала"
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
# ENTRY
# ============================================================

def send_entry_message(
    active: ActiveSignal,
    price: float
):

    setup = active.setup

    text = (

        f"🟢 *ВХОД АКТИВИРОВАН*\n\n"

        f"*{setup.coin}USDT — "
        f"{direction_ru(setup.direction)}*\n\n"

        f"Цена: `{fmt_price(price)}`\n"
        f"Уровень: `{fmt_price(setup.level)}`\n\n"

        f"🛑 STOP: `{fmt_price(setup.sl)}`\n"
        f"🎯 TP1: `{fmt_price(setup.tp1)}`\n"
        f"🎯 TP2: `{fmt_price(setup.tp2)}`\n"
        f"🏆 TP3: `{fmt_price(setup.tp3)}`\n\n"

        f"🔥 Работаем строго по плану."
    )

    try:

        bot.send_message(
            CHANNEL_ID,
            text,
            parse_mode="Markdown"
        )

    except Exception:

        log.exception(
            "Ошибка ENTRY"
        )


# ============================================================
# TP
# ============================================================

def send_tp_message(
    active: ActiveSignal,
    number: int,
    price: float
):

    setup = active.setup

    if number == 1:

        text = (

            f"🎯 *{setup.coin}USDT — TP1*\n\n"

            f"Цена: `{fmt_price(price)}`\n\n"

            f"💰 Закрываем *30%*\n"
            f"🛡 STOP → *БЕЗУБЫТОК*\n\n"

            f"🔥 Основное движение продолжаем."
        )

    elif number == 2:

        text = (

            f"🎯 *{setup.coin}USDT — TP2*\n\n"

            f"Цена: `{fmt_price(price)}`\n\n"

            f"💰 Закрываем ещё *30%*\n\n"

            f"🏆 Остаток держим к TP3."
        )

    else:

        text = (

            f"🏆 *{setup.coin}USDT — TP3*\n\n"

            f"Цена: `{fmt_price(price)}`\n\n"

            f"💎 *TP3 достигнут.*\n\n"

            f"Сетап полностью завершён. 🚀"
        )

    try:

        bot.send_message(
            CHANNEL_ID,
            text,
            parse_mode="Markdown"
        )

    except Exception:

        log.exception(
            "Ошибка TP"
        )


# ============================================================
# STOP
# ============================================================

def send_stop_message(
    active: ActiveSignal,
    price: float
):

    setup = active.setup

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
            result
            / risk
        )

    else:

        result_r = -1.0

    text = (

        f"🛑 *{setup.coin}USDT — STOP*\n\n"

        f"Цена выхода: "
        f"`{fmt_price(price)}`\n\n"

        f"Результат: "
        f"`{result_r:+.2f}R`\n\n"

        f"❗ Сетап завершён.\n"
        f"🧠 Следующий сигнал оцениваем заново."
    )

    try:

        bot.send_message(
            CHANNEL_ID,
            text,
            parse_mode="Markdown"
        )

    except Exception:

        log.exception(
            "Ошибка STOP"
        )

    db_execute(
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
            active.signal_id
        ),
        commit=True
    )


# ============================================================
# ACTIVE SIGNAL
# ============================================================

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

    setup.current_price = price

    # ========================================================
    # READY -> ACTIVE
    # ========================================================

    if not active.activated:

        if setup.direction == "LONG":

            hit = (
                price >= setup.level
            )

        else:

            hit = (
                price <= setup.level
            )

        if hit:

            active.activated = True

            update_status(
                active.signal_id,
                "ACTIVE"
            )

            db_execute(
                """
                UPDATE signals
                SET activated_at = ?
                WHERE signal_id = ?
                """,
                (
                    now_ts(),
                    active.signal_id
                ),
                commit=True
            )

            send_entry_message(
                active,
                price
            )

        return

    # ========================================================
    # STOP
    # ========================================================

    if setup.direction == "LONG":

        stop_hit = (
            price <= setup.sl
        )

    else:

        stop_hit = (
            price >= setup.sl
        )

    if stop_hit:

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

        if setup.direction == "LONG":

            hit = (
                price >= setup.tp1
            )

        else:

            hit = (
                price <= setup.tp1
            )

        if hit:

            active.tp1_hit = True

            send_tp_message(
                active,
                1,
                price
            )

            db_execute(
                """
                UPDATE signals
                SET tp1_hit_at = ?
                WHERE signal_id = ?
                """,
                (
                    now_ts(),
                    active.signal_id
                ),
                commit=True
            )

            # Безубыток.
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

        if setup.direction == "LONG":

            hit = (
                price >= setup.tp2
            )

        else:

            hit = (
                price <= setup.tp2
            )

        if hit:

            active.tp2_hit = True

            send_tp_message(
                active,
                2,
                price
            )

            db_execute(
                """
                UPDATE signals
                SET tp2_hit_at = ?
                WHERE signal_id = ?
                """,
                (
                    now_ts(),
                    active.signal_id
                ),
                commit=True
            )

    # ========================================================
    # TP3
    # ========================================================

    if (
        active.tp2_hit
        and not active.tp3_hit
    ):

        if setup.direction == "LONG":

            hit = (
                price >= setup.tp3
            )

        else:

            hit = (
                price <= setup.tp3
            )

        if hit:

            active.tp3_hit = True

            send_tp_message(
                active,
                3,
                price
            )

            db_execute(
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
                    active.signal_id
                ),
                commit=True
            )

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

        update_status(
            active.signal_id,
            "EXPIRED"
        )

        try:

            bot.send_message(
                CHANNEL_ID,

                (
                    f"🔴 *СЕТАП ОТМЕНЁН*\n\n"

                    f"*{active.setup.coin}USDT — "
                    f"{direction_ru(active.setup.direction)}*\n\n"

                    f"Цена не дала "
                    f"своевременного входа.\n\n"

                    f"🧠 *Рынок не догоняем.*"
                ),

                parse_mode="Markdown"
            )

        except Exception:

            log.exception(
                "Ошибка EXPIRED"
            )

        active_signals.pop(
            inst_id,
            None
        )


# ============================================================
# MORNING
# ============================================================

def morning_message():

    global last_morning_date

    if not MORNING_ENABLED:
        return

    current = local_now()

    if (
        current.hour != MORNING_HOUR
        or current.minute
        != MORNING_MINUTE
    ):
        return

    today = current.date()

    if (
        last_morning_date
        == today
    ):
        return

    text = (

        "🌅 *ДОБРОЕ УТРО, ТРЕЙДЕРЫ!*\n\n"

        "🚀 *QUANTUM SCALPER* "
        "начинает новый день.\n\n"

        "🔎 Ищем только ликвидные рынки.\n"
        "💧 Минимальный 24H оборот — "
        f"`{fmt_usd(MIN_24H_VOLUME_USD)}`.\n"
        "📊 Анализируем структуру 1H / 15M / 5M.\n"
        "🎯 Ждём подтверждённый пробой.\n"
        "🚫 Не догоняем цену.\n"
        "🛑 Не увеличиваем риск после убытка.\n\n"

        "🔥 *Три основных оружия:*\n"
        "1. Пробой + объём\n"
        "2. Сжатие → пробой\n"
        "3. Импульсный пробой\n\n"

        "💎 *Качество важнее количества.*\n\n"

        "Удачного дня! 🚀🔥"
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
# DAILY STATS
# ============================================================

def daily_stats():

    global last_daily_stats_date

    if not DAILY_STATS_ENABLED:
        return

    current = local_now()

    if (
        current.hour
        != DAILY_STATS_HOUR
        or current.minute
        != DAILY_STATS_MINUTE
    ):
        return

    today = current.date()

    if (
        last_daily_stats_date
        == today
    ):
        return

    start = datetime(
        today.year,
        today.month,
        today.day,
        tzinfo=ZoneInfo(
            TIMEZONE
        )
    ).timestamp()

    end = (
        start
        + 86400
    )

    row = db_execute(
        """
        SELECT
            COUNT(*),
            SUM(
                CASE
                WHEN status = 'TP3'
                THEN 1 ELSE 0
                END
            ),
            SUM(
                CASE
                WHEN status = 'SL'
                THEN 1 ELSE 0
                END
            ),
            SUM(
                CASE
                WHEN status = 'EXPIRED'
                THEN 1 ELSE 0
                END
            ),
            COALESCE(
                SUM(
                    CASE
                    WHEN result_r IS NOT NULL
                    THEN result_r
                    ELSE 0
                    END
                ),
                0
            )
        FROM signals
        WHERE created_at >= ?
        AND created_at < ?
        """,
        (
            start,
            end
        )
    ).fetchone()

    total = int(
        row[0] or 0
    )

    wins = int(
        row[1] or 0
    )

    losses = int(
        row[2] or 0
    )

    expired = int(
        row[3] or 0
    )

    total_r = float(
        row[4] or 0
    )

    closed = (
        wins + losses
    )

    if closed > 0:

        winrate = (
            wins
            / closed
            * 100
        )

    else:

        winrate = 0.0

    text = (

        "📊 *ДНЕВНАЯ СТАТИСТИКА*\n\n"

        f"📅 {today.strftime('%d.%m.%Y')}\n\n"

        f"🎯 Сигналов: `{total}`\n"
        f"🏆 TP3: `{wins}`\n"
        f"🛑 STOP: `{losses}`\n"
        f"🔴 Отменено: `{expired}`\n\n"

        f"📈 Winrate: `{winrate:.1f}%`\n"
        f"💰 Результат: `{total_r:+.2f}R`\n\n"

        f"🔥 *Анализ завершён. "
        f"Завтра ищем ещё лучше.*"
    )

    try:

        bot.send_message(
            CHANNEL_ID,
            text,
            parse_mode="Markdown"
        )

        last_daily_stats_date = today

    except Exception:

        log.exception(
            "Ошибка дневной статистики"
        )


# ============================================================
# WEEK KEY
# ============================================================

def week_key(
    dt: datetime
) -> str:

    iso = dt.isocalendar()

    return (
        f"{iso.year}-W"
        f"{iso.week:02d}"
    )


# ============================================================
# WEEKLY STATS
# ============================================================

def weekly_stats():

    global last_weekly_stats_key

    if not WEEKLY_STATS_ENABLED:
        return

    current = local_now()

    if current.weekday() != (
        WEEKLY_STATS_WEEKDAY
    ):
        return

    if (
        current.hour
        != WEEKLY_STATS_HOUR
        or current.minute
        != WEEKLY_STATS_MINUTE
    ):
        return

    key = week_key(
        current
    )

    if (
        last_weekly_stats_key
        == key
    ):
        return

    monday = (
        current.date()
        - timedelta(
            days=current.weekday()
        )
    )

    start_dt = datetime(
        monday.year,
        monday.month,
        monday.day,
        tzinfo=ZoneInfo(
            TIMEZONE
        )
    )

    end_dt = (
        start_dt
        + timedelta(days=7)
    )

    row = db_execute(
        """
        SELECT
            COUNT(*),
            SUM(
                CASE
                WHEN status = 'TP3'
                THEN 1 ELSE 0
                END
            ),
            SUM(
                CASE
                WHEN status = 'SL'
                THEN 1 ELSE 0
                END
            ),
            SUM(
                CASE
                WHEN status = 'EXPIRED'
                THEN 1 ELSE 0
                END
            ),
            COALESCE(
                SUM(
                    CASE
                    WHEN result_r IS NOT NULL
                    THEN result_r
                    ELSE 0
                    END
                ),
                0
            )
        FROM signals
        WHERE created_at >= ?
        AND created_at < ?
        """,
        (
            start_dt.timestamp(),
            end_dt.timestamp()
        )
    ).fetchone()

    total = int(
        row[0] or 0
    )

    wins = int(
        row[1] or 0
    )

    losses = int(
        row[2] or 0
    )

    expired = int(
        row[3] or 0
    )

    total_r = float(
        row[4] or 0
    )

    closed = (
        wins + losses
    )

    if closed > 0:

        winrate = (
            wins
            / closed
            * 100
        )

    else:

        winrate = 0.0

    # --------------------------------------------------------
    # ЛУЧШАЯ СТРАТЕГИЯ НЕДЕЛИ
    # --------------------------------------------------------

    strategy_row = db_execute(
        """
        SELECT
            strategy,
            COUNT(*) AS total,
            SUM(
                CASE
                WHEN status = 'TP3'
                THEN 1 ELSE 0
                END
            ) AS wins
        FROM signals
        WHERE created_at >= ?
        AND created_at < ?
        GROUP BY strategy
        ORDER BY wins DESC, total DESC
        LIMIT 1
        """,
        (
            start_dt.timestamp(),
            end_dt.timestamp()
        )
    ).fetchone()

    if strategy_row:

        best_strategy = (
            strategy_ru(
                strategy_row[0]
            )
        )

    else:

        best_strategy = "—"

    text = (

        "🏆 *НЕДЕЛЬНАЯ СТАТИСТИКА*\n\n"

        f"📅 {key}\n\n"

        f"🎯 Сигналов: `{total}`\n"
        f"🏆 TP3: `{wins}`\n"
        f"🛑 STOP: `{losses}`\n"
        f"🔴 Отменено: `{expired}`\n\n"

        f"📈 Winrate: `{winrate:.1f}%`\n"
        f"💰 Результат: `{total_r:+.2f}R`\n\n"

        f"🥇 Лучшая стратегия:\n"
        f"`{best_strategy}`\n\n"

        "🔥 *Неделя закончена.*\n"
        "🚀 *Новая неделя — новые возможности.*"
    )

    try:

        bot.send_message(
            CHANNEL_ID,
            text,
            parse_mode="Markdown"
        )

        last_weekly_stats_key = key

    except Exception:

        log.exception(
            "Ошибка недельной статистики"
        )


# ============================================================
# STARTUP
# ============================================================

def startup():

    text = (

        "🚀 *QUANTUM SCALPER V3 ONLINE*\n\n"

        "🟢 OKX — ONLINE\n"
        "🟢 Telegram — ONLINE\n"
        "🟢 Market Scanner — RUNNING\n\n"

        "🧠 *РЕЖИМ: АНАЛИЗ*\n\n"

        "💧 Ликвидность от: "
        f"`{fmt_usd(MIN_24H_VOLUME_USD)}` / 24H\n"

        "📊 Таймфреймы: "
        "`1H / 15M / 5M`\n\n"

        "🔥 *Стратегии:*\n"
        "• Пробой + объём\n"
        "• Сжатие → пробой\n"
        "• Импульсный пробой\n\n"

        f"⭐ Минимальный Score: "
        f"`{MIN_SCORE}/100`\n"

        f"⏱ TTL: "
        f"`{READY_TTL_MINUTES} мин`\n"

        f"🔒 Cooldown пары: "
        f"`{COOLDOWN_MINUTES} мин`\n\n"

        "🚫 *Бот НЕ торгует.*\n"
        "Он только анализирует рынок "
        "и публикует сигналы."
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

    global scan_count
    global signals_today

    scan_count += 1

    log.info(
        "========== СКАН #%s ==========",
        scan_count
    )

    # --------------------------------------------------------
    # Получаем все SWAP
    # --------------------------------------------------------

    tickers = get_tickers()

    # --------------------------------------------------------
    # Никакого MAX_SYMBOLS.
    #
    # Берём ВСЕ USDT-SWAP >= $100M.
    # --------------------------------------------------------

    liquid = [

        (
            inst_id,
            data
        )

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

    log.info(
        "Всего SWAP: %s | "
        ">= $100M: %s",
        len(tickers),
        len(liquid)
    )

    # ========================================================
    # Сначала сопровождаем существующие сигналы.
    # ========================================================

    for inst_id, active in list(
        active_signals.items()
    ):

        ticker = tickers.get(
            inst_id
        )

        if not ticker:
            continue

        try:

            manage_active_signal(
                inst_id,
                ticker["last"]
            )

        except Exception:

            log.exception(
                "Ошибка сопровождения %s",
                inst_id
            )

    # ========================================================
    # Анализ всех ликвидных монет.
    # ========================================================

    for inst_id, ticker in liquid:

        try:

            if inst_id in active_signals:

                continue

            if not can_create_signal(
                inst_id
            ):

                continue

            # ------------------------------------------------
            # 5M получаем первой.
            #
            # Это дешёвый предварительный фильтр.
            # Только перспективные монеты получают
            # дополнительные 15M/1H запросы.
            # ------------------------------------------------

            candles_5m = get_candles(
                inst_id,
                "5m",
                100
            )

            if not fast_5m_candidate(
                candles_5m
            ):

                continue

            # ------------------------------------------------
            # MULTI-TIMEFRAME
            # ------------------------------------------------

            candles_15m = get_candles(
                inst_id,
                "15m",
                100
            )

            candles_1h = get_candles(
                inst_id,
                "1H",
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
                "🔥 SIGNAL | %s | %s | %s | score=%s | vol=%s",
                setup.coin,
                setup.direction,
                setup.strategy,
                setup.score,
                fmt_usd(
                    setup.volume_24h
                )
            )

            # ------------------------------------------------
            # Публикуем ГРАФИК.
            # ------------------------------------------------

            photo_id, text_id = (
                publish_signal(
                    setup
                )
            )

            if not text_id:

                log.warning(
                    "Telegram не отправил сигнал %s",
                    setup.coin
                )

                continue

            created = now_ts()

            # ------------------------------------------------
            # Внутренний ID.
            #
            # Он НЕ показывается в Telegram.
            # Нужен только базе для сопровождения.
            # ------------------------------------------------

            signal_id = (
                f"{int(created)}_"
                f"{setup.inst_id}"
            )

            active = ActiveSignal(

                setup=setup,

                signal_id=signal_id,

                created_at=created,

                expires_at=(
                    created
                    + READY_TTL_MINUTES
                    * 60
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
                "READY: %s | %s",
                setup.coin,
                setup.score
            )

            # Небольшая пауза,
            # чтобы не создавать всплеск запросов.

            time.sleep(
                0.25
            )

        except Exception:

            log.exception(
                "Ошибка анализа %s",
                inst_id
            )

            continue


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
                "Scanner: 🟢 RUNNING\n\n"

                f"💧 Порог ликвидности: "
                f"`{fmt_usd(MIN_24H_VOLUME_USD)}`\n"

                f"🔥 Активных сетапов: "
                f"`{len(active_signals)}`\n"

                f"🎯 Сигналов сегодня: "
                f"`{signals_today}`\n"

                f"🔎 Сканов: "
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
        "QUANTUM SCALPER V3 START"
    )

    log.info(
        "Timezone: %s",
        TIMEZONE
    )

    log.info(
        "Min volume: %s",
        fmt_usd(
            MIN_24H_VOLUME_USD
        )
    )

    log.info(
        "Min score: %s",
        MIN_SCORE
    )

    log.info(
        "======================================"
    )

    # --------------------------------------------------------
    # Telegram
    # --------------------------------------------------------

    startup()

    # --------------------------------------------------------
    # Проверка OKX
    # --------------------------------------------------------

    try:

        tickers = get_tickers()

        liquid_count = sum(

            1

            for data
            in tickers.values()

            if (
                data["volume_24h"]
                >= MIN_24H_VOLUME_USD
            )
        )

        log.info(
            "OKX ONLINE | "
            "SWAP=%s | "
            "liquid=%s",
            len(tickers),
            liquid_count
        )

    except Exception:

        log.exception(
            "OKX не отвечает"
        )

    # --------------------------------------------------------
    # MAIN LOOP
    # --------------------------------------------------------

    while True:

        started = now_ts()

        try:

            morning_message()

            daily_stats()

            weekly_stats()

            expire_ready()

            scan_market()

            elapsed = (
                now_ts()
                - started
            )

            sleep_for = max(
                5,
                SCAN_INTERVAL_SECONDS
                - int(elapsed)
            )

            log.info(
                "Скан завершён за %.1fs. "
                "Следующий через %ss.",
                elapsed,
                sleep_for
            )

            time.sleep(
                sleep_for
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

            time.sleep(15)


# ============================================================
# ENTRY
# ============================================================

if __name__ == "__main__":

    main()
