import os
import time
import math
import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, List, Dict, Tuple, Callable

import requests
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

# ============================================================
# QUANTUM INTRADAY SWING ENGINE V3
# ============================================================
# 5M = trigger / entry timing
# 15M = setup structure / levels
# 1H = context
#
# Four genuinely different searches:
#   1. TREND PULLBACK
#   2. BREAKOUT + RETEST
#   3. EXTREME REVERSAL
#   4. PRE-BREAKOUT / LEVEL PRESSURE
#
# Important architecture rule:
# Each strategy searches for its OWN setup. There is no single
# generic "setup search" reused by all four strategies.
# Common validation is applied only after a strategy has produced
# a candidate.
# ============================================================

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', os.getenv('BOT_TOKEN', '')).strip()
CHANNEL_ID = os.getenv('CHANNEL_ID', '').strip()
OKX_BASE_URL = os.getenv('OKX_BASE_URL', 'https://www.okx.com').rstrip('/')
TIMEZONE = os.getenv('BOT_TIMEZONE', 'Europe/Kyiv')
DB_PATH = os.getenv('DB_PATH', 'quantum_swing.db')

if not TELEGRAM_TOKEN:
    raise RuntimeError('TELEGRAM_TOKEN is missing.')
if not CHANNEL_ID:
    raise RuntimeError('CHANNEL_ID is missing.')

# ---------------- market discovery ----------------
MIN_24H_TURNOVER_USD = float(os.getenv('MIN_24H_TURNOVER_USD', '5000000'))
PREFERRED_TURNOVER_USD = float(os.getenv('PREFERRED_TURNOVER_USD', '15000000'))
TOP_GAINERS = int(os.getenv('TOP_GAINERS', '25'))
TOP_LOSERS = int(os.getenv('TOP_LOSERS', '25'))
TOP_VOLATILE = int(os.getenv('TOP_VOLATILE', '35'))
NEW_ACTIVE_DAYS = int(os.getenv('NEW_ACTIVE_DAYS', '45'))
NEW_ACTIVE_COUNT = int(os.getenv('NEW_ACTIVE_COUNT', '20'))
DEEP_SCAN_LIMIT = int(os.getenv('DEEP_SCAN_LIMIT', '90'))
MIN_ACTIVE_24H_RANGE_PCT = float(os.getenv('MIN_ACTIVE_24H_RANGE_PCT', '4.0'))

# ---------------- activity / strategy thresholds ----------------
MIN_24H_MOVE_FOR_REVERSAL = float(os.getenv('MIN_24H_MOVE_FOR_REVERSAL', '12'))
STRONG_24H_MOVE = float(os.getenv('STRONG_24H_MOVE', '25'))
MIN_2H_IMPULSE = float(os.getenv('MIN_2H_IMPULSE', '4'))
MIN_ATR_5M_PCT = float(os.getenv('MIN_ATR_5M_PCT', '0.15'))
MIN_ATR_15M_PCT = float(os.getenv('MIN_ATR_15M_PCT', '0.35'))
MIN_VOLUME_RATIO = float(os.getenv('MIN_VOLUME_RATIO', '1.10'))
MIN_REVERSAL_VOLUME_RATIO = float(os.getenv('MIN_REVERSAL_VOLUME_RATIO', '1.10'))
MIN_BREAKOUT_VOLUME = float(os.getenv('MIN_BREAKOUT_VOLUME', '1.25'))

# ---------------- risk / entry ----------------
MAX_ENTRY_CHASE_PCT = float(os.getenv('MAX_ENTRY_CHASE_PCT', '1.00'))
MAX_RISK_PCT = float(os.getenv('MAX_RISK_PCT', '1.00'))
MIN_RISK_PCT = float(os.getenv('MIN_RISK_PCT', '0.20'))
TP1_R = float(os.getenv('TP1_R', '1.20'))
TP2_R = float(os.getenv('TP2_R', '2.00'))
TP3_R = float(os.getenv('TP3_R', '3.00'))
MIN_RR_TP2 = float(os.getenv('MIN_RR_TP2', '1.70'))
MIN_SCORE = int(os.getenv('MIN_SCORE', '78'))

# ---------------- runtime ----------------
SCAN_INTERVAL_SECONDS = int(os.getenv('SCAN_INTERVAL_SECONDS', '30'))
HTTP_TIMEOUT = int(os.getenv('HTTP_TIMEOUT', '10'))
REQUEST_RETRIES = int(os.getenv('REQUEST_RETRIES', '3'))
CANDLE_CACHE_SECONDS = int(os.getenv('CANDLE_CACHE_SECONDS', '15'))
READY_TTL_MINUTES = int(os.getenv('READY_TTL_MINUTES', '20'))
ACTIVE_MAX_HOURS = int(os.getenv('ACTIVE_MAX_HOURS', '12'))
COOLDOWN_MINUTES = int(os.getenv('COOLDOWN_MINUTES', '45'))
TELEGRAM_RETRY_SECONDS = int(os.getenv('TELEGRAM_RETRY_SECONDS', '20'))

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
log = logging.getLogger('QUANTUM')

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode='HTML')
session = requests.Session()
session.headers.update({'User-Agent': 'QuantumIntradaySwing/3.0', 'Accept': 'application/json'})

# ============================================================
# DATA TYPES
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
class Pivot:
    index: int
    price: float
    kind: str

@dataclass
class Level:
    price: float
    kind: str
    touches: int
    first_index: int
    last_index: int
    strength: float

@dataclass
class Setup:
    inst_id: str
    coin: str
    direction: str
    strategy: str
    pattern_name: str
    level: float
    level_kind: str
    entry_low: float
    entry_high: float
    sl: float
    tp1: float
    tp2: float
    tp3: float
    score: int
    strategy_score: int
    reason: str
    volume_24h: float
    change24: float
    range24: float
    atr5_pct: float
    atr15_pct: float
    volume_ratio: float
    candles_5m: List[Candle]
    points: List[Tuple[int, float, str]] = field(default_factory=list)
    hold_hours: str = '1–8 ч'

@dataclass
class PendingSignal:
    setup: Setup
    signal_id: int
    created_at: float
    expires_at: float
    message_id: int

@dataclass
class ActiveSignal:
    setup: Setup
    signal_id: int
    activated_at: float
    message_id: int
    tp1_sent: bool = False
    tp2_sent: bool = False
    tp3_sent: bool = False

pending: Dict[str, PendingSignal] = {}
active: Dict[str, ActiveSignal] = {}
sending_symbols = set()
sending_lock = threading.Lock()
candle_cache: Dict[Tuple[str, str], Tuple[float, List[Candle]]] = {}
ticker_cache: Dict[str, Tuple[float, dict]] = {}

# ============================================================
# DATABASE
# ============================================================

def db_connect():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA busy_timeout=5000')
    return con


db = db_connect()
db_lock = threading.Lock()

SIGNAL_COLUMNS = {
    'coin': 'TEXT',
    'pattern_name': 'TEXT',
    'level_kind': 'TEXT',
    'reason': 'TEXT',
    'volume_24h': 'REAL DEFAULT 0',
    'change24': 'REAL DEFAULT 0',
    'range24': 'REAL DEFAULT 0',
    'atr5_pct': 'REAL DEFAULT 0',
    'atr15_pct': 'REAL DEFAULT 0',
    'volume_ratio': 'REAL DEFAULT 0',
    'hold_hours': 'TEXT DEFAULT \'1–8 ч\'',
    'message_id': 'INTEGER DEFAULT 0',
    'strategy_score': 'INTEGER DEFAULT 0',
}


def init_db():
    with db_lock:
        db.execute('''CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inst_id TEXT, direction TEXT, strategy TEXT, level REAL,
            entry_low REAL, entry_high REAL, sl REAL, tp1 REAL, tp2 REAL, tp3 REAL,
            score INTEGER, status TEXT, created_at REAL, activated_at REAL,
            expires_at REAL, tp1_hit INTEGER DEFAULT 0, tp2_hit INTEGER DEFAULT 0,
            tp3_hit INTEGER DEFAULT 0, result TEXT DEFAULT '', closed_at REAL,
            r_multiple REAL DEFAULT 0
        )''')
        cols = {r[1] for r in db.execute('PRAGMA table_info(signals)').fetchall()}
        for name, typ in SIGNAL_COLUMNS.items():
            if name not in cols:
                db.execute(f'ALTER TABLE signals ADD COLUMN {name} {typ}')
        db.execute('''CREATE TABLE IF NOT EXISTS signal_votes (
            signal_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            vote TEXT NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY(signal_id, user_id)
        )''')
        db.execute('''CREATE TABLE IF NOT EXISTS bot_state (
            key TEXT PRIMARY KEY, value TEXT NOT NULL
        )''')
        db.commit()


init_db()

# ============================================================
# BASIC HELPERS
# ============================================================

def now_ts() -> float:
    return time.time()


def local_now() -> datetime:
    return datetime.now(ZoneInfo(TIMEZONE))


def get_coin(inst_id: str) -> str:
    return inst_id.replace('-USDT-SWAP', '')


def fmt_price(p: float) -> str:
    if p >= 1000:
        return f'{p:,.2f}'.replace(',', ' ')
    if p >= 100:
        return f'{p:,.2f}'.replace(',', ' ')
    if p >= 1:
        return f'{p:,.4f}'.replace(',', ' ')
    if p >= 0.01:
        return f'{p:.6f}'.rstrip('0').rstrip('.')
    return f'{p:.10f}'.rstrip('0').rstrip('.')


def pct_move(new: float, old: float) -> float:
    return (new - old) / max(abs(old), 1e-12) * 100


def clamp(x: float, a: float, b: float) -> float:
    return max(a, min(b, x))


def ema(vals: List[float], period: int) -> List[float]:
    if not vals:
        return []
    k = 2 / (period + 1)
    out = [vals[0]]
    for x in vals[1:]:
        out.append(x * k + out[-1] * (1 - k))
    return out


def atr(cs: List[Candle], period: int = 14) -> float:
    if len(cs) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(cs)):
        c, p = cs[i], cs[i - 1]
        trs.append(max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)))
    return sum(trs[-period:]) / period


def atr_pct(cs: List[Candle], period: int = 14) -> float:
    return atr(cs, period) / max(cs[-1].close, 1e-12) * 100 if cs else 0.0


def vol_ratio(cs: List[Candle], lookback: int = 20) -> float:
    if len(cs) < lookback + 1:
        return 0.0
    cur = cs[-1].quote_volume
    hist = [c.quote_volume for c in cs[-lookback-1:-1] if c.quote_volume > 0]
    return cur / (sum(hist) / len(hist)) if hist else 0.0


def body_ratio(c: Candle) -> float:
    r = c.high - c.low
    return abs(c.close - c.open) / r if r > 0 else 0.0


def close_location(c: Candle) -> float:
    r = c.high - c.low
    return (c.close - c.low) / r if r > 0 else 0.5


def wick_ratio(c: Candle) -> Tuple[float, float]:
    r = max(c.high - c.low, 1e-12)
    return (
        (c.high - max(c.open, c.close)) / r,
        (min(c.open, c.close) - c.low) / r,
    )


def confirmed_candles(cs: List[Candle]) -> List[Candle]:
    # Strategy generation must never use the live/unconfirmed candle.
    return [c for c in cs if c.confirmed]


def candle_direction(c: Candle) -> str:
    return 'LONG' if c.close >= c.open else 'SHORT'

# ============================================================
# OKX API
# ============================================================

def okx_get(path: str, params: dict) -> dict:
    err = None
    for attempt in range(1, REQUEST_RETRIES + 1):
        try:
            r = session.get(OKX_BASE_URL + path, params=params, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            payload = r.json()
            if str(payload.get('code', '')) != '0':
                raise RuntimeError(payload.get('msg', 'OKX error'))
            return payload
        except Exception as exc:
            err = exc
            log.warning('OKX FAILED | %s | %s/%s | %s', path, attempt, REQUEST_RETRIES, exc)
            if attempt < REQUEST_RETRIES:
                time.sleep(attempt * 0.7)
    raise RuntimeError(f'OKX request failed: {err}')


def get_instruments() -> Dict[str, dict]:
    data = okx_get('/api/v5/public/instruments', {'instType': 'SWAP'}).get('data', [])
    out = {}
    for x in data:
        iid = str(x.get('instId', ''))
        if not iid.endswith('-USDT-SWAP') or x.get('state') != 'live':
            continue
        try:
            out[iid] = {
                'tickSz': float(x.get('tickSz') or 0),
                'lotSz': float(x.get('lotSz') or 0),
                'listTime': int(x.get('listTime') or 0),
            }
        except (TypeError, ValueError):
            out[iid] = {}
    return out


def _ticker_from_row(x: dict) -> Optional[dict]:
    try:
        iid = str(x.get('instId', ''))
        last = float(x.get('last') or 0)
        op = float(x.get('open24h') or 0)
        hi = float(x.get('high24h') or 0)
        lo = float(x.get('low24h') or 0)
        vol = float(x.get('vol24h') or 0)
        vol_ccy = float(x.get('volCcy24h') or 0)
        if last <= 0 or op <= 0 or not iid.endswith('-USDT-SWAP'):
            return None
        # OKX SWAP ticker units matter here:
        #   volCcy24h = base-currency volume (e.g. ETH),
        #   vol24h    = number of contracts.
        # Therefore volCcy24h * last estimates USDT notional turnover.
        # Multiplying contract count by last is WRONG without ctVal, and using
        # volCcy24h as if it were already USD badly understates most altcoins.
        turnover = vol_ccy * last if vol_ccy > 0 else 0.0
        if not math.isfinite(turnover) or turnover < 0:
            turnover = 0.0
        return {
            'last': last, 'open24h': op, 'high24h': hi, 'low24h': lo,
            'vol24h_usd': turnover,
            'change24h_pct': (last / op - 1) * 100,
            'range24h_pct': (hi - lo) / last * 100 if hi > lo else 0.0,
            'ts': int(x.get('ts') or 0),
        }
    except (TypeError, ValueError):
        return None


def get_tickers() -> Dict[str, dict]:
    data = okx_get('/api/v5/market/tickers', {'instType': 'SWAP'}).get('data', [])
    out = {}
    for x in data:
        t = _ticker_from_row(x)
        if t:
            out[str(x.get('instId'))] = t
            ticker_cache[str(x.get('instId'))] = (now_ts(), t)
    return out


def get_ticker(inst_id: str) -> Optional[dict]:
    cached = ticker_cache.get(inst_id)
    if cached and now_ts() - cached[0] < 3:
        return cached[1]
    try:
        data = okx_get('/api/v5/market/ticker', {'instId': inst_id}).get('data', [])
        if not data:
            return None
        t = _ticker_from_row(data[0])
        if t:
            ticker_cache[inst_id] = (now_ts(), t)
        return t
    except Exception:
        return None


def get_candles(inst_id: str, bar: str, limit: int = 180) -> List[Candle]:
    key = (inst_id, bar)
    cached = candle_cache.get(key)
    if cached and now_ts() - cached[0] < CANDLE_CACHE_SECONDS:
        return cached[1]
    rows = okx_get('/api/v5/market/candles', {
        'instId': inst_id, 'bar': bar, 'limit': str(min(limit, 300))
    }).get('data', [])
    out: List[Candle] = []
    for r in reversed(rows):
        try:
            out.append(Candle(
                int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]),
                float(r[5] or 0), float(r[7] or 0), str(r[8]) == '1'
            ))
        except (IndexError, TypeError, ValueError):
            continue
    candle_cache[key] = (now_ts(), out)
    return out


def load_symbol(inst_id: str) -> Dict[str, List[Candle]]:
    return {
        '1h': get_candles(inst_id, '1H', 160),
        '15m': get_candles(inst_id, '15m', 220),
        '5m': get_candles(inst_id, '5m', 240),
    }


def market_data_is_fresh(data: Dict[str, List[Candle]]) -> bool:
    """Reject stale API snapshots; candle timestamps are in milliseconds."""
    now_ms = int(time.time() * 1000)
    # A confirmed candle timestamp is its opening time, so allow the interval
    # duration plus a modest exchange/cache delay.
    limits_ms = {'5m': 12 * 60_000, '15m': 32 * 60_000, '1h': 125 * 60_000}
    for tf, max_age in limits_ms.items():
        candles = confirmed_candles(data.get(tf, []))
        if not candles:
            return False
        age = now_ms - candles[-1].ts
        if age < -60_000 or age > max_age:
            log.debug('STALE CANDLES | tf=%s | age_ms=%s', tf, age)
            return False
    return True

# ============================================================
# UNIVERSE — broad market discovery, NOT strategy-specific
# ============================================================

def build_universe(instruments: Dict[str, dict], tickers: Dict[str, dict]):
    """Rank liquid, active swaps by balanced opportunity, not 24H winners alone.

    The scanner still uses DEEP_SCAN_LIMIT as a per-cycle workload batch, not a
    signal quota. A broad ranking avoids filling the scan with only the biggest
    24H pumps/dumps and gives liquid mid-movers a chance to be evaluated.
    """
    base = []
    for iid, t in tickers.items():
        if iid not in instruments or not iid.endswith('-USDT-SWAP'):
            continue
        turnover = max(float(t.get('vol24h_usd', 0.0)), 0.0)
        move = abs(float(t.get('change24h_pct', 0.0)))
        day_range = max(float(t.get('range24h_pct', 0.0)), 0.0)
        if turnover < MIN_24H_TURNOVER_USD:
            continue

        # Log scaling rewards real liquidity without letting a few mega-caps
        # dominate; momentum is useful but saturates so late vertical pumps do
        # not automatically outrank coins forming a fresh structure.
        liquidity = clamp(math.log10(max(turnover, 1.0) / max(MIN_24H_TURNOVER_USD, 1.0)) * 7.0, 0, 14)
        movement = clamp(move * 0.85, 0, 18)
        active_range = clamp(day_range * 1.15, 0, 22)
        balanced_motion = max(0.0, 10.0 - abs(day_range - max(move, 3.0)) * 0.35)
        score = liquidity + movement + active_range + balanced_motion
        base.append((iid, t, score))

    # Rank the whole eligible universe. scan_market rotates through batches so
    # coins outside the highest-ranked batch are still inspected on later cycles.
    ranked = sorted(base, key=lambda z: z[2], reverse=True)
    log.info('UNIVERSE | eligible=%d', len(ranked))
    return ranked

# ============================================================
# STRUCTURE / LEVEL ENGINE
# ============================================================

def pivots(cs: List[Candle], left: int = 2, right: int = 2) -> List[Pivot]:
    out = []
    if len(cs) < left + right + 1:
        return out
    for i in range(left, len(cs) - right):
        window = cs[i-left:i+right+1]
        if cs[i].high >= max(c.high for c in window):
            out.append(Pivot(i, cs[i].high, 'HIGH'))
        if cs[i].low <= min(c.low for c in window):
            out.append(Pivot(i, cs[i].low, 'LOW'))
    return out


def recent_pivots(cs: List[Candle], lookback: int = 100) -> List[Pivot]:
    start = max(0, len(cs) - lookback)
    raw = pivots(cs[start:], 2, 2)
    return [Pivot(p.index + start, p.price, p.kind) for p in raw]


def cluster_levels(cs: List[Candle], tolerance_pct: float = 0.30) -> List[Level]:
    ps = recent_pivots(cs, min(120, len(cs)))
    levels: List[Level] = []
    for p in ps:
        merged = False
        for j, lv in enumerate(levels):
            if lv.kind != p.kind:
                continue
            if abs(p.price - lv.price) / max(lv.price, 1e-12) * 100 <= tolerance_pct:
                touches = lv.touches + 1
                price = (lv.price * lv.touches + p.price) / touches
                levels[j] = Level(price, lv.kind, touches,
                                  min(lv.first_index, p.index),
                                  max(lv.last_index, p.index),
                                  min(100.0, 25 + touches * 18 + (lv.last_index - lv.first_index) * 0.10))
                merged = True
                break
        if not merged:
            levels.append(Level(p.price, p.kind, 1, p.index, p.index, 43.0))
    return sorted(levels, key=lambda x: (x.touches, x.strength), reverse=True)


def nearest_levels(price: float, levels: List[Level]):
    below = sorted([x for x in levels if x.price < price], key=lambda x: x.price, reverse=True)
    above = sorted([x for x in levels if x.price > price], key=lambda x: x.price)
    return (below[0] if below else None, above[0] if above else None)


def trend_state(cs: List[Candle]) -> Optional[str]:
    if len(cs) < 60:
        return None
    closes = [c.close for c in cs]
    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    slope20 = pct_move(e20[-1], e20[-8])
    if e20[-1] > e50[-1] and slope20 > 0.15:
        return 'LONG'
    if e20[-1] < e50[-1] and slope20 < -0.15:
        return 'SHORT'
    return None


def impulse_stats(c5: List[Candle]):
    def move(n):
        if len(c5) <= n:
            return 0.0
        return pct_move(c5[-1].close, c5[-1-n].close)
    return move(12), move(24), move(36)


def local_extreme(c5: List[Candle], direction: str, lookback: int = 60):
    x = c5[-lookback-1:-1] if len(c5) > lookback + 1 else c5[:-1]
    if not x:
        return None
    return (min(x, key=lambda c: c.low) if direction == 'LONG'
            else max(x, key=lambda c: c.high))


def range_pct(cs: List[Candle]) -> float:
    if not cs:
        return 0.0
    hi = max(c.high for c in cs)
    lo = min(c.low for c in cs)
    return (hi - lo) / max(cs[-1].close, 1e-12) * 100

# ============================================================
# COMMON QUALITY / RISK
# ============================================================

def market_score(
    ticker: dict,
    c5: List[Candle],
    c15: List[Candle],
    vr: float,
    c1h: Optional[List[Candle]] = None,
    direction: Optional[str] = None,
    strategy: Optional[str] = None,
) -> int:
    """Continuous market-quality score; structural pattern checks stay in builders."""
    score = 48.0
    turnover = max(float(ticker.get('vol24h_usd', 0.0)), 1.0)
    score += clamp(math.log10(turnover / max(MIN_24H_TURNOVER_USD, 1.0)) * 3.2, 0, 9)
    score += clamp(atr_pct(c5) / max(MIN_ATR_5M_PCT, 0.01), 0, 2.2) * 2.0
    score += clamp(atr_pct(c15) / max(MIN_ATR_15M_PCT, 0.01), 0, 2.0) * 2.0
    score += clamp(math.log(max(vr, 0.05), 1.7) * 4.0, -5, 9)

    # Reward aligned multi-timeframe structure; do not hard-reject a setup just
    # because a timeframe is neutral or mildly opposed.
    if direction in ('LONG', 'SHORT'):
        sign = 1 if direction == 'LONG' else -1
        for candles, weight in ((c5, 3.0), (c15, 4.0), (c1h or [], 5.0)):
            if len(candles) < 25:
                continue
            closes = [c.close for c in candles]
            e20, e50 = ema(closes, 20), ema(closes, min(50, max(20, len(closes)-1)))
            slope = pct_move(e20[-1], e20[-5]) if len(e20) >= 5 else 0.0
            alignment = sign * slope
            if sign * (e20[-1] - e50[-1]) > 0:
                score += weight * 0.55
            elif sign * (e20[-1] - e50[-1]) < 0:
                score -= weight * 0.30
            score += clamp(alignment * 3.0, -weight * 0.45, weight * 0.45)

        cur = c5[-1] if c5 else None
        if cur:
            loc = close_location(cur)
            directional_loc = loc if direction == 'LONG' else 1.0 - loc
            score += (directional_loc - 0.5) * 8.0
            if candle_direction(cur) == direction:
                score += min(3.0, body_ratio(cur) * 3.0)

        # For continuation strategies, 24H direction should support the trade;
        # reversals are intentionally evaluated against the old move.
        if strategy != 'EXTREME REVERSAL':
            signed_day_move = sign * float(ticker.get('change24h_pct', 0.0))
            score += clamp(signed_day_move * 0.18, -5.0, 5.0)

    # Avoid treating extreme 24H movement as quality by itself.
    day_move = abs(float(ticker.get('change24h_pct', 0.0)))
    if day_move > 35 and strategy != 'EXTREME REVERSAL':
        score -= clamp((day_move - 35) * 0.10, 0, 5)
    return int(clamp(round(score), 0, 99))


def validate_zone_risk(direction: str, entry_low: float, entry_high: float, sl: float) -> bool:
    # Worst-case entry, not midpoint. This prevents a signal from exceeding 1%
    # risk when the user enters at the unfavorable edge of the zone.
    if direction == 'LONG':
        if sl >= entry_low:
            return False
        worst_entry = entry_high
    else:
        if sl <= entry_high:
            return False
        worst_entry = entry_low
    risk_pct = abs(worst_entry - sl) / max(worst_entry, 1e-12) * 100
    return MIN_RISK_PCT <= risk_pct <= MAX_RISK_PCT


def target_levels(
    direction: str,
    entry: float,
    sl: float,
    c15: List[Candle],
    preferred_targets: Optional[List[float]] = None,
    min_target_pcts: Optional[Tuple[float, float, float]] = None,
):
    """Build ordered targets without accepting levels clustered right at entry.

    min_target_pcts is used by PRE-BREAKOUT because the trigger level itself is
    not a meaningful profit target; there must be room beyond that level.
    """
    risk = abs(entry - sl)
    if risk <= 0 or entry <= 0:
        return None
    levels = cluster_levels(c15)
    sign = 1 if direction == 'LONG' else -1
    structural = sorted(
        [lv.price for lv in levels if sign * (lv.price - entry) > 0],
        reverse=(direction == 'SHORT')
    )
    candidates = []
    if preferred_targets:
        candidates.extend(preferred_targets)
    candidates.extend(structural)

    # Baseline targets are risk-based, but for PRE-BREAKOUT also require enough
    # absolute price movement to make the signal worthwhile.
    floors = min_target_pcts or (0.0, 0.0, 0.0)
    targets = [
        entry + sign * max(risk * TP1_R, entry * floors[0]),
        entry + sign * max(risk * TP2_R, entry * floors[1]),
        entry + sign * max(risk * TP3_R, entry * floors[2]),
    ]

    valid = []
    target_r_multiples = (TP1_R, TP2_R, TP3_R)
    for p in candidates:
        move = sign * (p - entry)
        slot = min(len(valid), 2)
        required_move = max(risk * target_r_multiples[slot], entry * floors[slot])
        if move < required_move:
            continue
        # Discard duplicate/near-identical levels; they create meaningless TP2/TP3.
        if valid and abs(p - valid[-1]) / entry < 0.0035:
            continue
        valid.append(p)
        if len(valid) == 3:
            break

    for i in range(min(len(valid), 3)):
        targets[i] = valid[i]

    # Keep targets meaningfully separated. A tight cluster of structural levels
    # must not produce three nearly identical exit prices.
    min_gap = max(risk * 0.45, entry * 0.0035)
    if direction == 'LONG':
        targets[0] = max(targets[0], entry + max(risk, entry * floors[0]))
        targets[1] = max(targets[1], targets[0] + min_gap, entry + entry * floors[1])
        targets[2] = max(targets[2], targets[1] + min_gap, entry + entry * floors[2])
    else:
        targets[0] = min(targets[0], entry - max(risk, entry * floors[0]))
        targets[1] = min(targets[1], targets[0] - min_gap, entry - entry * floors[1])
        targets[2] = min(targets[2], targets[1] - min_gap, entry - entry * floors[2])
    return tuple(targets)


def common_validate(setup: Setup) -> bool:
    if not (setup.entry_low > 0 and setup.entry_high > 0 and setup.sl > 0):
        return False
    if setup.entry_low > setup.entry_high:
        return False
    if not validate_zone_risk(setup.direction, setup.entry_low, setup.entry_high, setup.sl):
        return False
    # Evaluate RR at the unfavorable edge of the entry zone, not the midpoint.
    worst_entry = setup.entry_high if setup.direction == 'LONG' else setup.entry_low
    risk = abs(worst_entry - setup.sl)
    if risk <= 0:
        return False
    reward2 = (setup.tp2 - worst_entry) if setup.direction == 'LONG' else (worst_entry - setup.tp2)
    rr2 = reward2 / risk
    if rr2 < MIN_RR_TP2:
        return False
    if setup.direction == 'LONG':
        if not (setup.sl < setup.entry_low < setup.entry_high < setup.tp1 < setup.tp2 < setup.tp3):
            return False
    else:
        if not (setup.tp3 < setup.tp2 < setup.tp1 < setup.entry_low < setup.entry_high < setup.sl):
            return False
    if setup.score < MIN_SCORE:
        return False
    return True


def make_entry_zone(center: float, atr5: float, direction: str, max_width_pct: float = 0.30):
    width = min(max(atr5 * 0.12, center * 0.00018), center * max_width_pct / 100)
    return center - width, center + width


def strategy_score(base: int, bonuses: List[int], penalties: List[int]) -> int:
    return int(clamp(base + sum(bonuses) - sum(penalties), 0, 99))

# ============================================================
# STRATEGY 1 — TREND PULLBACK
# ============================================================

def build_pullback(iid, ticker, data) -> Optional[Setup]:
    c5 = confirmed_candles(data['5m'])
    c15 = confirmed_candles(data['15m'])
    c1h = confirmed_candles(data['1h'])
    if min(len(c5), len(c15), len(c1h)) < 80:
        return None

    trend = trend_state(c1h)
    if not trend:
        return None

    # Strategy-specific search: locate a meaningful recent impulse, then a
    # retracement toward 15M EMA20 / prior structure, then a 5M reclaim.
    closes15 = [c.close for c in c15]
    e20 = ema(closes15, 20)
    recent15 = c15[-14:-1]
    if len(recent15) < 8:
        return None
    vr = vol_ratio(c5)
    current = c5[-1]
    previous = c5[-2]
    atr5 = atr(c5)
    # A pullback can form on quiet volume, but the actual 5M trigger should
    # not be an almost inactive candle.
    if vr < float(os.getenv('MIN_PULLBACK_TRIGGER_VOLUME', '0.55')):
        return None

    if trend == 'LONG':
        impulse_window = c15[-26:-12]
        if len(impulse_window) < 8:
            return None
        impulse = pct_move(max(c.close for c in impulse_window), min(c.close for c in impulse_window))
        pull_low = min(c.low for c in recent15)
        touched_ema = any(c.low <= e20[-1] * 1.004 for c in recent15)
        held_structure = pull_low > min(c.low for c in c15[-35:-14]) * 0.998
        trigger = (current.close > current.open and
                   current.close > previous.high and
                   current.close > e20[-1])
        if impulse < 2.2 or not touched_ema or not held_structure or not trigger:
            return None
        # Do not enter at the top of a fresh impulse. The trigger must still be
        # reasonably close to the pullback base.
        entry_center = current.close
        if pct_move(entry_center, pull_low) > 2.2:
            return None
        sl = pull_low - atr5 * 0.28
        level = pull_low
        preferred = []
        above = sorted(lv.price for lv in cluster_levels(c15) if lv.price > entry_center)
        if above:
            preferred = [above[0]]
        reason = (f'1H LONG → 15M импульс {impulse:.1f}% → откат к EMA20/структуре → '
                  f'5M reclaim; объём x{vr:.2f}')
        points = [(len(c5[-96:]) - 3, pull_low, 'PULLBACK'),
                  (len(c5[-96:]) - 1, entry_center, 'TRIGGER')]
    else:
        impulse_window = c15[-26:-12]
        if len(impulse_window) < 8:
            return None
        impulse = abs(pct_move(min(c.close for c in impulse_window), max(c.close for c in impulse_window)))
        pull_high = max(c.high for c in recent15)
        touched_ema = any(c.high >= e20[-1] * 0.996 for c in recent15)
        held_structure = pull_high < max(c.high for c in c15[-35:-14]) * 1.002
        trigger = (current.close < current.open and
                   current.close < previous.low and
                   current.close < e20[-1])
        if impulse < 2.2 or not touched_ema or not held_structure or not trigger:
            return None
        entry_center = current.close
        if pct_move(pull_high, entry_center) > 2.2:
            return None
        sl = pull_high + atr5 * 0.28
        level = pull_high
        below = sorted((lv.price for lv in cluster_levels(c15) if lv.price < entry_center), reverse=True)
        preferred = [below[0]] if below else []
        reason = (f'1H SHORT → 15M импульс {impulse:.1f}% → откат к EMA20/структуре → '
                  f'5M reclaim вниз; объём x{vr:.2f}')
        points = [(len(c5[-96:]) - 3, pull_high, 'PULLBACK'),
                  (len(c5[-96:]) - 1, entry_center, 'TRIGGER')]

    lo, hi = make_entry_zone(entry_center, atr5, trend)
    if trend == 'LONG':
        lo = max(lo, level - atr5 * 0.15)
    else:
        hi = min(hi, level + atr5 * 0.15)
    tps = target_levels(trend, (lo + hi) / 2, sl, c15, preferred)
    if not tps:
        return None
    base = market_score(ticker, c5, c15, vr, c1h, trend, 'TREND PULLBACK')
    ss = strategy_score(base, [8, 5 if vr >= 1.3 else 0, 4], [])
    setup = Setup(iid, get_coin(iid), trend, 'TREND PULLBACK', 'ТРЕНД → ОТКАТ → ПРОДОЛЖЕНИЕ',
                  level, 'PULLBACK STRUCTURE', lo, hi, sl, *tps, ss, ss, reason,
                  ticker['vol24h_usd'], ticker['change24h_pct'], ticker['range24h_pct'],
                  atr_pct(c5), atr_pct(c15), vr, c5[-96:], points, '1–8 ч')
    return setup if common_validate(setup) else None

# ============================================================
# STRATEGY 2 — BREAKOUT + RETEST
# ============================================================

def build_breakout_retest(iid, ticker, data) -> Optional[Setup]:
    c5 = confirmed_candles(data['5m'])
    c15 = confirmed_candles(data['15m'])
    c1h = confirmed_candles(data['1h'])
    if min(len(c5), len(c15), len(c1h)) < 90:
        return None

    levels = cluster_levels(c15, 0.28)
    if not levels:
        return None
    vr = vol_ratio(c5)
    atr5 = atr(c5)
    # Search backward: a breakout candle MUST precede the retest candles.
    # The latest candle is the confirmation, not the original breakout.
    end = len(c5) - 1
    best = None
    best_rank = float('-inf')
    # Require three distinct phases: breakout candle -> at least one holding
    # candle -> retest/confirmation candle. This prevents one candle from being
    # labelled both breakout and retest.
    for retest_idx in range(max(12, end - 7), end + 1):
        confirmation = c5[retest_idx]
        breakout_idx = retest_idx - 2
        hold_idx = retest_idx - 1
        if breakout_idx < 5:
            continue
        holding = c5[hold_idx]
        breakout = c5[breakout_idx]
        for lv in levels:
            if lv.touches < 2:
                continue
            # LONG: breakout closes above resistance, later candle returns to it
            # and holds above it.
            if lv.kind == 'HIGH':
                if not (breakout.close > lv.price and breakout.open <= lv.price * 1.001):
                    continue
                if body_ratio(breakout) < 0.45:
                    continue
                if holding.close <= lv.price:
                    continue
                if not (confirmation.low <= lv.price * 1.004 and confirmation.close > lv.price):
                    continue
                if confirmation.close <= confirmation.open:
                    continue
                if confirmation.close <= holding.close * 0.995:
                    continue
                if retest_idx != end:
                    continue
                direction = 'LONG'
                level = lv.price
                sl = min(lv.price, confirmation.low) - atr5 * 0.25
                entry = confirmation.close
                points = [(breakout_idx - max(0, len(c5) - 96), breakout.close, 'BREAK'),
                          (retest_idx - max(0, len(c5) - 96), level, 'RETEST'),
                          (end - max(0, len(c5) - 96), entry, 'CONFIRM')]
                candidate_rank = (lv.strength * 0.20 + lv.touches * 2.0 +
                                  body_ratio(breakout) * 5.0 -
                                  abs(entry - level) / max(entry, 1e-12) * 100 * 2.0)
                if candidate_rank > best_rank:
                    best_rank = candidate_rank
                    best = (direction, level, sl, entry, lv.touches, points)
            # SHORT: breakout closes below support, later candle retests it.
            if lv.kind == 'LOW':
                if not (breakout.close < lv.price and breakout.open >= lv.price * 0.999):
                    continue
                if body_ratio(breakout) < 0.45:
                    continue
                if holding.close >= lv.price:
                    continue
                if not (confirmation.high >= lv.price * 0.996 and confirmation.close < lv.price):
                    continue
                if confirmation.close >= confirmation.open:
                    continue
                if confirmation.close >= holding.close * 1.005:
                    continue
                if retest_idx != end:
                    continue
                direction = 'SHORT'
                level = lv.price
                sl = max(lv.price, confirmation.high) + atr5 * 0.25
                entry = confirmation.close
                points = [(breakout_idx - max(0, len(c5) - 96), breakout.close, 'BREAK'),
                          (retest_idx - max(0, len(c5) - 96), level, 'RETEST'),
                          (end - max(0, len(c5) - 96), entry, 'CONFIRM')]
                candidate_rank = (lv.strength * 0.20 + lv.touches * 2.0 +
                                  body_ratio(breakout) * 5.0 -
                                  abs(entry - level) / max(entry, 1e-12) * 100 * 2.0)
                if candidate_rank > best_rank:
                    best_rank = candidate_rank
                    best = (direction, level, sl, entry, lv.touches, points)

    if not best or vr < MIN_BREAKOUT_VOLUME:
        return None
    direction, level, sl, entry, touches, points = best
    if abs(entry - level) / max(entry, 1e-12) * 100 > MAX_ENTRY_CHASE_PCT:
        return None
    lo, hi = make_entry_zone(entry, atr5, direction)
    tps = target_levels(direction, (lo + hi) / 2, sl, c15)
    if not tps:
        return None
    base = market_score(ticker, c5, c15, vr, c1h, direction, 'BREAKOUT + RETEST')
    ss = strategy_score(base, [10, min(8, touches * 2), 5 if vr >= 1.6 else 0], [])
    reason = (f'15M уровень {touches}× → отдельная свеча ПРОБОЙ → отдельная свеча РЕТЕСТ → '
              f'5M удержание; объём x{vr:.2f}')
    setup = Setup(iid, get_coin(iid), direction, 'BREAKOUT + RETEST', 'ПРОБОЙ → РЕТЕСТ → УДЕРЖАНИЕ',
                  level, 'BROKEN HORIZONTAL LEVEL', lo, hi, sl, *tps, ss, ss, reason,
                  ticker['vol24h_usd'], ticker['change24h_pct'], ticker['range24h_pct'],
                  atr_pct(c5), atr_pct(c15), vr, c5[-96:], points, '1–6 ч')
    return setup if common_validate(setup) else None

# ============================================================
# STRATEGY 3 — EXTREME REVERSAL
# ============================================================

def build_reversal(iid, ticker, data) -> Optional[Setup]:
    c5 = confirmed_candles(data['5m'])
    c15 = confirmed_candles(data['15m'])
    c1h = confirmed_candles(data['1h'])
    if min(len(c5), len(c15), len(c1h)) < 80:
        return None
    ch = ticker['change24h_pct']
    if abs(ch) < MIN_24H_MOVE_FOR_REVERSAL:
        return None
    if atr_pct(c5) < MIN_ATR_5M_PCT or atr_pct(c15) < MIN_ATR_15M_PCT:
        return None

    _, m2h, _ = impulse_stats(c5)
    direction = 'SHORT' if ch > 0 else 'LONG'
    if direction == 'SHORT' and m2h < MIN_2H_IMPULSE:
        return None
    if direction == 'LONG' and m2h > -MIN_2H_IMPULSE:
        return None

    # Strategy-specific search: exhaustion + rejection + break of the very
    # short-term structure. It does NOT require continuation in the old trend.
    extreme = local_extreme(c5, 'SHORT' if direction == 'SHORT' else 'LONG', 60)
    if extreme is None:
        return None
    cur = c5[-1]
    prev = c5[-2]
    vr = vol_ratio(c5)
    if vr < MIN_REVERSAL_VOLUME_RATIO:
        return None
    upper, lower = wick_ratio(cur)

    if direction == 'SHORT':
        retrace = pct_move(extreme.high, cur.close)
        recent_low = min(c.low for c in c5[-5:-1])
        if retrace < 0.55 or cur.close >= recent_low or cur.close >= cur.open:
            return None
        if body_ratio(cur) < 0.40 or (upper < 0.10 and cur.high < max(c.high for c in c5[-6:-1])):
            return None
        sl = max(extreme.high, max(c.high for c in c5[-4:])) + atr(c5) * 0.25
        level = extreme.high
        pattern = 'ИСТОЩЕНИЕ РОСТА → СЛОМ → SHORT'
        reason = f'рост 24H {ch:+.1f}% → 2H {m2h:+.1f}% → экстремум → rejection → слом 5M'
        points = [(max(0, len(c5[-96:]) - 60), level, 'EXTREME HIGH'),
                  (len(c5[-96:]) - 1, cur.close, 'STRUCTURE BREAK')]
    else:
        retrace = pct_move(cur.close, extreme.low)
        recent_high = max(c.high for c in c5[-5:-1])
        if retrace < 0.55 or cur.close <= recent_high or cur.close <= cur.open:
            return None
        if body_ratio(cur) < 0.40 or (lower < 0.10 and cur.low > min(c.low for c in c5[-6:-1])):
            return None
        sl = min(extreme.low, min(c.low for c in c5[-4:])) - atr(c5) * 0.25
        level = extreme.low
        pattern = 'ИСТОЩЕНИЕ ПАДЕНИЯ → СЛОМ → LONG'
        reason = f'падение 24H {ch:+.1f}% → 2H {m2h:+.1f}% → экстремум → rejection → слом 5M'
        points = [(max(0, len(c5[-96:]) - 60), level, 'EXTREME LOW'),
                  (len(c5[-96:]) - 1, cur.close, 'STRUCTURE BREAK')]

    entry = cur.close
    if abs(entry - level) / max(entry, 1e-12) * 100 > MAX_ENTRY_CHASE_PCT * 1.5:
        return None
    lo, hi = make_entry_zone(entry, atr(c5), direction)
    tps = target_levels(direction, (lo + hi) / 2, sl, c15)
    if not tps:
        return None
    base = market_score(ticker, c5, c15, vr, c1h, direction, 'EXTREME REVERSAL')
    ss = strategy_score(base, [8 if abs(ch) >= STRONG_24H_MOVE else 4,
                               7 if abs(m2h) >= 8 else 3,
                               5 if vr >= 1.5 else 0], [])
    setup = Setup(iid, get_coin(iid), direction, 'EXTREME REVERSAL', pattern,
                  level, 'EXTREME', lo, hi, sl, *tps, ss, ss, reason,
                  ticker['vol24h_usd'], ch, ticker['range24h_pct'], atr_pct(c5),
                  atr_pct(c15), vr, c5[-96:], points, '1–8 ч')
    return setup if common_validate(setup) else None

# ============================================================
# STRATEGY 4 — PRE-BREAKOUT / HORIZONTAL LEVEL PRESSURE
# ============================================================

def _pressure_metrics(c5: List[Candle], level: float, direction: str):
    window = c5[-12:-1]
    if len(window) < 8:
        return None
    atr5 = atr(c5)
    # Only count genuine interactions with the level, not candles far away.
    near_limit = max(atr5 * 0.85, level * 0.003)
    touches = 0
    distances = []
    for c in window:
        if direction == 'LONG':
            d = level - c.high
            if d >= -near_limit and c.high <= level * 1.0015:
                touches += 1
                distances.append(max(0.0, d))
        else:
            d = c.low - level
            if d >= -near_limit and c.low >= level * 0.9985:
                touches += 1
                distances.append(max(0.0, d))
    if touches < 3:
        return None
    # Compare early vs late distance to the level. Pressure requires price to
    # get closer, not repeatedly reject by larger distances.
    early = window[:max(3, len(window)//2)]
    late = window[max(3, len(window)//2):]
    def avg_distance(group):
        vals = []
        for c in group:
            vals.append(abs(level - (c.close if direction == 'LONG' else c.close)))
        return sum(vals) / len(vals) if vals else 0.0
    early_d = avg_distance(early)
    late_d = avg_distance(late)
    compression = late_d < early_d * 0.82 if early_d > 0 else False
    return touches, compression, early_d, late_d


def build_pre_breakout(iid, ticker, data) -> Optional[Setup]:
    c5 = confirmed_candles(data['5m'])
    c15 = confirmed_candles(data['15m'])
    c1h = confirmed_candles(data['1h'])
    if min(len(c5), len(c15), len(c1h)) < 90:
        return None

    levels = cluster_levels(c15, 0.28)
    current = c5[-1]
    previous = c5[-2]
    atr5 = atr(c5)
    vr = vol_ratio(c5)
    if not levels or atr5 <= 0:
        return None
    # Quiet volume can precede a breakout, but an almost dead trigger is not
    # enough evidence by itself. Compression is still checked separately.
    if vr < float(os.getenv('MIN_PREBREAKOUT_VOLUME', '0.30')):
        return None

    candidates = []
    # This search is deliberately about an UNBROKEN horizontal level.
    for lv in levels:
        if lv.touches < 3:
            continue
        if lv.kind == 'HIGH' and lv.price > current.close:
            distance_pct = (lv.price - current.close) / current.close * 100
            if distance_pct <= MAX_ENTRY_CHASE_PCT:
                candidates.append(('LONG', lv))
        if lv.kind == 'LOW' and lv.price < current.close:
            distance_pct = (current.close - lv.price) / current.close * 100
            if distance_pct <= MAX_ENTRY_CHASE_PCT:
                candidates.append(('SHORT', lv))

    if not candidates:
        return None
        # A pre-breakout needs a live trigger candle, not just a level and compression.
    # Quiet compression is acceptable, but the latest closed candle must lean in
    # the proposed direction.
    candle_range = max(current.high - current.low, 1e-12)
    close_location = (current.close - current.low) / candle_range

    best = None
    best_rank = float('-inf')
    for direction, lv in candidates:
        # Level must remain unbroken on the recent confirmed candles.
        recent = c5[-10:]
        if direction == 'LONG':
            if any(c.close > lv.price * 1.001 for c in recent):
                continue
        else:
            if any(c.close < lv.price * 0.999 for c in recent):
                continue
        metrics = _pressure_metrics(c5, lv.price, direction)
        if not metrics:
            continue
        touches, compression, early_d, late_d = metrics
        if not compression:
            continue

        if direction == 'LONG' and (current.close <= current.open or close_location < 0.58):
            continue
        if direction == 'SHORT' and (current.close >= current.open or close_location > 0.42):
            continue

        # Reject PRE-BREAKOUT entries directly against a clearly accelerating 1H trend.
        ema1h = ema([c.close for c in c1h], 20)
        if len(ema1h) >= 6:
            if direction == 'LONG' and c1h[-1].close < ema1h[-1] and ema1h[-1] < ema1h[-5] * 0.997:
                continue
            if direction == 'SHORT' and c1h[-1].close > ema1h[-1] and ema1h[-1] > ema1h[-5] * 1.003:
                continue

        # Structure must press toward the level: LONG higher lows, SHORT lower highs.
        last8 = c5[-9:-1]
        if direction == 'LONG':
            lows = [c.low for c in last8]
            if not (lows[-1] > min(lows[:4]) * 1.001):
                continue
            # No hard rejection from the resistance in the latest candle.
            if current.close < previous.close and upper_wick_rejection(current, lv.price, 'LONG'):
                continue
            entry = min(current.close, lv.price - atr5 * 0.12)
            sl_base = min(c.low for c in c5[-6:])
            sl = sl_base - atr5 * 0.22
            # Entry should be close to the level, but still BEFORE it.
            if entry >= lv.price:
                continue
            distance = (lv.price - entry) / entry * 100
            if distance > MAX_ENTRY_CHASE_PCT:
                continue
            # The resistance itself is the breakout trigger, not a profit target.
            # Targets must sit beyond it, otherwise TP1 is often only a tiny move.
            preferred = []
            next_levels = sorted([x.price for x in levels if x.price > lv.price])
            preferred.extend(next_levels[:3])
            pattern = 'ДАВЛЕНИЕ НА СОПРОТИВЛЕНИЕ → PRE-BREAKOUT LONG'
            reason = (f'горизонтальное сопротивление {touches}× → сжатие → higher lows → '
                      f'цена {distance:.2f}% под уровнем; вход ДО пробоя')
            points = [(len(c5[-96:]) - 8, lv.price, 'RESISTANCE'),
                      (len(c5[-96:]) - 1, entry, 'PRE-BREAKOUT')]
            quality = touches + (3 if compression else 0) + (2 if vr >= 1.3 else 0)
            candidate_rank = quality * 3 + lv.strength * 0.08 + touches * 1.5 - distance * 4.0
            if candidate_rank > best_rank:
                best_rank = candidate_rank
                best = (direction, lv, entry, sl, preferred, pattern, reason, points, quality)
        else:
            highs = [c.high for c in last8]
            if not (highs[-1] < max(highs[:4]) * 0.999):
                continue
            if current.close > previous.close and upper_wick_rejection(current, lv.price, 'SHORT'):
                continue
            entry = max(current.close, lv.price + atr5 * 0.12)
            sl_base = max(c.high for c in c5[-6:])
            sl = sl_base + atr5 * 0.22
            if entry <= lv.price:
                continue
            distance = (entry - lv.price) / entry * 100
            if distance > MAX_ENTRY_CHASE_PCT:
                continue
            # The support itself is the breakdown trigger, not a profit target.
            preferred = []
            next_levels = sorted([x.price for x in levels if x.price < lv.price], reverse=True)
            preferred.extend(next_levels[:3])
            pattern = 'ДАВЛЕНИЕ НА ПОДДЕРЖКУ → PRE-BREAKOUT SHORT'
            reason = (f'горизонтальная поддержка {touches}× → сжатие → lower highs → '
                      f'цена {distance:.2f}% над уровнем; вход ДО пробоя')
            points = [(len(c5[-96:]) - 8, lv.price, 'SUPPORT'),
                      (len(c5[-96:]) - 1, entry, 'PRE-BREAKOUT')]
            quality = touches + (3 if compression else 0) + (2 if vr >= 1.3 else 0)
            candidate_rank = quality * 3 + lv.strength * 0.08 + touches * 1.5 - distance * 4.0
            if candidate_rank > best_rank:
                best_rank = candidate_rank
                best = (direction, lv, entry, sl, preferred, pattern, reason, points, quality)

    if not best:
        return None
    direction, lv, entry, sl, preferred, pattern, reason, points, quality = best
    # For pre-breakout the zone is intentionally anchored BEFORE the level.
    if direction == 'LONG':
        width = max(atr5 * 0.10, entry * 0.0002)
        lo, hi = entry - width, min(entry + width, lv.price - entry * 0.00015)
    else:
        width = max(atr5 * 0.10, entry * 0.0002)
        lo, hi = max(entry - width, lv.price + entry * 0.00015), entry + width
    if lo >= hi:
        return None
    tps = target_levels(direction, (lo + hi) / 2, sl, c15, preferred)
    if not tps:
        return None
    base = market_score(ticker, c5, c15, vr, c1h, direction, 'PRE-BREAKOUT')
    ss = strategy_score(base, [10, min(10, quality * 2), 5 if vr >= 1.4 else 0], [])
    setup = Setup(iid, get_coin(iid), direction, 'PRE-BREAKOUT', pattern,
                  lv.price, 'HORIZONTAL LEVEL', lo, hi, sl, *tps, ss, ss, reason,
                  ticker['vol24h_usd'], ticker['change24h_pct'], ticker['range24h_pct'],
                  atr_pct(c5), atr_pct(c15), vr, c5[-96:], points, '1–6 ч')
    return setup if common_validate(setup) else None


def upper_wick_rejection(c: Candle, level: float, direction: str) -> bool:
    upper, lower = wick_ratio(c)
    if direction == 'LONG':
        return c.high >= level * 0.998 and upper > 0.42
    return c.low <= level * 1.002 and lower > 0.42


STRATEGIES: Tuple[Callable, ...] = (
    build_pullback,
    build_breakout_retest,
    build_reversal,
    build_pre_breakout,
)

# ============================================================
# TELEGRAM VOTES
# ============================================================

VOTES = {'strong': '🔥', 'good': '👍', 'weak': '👎', 'miss': '❌', 'watch': '👀'}


def vote_keyboard(signal_id: int):
    labels = [('strong', '🔥 Сильный'), ('good', '👍 Норм'),
              ('weak', '👎 Слабый'), ('miss', '❌ Мимо'), ('watch', '👀 Наблюдаю')]
    buttons = []
    with db_lock:
        counts = dict(db.execute(
            'SELECT vote, COUNT(*) FROM signal_votes WHERE signal_id=? GROUP BY vote',
            (signal_id,)).fetchall())
    for key, label in labels:
        buttons.append(InlineKeyboardButton(f'{label} {counts.get(key, 0)}',
                                            callback_data=f'vote:{signal_id}:{key}'))
    return InlineKeyboardMarkup([buttons[:2], buttons[2:4], buttons[4:]])


@bot.callback_query_handler(func=lambda call: bool(call.data and call.data.startswith('vote:')))
def on_vote(call):
    try:
        _, sid_s, vote = call.data.split(':', 2)
        sid = int(sid_s)
        if vote not in VOTES:
            raise ValueError('invalid vote')
        with db_lock:
            db.execute('INSERT OR REPLACE INTO signal_votes(signal_id,user_id,vote,created_at) VALUES(?,?,?,?)',
                       (sid, call.from_user.id, vote, now_ts()))
            db.commit()
        try:
            bot.answer_callback_query(call.id, 'Голос учтён. Его можно изменить.')
        except Exception:
            pass
        try:
            bot.edit_message_reply_markup(CHANNEL_ID, call.message.message_id,
                                          reply_markup=vote_keyboard(sid))
        except Exception:
            pass
    except Exception:
        try:
            bot.answer_callback_query(call.id, 'Не удалось сохранить реакцию.')
        except Exception:
            pass

# ============================================================
# CHART / MESSAGE
# ============================================================

def make_chart(setup: Setup) -> str:
    cs = setup.candles_5m[-96:]
    if len(cs) < 35:
        raise RuntimeError('Not enough candles for chart')
    path = f'/tmp/quantum_{setup.coin}_{int(time.time()*1000)}.png'
    fig, ax = plt.subplots(figsize=(15, 8.5), dpi=140)
    width = .58
    for i, c in enumerate(cs):
        rising = c.close >= c.open
        color = '#16a34a' if rising else '#dc2626'
        ax.vlines(i, c.low, c.high, color=color, linewidth=1.1, zorder=2)
        y = min(c.open, c.close)
        h = max(abs(c.close - c.open), c.close * 0.00002)
        ax.add_patch(Rectangle((i - width/2, y), width, h,
                               facecolor=color, edgecolor=color, linewidth=.5, zorder=3))

    def hline(price, color, label, lw=1.8, ls='--'):
        ax.axhline(price, color=color, linewidth=lw, linestyle=ls, zorder=4)
        ax.text(len(cs)-1.2, price, f'  {label} {fmt_price(price)}', ha='right', va='bottom',
                fontsize=9, fontweight='bold', color=color,
                bbox=dict(facecolor='white', alpha=.82, edgecolor='none', pad=1.5))

    hline(setup.level, '#7c3aed', setup.level_kind, 1.9, '-')
    hline((setup.entry_low + setup.entry_high)/2, '#2563eb', 'ВХОД', 2.4, '-')
    ax.axhspan(setup.entry_low, setup.entry_high, alpha=.10, color='#2563eb', zorder=1)
    hline(setup.sl, '#dc2626', 'СТОП', 2.1, '--')
    hline(setup.tp1, '#16a34a', 'TP1', 1.5, ':')
    hline(setup.tp2, '#16a34a', 'TP2', 1.7, ':')
    hline(setup.tp3, '#15803d', 'TP3', 1.9, '--')

    for idx, p, label in setup.points:
        idx = int(clamp(idx, 0, len(cs)-1))
        ax.scatter([idx], [p], s=48, marker='o', zorder=7, color='#111827')
        offset = 16 if any(x in label for x in ('LOW', 'TRIGGER', 'PRE', 'RETEST')) else -20
        ax.annotate(label, (idx, p), xytext=(0, offset), textcoords='offset points',
                    ha='center', fontsize=9, fontweight='bold', color='#111827')

    ax.set_title(f'{setup.coin}USDT · {setup.strategy} · {setup.direction} · 5M',
                 fontsize=18, fontweight='bold', pad=14)
    ax.text(.01, .965, f'15M structure · 1H context · {setup.pattern_name}',
            transform=ax.transAxes, va='top', fontsize=10.5, fontweight='bold')
    ax.grid(True, alpha=.14)
    ax.tick_params(labelsize=9)
    ax.set_xlim(-1, len(cs))
    plt.tight_layout()
    fig.savefig(path, facecolor='white', bbox_inches='tight')
    plt.close(fig)
    return path


def build_signal_text(s: Setup) -> str:
    entry = (s.entry_low + s.entry_high) / 2
    worst_entry = s.entry_high if s.direction == 'LONG' else s.entry_low
    risk = abs(worst_entry - s.sl) / max(worst_entry, 1e-12) * 100
    worst_risk = abs(worst_entry - s.sl)
    reward2 = (s.tp2 - worst_entry) if s.direction == 'LONG' else (worst_entry - s.tp2)
    rr2 = reward2 / max(worst_risk, 1e-12)
    side = '🟢 LONG' if s.direction == 'LONG' else '🔴 SHORT'
    return (
        f'<b>{side} · {s.coin}USDT</b>\n'
        f'<b>{s.strategy}</b> · 5M / 15M / 1H\n\n'
        f'📌 <b>Сетап:</b> {s.pattern_name}\n'
        f'📈 <b>24H:</b> {s.change24:+.1f}% · Range {s.range24:.1f}%\n'
        f'⚡ <b>ATR:</b> 5M {s.atr5_pct:.2f}% · 15M {s.atr15_pct:.2f}%\n'
        f'💧 <b>Volume:</b> x{s.volume_ratio:.2f} · Turnover ${s.volume_24h/1_000_000:.1f}M\n\n'
        f'🎯 <b>Вход:</b> <code>{fmt_price(s.entry_low)} – {fmt_price(s.entry_high)}</code>\n'
        f'🛑 <b>Стоп:</b> <code>{fmt_price(s.sl)}</code> · риск по краю зоны {risk:.2f}%\n'
        f'🎯 <b>TP1:</b> <code>{fmt_price(s.tp1)}</code>\n'
        f'🎯 <b>TP2:</b> <code>{fmt_price(s.tp2)}</code> · RR 1:{rr2:.1f}\n'
        f'🎯 <b>TP3:</b> <code>{fmt_price(s.tp3)}</code>\n\n'
        f'🧠 <b>Почему:</b> {s.reason}.\n'
        f'⏱ <b>Ожидаемое удержание:</b> {s.hold_hours}\n\n'
        f'⚠️ Внимательно проверьте сделку перед входом.\n'
        f'🛡 Риск на одну сделку — <b>не более 1% депозита</b>.\n'
        f'🚫 Не догоняйте цену после ухода от зоны входа.'
    )

# ============================================================
# PERSISTENCE / RECOVERY
# ============================================================

def insert_signal(s: Setup) -> int:
    created = now_ts()
    with db_lock:
        cur = db.execute('''INSERT INTO signals(
            inst_id,direction,strategy,level,entry_low,entry_high,sl,tp1,tp2,tp3,
            score,status,created_at,expires_at,coin,pattern_name,level_kind,reason,
            volume_24h,change24,range24,atr5_pct,atr15_pct,volume_ratio,hold_hours,strategy_score
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (s.inst_id, s.direction, s.strategy, s.level, s.entry_low, s.entry_high,
         s.sl, s.tp1, s.tp2, s.tp3, s.score, 'READY', created,
         created + READY_TTL_MINUTES*60, s.coin, s.pattern_name, s.level_kind,
         s.reason, s.volume_24h, s.change24, s.range24, s.atr5_pct,
         s.atr15_pct, s.volume_ratio, s.hold_hours, s.strategy_score))
        sid = cur.lastrowid
        db.commit()
    return sid


def update_message_id(signal_id: int, message_id: int):
    with db_lock:
        db.execute('UPDATE signals SET message_id=? WHERE id=?', (message_id, signal_id))
        db.commit()


def setup_from_row(row) -> Setup:
    return Setup(
        inst_id=row['inst_id'], coin=row['coin'] or get_coin(row['inst_id']),
        direction=row['direction'], strategy=row['strategy'],
        pattern_name=row['pattern_name'] or row['strategy'], level=float(row['level']),
        level_kind=row['level_kind'] or 'LEVEL', entry_low=float(row['entry_low']),
        entry_high=float(row['entry_high']), sl=float(row['sl']), tp1=float(row['tp1']),
        tp2=float(row['tp2']), tp3=float(row['tp3']), score=int(row['score'] or 0),
        strategy_score=int(row['strategy_score'] or row['score'] or 0),
        reason=row['reason'] or '', volume_24h=float(row['volume_24h'] or 0),
        change24=float(row['change24'] or 0), range24=float(row['range24'] or 0),
        atr5_pct=float(row['atr5_pct'] or 0), atr15_pct=float(row['atr15_pct'] or 0),
        volume_ratio=float(row['volume_ratio'] or 0), candles_5m=[], points=[],
        hold_hours=row['hold_hours'] or '1–8 ч')


def recover_runtime_state():
    with db_lock:
        cols = [r[1] for r in db.execute('PRAGMA table_info(signals)').fetchall()]
        rows = db.execute('SELECT * FROM signals WHERE status IN (\'READY\',\'ACTIVE\')').fetchall()
        colmap = {c: i for i, c in enumerate(cols)}
    recovered = 0
    for raw in rows:
        row = {k: raw[i] for k, i in colmap.items()}
        try:
            s = setup_from_row(row)
            if row['status'] == 'READY':
                pending[s.inst_id] = PendingSignal(s, int(row['id']), float(row['created_at']),
                                                   float(row['expires_at']), int(row['message_id'] or 0))
            else:
                active[s.inst_id] = ActiveSignal(s, int(row['id']), float(row['activated_at'] or row['created_at']),
                                                 int(row['message_id'] or 0), bool(row['tp1_hit']),
                                                 bool(row['tp2_hit']), bool(row['tp3_hit']))
            recovered += 1
        except Exception:
            log.exception('RECOVERY FAILED | row=%s', raw[:5])
    log.info('RECOVERY | restored=%d pending=%d active=%d', recovered, len(pending), len(active))

# ============================================================
# SEND SIGNAL
# ============================================================

def send_signal(s: Setup) -> bool:
    path = None
    sid = None
    with sending_lock:
        if s.inst_id in sending_symbols or s.inst_id in pending or s.inst_id in active:
            log.info('DUPLICATE BLOCKED | %s | already sending/pending/active', s.inst_id)
            return False
        sending_symbols.add(s.inst_id)
    try:
        path = make_chart(s)
        sid = insert_signal(s)
        with open(path, 'rb') as f:
            msg = bot.send_photo(
                CHANNEL_ID, f, caption=build_signal_text(s), parse_mode='HTML',
                show_caption_above_media=True, reply_markup=vote_keyboard(sid)
            )
        update_message_id(sid, msg.message_id)
        created = now_ts()
        pending[s.inst_id] = PendingSignal(s, sid, created, created + READY_TTL_MINUTES*60, msg.message_id)
        log.info('SIGNAL SENT | %s | %s | %s | score=%s | id=%s',
                 s.coin, s.direction, s.strategy, s.score, sid)
        return True
    except Exception:
        log.exception('SIGNAL SEND FAILED | %s', s.inst_id)
        if sid is not None:
            with db_lock:
                db.execute("UPDATE signals SET status='SEND_FAILED' WHERE id=?", (sid,))
                db.commit()
        return False
    finally:
        with sending_lock:
            sending_symbols.discard(s.inst_id)
        if path:
            try:
                os.remove(path)
            except OSError:
                pass


def send_update(text: str):
    try:
        bot.send_message(CHANNEL_ID, text, parse_mode='HTML')
    except Exception:
        log.exception('TELEGRAM UPDATE FAILED')

# ============================================================
# LIFECYCLE
# ============================================================

def price_in_entry(s: Setup, price: float) -> bool:
    return s.entry_low <= price <= s.entry_high


def lifecycle_for_symbol(iid: str, cs: List[Candle]):
    if len(cs) < 3:
        return
    live = cs[-1]
    confirmed = confirmed_candles(cs)
    if not confirmed:
        return
    last = confirmed[-1]

    p = pending.get(iid)
    if p:
        ticker = get_ticker(iid)
        live_price = ticker['last'] if ticker else live.close
        touched = price_in_entry(p.setup, live_price) or (live.low <= p.setup.entry_high and live.high >= p.setup.entry_low)
        if now_ts() > p.expires_at:
            with db_lock:
                db.execute("UPDATE signals SET status='EXPIRED' WHERE id=? AND status='READY'", (p.signal_id,))
                db.commit()
            pending.pop(iid, None)
        elif touched:
            with db_lock:
                db.execute("UPDATE signals SET status='ACTIVE',activated_at=? WHERE id=? AND status='READY'",
                           (now_ts(), p.signal_id))
                db.commit()
            active[iid] = ActiveSignal(p.setup, p.signal_id, now_ts(), p.message_id)
            pending.pop(iid, None)

    a = active.get(iid)
    if not a:
        return
    s = a.setup
    # Candle timestamps are opening times in milliseconds. Do not let a candle
    # that closed before entry activation retroactively hit TP/SL for this trade.
    if last.ts / 1000.0 < a.activated_at:
        return
    long = s.direction == 'LONG'
    hit1 = last.high >= s.tp1 if long else last.low <= s.tp1
    hit2 = last.high >= s.tp2 if long else last.low <= s.tp2
    hit3 = last.high >= s.tp3 if long else last.low <= s.tp3
    stop = last.low <= s.sl if long else last.high >= s.sl

    with db_lock:
        row = db.execute('SELECT tp1_hit,tp2_hit,tp3_hit,status FROM signals WHERE id=?', (a.signal_id,)).fetchone()
    if not row:
        return
    h1, h2, h3, status = row
    if status not in ('ACTIVE', 'READY'):
        active.pop(iid, None)
        return

    # Conservative candle rule: if stop and ANY not-yet-recorded target are
    # both inside the same confirmed candle, assume stop happened first.
    ambiguous = stop and (hit1 or hit2 or hit3)
    if ambiguous:
        with db_lock:
            db.execute("UPDATE signals SET status='CLOSED_LOSS',result='SL',closed_at=?,r_multiple=-1 WHERE id=?",
                       (now_ts(), a.signal_id))
            db.commit()
        send_update(f'🔴 <b>STOP — {s.coin}USDT</b>\nСтоп и цель оказались в одной подтверждённой свече. Результат засчитан консервативно по SL.')
        active.pop(iid, None)
        return

    if hit1 and not h1:
        with db_lock:
            db.execute('UPDATE signals SET tp1_hit=1 WHERE id=?', (a.signal_id,))
            db.commit()
        a.tp1_sent = True
        send_update(f'🟢 <b>TP1 — {s.coin}USDT</b>\nЦена достигла <code>{fmt_price(s.tp1)}</code>.')
    if hit2 and not h2:
        with db_lock:
            db.execute('UPDATE signals SET tp2_hit=1 WHERE id=?', (a.signal_id,))
            db.commit()
        a.tp2_sent = True
        send_update(f'🚀 <b>TP2 — {s.coin}USDT</b>\nЦена достигла <code>{fmt_price(s.tp2)}</code>.')
    if hit3 and not h3:
        entry = (s.entry_low + s.entry_high) / 2
        r = abs(s.tp3 - entry) / max(abs(entry - s.sl), 1e-12)
        with db_lock:
            db.execute("UPDATE signals SET tp3_hit=1,status='CLOSED_WIN',result='TP3',closed_at=?,r_multiple=? WHERE id=?",
                       (now_ts(), r, a.signal_id))
            db.commit()
        send_update(f'🏁 <b>TP3 — {s.coin}USDT</b>\nЦена достигла <code>{fmt_price(s.tp3)}</code>. Результат: TP3.')
        active.pop(iid, None)
        return
    if stop:
        with db_lock:
            db.execute("UPDATE signals SET status='CLOSED_LOSS',result='SL',closed_at=?,r_multiple=-1 WHERE id=?",
                       (now_ts(), a.signal_id))
            db.commit()
        send_update(f'🔴 <b>STOP — {s.coin}USDT</b>\nЦена достигла <code>{fmt_price(s.sl)}</code>.')
        active.pop(iid, None)
        return

    if now_ts() - a.activated_at > ACTIVE_MAX_HOURS * 3600:
        with db_lock:
            db.execute("UPDATE signals SET status='TIMEOUT',result='TIMEOUT',closed_at=? WHERE id=?",
                       (now_ts(), a.signal_id))
            db.commit()
        send_update(f'⚪ <b>TIMEOUT — {s.coin}USDT</b>\nСделка закрыта по времени.')
        active.pop(iid, None)


def update_signal_results():
    symbols = set(pending) | set(active)
    for iid in list(symbols):
        try:
            lifecycle_for_symbol(iid, get_candles(iid, '5m', 90))
        except Exception:
            log.exception('LIFECYCLE FAILED | %s', iid)

# ============================================================
# ANTI-DUPLICATE — no daily signal cap
# ============================================================

def can_send(iid: str) -> bool:
    # Block symbols that are currently being sent, READY, or ACTIVE. Also keep
    # a DB-backed cooldown so a quick process restart cannot repost the same
    # coin immediately. Cooldown prevents reposting the same symbol immediately. No global daily/hourly quota is applied.
    with sending_lock:
        if iid in sending_symbols:
            return False
    if iid in pending or iid in active:
        return False
    with db_lock:
        row = db.execute("SELECT created_at,status FROM signals WHERE inst_id=? AND status NOT IN ('SEND_FAILED') ORDER BY created_at DESC LIMIT 1",
                         (iid,)).fetchone()
    if not row:
        return True
    created_at, status = float(row[0]), str(row[1] or '')
    if status in ('READY', 'ACTIVE'):
        return False
    return now_ts() - created_at >= COOLDOWN_MINUTES * 60

# ============================================================
# SCANNER — four independent strategy searches
# ============================================================

_universe_last_scanned: Optional[str] = None

def scan_market():
    global _universe_last_scanned
    instruments = get_instruments()
    tickers = get_tickers()
    ranked_universe = build_universe(instruments, tickers)
    if ranked_universe:
        batch_size = len(ranked_universe) if DEEP_SCAN_LIMIT <= 0 else min(DEEP_SCAN_LIMIT, len(ranked_universe))
        start = 0
        if _universe_last_scanned:
            previous_positions = [i for i, item in enumerate(ranked_universe) if item[0] == _universe_last_scanned]
            if previous_positions:
                start = (previous_positions[0] + 1) % len(ranked_universe)
        rotated = ranked_universe[start:] + ranked_universe[:start]
        candidates = rotated[:batch_size]
        if candidates:
            _universe_last_scanned = candidates[-1][0]
    else:
        candidates = []
    raw: List[Setup] = []
    strategy_counts = {fn.__name__: 0 for fn in STRATEGIES}

    for iid, ticker, _ in candidates:
        if not can_send(iid):
            continue
        try:
            data = load_symbol(iid)
            if not market_data_is_fresh(data):
                log.debug('SYMBOL SKIPPED | %s | stale or incomplete candle data', iid)
                continue
            # Common activity gate only. No strategy-specific pattern is filtered here.
            c5 = confirmed_candles(data['5m'])
            c15 = confirmed_candles(data['15m'])
            if len(c5) < 80 or len(c15) < 80:
                continue
            if atr_pct(c5) < MIN_ATR_5M_PCT or atr_pct(c15) < MIN_ATR_15M_PCT:
                continue

            for builder in STRATEGIES:
                try:
                    setup = builder(iid, ticker, data)
                    if setup:
                        raw.append(setup)
                        strategy_counts[builder.__name__] += 1
                except Exception:
                    log.exception('STRATEGY ERROR | %s | %s', iid, builder.__name__)
        except Exception:
            log.exception('SYMBOL SCAN FAILED | %s', iid)

    # A symbol can have multiple valid strategies. Send only the best normalized
    # candidate for that symbol to avoid competing instructions.
    best: Dict[str, Setup] = {}
    for s in raw:
        if s.inst_id not in best or (s.score, s.strategy_score) > (best[s.inst_id].score, best[s.inst_id].strategy_score):
            best[s.inst_id] = s

    final = sorted(best.values(), key=lambda x: (x.score, x.strategy_score), reverse=True)
    log.info('SCAN | market=%d eligible=%d batch=%d raw=%d final=%d | %s',
             len(tickers), len(ranked_universe), len(candidates), len(raw), len(final), strategy_counts)
    for s in final:
        if can_send(s.inst_id):
            send_signal(s)

# ============================================================
# WEEKLY REPORT
# ============================================================

def weekly_report():
    n = local_now()
    if n.weekday() != 0 or n.hour < 9:
        return
    key = n.date().isoformat()
    with db_lock:
        row = db.execute("SELECT value FROM bot_state WHERE key='weekly_report'").fetchone()
    if row and row[0] == key:
        return
    since = (n - timedelta(days=7)).timestamp()
    with db_lock:
        total = db.execute('SELECT COUNT(*) FROM signals WHERE created_at>=?', (since,)).fetchone()[0]
        wins = db.execute("SELECT COUNT(*) FROM signals WHERE created_at>=? AND result='TP3'", (since,)).fetchone()[0]
        losses = db.execute("SELECT COUNT(*) FROM signals WHERE created_at>=? AND result='SL'", (since,)).fetchone()[0]
        timeouts = db.execute("SELECT COUNT(*) FROM signals WHERE created_at>=? AND result='TIMEOUT'", (since,)).fetchone()[0]
    try:
        bot.send_message(CHANNEL_ID,
            f'📊 <b>QUANTUM — неделя</b>\n\nСигналов: <b>{total}</b>\n'
            f'TP3: <b>{wins}</b>\nSTOP: <b>{losses}</b>\nTIMEOUT: <b>{timeouts}</b>',
            parse_mode='HTML')
        with db_lock:
            db.execute("INSERT OR REPLACE INTO bot_state(key,value) VALUES('weekly_report',?)", (key,))
            db.commit()
    except Exception:
        log.exception('WEEKLY REPORT FAILED')

# ============================================================
# TELEGRAM POLLING — hardened against 409 / duplicate processes
# ============================================================

polling_stop = threading.Event()
polling_lock_file = None


def acquire_single_process_lock():
    global polling_lock_file
    try:
        import fcntl
    except ImportError:
        log.warning('PROCESS LOCK | fcntl unavailable; relying on deployment single-instance setting')
        return True
    path = os.getenv('TELEGRAM_POLLING_LOCK', '/tmp/quantum_telegram_polling.lock')
    polling_lock_file = open(path, 'w')
    try:
        fcntl.flock(polling_lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        polling_lock_file.write(str(os.getpid()))
        polling_lock_file.flush()
        log.info('PROCESS LOCK | acquired | pid=%s', os.getpid())
        return True
    except BlockingIOError:
        log.error('PROCESS LOCK | another QUANTUM process is already polling Telegram')
        return False


def start_telegram_polling():
    if not acquire_single_process_lock():
        # Keep the market scanner alive; this process must not compete for
        # getUpdates if another local process already owns the lock.
        log.error('TELEGRAM POLLING DISABLED | another local process owns the lock')
        return
    try:
        bot.remove_webhook()
        time.sleep(0.5)
    except Exception:
        log.exception('TELEGRAM WEBHOOK CLEANUP FAILED')

    def runner():
        backoff = 3
        while not polling_stop.is_set():
            try:
                log.info('TELEGRAM POLLING START')
                # polling(non_stop=False) lets a 409 escape to this handler;
                # infinity_polling can internally reconnect forever on conflict.
                bot.polling(non_stop=False, skip_pending=True, timeout=20,
                            long_polling_timeout=20, allowed_updates=['callback_query'])
                if not polling_stop.is_set():
                    log.warning('TELEGRAM POLLING RETURNED | retrying in %ss', backoff)
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 60)
            except Exception as exc:
                text = str(exc).lower()
                is_409 = getattr(exc, 'error_code', None) == 409 or ('409' in text and 'conflict' in text) or 'terminated by other getupdates' in text
                if is_409:
                    # Do not create a polling tug-of-war. Market scanning and
                    # outgoing messages may continue, but callbacks need the
                    # one process that owns polling. Fix extra Render service.
                    log.error('TELEGRAM 409 CONFLICT | polling stopped in this process. Stop every other service/instance using this token except one.')
                    return
                log.exception('TELEGRAM POLLING ERROR | %s', exc)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)

    threading.Thread(target=runner, name='telegram-polling', daemon=True).start()

# ============================================================
# STARTUP HEALTH
# ============================================================

def startup_healthcheck():
    try:
        payload = okx_get('/api/v5/public/time', {})
        if str(payload.get('code')) != '0':
            raise RuntimeError('OKX time endpoint failed')
        log.info('HEALTH | OKX API OK')
    except Exception:
        log.exception('HEALTH | OKX API FAILED')
    try:
        me = bot.get_me()
        log.info('HEALTH | TELEGRAM OK | @%s', me.username)
    except Exception:
        log.exception('HEALTH | TELEGRAM FAILED')

# ============================================================
# MAIN
# ============================================================

def main():
    log.info('============================================================')
    log.info('QUANTUM INTRADAY SWING ENGINE V3 STARTED')
    log.info('Strategies: Trend Pullback | Breakout+Retest | Extreme Reversal | Pre-Breakout')
    log.info('No daily signal cap; only duplicate/cooldown protection.')
    log.info('============================================================')

    startup_healthcheck()
    recover_runtime_state()
    start_telegram_polling()

    while True:
        try:
            update_signal_results()
            weekly_report()
            scan_market()
        except KeyboardInterrupt:
            log.info('STOPPED')
            polling_stop.set()
            break
        except Exception:
            log.exception('MAIN LOOP ERROR')
        time.sleep(SCAN_INTERVAL_SECONDS)


if __name__ == '__main__':
    main()
