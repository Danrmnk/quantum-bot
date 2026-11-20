import os
import time
import math
import logging
import sqlite3
import json
from html import escape
import threading
import fcntl
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, List, Dict, Tuple

import requests
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

# ============================================================
# QUANTUM INTRADAY SWING ENGINE V2.3
# ------------------------------------------------------------
# Purpose:
#   Find high-momentum crypto futures setups for 1-8 hour holds.
#   5M = trigger/entry, 15M = structure, 1H = context.
#
# Active strategies:
#   1) Breakout + Retest      structure break -> retest -> continuation
#   2) Trend Pullback          1H trend -> 15M pullback -> 5M trigger
#   3) Mean Reversion          stretched 15M price -> 5M reversal
# Extreme Reversal is retained for history/research, but is not active in STRATEGIES.
#
# Important:
#   This is a signal engine, not a profitability guarantee.
#   The code deliberately prefers NO SIGNAL over a weak signal.
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
MIN_24H_TURNOVER_USD = float(os.getenv('MIN_24H_TURNOVER_USD', '60000000'))
PREFERRED_TURNOVER_USD = float(os.getenv('PREFERRED_TURNOVER_USD', '100000000'))
TOP_GAINERS = int(os.getenv('TOP_GAINERS', '20'))
TOP_LOSERS = int(os.getenv('TOP_LOSERS', '20'))
TOP_VOLATILE = int(os.getenv('TOP_VOLATILE', '25'))
TOP_TURNOVER = int(os.getenv('TOP_TURNOVER', '25'))
NEW_ACTIVE_DAYS = int(os.getenv('NEW_ACTIVE_DAYS', '45'))
NEW_ACTIVE_COUNT = int(os.getenv('NEW_ACTIVE_COUNT', '15'))
DEEP_SCAN_LIMIT = int(os.getenv('DEEP_SCAN_LIMIT', '70'))
MIN_ACTIVE_24H_RANGE_PCT = float(os.getenv('MIN_ACTIVE_24H_RANGE_PCT', '5.0'))

# ---------------- momentum/reversal ----------------
MIN_24H_MOVE_FOR_REVERSAL = float(os.getenv('MIN_24H_MOVE_FOR_REVERSAL', '15'))
STRONG_24H_MOVE = float(os.getenv('STRONG_24H_MOVE', '30'))
MIN_2H_IMPULSE = float(os.getenv('MIN_2H_IMPULSE', '5'))
MIN_1H_IMPULSE = float(os.getenv('MIN_1H_IMPULSE', '3'))
MIN_ATR_5M_PCT = float(os.getenv('MIN_ATR_5M_PCT', '0.18'))
MIN_ATR_15M_PCT = float(os.getenv('MIN_ATR_15M_PCT', '0.45'))
MIN_VOLUME_RATIO = float(os.getenv('MIN_VOLUME_RATIO', '1.20'))
MIN_REVERSAL_VOLUME_RATIO = float(os.getenv('MIN_REVERSAL_VOLUME_RATIO', '1.15'))

# ---------------- strategy/risk ----------------
MIN_BREAKOUT_VOLUME = float(os.getenv('MIN_BREAKOUT_VOLUME', '1.35'))
MAX_ENTRY_CHASE_PCT = float(os.getenv('MAX_ENTRY_CHASE_PCT', '1.00'))
# Stop distance is structural; account risk must be controlled by position sizing.
TP1_R = float(os.getenv('TP1_R', '1.20'))
TP2_R = float(os.getenv('TP2_R', '2.00'))
TP3_R = float(os.getenv('TP3_R', '3.00'))
MIN_TARGET_MOVE_PCT = float(os.getenv('MIN_TARGET_MOVE_PCT', '2.00'))
MIN_TARGET_RR = float(os.getenv('MIN_TARGET_RR', '2.00'))
STOP_BUFFER_ATR_5M = float(os.getenv('STOP_BUFFER_ATR_5M', '0.25'))
STOP_BUFFER_ATR_15M = float(os.getenv('STOP_BUFFER_ATR_15M', '0.18'))
BREAKEVEN_BUFFER_PCT = float(os.getenv('BREAKEVEN_BUFFER_PCT', '0.00'))
TP1_CLOSE_PCT = float(os.getenv('TP1_CLOSE_PCT', '30'))
TP2_CLOSE_PCT = float(os.getenv('TP2_CLOSE_PCT', '30'))
TP3_CLOSE_PCT = float(os.getenv('TP3_CLOSE_PCT', '40'))
if any(x < 0 or x > 100 for x in (TP1_CLOSE_PCT, TP2_CLOSE_PCT, TP3_CLOSE_PCT)) or abs(TP1_CLOSE_PCT + TP2_CLOSE_PCT + TP3_CLOSE_PCT - 100.0) > 1e-6:
    raise RuntimeError('TP1_CLOSE_PCT + TP2_CLOSE_PCT + TP3_CLOSE_PCT must equal 100')
MIN_SCORE = int(os.getenv('MIN_SCORE', '80'))

# ---------------- runtime ----------------
SCAN_INTERVAL_SECONDS = int(os.getenv('SCAN_INTERVAL_SECONDS', '30'))
HTTP_TIMEOUT = int(os.getenv('HTTP_TIMEOUT', '10'))
REQUEST_RETRIES = int(os.getenv('REQUEST_RETRIES', '3'))
CANDLE_CACHE_SECONDS = int(os.getenv('CANDLE_CACHE_SECONDS', '15'))
READY_TTL_MINUTES = int(os.getenv('READY_TTL_MINUTES', '20'))
ACTIVE_MAX_HOURS = int(os.getenv('ACTIVE_MAX_HOURS', '12'))
COOLDOWN_MINUTES = int(os.getenv('COOLDOWN_MINUTES', '120'))

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
log = logging.getLogger('QUANTUM')
bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode='HTML')
session = requests.Session()
session.headers.update({'User-Agent': 'QuantumIntradaySwing/2.0', 'Accept': 'application/json'})

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
    kind: str  # HIGH / LOW

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
    oi_usd: float = 0.0
    btc_context: str = 'NEUTRAL'

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
    current_sl: Optional[float] = None
    realized_r: float = 0.0

pending: Dict[str, PendingSignal] = {}
active: Dict[str, ActiveSignal] = {}
candle_cache: Dict[Tuple[str, str, int], Tuple[float, List[Candle]]] = {}

# ============================================================
# DATABASE + MIGRATION
# ============================================================

def db_connect():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA busy_timeout=5000')
    return con


db = db_connect()
db_lock = threading.Lock()
_instance_lock_handle = None

def acquire_instance_lock():
    """Prevent two bot processes in the same Render instance/container."""
    global _instance_lock_handle
    lock_path = os.getenv('INSTANCE_LOCK_PATH', '/tmp/quantum_bot.lock')
    handle = open(lock_path, 'w')
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise RuntimeError('Another QUANTUM process is already running in this container')
    handle.write(str(os.getpid()))
    handle.flush()
    _instance_lock_handle = handle


SIGNAL_COLUMNS = {
    'inst_id':'TEXT', 'direction':'TEXT', 'strategy':'TEXT', 'level':'REAL',
    'entry_low':'REAL', 'entry_high':'REAL', 'sl':'REAL', 'tp1':'REAL', 'tp2':'REAL', 'tp3':'REAL',
    'score':'INTEGER', 'status':'TEXT', 'created_at':'REAL', 'activated_at':'REAL',
    'expires_at':'REAL', 'tp1_hit':'INTEGER DEFAULT 0', 'tp2_hit':'INTEGER DEFAULT 0',
    'tp3_hit':'INTEGER DEFAULT 0', 'result':'TEXT DEFAULT ''', 'closed_at':'REAL',
    'r_multiple':'REAL DEFAULT 0',
    'change24':'REAL DEFAULT 0', 'range24':'REAL DEFAULT 0',
    'atr5_pct':'REAL DEFAULT 0', 'atr15_pct':'REAL DEFAULT 0',
    'volume_ratio':'REAL DEFAULT 0', 'risk_pct':'REAL DEFAULT 0',
    'current_sl':'REAL', 'setup_json':'TEXT', 'message_id':'INTEGER', 'realized_r':'REAL DEFAULT 0',
    'post_stop_tp1_hit':'INTEGER DEFAULT 0', 'post_stop_tp2_hit':'INTEGER DEFAULT 0',
    'post_stop_tp3_hit':'INTEGER DEFAULT 0', 'post_stop_best_move_pct':'REAL DEFAULT 0',
    'post_stop_checked_at':'REAL', 'last_processed_ts':'INTEGER DEFAULT 0'
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
            signal_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            vote TEXT NOT NULL, created_at REAL NOT NULL,
            PRIMARY KEY(signal_id, user_id)
        )''')
        db.execute('''CREATE TABLE IF NOT EXISTS bot_state (
            key TEXT PRIMARY KEY, value TEXT NOT NULL
        )''')
        db.execute('''CREATE TABLE IF NOT EXISTS learning_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at REAL NOT NULL,
            window_hours INTEGER NOT NULL,
            strategy TEXT NOT NULL,
            samples INTEGER NOT NULL,
            wins INTEGER NOT NULL,
            losses INTEGER NOT NULL,
            be INTEGER NOT NULL,
            timeouts INTEGER NOT NULL,
            total_r REAL NOT NULL,
            adjustment REAL NOT NULL
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


def avg_volume(cs: List[Candle], n: int = 20) -> float:
    x = [c.quote_volume for c in cs[-n:] if c.quote_volume > 0]
    return sum(x) / len(x) if x else 0.0


def body_ratio(c: Candle) -> float:
    r = c.high - c.low
    return abs(c.close - c.open) / r if r > 0 else 0.0


def close_location(c: Candle) -> float:
    r = c.high - c.low
    return (c.close - c.low) / r if r > 0 else 0.5


def wick_ratio(c: Candle) -> Tuple[float, float]:
    r = max(c.high - c.low, 1e-12)
    upper = c.high - max(c.open, c.close)
    lower = min(c.open, c.close) - c.low
    return upper / r, lower / r


def true_range_pct(c: Candle) -> float:
    return (c.high - c.low) / max(c.close, 1e-12) * 100

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
                time.sleep(attempt * 0.8)
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


def get_tickers() -> Dict[str, dict]:
    data = okx_get('/api/v5/market/tickers', {'instType': 'SWAP'}).get('data', [])
    out = {}
    for x in data:
        iid = str(x.get('instId', ''))
        if not iid.endswith('-USDT-SWAP'):
            continue
        try:
            last = float(x.get('last') or 0)
            op = float(x.get('open24h') or 0)
            hi = float(x.get('high24h') or 0)
            lo = float(x.get('low24h') or 0)
            vol = float(x.get('vol24h') or 0)
            vol_ccy = float(x.get('volCcy24h') or 0)
            if last <= 0 or op <= 0:
                continue
            # For OKX derivatives, volCcy24h is volume in base currency,
            # while vol24h is a contract count. Convert base volume to USDT
            # notional; never multiply contract count by last price.
            turnover = vol_ccy * last
            if turnover <= 0:
                continue
            change = (last / op - 1) * 100
            rng = (hi - lo) / last * 100 if hi > lo else 0
            out[iid] = {
                'last': last, 'open24h': op, 'high24h': hi, 'low24h': lo,
                'vol24h_usd': turnover, 'change24h_pct': change,
                'range24h_pct': rng, 'ts': int(x.get('ts') or 0)
            }
        except (TypeError, ValueError):
            continue
    return out


def get_open_interest_map() -> Dict[str, float]:
    """Fetch one market-wide OI snapshot; OI is context, never a standalone trigger."""
    try:
        rows = okx_get('/api/v5/public/open-interest', {'instType': 'SWAP'}).get('data', [])
        out = {}
        for row in rows:
            iid = str(row.get('instId') or '')
            try:
                value = float(row.get('oiUsd') or 0.0)
            except (TypeError, ValueError):
                value = 0.0
            if iid and value > 0:
                out[iid] = value
        log.info('OPEN INTEREST | instruments=%d', len(out))
        return out
    except Exception:
        log.exception('OPEN INTEREST SNAPSHOT FAILED | continuing without OI context')
        return {}


def get_candles(inst_id: str, bar: str, limit: int = 180) -> List[Candle]:
    key = (inst_id, bar, int(limit))
    cached = candle_cache.get(key)
    if cached and now_ts() - cached[0] < CANDLE_CACHE_SECONDS:
        return cached[1]
    rows = okx_get('/api/v5/market/candles', {
        'instId': inst_id, 'bar': bar, 'limit': str(min(limit, 300))
    }).get('data', [])
    out = []
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
    # Setup builders must never use a still-forming candle as confirmation.
    return {
        '1h': [c for c in get_candles(inst_id, '1H', 160) if c.confirmed],
        '15m': [c for c in get_candles(inst_id, '15m', 180) if c.confirmed],
        '5m': [c for c in get_candles(inst_id, '5m', 200) if c.confirmed],
    }

# ============================================================
# MARKET UNIVERSE
# ============================================================

def build_strategy_universes(instruments: Dict[str, dict], tickers: Dict[str, dict]):
    """Separate discovery pools per strategy; no single generic pool is reused blindly."""
    base = []
    for iid, t in tickers.items():
        if iid not in instruments or t['vol24h_usd'] < MIN_24H_TURNOVER_USD:
            continue
        if t['range24h_pct'] < MIN_ACTIVE_24H_RANGE_PCT:
            continue
        activity = clamp((t['vol24h_usd'] / PREFERRED_TURNOVER_USD) * 20, 0, 20)
        movement = clamp(abs(t['change24h_pct']) * 1.15, 0, 40)
        volatility = clamp(t['range24h_pct'] * 1.8, 0, 40)
        base.append((iid, t, activity + movement + volatility))

    gainers = sorted(base, key=lambda z: z[1]['change24h_pct'], reverse=True)[:TOP_GAINERS]
    losers = sorted(base, key=lambda z: z[1]['change24h_pct'])[:TOP_LOSERS]
    volatile = sorted(base, key=lambda z: z[1]['range24h_pct'], reverse=True)[:TOP_VOLATILE]
    liquid = sorted(base, key=lambda z: z[1]['vol24h_usd'], reverse=True)[:TOP_TURNOVER]
    cutoff = int((local_now() - timedelta(days=NEW_ACTIVE_DAYS)).timestamp() * 1000)
    new_active = sorted(
        [z for z in base if instruments[z[0]].get('listTime', 0) >= cutoff],
        key=lambda z: z[2], reverse=True
    )[:NEW_ACTIVE_COUNT]

    def dedupe(*groups):
        pool = {}
        for group in groups:
            for item in group:
                pool[item[0]] = item
        return sorted(pool.values(), key=lambda z: z[2], reverse=True)

    pools = {
        'BREAKOUT + RETEST': dedupe(volatile, liquid, new_active),
        'TREND PULLBACK': dedupe(gainers, losers, liquid, new_active),
        'MEAN REVERSION': dedupe(gainers, losers, volatile),
    }
    log.info(
        'STRATEGY UNIVERSES | breakout=%d pullback=%d mean_reversion=%d',
        len(pools['BREAKOUT + RETEST']), len(pools['TREND PULLBACK']),
        len(pools['MEAN REVERSION'])
    )
    return pools


# ============================================================
# STRUCTURE ENGINE
# ============================================================

def pivots(cs: List[Candle], left: int = 2, right: int = 2) -> List[Pivot]:
    out = []
    if len(cs) < left + right + 1:
        return out
    for i in range(left, len(cs) - right):
        h = cs[i].high
        l = cs[i].low
        if h >= max(c.high for c in cs[i-left:i+right+1]):
            out.append(Pivot(i, h, 'HIGH'))
        if l <= min(c.low for c in cs[i-left:i+right+1]):
            out.append(Pivot(i, l, 'LOW'))
    return out


def recent_pivots(cs: List[Candle], lookback: int = 80) -> List[Pivot]:
    start = max(0, len(cs) - lookback)
    raw = pivots(cs[start:], 2, 2)
    return [Pivot(p.index + start, p.price, p.kind) for p in raw]


def cluster_levels(cs: List[Candle], tolerance_pct: float = 0.35) -> List[Tuple[float, str, int]]:
    ps = recent_pivots(cs, min(100, len(cs)))
    clusters: List[Tuple[float, str, int]] = []
    for p in ps:
        merged = False
        for j, (price, kind, touches) in enumerate(clusters):
            if kind == p.kind and abs(p.price - price) / max(price, 1e-12) * 100 <= tolerance_pct:
                new_price = (price * touches + p.price) / (touches + 1)
                clusters[j] = (new_price, kind, touches + 1)
                merged = True
                break
        if not merged:
            clusters.append((p.price, p.kind, 1))
    return sorted(clusters, key=lambda z: z[2], reverse=True)


def nearest_support_resistance(price: float, levels: List[Tuple[float, str, int]]):
    support = None
    resistance = None
    for p, kind, touches in levels:
        if p <= price and (support is None or p > support[0]):
            support = (p, kind, touches)
        if p >= price and (resistance is None or p < resistance[0]):
            resistance = (p, kind, touches)
    return support, resistance


def trend_state(cs: List[Candle]) -> Optional[str]:
    if len(cs) < 60:
        return None
    closes = [c.close for c in cs]
    e20 = ema(closes, 20)[-1]
    e50 = ema(closes, 50)[-1]
    slope20 = pct_move(ema(closes, 20)[-1], ema(closes, 20)[-8])
    if e20 > e50 and slope20 > 0.15:
        return 'LONG'
    if e20 < e50 and slope20 < -0.15:
        return 'SHORT'
    return None


def impulse_stats(c5: List[Candle]):
    def move(n):
        if len(c5) <= n:
            return 0.0
        return pct_move(c5[-1].close, c5[-1-n].close)
    return move(12), move(24), move(36)


def local_extreme(c5: List[Candle], direction: str, lookback: int = 48):
    x = c5[-lookback-1:-1] if len(c5) > lookback + 1 else c5[:-1]
    if not x:
        return None
    if direction == 'LONG':
        return min(x, key=lambda c: c.low)
    return max(x, key=lambda c: c.high)

# ============================================================
# QUALITY / RISK / TARGET ENGINE
# ============================================================

def base_score(ticker, c5, c15, vr) -> int:
    score = 58
    ch = abs(ticker['change24h_pct'])
    rng = ticker['range24h_pct']
    if ch >= 20: score += 5
    if ch >= STRONG_24H_MOVE: score += 7
    if ch >= 45: score += 4
    if rng >= 10: score += 5
    if rng >= 20: score += 4
    if atr_pct(c5) >= MIN_ATR_5M_PCT: score += 4
    if atr_pct(c15) >= MIN_ATR_15M_PCT: score += 4
    if vr >= 1.2: score += 3
    if vr >= 1.5: score += 4
    if vr >= 2.0: score += 3
    if ticker['vol24h_usd'] >= PREFERRED_TURNOVER_USD: score += 3
    return min(score, 99)


def stop_is_structurally_valid(direction: str, entry: float, sl: float) -> bool:
    """Validate stop side and non-zero distance; stop width is NOT hard-coded."""
    if entry <= 0 or sl <= 0 or abs(entry - sl) <= 0:
        return False
    return sl < entry if direction == 'LONG' else sl > entry


def reward_r(direction: str, entry: float, sl: float, target: float) -> float:
    risk = abs(entry - sl)
    if risk <= 0:
        return 0.0
    move = (target - entry) if direction == 'LONG' else (entry - target)
    return max(0.0, move / risk)


def realized_gross_r(direction: str, entry: float, sl: float,
                     hit1: bool, hit2: bool, hit3: bool,
                     exit_price: Optional[float] = None,
                     tp1: Optional[float] = None, tp2: Optional[float] = None,
                     tp3: Optional[float] = None) -> float:
    """Weighted realized R using configured partial exits; excludes fees/slippage."""
    total = 0.0
    if hit1 and tp1 is not None:
        total += TP1_CLOSE_PCT / 100.0 * reward_r(direction, entry, sl, tp1)
    if hit2 and tp2 is not None:
        total += TP2_CLOSE_PCT / 100.0 * reward_r(direction, entry, sl, tp2)
    if hit3 and tp3 is not None:
        total += TP3_CLOSE_PCT / 100.0 * reward_r(direction, entry, sl, tp3)
    if exit_price is not None:
        remaining_pct = 100.0
        if hit1: remaining_pct -= TP1_CLOSE_PCT
        if hit2: remaining_pct -= TP2_CLOSE_PCT
        if hit3: remaining_pct -= TP3_CLOSE_PCT
        move = (exit_price - entry) if direction == 'LONG' else (entry - exit_price)
        total += max(0.0, remaining_pct) / 100.0 * move / max(abs(entry - sl), 1e-12)
    return total


def structural_stop(direction: str, extreme: float, atr_value: float, buffer_mult: float) -> float:
    """Place stop beyond the invalidation extreme with volatility buffer."""
    buffer = max(atr_value * buffer_mult, extreme * 0.00015)
    return extreme - buffer if direction == 'LONG' else extreme + buffer


def validate_risk(entry: float, sl: float) -> bool:
    # Backward-compatible helper: only rejects invalid/zero distances.
    # Deposit risk is controlled by position sizing, not by stop distance in %.
    return entry > 0 and sl > 0 and abs(entry - sl) > 0


def target_levels(direction: str, entry: float, sl: float, c15: List[Candle]):
    """Build targets from risk AND available structure; require >=2% TP2 room and >=2R."""
    risk = abs(entry - sl)
    if risk <= 0 or not c15:
        return None
    levels = cluster_levels(c15)
    if direction == 'LONG':
        obstacles = sorted(p for p, kind, touches in levels if kind == 'HIGH' and p > entry and touches >= 2)
        sign = 1.0
    else:
        obstacles = sorted((p for p, kind, touches in levels if kind == 'LOW' and p < entry and touches >= 2), reverse=True)
        sign = -1.0

    # The nearest meaningful opposing level is a hard planning boundary.
    obstacle = obstacles[0] if obstacles else None
    if obstacle is None:
        return None  # No verified opposing structure: do not invent a target.
    min_move = entry * MIN_TARGET_MOVE_PCT / 100.0
    tp2_distance = max(risk * MIN_TARGET_RR, min_move)
    tp2 = entry + sign * tp2_distance
    tp1 = entry + sign * max(risk * min(TP1_R, 1.0), min_move * 0.45)
    tp3 = entry + sign * max(risk * TP3_R, tp2_distance + risk * 0.75)

    available = abs(obstacle - entry)
    # Do not project a target through a nearby structure.
    if tp2_distance >= available * 0.92:
        return None
    if abs(tp3 - entry) >= available * 0.95:
        tp3 = entry + sign * available * 0.94

    # Ensure strict target ordering and enough room beyond TP2.
    if direction == 'LONG':
        if not (entry < tp1 < tp2 < tp3):
            return None
    else:
        if not (entry > tp1 > tp2 > tp3):
            return None
    rr2 = abs(tp2 - entry) / risk
    move2 = abs(tp2 - entry) / entry * 100.0
    if rr2 < MIN_TARGET_RR or move2 < MIN_TARGET_MOVE_PCT:
        return None
    return tp1, tp2, tp3


def entry_zone(entry: float, atr5: float, direction: str):
    width = min(atr5 * 0.18, entry * MAX_ENTRY_CHASE_PCT / 100 * 0.35)
    width = max(width, entry * 0.00025)
    if direction == 'LONG':
        return entry - width, entry + width
    return entry - width, entry + width


def final_score(base: int, bonuses: List[int], penalties: List[int]) -> int:
    return max(0, min(99, base + sum(bonuses) - sum(penalties)))

# ============================================================
# STRATEGY 1 — EXTREME REVERSAL
# ============================================================

def build_reversal(iid, ticker, data) -> Optional[Setup]:
    c5, c15, c1h = data['5m'], data['15m'], data['1h']
    if min(len(c5), len(c15), len(c1h)) < 70:
        return None
    ch = ticker['change24h_pct']
    if abs(ch) < MIN_24H_MOVE_FOR_REVERSAL:
        return None
    if atr_pct(c5) < MIN_ATR_5M_PCT or atr_pct(c15) < MIN_ATR_15M_PCT:
        return None

    m1h, m2h, _ = impulse_stats(c5)
    direction = 'SHORT' if ch > 0 else 'LONG'
    if direction == 'SHORT' and m2h < MIN_2H_IMPULSE:
        return None
    if direction == 'LONG' and m2h > -MIN_2H_IMPULSE:
        return None

    extreme = local_extreme(c5, 'SHORT' if direction == 'SHORT' else 'LONG', 60)
    if extreme is None:
        return None
    cur = c5[-1]
    prev = c5[-2]
    vr = vol_ratio(c5)
    if vr < MIN_REVERSAL_VOLUME_RATIO:
        return None

    if direction == 'SHORT':
        retrace = pct_move(extreme.high, cur.close)
        upper, lower = wick_ratio(cur)
        # Strong rejection from the high + bearish confirmation.
        if retrace < 0.55:
            return None
        if not (cur.close < cur.open and cur.close < prev.low * 1.001):
            return None
        if body_ratio(cur) < 0.42 or close_location(cur) > 0.48:
            return None
        if upper < 0.10 and cur.high < max(c.high for c in c5[-6:-1]):
            return None
        sl = max(extreme.high, max(c.high for c in c5[-4:])) + atr(c5) * 0.28
        level = extreme.high
        point_label = 'EXTREME HIGH'
        pattern = 'ТОП МОВЕР → ПЕРВЫЙ РАЗВОРОТ SHORT'
        reason = f'сильный рост 24H {ch:+.1f}% → 2H {m2h:+.1f}% → экстремум → медвежье подтверждение'
        bonuses = [7 if ch >= STRONG_24H_MOVE else 3, 6 if m2h >= 8 else 3, 5 if vr >= 1.5 else 0]
    else:
        retrace = pct_move(cur.close, extreme.low)
        upper, lower = wick_ratio(cur)
        if retrace < 0.55:
            return None
        if not (cur.close > cur.open and cur.close > prev.high * 0.999):
            return None
        if body_ratio(cur) < 0.42 or close_location(cur) < 0.52:
            return None
        if lower < 0.10 and cur.low > min(c.low for c in c5[-6:-1]):
            return None
        sl = min(extreme.low, min(c.low for c in c5[-4:])) - atr(c5) * 0.28
        level = extreme.low
        point_label = 'EXTREME LOW'
        pattern = 'ТОП ЛУЗЕР → ПЕРВЫЙ РАЗВОРОТ LONG'
        reason = f'сильное падение 24H {ch:+.1f}% → 2H {m2h:+.1f}% → экстремум → бычье подтверждение'
        bonuses = [7 if ch <= -STRONG_24H_MOVE else 3, 6 if m2h <= -8 else 3, 5 if vr >= 1.5 else 0]

    entry = cur.close
    if abs(entry - level) / max(entry, 1e-12) * 100 > MAX_ENTRY_CHASE_PCT * 1.8:
        return None
    if not stop_is_structurally_valid(direction, entry, sl):
        return None
    tps = target_levels(direction, entry, sl, c15)
    if not tps:
        return None
    score = final_score(base_score(ticker, c5, c15, vr), bonuses, [])
    # Higher-timeframe agreement adds quality but does not create the setup.
    tstate = trend_state(c15)
    if (direction == 'LONG' and tstate == 'LONG') or (direction == 'SHORT' and tstate == 'SHORT'):
        score = min(99, score + 3)
    if score < MIN_SCORE:
        return None
    lo, hi = entry_zone(entry, atr(c5), direction)
    chart = c5[-96:]
    ext_index = max(0, len(chart) - 2 - 60)
    return Setup(iid, get_coin(iid), direction, 'EXTREME REVERSAL', pattern,
                 level, point_label, lo, hi, sl, *tps, score, reason,
                 ticker['vol24h_usd'], ch, ticker['range24h_pct'], atr_pct(c5),
                 atr_pct(c15), vr, chart,
                 [(ext_index, level, point_label), (len(chart)-1, entry, 'CONFIRM')],
                 '1–8 ч')

# ============================================================
# STRATEGY 2 — BREAKOUT + RETEST
# ============================================================

def build_breakout(iid, ticker, data) -> Optional[Setup]:
    c5, c15, c1h = data['5m'], data['15m'], data['1h']
    if min(len(c5), len(c15), len(c1h)) < 70:
        return None
    vr = vol_ratio(c5)
    if vr < MIN_BREAKOUT_VOLUME:
        return None
    levels = cluster_levels(c15, 0.30)
    if not levels:
        return None
    cur = c5[-1]
    direction = level = None
    touches = 0
    break_index = None

    # A completed breakout candle must precede a separate retest candle.
    scan_start = max(1, len(c5) - 14)
    for p, kind, n_touches in levels:
        if n_touches < 2:
            continue
        for j in range(scan_start, len(c5) - 1):
            b = c5[j]
            if kind == 'HIGH' and c5[j-1].close <= p and b.close > p and b.close > b.open:
                if cur.low <= p * 1.003 and cur.close > p and cur.close > cur.open:
                    if direction is None or j > break_index:
                        direction, level, touches, break_index = 'LONG', p, n_touches, j
            elif kind == 'LOW' and c5[j-1].close >= p and b.close < p and b.close < b.open:
                if cur.high >= p * 0.997 and cur.close < p and cur.close < cur.open:
                    if direction is None or j > break_index:
                        direction, level, touches, break_index = 'SHORT', p, n_touches, j
    if direction is None or break_index is None or break_index >= len(c5) - 1:
        return None

    # The breakout must remain valid between the breakout candle and the
    # distinct retest candle. Any confirmed close back through the level
    # invalidates the sequence; do not reuse a stale breakout from the past.
    between = c5[break_index + 1:-1]
    if direction == 'LONG':
        if any(c.close <= level for c in between):
            return None
        if not (cur.low <= level * 1.003 and cur.high >= level * 0.997):
            return None
    else:
        if any(c.close >= level for c in between):
            return None
        if not (cur.high >= level * 0.997 and cur.low <= level * 1.003):
            return None

    # Reject a retest that closes back through the broken level.
    if direction == 'LONG':
        if cur.close <= level or cur.low > level * 1.003 or close_location(cur) < 0.58:
            return None
        retest_extreme = min(c.low for c in c5[break_index+1:])
        sl = structural_stop('LONG', retest_extreme, atr(c5), STOP_BUFFER_ATR_5M)
        reason = f'15M сопротивление {touches}× → отдельный закрытый пробой → последующий ретест/удержание → 5M подтверждение; объём x{vr:.2f}'
    else:
        if cur.close >= level or cur.high < level * 0.997 or close_location(cur) > 0.42:
            return None
        retest_extreme = max(c.high for c in c5[break_index+1:])
        sl = structural_stop('SHORT', retest_extreme, atr(c5), STOP_BUFFER_ATR_5M)
        reason = f'15M поддержка {touches}× → отдельный закрытый пробой → последующий ретест/удержание → 5M подтверждение; объём x{vr:.2f}'

    entry = cur.close
    if abs(entry-level)/max(entry,1e-12)*100 > MAX_ENTRY_CHASE_PCT:
        return None
    if not stop_is_structurally_valid(direction, entry, sl):
        return None
    tps = target_levels(direction, entry, sl, c15)
    if not tps:
        return None
    score = final_score(base_score(ticker,c5,c15,vr), [8, min(6,touches), 5 if vr >= 1.8 else 0], [])
    if score < MIN_SCORE:
        return None
    lo, hi = entry_zone(entry, atr(c5), direction)
    chart = c5[-96:]
    return Setup(iid,get_coin(iid),direction,'BREAKOUT + RETEST','ПРОБОЙ + ОТДЕЛЬНЫЙ РЕТЕСТ',level,'BROKEN LEVEL',lo,hi,sl,*tps,score,reason,
                 ticker['vol24h_usd'],ticker['change24h_pct'],ticker['range24h_pct'],atr_pct(c5),atr_pct(c15),vr,chart,
                 [(max(0,len(chart)-(len(c5)-break_index)),level,'BREAKOUT'),(len(chart)-1,entry,'RETEST CONFIRM')],'1–6 ч')


# ============================================================
# STRATEGY 3 — TREND PULLBACK
# ============================================================

def build_pullback(iid, ticker, data) -> Optional[Setup]:
    c5,c15,c1h=data['5m'],data['15m'],data['1h']
    if min(len(c5),len(c15),len(c1h))<70:
        return None
    trend=trend_state(c1h)
    if not trend:
        return None
    e20_15=ema([c.close for c in c15],20)
    if len(e20_15)<10:
        return None
    cur,prev=c5[-1],c5[-2]
    vr=vol_ratio(c5)
    recent=c15[-10:-2]
    if len(recent)<5:
        return None

    if trend=='LONG':
        pull_low=min(c.low for c in recent)
        # Price must have pulled back toward 15M EMA, then reclaim it on 5M.
        if not any(c.low <= e20_15[-3]*1.003 for c in recent):
            return None
        if not (cur.close > e20_15[-1] and cur.close > prev.high and cur.close > cur.open):
            return None
        level=pull_low
        sl=structural_stop('LONG', pull_low, atr(c15), STOP_BUFFER_ATR_15M)
        reason=f'1H восходящий тренд → 15M откат к EMA20 → 5M возврат выше уровня; объём x{vr:.2f}'
    else:
        pull_high=max(c.high for c in recent)
        if not any(c.high >= e20_15[-3]*0.997 for c in recent):
            return None
        if not (cur.close < e20_15[-1] and cur.close < prev.low and cur.close < cur.open):
            return None
        level=pull_high
        sl=structural_stop('SHORT', pull_high, atr(c15), STOP_BUFFER_ATR_15M)
        reason=f'1H нисходящий тренд → 15M откат к EMA20 → 5M возврат ниже уровня; объём x{vr:.2f}'

    entry=cur.close
    if not stop_is_structurally_valid(trend, entry, sl):
        return None
    if abs(entry-level)/max(entry,1e-12)*100>MAX_ENTRY_CHASE_PCT*1.5:
        return None
    tps=target_levels(trend,entry,sl,c15)
    if not tps:
        return None
    score=final_score(base_score(ticker,c5,c15,vr),[8,5 if vr>=1.2 else 0,5],[])
    if score<MIN_SCORE:
        return None
    lo,hi=entry_zone(entry,atr(c5),trend)
    return Setup(iid,get_coin(iid),trend,'TREND PULLBACK','ТРЕНДОВЫЙ ОТКАТ',level,'PULLBACK LOW' if trend=='LONG' else 'PULLBACK HIGH',lo,hi,sl,*tps,score,reason,
                 ticker['vol24h_usd'],ticker['change24h_pct'],ticker['range24h_pct'],atr_pct(c5),atr_pct(c15),vr,c5[-96:],
                 [(len(c5[-96:])-1,entry,'TRIGGER')],'1–8 ч')

# ============================================================
# STRATEGY 4 — MEAN REVERSION
# ============================================================

def build_mean_reversion(iid,ticker,data)->Optional[Setup]:
    c5,c15,c1h=data['5m'],data['15m'],data['1h']
    if min(len(c5),len(c15),len(c1h))<70:
        return None
    e20=ema([c.close for c in c15],20)[-1]
    cur,prev=c5[-1],c5[-2]
    deviation=(cur.close-e20)/max(e20,1e-12)*100
    direction='LONG' if deviation<=-2.5 else 'SHORT' if deviation>=2.5 else None
    if not direction:
        return None
    vr=vol_ratio(c5)
    if vr<MIN_VOLUME_RATIO:
        return None

    # Require a real liquidity sweep of a recent 5M swing and a close back inside.
    prior=c5[-13:-1]
    if len(prior)<8:
        return None
    if direction=='LONG':
        swept_low=min(c.low for c in prior)
        if not (cur.low < swept_low and cur.close > swept_low and cur.close > cur.open
                and cur.close > prev.high and close_location(cur)>=0.62):
            return None
        sl=structural_stop('LONG', cur.low, atr(c5), STOP_BUFFER_ATR_5M)
        level=swept_low
    else:
        swept_high=max(c.high for c in prior)
        if not (cur.high > swept_high and cur.close < swept_high and cur.close < cur.open
                and cur.close < prev.low and close_location(cur)<=0.38):
            return None
        sl=structural_stop('SHORT', cur.high, atr(c5), STOP_BUFFER_ATR_5M)
        level=swept_high

    entry=cur.close
    if not stop_is_structurally_valid(direction,entry,sl):
        return None
    tps=target_levels(direction,entry,sl,c15)
    if not tps:
        return None
    score=final_score(base_score(ticker,c5,c15,vr),[7,5 if abs(deviation)>=3.5 else 0,4 if vr>=1.5 else 0],[])
    if score<MIN_SCORE:
        return None
    lo,hi=entry_zone(entry,atr(c5),direction)
    trend15=trend_state(c15)
    context_note='контекст 15M согласуется' if trend15==direction else 'контртрендовый разворот; требуется подтверждённое снятие ликвидности'
    return Setup(iid,get_coin(iid),direction,'MEAN REVERSION','СНЯТИЕ ЛИКВИДНОСТИ → ВОЗВРАТ',level,'LIQUIDITY SWEEP',lo,hi,sl,*tps,score,
                 f'отклонение от 15M EMA20 {deviation:+.2f}% → sweep экстремума → возврат и 5M подтверждение; {context_note}',
                 ticker['vol24h_usd'],ticker['change24h_pct'],ticker['range24h_pct'],atr_pct(c5),atr_pct(c15),vr,c5[-96:],
                 [(len(c5[-96:])-1,level,'SWEEP'),(len(c5[-96:])-1,entry,'REVERSAL')],'1–6 ч')


STRATEGIES = (
    build_breakout,
    build_pullback,
    build_mean_reversion,
)  # EXTREME REVERSAL retained in source/history, disabled from active scanning

# ============================================================
# TELEGRAM VOTES
# ============================================================
VOTES={
    'strong':'🔥', 'good':'👍', 'weak':'👎', 'miss':'❌', 'watch':'👀'
}


def vote_keyboard(signal_id:int):
    labels=[('strong','🔥 Сильный'),('good','👍 Норм'),('weak','👎 Слабый'),('miss','❌ Мимо'),('watch','👀 Наблюдаю')]
    buttons=[]
    for key,label in labels:
        with db_lock:
            n=db.execute('SELECT COUNT(*) FROM signal_votes WHERE signal_id=? AND vote=?',(signal_id,key)).fetchone()[0]
        buttons.append(InlineKeyboardButton(f'{label} {n}',callback_data=f'vote:{signal_id}:{key}'))
    return InlineKeyboardMarkup([buttons[:2],buttons[2:4],buttons[4:]])


@bot.callback_query_handler(func=lambda call: bool(call.data and call.data.startswith('vote:')))
def on_vote(call):
    try:
        _, sid_s, vote=call.data.split(':',2)
        sid=int(sid_s)
        if vote not in VOTES:
            raise ValueError('invalid vote')
        with db_lock:
            db.execute('INSERT OR REPLACE INTO signal_votes(signal_id,user_id,vote,created_at) VALUES(?,?,?,?)',
                       (sid,call.from_user.id,vote,now_ts()))
            db.commit()
        try:
            bot.answer_callback_query(call.id,'Голос учтён. Его можно изменить.')
        except Exception:
            pass
        try:
            bot.edit_message_reply_markup(CHANNEL_ID,call.message.message_id,reply_markup=vote_keyboard(sid))
        except Exception:
            pass
    except Exception:
        try:
            bot.answer_callback_query(call.id,'Не удалось сохранить реакцию.')
        except Exception:
            pass


def start_telegram_polling():
    def runner():
        delay = 5
        while True:
            try:
                log.info('TELEGRAM POLLING START')
                # Safe for polling deployments; do not discard pending updates here.
                bot.delete_webhook(drop_pending_updates=False)
                bot.infinity_polling(skip_pending=False, timeout=20, long_polling_timeout=20)
                log.warning('TELEGRAM POLLING RETURNED | restarting in %ss', delay)
            except Exception as exc:
                msg = str(exc)
                if '409' in msg or 'Conflict' in msg or 'getUpdates' in msg:
                    log.critical('TELEGRAM 409/GETUPDATES CONFLICT | stop this instance: another process is polling this bot token')
                    break
                else:
                    log.exception('TELEGRAM POLLING ERROR | retrying in %ss', delay)
            time.sleep(delay)
            delay = min(delay * 2, 60)

    t=threading.Thread(target=runner,name='telegram-polling',daemon=True)
    t.start()


# ============================================================
# CHART
# ============================================================

def draw_setup_chart(ax, setup: Setup, show_title: bool = True):
    """Render the same trade-aware candlestick chart for Telegram and PDF."""
    cs = setup.candles_5m[-96:]
    if len(cs) < 20:
        raise RuntimeError('Not enough confirmed candles for chart')

    width = 0.58
    for i, c in enumerate(cs):
        rising = c.close >= c.open
        color = '#16a34a' if rising else '#dc2626'
        ax.vlines(i, c.low, c.high, color=color, linewidth=1.05, zorder=2)
        y = min(c.open, c.close)
        h = max(abs(c.close-c.open), c.close*0.00002)
        ax.add_patch(Rectangle((i-width/2, y), width, h,
                               facecolor=color, edgecolor=color, linewidth=.45, zorder=3))

    def hline(price, color, label, lw=1.5, ls='--', alpha=0.95):
        ax.axhline(price, color=color, linewidth=lw, linestyle=ls, alpha=alpha, zorder=1)
        ax.text(0.995, price, f' {label} {fmt_price(price)}',
                transform=ax.get_yaxis_transform(), ha='right', va='bottom',
                fontsize=7.5, fontweight='bold', color=color,
                bbox=dict(facecolor='white', alpha=.82, edgecolor='none', pad=1.0))

    # Nearest structural support/resistance, independently derived from confirmed 5M pivots.
    last_price = cs[-1].close
    levels = cluster_levels(cs, tolerance_pct=0.30)
    supports = sorted([x for x in levels if x[1] == 'LOW' and x[0] < last_price],
                      key=lambda x: last_price-x[0])[:2]
    resistances = sorted([x for x in levels if x[1] == 'HIGH' and x[0] > last_price],
                         key=lambda x: x[0]-last_price)[:2]
    for j, (price, _kind, touches) in enumerate(supports):
        hline(price, '#0891b2', f'SUPPORT {touches}x', 1.0, ':', .78)
    for j, (price, _kind, touches) in enumerate(resistances):
        hline(price, '#d97706', f'RESISTANCE {touches}x', 1.0, ':', .78)

    hline(setup.level, '#7c3aed', setup.level_kind, 1.65, '-')
    entry = (setup.entry_low + setup.entry_high) / 2
    hline(entry, '#2563eb', 'ENTRY', 2.0, '-')
    ax.axhspan(setup.entry_low, setup.entry_high, alpha=.12, color='#2563eb', zorder=1)
    hline(setup.sl, '#dc2626', 'INITIAL SL', 1.8, '--')
    hline(setup.tp1, '#16a34a', 'TP1', 1.2, ':')
    hline(setup.tp2, '#16a34a', 'TP2', 1.45, ':')
    hline(setup.tp3, '#15803d', 'TP3', 1.6, '--')

    for idx, price, label in setup.points:
        idx = int(clamp(idx, 0, len(cs)-1))
        ax.scatter([idx], [price], s=40, marker='o', zorder=6, color='#111827')
        offset = 13 if any(k in label.upper() for k in ('LOW', 'TRIGGER', 'REVERSAL')) else -17
        ax.annotate(label, (idx, price), xytext=(0, offset), textcoords='offset points',
                    ha='center', fontsize=7.5, fontweight='bold', color='#111827')

    # Explicit directional position marker: this is a LONG/SHORT trade setup, not just price levels.
    marker = '^' if setup.direction == 'LONG' else 'v'
    marker_color = '#15803d' if setup.direction == 'LONG' else '#b91c1c'
    signal_x = len(cs)-1
    ax.scatter([signal_x], [entry], marker=marker, s=180, color=marker_color,
               edgecolors='white', linewidths=1.2, zorder=9)
    label = 'LONG POSITION' if setup.direction == 'LONG' else 'SHORT POSITION'
    vertical = 28 if setup.direction == 'LONG' else -32
    ax.annotate(label, (signal_x, entry), xytext=(-48, vertical), textcoords='offset points',
                arrowprops=dict(arrowstyle='->', color=marker_color, lw=1.6),
                fontsize=9, fontweight='bold', color=marker_color,
                bbox=dict(boxstyle='round,pad=.25', fc='white', ec=marker_color, alpha=.95),
                ha='right', zorder=10)

    if show_title:
        ax.set_title(f'{setup.coin}USDT · {setup.strategy} · {setup.direction} POSITION · 5M',
                     fontsize=14, fontweight='bold', pad=12, color='#111827')
    ax.grid(True, alpha=.14, color='#94a3b8')
    ax.tick_params(labelsize=8, colors='#475569')
    ax.set_xlim(-1, len(cs)+1)
    ax.set_ylabel('Цена')


def make_chart(setup:Setup)->str:
    cs = setup.candles_5m[-96:]
    if len(cs) < 35:
        raise RuntimeError('Not enough candles for chart')
    path = f'/tmp/quantum_{setup.coin}_{int(time.time()*1000)}.png'
    fig, ax = plt.subplots(figsize=(15, 8.5), dpi=150)
    fig.patch.set_facecolor('white')
    ax.set_facecolor('white')
    draw_setup_chart(ax, setup)
    ax.text(.01, .965, f'15M structure · 1H context · {setup.pattern_name}',
            transform=ax.transAxes, va='top', fontsize=9.5, fontweight='bold', color='#111827')
    plt.tight_layout()
    fig.savefig(path, facecolor='white', bbox_inches='tight')
    plt.close(fig)
    return path

# ============================================================
# SIGNAL MESSAGE
# ============================================================

def build_signal_text(s:Setup)->str:
    entry=(s.entry_low+s.entry_high)/2
    risk=abs(entry-s.sl)/max(entry,1e-12)*100
    rr2=abs(s.tp2-entry)/max(abs(entry-s.sl),1e-12)
    move2=abs(s.tp2-entry)/max(entry,1e-12)*100
    side='🟢 LONG' if s.direction=='LONG' else '🔴 SHORT'
    oi_line = (f'📊 <b>Open Interest:</b> ${s.oi_usd/1_000_000:.1f}M'
               if s.oi_usd > 0 else '📊 <b>Open Interest:</b> unavailable')
    return (
        f'<b>{side} · {s.coin}USDT</b>\n'
        f'<b>{s.strategy}</b> · 5M / 15M / 1H\n\n'
        f'📌 <b>Сетап:</b> {s.pattern_name}\n'
        f'📈 <b>24H:</b> {s.change24:+.1f}% · Range {s.range24:.1f}%\n'
        f'⚡ <b>ATR:</b> 5M {s.atr5_pct:.2f}% · 15M {s.atr15_pct:.2f}%\n'
        f'💧 <b>Volume:</b> x{s.volume_ratio:.2f} · Turnover ${s.volume_24h/1_000_000:.1f}M\n'
        f'{oi_line} · BTC 1H: {s.btc_context}\n\n'
        f'🎯 <b>Вход:</b> <code>{fmt_price(s.entry_low)} – {fmt_price(s.entry_high)}</code>\n'
        f'🛑 <b>Стоп:</b> <code>{fmt_price(s.sl)}</code> · расстояние до стопа {risk:.2f}%\n'
        f'🎯 <b>TP1:</b> <code>{fmt_price(s.tp1)}</code>\n'
        f'🎯 <b>TP2:</b> <code>{fmt_price(s.tp2)}</code> · RR 1:{rr2:.1f} · потенциал {move2:.2f}%\n'
        f'🎯 <b>TP3:</b> <code>{fmt_price(s.tp3)}</code>\n'
        f'📦 <b>План фиксации:</b> TP1 {TP1_CLOSE_PCT:.0f}% · TP2 {TP2_CLOSE_PCT:.0f}% · TP3 {TP3_CLOSE_PCT:.0f}%\n\n'
        f'🧠 <b>Почему:</b> {s.reason}.\n'
        f'⏱ <b>Ожидаемое удержание:</b> {s.hold_hours}\n\n'
        f'⚠️ Внимательно проверьте сделку перед входом.\n'
        f'🛡 Подберите размер позиции так, чтобы денежный риск по депозиту не превышал ваш лимит.\n'
        f'🚫 Не догоняйте цену после ухода от зоны входа.'
    )


def insert_signal(s:Setup)->int:
    # Fail closed on malformed levels before publishing or persisting a signal.
    entry = (float(s.entry_low) + float(s.entry_high)) / 2.0
    if s.direction not in ('LONG', 'SHORT'):
        raise ValueError(f'Invalid signal direction: {s.direction}')
    if not (0 < s.entry_low <= s.entry_high and s.sl > 0 and s.tp1 > 0 and s.tp2 > 0 and s.tp3 > 0):
        raise ValueError('Invalid or non-positive entry/stop/target level')
    if s.direction == 'LONG':
        if not (s.sl < entry < s.tp1 < s.tp2 < s.tp3):
            raise ValueError('LONG levels must satisfy SL < entry < TP1 < TP2 < TP3')
    else:
        if not (s.tp3 < s.tp2 < s.tp1 < entry < s.sl):
            raise ValueError('SHORT levels must satisfy TP3 < TP2 < TP1 < entry < SL')
    created=now_ts()
    with db_lock:
        risk_pct = abs(((s.entry_low+s.entry_high)/2.0) - s.sl) / max((s.entry_low+s.entry_high)/2.0, 1e-12) * 100
        setup_json = json.dumps(asdict(s), ensure_ascii=False, separators=(',', ':'))
        cur=db.execute('''INSERT INTO signals(inst_id,direction,strategy,level,entry_low,entry_high,sl,tp1,tp2,tp3,score,status,created_at,expires_at,change24,range24,atr5_pct,atr15_pct,volume_ratio,risk_pct,setup_json)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                       (s.inst_id,s.direction,s.strategy,s.level,s.entry_low,s.entry_high,s.sl,s.tp1,s.tp2,s.tp3,s.score,'READY',created,created+READY_TTL_MINUTES*60,
                        s.change24,s.range24,s.atr5_pct,s.atr15_pct,s.volume_ratio,risk_pct,setup_json))
        sid=cur.lastrowid; db.commit()
    return sid


def send_signal(s:Setup)->bool:
    path=None; sid=None
    try:
        path=make_chart(s)
        sid=insert_signal(s)
        with open(path,'rb') as f:
            msg=bot.send_photo(CHANNEL_ID,f,caption=build_signal_text(s),parse_mode='HTML',show_caption_above_media=True,reply_markup=vote_keyboard(sid))
        with db_lock:
            db.execute('UPDATE signals SET message_id=? WHERE id=?', (msg.message_id, sid)); db.commit()
        pending[s.inst_id]=PendingSignal(s,sid,now_ts(),now_ts()+READY_TTL_MINUTES*60,msg.message_id)
        log.info('SIGNAL SENT | %s | %s | %s | score=%s | id=%s',s.coin,s.direction,s.strategy,s.score,sid)
        return True
    except Exception:
        log.exception('SIGNAL SEND FAILED | %s',s.inst_id)
        if sid is not None:
            with db_lock:
                db.execute("UPDATE signals SET status='SEND_FAILED' WHERE id=?",(sid,)); db.commit()
        return False
    finally:
        if path:
            try: os.remove(path)
            except OSError: pass

# ============================================================
# RESTORE LIVE SIGNAL STATE AFTER PROCESS RESTART
# ============================================================

def _setup_from_json(raw: str) -> Setup:
    d = json.loads(raw)
    d['candles_5m'] = [Candle(**c) for c in d.get('candles_5m', [])]
    d['points'] = [tuple(x) for x in d.get('points', [])]
    return Setup(**d)


def restore_live_signals():
    """Rebuild READY/ACTIVE in-memory state from durable SQLite rows."""
    with db_lock:
        rows = db.execute("""SELECT id, inst_id, status, created_at, expires_at,
                                  activated_at, tp1_hit, tp2_hit, tp3_hit,
                                  current_sl, sl, setup_json, message_id, realized_r,
                                  level, entry_low, entry_high, tp1, tp2, tp3, score, direction, strategy,
                                  change24, range24, atr5_pct, atr15_pct, volume_ratio
                           FROM signals WHERE status IN ('READY','ACTIVE')""").fetchall()
    restored = 0
    for row in rows:
        (sid, iid, status, created, expires, activated, h1, h2, h3,
         current_sl, initial_sl, raw, message_id, realized_r, level, entry_low, entry_high,
         tp1, tp2, tp3, score, legacy_direction, legacy_strategy, change24, range24, atr5_pct, atr15_pct, volume_ratio) = row
        try:
            if raw:
                setup = _setup_from_json(raw)
            else:
                # Backward-compatible recovery for READY/ACTIVE rows created
                # before setup_json existed in SQLite.
                coin = str(iid).split('-')[0]
                setup = Setup(
                    iid, coin, str(legacy_direction or 'LONG'), str(legacy_strategy or 'LEGACY'),
                    'LEGACY RESTORED', float(level or 0), 'LEVEL',
                    float(entry_low), float(entry_high), float(initial_sl),
                    float(tp1), float(tp2), float(tp3), int(score or 0),
                    'Сигнал восстановлен из старой записи БД; исходные детали сетапа отсутствуют',
                    0.0, float(change24 or 0), float(range24 or 0),
                    float(atr5_pct or 0), float(atr15_pct or 0), float(volume_ratio or 0), [], [], '1–8 ч'
                )
            mid = int(message_id or 0)
            restored_realized_r = float(realized_r or 0.0)
            if status == 'ACTIVE' and restored_realized_r == 0.0 and (h1 or h2):
                entry_mid = (float(entry_low) + float(entry_high)) / 2.0
                restored_realized_r = realized_gross_r(
                    setup.direction, entry_mid, setup.sl, bool(h1), bool(h2), False,
                    tp1=setup.tp1, tp2=setup.tp2, tp3=setup.tp3
                )
            if status == 'READY':
                if now_ts() >= float(expires or (created + READY_TTL_MINUTES*60)):
                    with db_lock:
                        db.execute("UPDATE signals SET status='EXPIRED', result='NO_ENTRY', closed_at=? WHERE id=? AND status='READY'", (now_ts(), sid)); db.commit()
                    continue
                pending[iid] = PendingSignal(setup, sid, float(created), float(expires), mid)
            else:
                restored_sl = float(current_sl) if current_sl is not None else float(initial_sl)
                if h1 and current_sl is None:
                    entry = (float(entry_low) + float(entry_high)) / 2.0
                    fee = entry * BREAKEVEN_BUFFER_PCT / 100.0
                    restored_sl = entry + fee if setup.direction == 'LONG' else entry - fee
                active[iid] = ActiveSignal(
                    setup, sid, float(activated or created), mid,
                    bool(h1), bool(h2), bool(h3),
                    restored_sl, restored_realized_r
                )
            restored += 1
        except Exception:
            log.exception('LIVE SIGNAL RESTORE FAILED | id=%s | %s', sid, iid)
    log.info('LIVE SIGNALS RESTORED | count=%d', restored)


# ============================================================
# SIGNAL LIFECYCLE
# ============================================================

def send_update(text:str):
    # VIP channel policy: no instant TP/SL/TIMEOUT reports.
    # Lifecycle results remain in SQLite and are included only in the 24H audit.
    log.info('LIFECYCLE UPDATE SUPPRESSED | %s', text.replace('\n', ' | '))


def price_in_entry(s:Setup, price:float)->bool:
    return s.entry_low <= price <= s.entry_high


def _lifecycle_for_candle(iid: str, last: Candle):
    if not last.confirmed:
        return
    # 1) Pending: wait for actual entry-zone touch on a CLOSED candle.
    p=pending.get(iid)
    newly_activated = False
    if p:
        s=p.setup
        if last.ts / 1000.0 > p.expires_at:
            with db_lock:
                db.execute("UPDATE signals SET status='EXPIRED',result='NO_ENTRY',closed_at=? WHERE id=? AND status='READY'",(now_ts(),p.signal_id)); db.commit()
            pending.pop(iid,None)
        elif (last.ts / 1000.0 >= p.created_at
              and (price_in_entry(s,last.close) or (last.low<=s.entry_high and last.high>=s.entry_low))):
            activation_time = last.ts / 1000.0
            with db_lock:
                db.execute("UPDATE signals SET status='ACTIVE',activated_at=? WHERE id=? AND status='READY'",(activation_time,p.signal_id)); db.commit()
            active[iid]=ActiveSignal(s,p.signal_id,activation_time,p.message_id,current_sl=s.sl)
            pending.pop(iid,None)
            newly_activated = True

    # Do not evaluate TP/SL on the same OHLC candle that first touched entry:
    # the candle cannot reveal whether entry happened before the target/stop.
    if newly_activated:
        return

    a=active.get(iid)
    if not a:
        return
    s=a.setup
    # Use the latest confirmed candle. If TP and SL are both inside the same
    # candle, the exact intrabar order is unknown; conservatively count SL first.
    if not last.confirmed:
        return
    long=s.direction=='LONG'
    hit1=last.high>=s.tp1 if long else last.low<=s.tp1
    hit2=last.high>=s.tp2 if long else last.low<=s.tp2
    hit3=last.high>=s.tp3 if long else last.low<=s.tp3
    live_sl = a.current_sl if a.current_sl is not None else s.sl
    stop=last.low<=live_sl if long else last.high>=live_sl
    both=stop and hit1

    with db_lock:
        row=db.execute('SELECT tp1_hit,tp2_hit,tp3_hit,realized_r FROM signals WHERE id=?',(a.signal_id,)).fetchone()
    if not row:
        return
    h1,h2,h3,realized_r=row
    realized_r=float(realized_r or 0.0)

    if both and not h1:
        # Conservative handling for an ambiguous candle.
        with db_lock:
            db.execute("UPDATE signals SET status='CLOSED_LOSS',result='SL',closed_at=?,r_multiple=-1 WHERE id=?",(now_ts(),a.signal_id)); db.commit()
        send_update(f'🔴 <b>STOP — {s.coin}USDT</b>\nСтоп затронут в свече с неоднозначным пересечением целей. Результат засчитан консервативно по SL.')
        active.pop(iid,None)
        return

    if h1 and stop:
        # TP1 was reached on an earlier confirmed candle: the live stop is now BE.
        # If BE and a later target are both inside this candle, count BE conservatively.
        with db_lock:
            db.execute("UPDATE signals SET status='CLOSED_WIN',result='PARTIAL_BE',closed_at=?,r_multiple=?,realized_r=? WHERE id=?",(now_ts(),realized_r,realized_r,a.signal_id)); db.commit()
        send_update(f'⚖️ <b>ЗАЩИЩЁННАЯ СДЕЛКА — {s.coin}USDT</b>\nОстаток закрылся по безубытку после частичной фиксации. Валовый результат: {realized_r:+.2f}R до комиссий.')
        active.pop(iid,None)
        return

    if hit1 and not h1:
        a.tp1_sent=True
        entry=(s.entry_low+s.entry_high)/2.0
        realized_r += TP1_CLOSE_PCT / 100.0 * reward_r(s.direction, entry, s.sl, s.tp1)
        a.realized_r = realized_r
        fee_buffer=entry*BREAKEVEN_BUFFER_PCT/100.0
        a.current_sl = entry + fee_buffer if long else entry - fee_buffer
        with db_lock:
            db.execute("UPDATE signals SET tp1_hit=1,current_sl=?,realized_r=? WHERE id=?", (a.current_sl, realized_r, a.signal_id)); db.commit()
        send_update(f'🟢 <b>TP1 — {s.coin}USDT</b>\nЦена достигла <code>{fmt_price(s.tp1)}</code>.\nЧасть позиции можно зафиксировать по своему плану.')
    # If TP1 is first detected on this candle, defer TP2/TP3 until a later
    # confirmed candle: OHLC data cannot prove the intrabar path or ordering.
    newly_hit_tp1 = hit1 and not h1
    if newly_hit_tp1:
        h1 = True
    if hit2 and not h2 and not newly_hit_tp1:
        entry=(s.entry_low+s.entry_high)/2.0
        realized_r += TP2_CLOSE_PCT / 100.0 * reward_r(s.direction, entry, s.sl, s.tp2)
        a.realized_r = realized_r
        with db_lock:
            db.execute("UPDATE signals SET tp2_hit=1,realized_r=? WHERE id=?",(realized_r,a.signal_id)); db.commit()
        a.tp2_sent=True
        h2 = True
        send_update(f'🚀 <b>TP2 — {s.coin}USDT</b>\nЦена достигла <code>{fmt_price(s.tp2)}</code>.\nОстаток сопровождаем по плану.')
    if hit3 and not h3 and not newly_hit_tp1:
        entry=(s.entry_low+s.entry_high)/2
        realized_r += TP3_CLOSE_PCT / 100.0 * reward_r(s.direction, entry, s.sl, s.tp3)
        a.realized_r = realized_r
        with db_lock:
            db.execute("UPDATE signals SET tp3_hit=1,status='CLOSED_WIN',result='TP3',closed_at=?,r_multiple=?,realized_r=? WHERE id=?",(now_ts(),realized_r,realized_r,a.signal_id)); db.commit()
        send_update(f'🏁 <b>TP3 — {s.coin}USDT</b>\nЦена достигла <code>{fmt_price(s.tp3)}</code>.\nРезультат зафиксирован: TP3.')
        active.pop(iid,None)
        return
    if stop:
        final_status = 'CLOSED_WIN' if h1 else 'CLOSED_LOSS'
        final_result = 'PARTIAL_BE' if h1 else 'SL'
        final_r = realized_r if h1 else -1.0
        with db_lock:
            db.execute("UPDATE signals SET status=?,result=?,closed_at=?,r_multiple=?,realized_r=? WHERE id=?",(final_status,final_result,now_ts(),final_r,final_r,a.signal_id)); db.commit()
        send_update(f'🔴 <b>СТОП — {s.coin}USDT</b>\nЦена достигла действующего стопа <code>{fmt_price(live_sl)}</code>.\nВаловый итог: {final_r:+.2f}R до комиссий.')
        active.pop(iid,None)
        return

    if last.ts / 1000.0 - a.activated_at > ACTIVE_MAX_HOURS*3600:
        entry=(s.entry_low+s.entry_high)/2.0
        timeout_r = realized_gross_r(s.direction, entry, s.sl, bool(h1), bool(h2), False,
                                     exit_price=last.close, tp1=s.tp1, tp2=s.tp2, tp3=s.tp3)
        with db_lock:
            db.execute("UPDATE signals SET status='TIMEOUT',result='TIMEOUT',closed_at=?,r_multiple=?,realized_r=? WHERE id=?",(last.ts / 1000.0,timeout_r,timeout_r,a.signal_id)); db.commit()
        send_update(f'⚪ <b>TIMEOUT — {s.coin}USDT</b>\nОстаток закрыт по последней подтверждённой цене. Валовый итог: {timeout_r:+.2f}R до комиссий.')
        active.pop(iid,None)


def lifecycle_for_symbol(iid: str, cs: List[Candle]):
    """Replay every newly closed 5m candle exactly once, including after downtime."""
    if not cs:
        return
    with db_lock:
        rows = db.execute(
            "SELECT id, status, COALESCE(last_processed_ts,0), COALESCE(activated_at,0) FROM signals WHERE inst_id=? AND status IN ('READY','ACTIVE') ORDER BY created_at DESC LIMIT 1",
            (iid,)
        ).fetchone()
    if not rows:
        return
    signal_id, status, cursor, activation_ts = rows
    cursor = int(cursor or 0)
    activation_ms = int(float(activation_ts or 0) * 1000)
    for candle in sorted((c for c in cs if c.confirmed), key=lambda x: x.ts):
        candle_ts = int(candle.ts)
        if candle_ts <= cursor:
            continue
        # Ignore candles completed before this signal was published.
        with db_lock:
            created_row = db.execute("SELECT created_at,status FROM signals WHERE id=?", (signal_id,)).fetchone()
        if not created_row or created_row[1] not in ('READY','ACTIVE'):
            break
        if candle_ts / 1000.0 >= float(created_row[0]) and (not activation_ms or candle_ts > activation_ms):
            _lifecycle_for_candle(iid, candle)
        with db_lock:
            db.execute("UPDATE signals SET last_processed_ts=? WHERE id=?", (candle_ts, signal_id))
            db.commit()
            state = db.execute("SELECT status FROM signals WHERE id=?", (signal_id,)).fetchone()
        cursor = candle_ts
        if not state or state[0] not in ('READY','ACTIVE'):
            break


def update_signal_results():
    symbols=set(pending)|set(active)
    for iid in list(symbols):
        try:
            cs=get_candles(iid,'5m',300)
            lifecycle_for_symbol(iid,cs)
        except Exception:
            log.exception('LIFECYCLE FAILED | %s',iid)

# ============================================================
# LIMITS / COOLDOWN
# ============================================================

def can_send(iid:str)->bool:
    # No global hourly/daily signal quota.
    # Keep only anti-duplicate protection for the same instrument/setup.
    if iid in pending or iid in active:
        return False
    with db_lock:
        row=db.execute(
            "SELECT created_at FROM signals "
            "WHERE inst_id=? AND status NOT IN ('SEND_FAILED') "
            "ORDER BY created_at DESC LIMIT 1",
            (iid,)
        ).fetchone()
    return not row or now_ts()-float(row[0])>=COOLDOWN_MINUTES*60

# ============================================================
# SCANNER
# ============================================================

def get_btc_market_context() -> str:
    """1H BTC trend is a soft market-regime context, never a hard signal gate."""
    try:
        btc = [c for c in get_candles('BTC-USDT-SWAP', '1H', 100) if c.confirmed]
        return trend_state(btc) or 'NEUTRAL'
    except Exception:
        log.exception('BTC CONTEXT FAILED | using NEUTRAL')
        return 'NEUTRAL'


def scan_market():
    instruments = get_instruments()
    tickers = get_tickers()
    oi_map = get_open_interest_map()
    for iid, ticker in tickers.items():
        ticker['oi_usd'] = oi_map.get(iid, 0.0)

    strategy_universes = build_strategy_universes(instruments, tickers)
    btc_context = get_btc_market_context()
    strategy_builders = [
        ('BREAKOUT + RETEST', build_breakout),
        ('TREND PULLBACK', build_pullback),
        ('MEAN REVERSION', build_mean_reversion),
    ]
    raw = []
    data_cache = {}
    seen_candidates = set()

    for strategy_name, builder in strategy_builders:
        pool = strategy_universes.get(strategy_name, [])
        for iid, ticker, _ in pool:
            seen_candidates.add(iid)
            if not can_send(iid):
                continue
            try:
                if iid not in data_cache:
                    data_cache[iid] = load_symbol(iid)
                data = data_cache[iid]
                c5, c15 = data['5m'], data['15m']
                if len(c5) < 70 or len(c15) < 70:
                    continue
                if atr_pct(c5) < MIN_ATR_5M_PCT or atr_pct(c15) < MIN_ATR_15M_PCT:
                    continue
                try:
                    setup = builder(iid, ticker, data)
                    if setup:
                        setup.oi_usd = float(ticker.get('oi_usd') or 0.0)
                        setup.btc_context = btc_context
                        if btc_context == setup.direction:
                            setup.score = min(99, setup.score + 1)
                        elif btc_context in ('LONG', 'SHORT') and btc_context != setup.direction:
                            setup.score = max(0, setup.score - 2)
                        setup.reason += f'; BTC 1H context: {btc_context}'
                        raw.append(setup)
                except Exception as exc:
                    log.debug('STRATEGY FAILED | %s | %s | %s', iid, builder.__name__, exc)
            except Exception as exc:
                log.warning('CANDIDATE FAILED | %s | %s', iid, exc)

    # Learning only ranks already-built setups; it never creates signals.
    apply_learning_to_raw(raw)

    # One best setup per instrument prevents competing signals without a daily quota.
    best = {}
    for setup in raw:
        if setup.inst_id not in best or setup.score > best[setup.inst_id].score:
            best[setup.inst_id] = setup
    final = sorted(best.values(), key=lambda x: x.score, reverse=True)
    log.info(
        'SCAN | market=%d unique_candidates=%d raw=%d final=%d',
        len(tickers), len(seen_candidates), len(raw), len(final)
    )

    for setup in final:
        if can_send(setup.inst_id):
            send_signal(setup)


# ============================================================
# SELF-LEARNING ENGINE — REPORT-ONLY DATA + BOUNDED ADAPTATION
# ============================================================

LEARNING_MIN_SAMPLES = int(os.getenv('LEARNING_MIN_SAMPLES', '12'))
LEARNING_MAX_ADJUSTMENT = float(os.getenv('LEARNING_MAX_ADJUSTMENT', '4.0'))
LEARNING_WINDOW_DAYS = int(os.getenv('LEARNING_WINDOW_DAYS', '30'))
LEARNING_MIN_R = float(os.getenv('LEARNING_MIN_R', '0.0'))


def _learning_rows(hours: int = 24):
    since = now_ts() - hours * 3600
    with db_lock:
        return db.execute(
            """SELECT strategy, result, r_multiple, volume_ratio, atr5_pct, atr15_pct,
                      change24, range24, risk_pct, created_at
               FROM signals
               WHERE created_at >= ? AND created_at <= ?
                 AND status NOT IN ('SEND_FAILED','READY','EXPIRED')
               ORDER BY created_at ASC""",
            (since, now_ts())
        ).fetchall()


def _bounded_adjustment(total_r: float, wins: int, losses: int, samples: int) -> float:
    if samples < LEARNING_MIN_SAMPLES:
        return 0.0
    wr = wins / max(wins + losses, 1)
    # Conservative: reward positive expectancy, penalize negative expectancy.
    expectancy = total_r / max(samples, 1)
    raw = expectancy * 2.0 + (wr - 0.5) * 4.0
    return clamp(raw, -LEARNING_MAX_ADJUSTMENT, LEARNING_MAX_ADJUSTMENT)


def run_learning_engine() -> Dict[str, float]:
    """Analyze recent outcomes and update bounded strategy adjustments.

    This layer never changes universe construction or any strategy builder.
    It only changes ranking of already-created raw setups after enough evidence.
    No single day can move a strategy by more than LEARNING_MAX_ADJUSTMENT points.
    """
    rows = _learning_rows(24 * LEARNING_WINDOW_DAYS)
    by_strategy = {}
    for strategy, result, r_multiple, *_ in rows:
        if result not in ('TP3','PARTIAL_BE','BE','SL','TIMEOUT'):
            continue
        st = by_strategy.setdefault(strategy, {'samples':0,'wins':0,'losses':0,'be':0,'timeouts':0,'r':0.0})
        st['samples'] += 1
        st['r'] += float(r_multiple or 0.0)
        if result in ('TP3','PARTIAL_BE'): st['wins'] += 1
        elif result == 'SL': st['losses'] += 1
        elif result == 'BE': st['be'] += 1
        elif result == 'TIMEOUT': st['timeouts'] += 1

    adjustments = {}
    now = now_ts()
    with db_lock:
        for strategy, st in by_strategy.items():
            adj = _bounded_adjustment(st['r'], st['wins'], st['losses'], st['samples'])
            adjustments[strategy] = adj
            db.execute(
                "INSERT INTO learning_history(run_at,window_hours,strategy,samples,wins,losses,be,timeouts,total_r,adjustment) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (now, 24*LEARNING_WINDOW_DAYS, strategy, st['samples'], st['wins'], st['losses'], st['be'], st['timeouts'], st['r'], adj)
            )
        db.execute(
            "INSERT OR REPLACE INTO bot_state(key,value) VALUES(?,?)",
            ('learning_adjustments', json.dumps(adjustments, ensure_ascii=False, sort_keys=True))
        )
        db.execute(
            "INSERT OR REPLACE INTO bot_state(key,value) VALUES(?,?)",
            ('learning_last_run', str(now))
        )
        db.commit()

    if adjustments:
        log.info('LEARNING | window=%dd | adjustments=%s', LEARNING_WINDOW_DAYS, adjustments)
    else:
        log.info('LEARNING | insufficient closed history for adaptation')
    return adjustments


def get_learning_adjustment(strategy: str) -> float:
    try:
        with db_lock:
            row = db.execute("SELECT value FROM bot_state WHERE key='learning_adjustments'").fetchone()
        if not row:
            return 0.0
        data = json.loads(row[0])
        return float(data.get(strategy, 0.0))
    except Exception:
        log.exception('LEARNING READ FAILED')
        return 0.0


def apply_learning_to_raw(raw: List[Setup]) -> List[Setup]:
    """Rank raw setups using bounded historical strategy evidence.

    Universe, candidate selection and all strategy builders remain untouched.
    """
    for s in raw:
        adj = get_learning_adjustment(s.strategy)
        if adj:
            s.score = int(clamp(s.score + adj, 0, 99))
    return raw


# ============================================================
# DAILY REPORT — once a day at 21:00, rolling last 24 hours
# ============================================================

def daily_report():
    """
    Единственный публичный дневной отчёт.

    ВАЖНО: этот блок НЕ участвует в поиске монет, построении сетапов,
    scoring, cooldown или отправке новых сигналов. Он работает только
    с уже сохранёнными сигналами из таблицы signals.

    В 21:00 Europe/Kyiv один раз формируется rolling-отчёт за последние
    24 часа. Состояния сделок берутся из единого механизма сопровождения; отчёт
    не изменяет сделки и не запускает параллельную сверку.
    """
    n = local_now()

    # Отправляем после 21:00 один раз за календарный день.
    # Если процесс был недоступен в 21:00, отчёт уйдёт при первом следующем цикле.
    if n.hour < 21:
        return

    day_key = n.date().isoformat()
    state_key = 'daily_report_21'

    with db_lock:
        daily_state_row = db.execute(
            "SELECT value FROM bot_state WHERE key=?", (state_key,)
        ).fetchone()

    # Ровно один отчёт за календарный день.
    if daily_state_row and daily_state_row[0] == day_key:
        return

    since_dt = n - timedelta(hours=24)
    since = since_dt.timestamp()
    until = n.timestamp()

    # Сигналы, опубликованные за последние 24 часа, плюс сделки,
    # активированные в этом окне и всё ещё находящиеся в работе.
    with db_lock:
        rows = db.execute(
            """SELECT id, inst_id, direction, strategy, status, result,
                      tp1_hit, tp2_hit, tp3_hit, created_at, activated_at,
                      expires_at, closed_at, r_multiple,
                      entry_low, entry_high, sl, tp1, tp2, tp3, current_sl, realized_r
               FROM signals
               WHERE (created_at >= ? OR status='ACTIVE' OR closed_at >= ?
                      OR (result='SL' AND post_stop_checked_at IS NULL))
                 AND created_at <= ?
                 AND status != 'SEND_FAILED'
               ORDER BY created_at ASC""",
            (since, since, until)
        ).fetchall()

    # ------------------------------------------------------------
    # REPORT-ONLY RECONCILIATION
    # ------------------------------------------------------------
    # Здесь НИКОГДА не вызывается scan_market() и не запускается поиск
    # новых монет. Проверяются только уже существующие signal_id.
    def reconcile_row(r):
        # Reporting is strictly read-only for trade lifecycle. All state changes
        # are performed by lifecycle_for_symbol() in the main event loop.
        return r

    reconciled_rows = [reconcile_row(r) for r in rows]

    # Audit what price did AFTER an SL. Diagnostic only: it never rewrites the
    # original stop outcome or automatically widens future stops.
    post_stop_stats = {'checked': 0, 'tp1': 0, 'tp2': 0, 'tp3': 0}
    for r in reconciled_rows:
        if r[5] != 'SL' or not r[12]:
            continue
        sid, inst_id, direction = r[0], r[1], r[2]
        entry = (float(r[14]) + float(r[15])) / 2.0
        if now_ts() - float(r[12]) < ACTIVE_MAX_HOURS * 3600:
            continue
        try:
            with db_lock:
                audit_row = db.execute(
                    "SELECT post_stop_checked_at FROM signals WHERE id=?", (sid,)
                ).fetchone()
            if not (audit_row and audit_row[0]):
                after = [
                    c for c in get_candles(inst_id, '5m', 300)
                    if c.confirmed and c.ts / 1000.0 > float(r[12])
                ]
                if after:
                    long = direction == 'LONG'
                    hit1 = any((c.high >= float(r[17]) if long else c.low <= float(r[17])) for c in after)
                    hit2 = any((c.high >= float(r[18]) if long else c.low <= float(r[18])) for c in after)
                    hit3 = any((c.high >= float(r[19]) if long else c.low <= float(r[19])) for c in after)
                    best = max(0.0, max(
                        ((c.high - entry) / entry * 100.0 if long else (entry - c.low) / entry * 100.0)
                        for c in after
                    ))
                    with db_lock:
                        db.execute(
                            """UPDATE signals SET post_stop_tp1_hit=?, post_stop_tp2_hit=?,
                               post_stop_tp3_hit=?, post_stop_best_move_pct=?, post_stop_checked_at=?
                               WHERE id=?""",
                            (int(hit1), int(hit2), int(hit3), best, now_ts(), sid)
                        )
                        db.commit()
            with db_lock:
                audit_row = db.execute(
                    "SELECT post_stop_checked_at, post_stop_tp1_hit, post_stop_tp2_hit, post_stop_tp3_hit FROM signals WHERE id=?",
                    (sid,)
                ).fetchone()
            if audit_row and audit_row[0]:
                post_stop_stats['checked'] += 1
                post_stop_stats['tp1'] += int(audit_row[1] or 0)
                post_stop_stats['tp2'] += int(audit_row[2] or 0)
                post_stop_stats['tp3'] += int(audit_row[3] or 0)
        except Exception:
            log.exception('POST-STOP AUDIT FAILED | id=%s | %s', sid, inst_id)

    # Learn only when the daily report itself is due, once per calendar day.
    if not (daily_state_row and daily_state_row[0] == day_key):
        run_learning_engine()
    total = len(reconciled_rows)

    def is_entry(r):
        return (
            r[10] is not None
            or r[4] in ('ACTIVE', 'CLOSED_WIN', 'CLOSED_LOSS', 'CLOSED_BE', 'TIMEOUT')
        )

    entries = sum(1 for r in reconciled_rows if is_entry(r))
    tp1_count = sum(1 for r in reconciled_rows if int(r[6] or 0))
    # Terminal outcome is mutually exclusive: one signal -> one final bucket.
    positive_count = sum(1 for r in reconciled_rows if r[5] in ('TP3', 'PARTIAL_BE'))
    protected_count = sum(1 for r in reconciled_rows if r[5] == 'PARTIAL_BE')
    tp2_count = sum(1 for r in reconciled_rows if int(r[7] or 0))
    tp3_count = sum(1 for r in reconciled_rows if int(r[8] or 0))
    be_count = sum(1 for r in reconciled_rows if r[5] == 'BE')
    stop_count = sum(1 for r in reconciled_rows if r[5] == 'SL')
    timeout_count = sum(1 for r in reconciled_rows if r[5] == 'TIMEOUT')
    no_entry_count = sum(1 for r in reconciled_rows if r[5] == 'NO_ENTRY')
    open_count = sum(1 for r in reconciled_rows if r[4] in ('READY', 'ACTIVE') and r[5] not in ('TP2','TP3','BE','SL','TIMEOUT','NO_ENTRY'))
    closed_count = positive_count + be_count + stop_count + timeout_count
    win_rate = positive_count / closed_count * 100 if closed_count else 0.0
    total_r = sum(
        float(r[13] or 0.0)
        for r in reconciled_rows
        if r[5] in ('TP3', 'PARTIAL_BE', 'BE', 'SL', 'TIMEOUT')
    )

    # ------------------------------------------------------------
    # STRATEGY STATISTICS
    # ------------------------------------------------------------
    strategy_stats = {}
    for r in reconciled_rows:
        name = r[3] or 'Без названия'
        st = strategy_stats.setdefault(name, {
            'signals': 0, 'entries': 0, 'positive': 0, 'be': 0,
            'sl': 0, 'timeout': 0, 'no_entry': 0, 'r': 0.0
        })
        st['signals'] += 1
        if is_entry(r):
            st['entries'] += 1
        if r[5] in ('TP3', 'PARTIAL_BE'):
            st['positive'] += 1
            if r[5] == 'PARTIAL_BE':
                st['protected'] = st.get('protected', 0) + 1
        elif r[5] == 'BE':
            st['be'] += 1
        elif r[5] == 'SL':
            st['sl'] += 1
        elif r[5] == 'TIMEOUT':
            st['timeout'] += 1
        elif r[5] == 'NO_ENTRY':
            st['no_entry'] += 1
        if r[5] in ('TP3', 'PARTIAL_BE', 'BE', 'SL', 'TIMEOUT'):
            st['r'] += float(r[13] or 0.0)

    # Сначала суммарный R, затем количество плюсовых при равенстве.
    ranked = sorted(
        strategy_stats.items(),
        key=lambda x: (x[1]['r'], x[1]['positive'], -x[1]['sl']),
        reverse=True
    )

    # ------------------------------------------------------------
    # PDF REPORT — SUMMARY + ONE PAGE PER SIGNAL
    # ------------------------------------------------------------
    pdf_path = f'/tmp/quantum_daily_{day_key}.pdf'
    pdf_created = False

    try:
        from matplotlib.backends.backend_pdf import PdfPages

        with PdfPages(pdf_path) as pdf:
            fig = plt.figure(figsize=(11.7, 8.3), dpi=110)
            fig.patch.set_facecolor('white')
            fig.text(0.06, 0.93, 'QUANTUM | VIP SCANNER', fontsize=22, fontweight='bold')
            fig.text(0.06, 0.885, 'ДНЕВНОЙ АУДИТ · ПОСЛЕДНИЕ 24 ЧАСА', fontsize=14, fontweight='bold')
            fig.text(0.06, 0.845, f'{since_dt:%d.%m.%Y %H:%M} — {n:%d.%m.%Y %H:%M} · {TIMEZONE}', fontsize=9.5)

            summary = (
                f'Сигналов опубликовано: {total}\n'
                f'Фактических входов: {entries}\n'
                f'План фиксации: TP1 {TP1_CLOSE_PCT:.0f}% · TP2 {TP2_CLOSE_PCT:.0f}% · TP3 {TP3_CLOSE_PCT:.0f}%\n'
                f'Положительных закрытий (TP3 или частичная фиксация + BE): {positive_count}\n'
                f'Защищённых после частичной фиксации: {protected_count}\n'
                f'Чистый BE без фиксации: {be_count}\n'
                f'Стоп: {stop_count}\n'
                f'TIMEOUT: {timeout_count}\n'
                f'Без входа: {no_entry_count}\n'
                f'Остались в работе: {open_count}\n\n'
                f'TP1 достигнут: {tp1_count}\n'
                f'TP2 достигнут: {tp2_count}\n'
                f'TP3 достигнут: {tp3_count}\n'
                f'После SL: проверено {post_stop_stats["checked"]} · '
                f'позже TP1 {post_stop_stats["tp1"]} · TP2 {post_stop_stats["tp2"]} · TP3 {post_stop_stats["tp3"]}\n'
                f'Win rate закрытых: {win_rate:.1f}%\n'
                f'Суммарный валовый результат до комиссий: {total_r:+.2f}R'
            )
            fig.text(0.07, 0.74, summary, fontsize=13, va='top', linespacing=1.65)

            y = 0.72
            fig.text(0.52, y, 'РЕЗУЛЬТАТ ПО СТРАТЕГИЯМ', fontsize=13, fontweight='bold')
            y -= 0.055
            if ranked:
                for name, st in ranked:
                    closed = st['positive'] + st['be'] + st['sl'] + st['timeout']
                    wr = st['positive'] / closed * 100 if closed else 0.0
                    block = (
                        f'{name}\n'
                        f'  сигналов {st["signals"]} · входов {st["entries"]} · '
                        f'плюс {st["positive"]} · BE {st["be"]} · стоп {st["sl"]}\n'
                        f'  Win rate {wr:.1f}% · результат {st["r"]:+.2f}R'
                    )
                    fig.text(0.52, y, block, fontsize=10.5, va='top', linespacing=1.35)
                    y -= 0.12
                best_name, best = ranked[0]
                fig.text(0.52, max(0.08, y), f'ЛУЧШАЯ: {best_name} · {best["r"]:+.2f}R', fontsize=12, fontweight='bold')
            else:
                fig.text(0.52, y, 'За последние 24 часа сигналов не было.', fontsize=11)

            fig.text(
                0.06, 0.055,
                'Аудит: только существующие сигналы из БД + подтверждённые 5M свечи OKX. '
                'Если TP и SL попали в одну свечу до TP1 — консервативно засчитывается SL.',
                fontsize=8.5
            )
            pdf.savefig(fig, bbox_inches='tight')
            plt.close(fig)

            # Detailed per-signal audit pages with the saved setup chart and levels.
            for r in reconciled_rows:
                sid, inst_id, direction, strategy, status, result = r[:6]
                entry_low, entry_high, sl, tp1, tp2, tp3 = map(float, r[14:20])
                with db_lock:
                    setup_row = db.execute('SELECT setup_json FROM signals WHERE id=?', (sid,)).fetchone()
                setup_data = {}
                if setup_row and setup_row[0]:
                    try:
                        setup_data = json.loads(setup_row[0])
                    except (TypeError, ValueError):
                        setup_data = {}
                candles = setup_data.get('candles_5m', [])
                fig = plt.figure(figsize=(11.7, 8.3), dpi=110)
                fig.patch.set_facecolor('white')
                coin = str(setup_data.get('coin') or inst_id.replace('-USDT-SWAP',''))
                fig.text(0.06, 0.94, f'{coin}USDT · {direction}', fontsize=20, fontweight='bold')
                fig.text(0.06, 0.895, f'{strategy} · signal #{sid} · score {setup_data.get("score", "—")}', fontsize=11)
                fig.text(0.06, 0.85, f'Status: {status} · Result: {result or "IN PROGRESS"} · TP1/2/3: {r[6]}/{r[7]}/{r[8]}', fontsize=10)
                fig.text(0.06, 0.80, f'Entry: {entry_low:.10g} – {entry_high:.10g}    SL: {sl:.10g}', fontsize=10)
                fig.text(0.06, 0.765, f'TP1: {tp1:.10g}    TP2: {tp2:.10g}    TP3: {tp3:.10g}    OI: ${float(setup_data.get("oi_usd") or 0)/1_000_000:.1f}M    BTC 1H: {setup_data.get("btc_context", "NEUTRAL")}', fontsize=10)
                ax = fig.add_axes([0.08, 0.20, 0.86, 0.50])
                try:
                    if setup_data and candles:
                        saved_setup = _setup_from_json(json.dumps(setup_data, ensure_ascii=False))
                        draw_setup_chart(ax, saved_setup, show_title=False)
                        if int(r[6] or 0) and r[20] is not None and abs(float(r[20]) - float(sl)) > 1e-12:
                            ax.axhline(float(r[20]), color='#0f766e', linewidth=1.8, linestyle='-.', alpha=.95)
                            ax.text(.99, float(r[20]), f' CURRENT BE/SL {fmt_price(float(r[20]))}',
                                    transform=ax.get_yaxis_transform(), ha='right', va='bottom',
                                    fontsize=7.5, color='#0f766e', fontweight='bold')
                    else:
                        ax.text(0.5, 0.5, 'Saved chart data unavailable', ha='center', va='center', transform=ax.transAxes)
                except Exception as chart_exc:
                    log.warning('DAILY REPORT CHART FALLBACK | signal=%s | %s', sid, chart_exc)
                    ax.text(0.5, 0.5, 'Не удалось восстановить свечной график', ha='center', va='center', transform=ax.transAxes)
                ax.set_title('Свечной график сигнала · 5M · LONG/SHORT · S/R')
                reason = str(setup_data.get('reason') or 'Причина сетапа не сохранена')
                fig.text(0.08, 0.12, 'Причина: ' + reason[:450], fontsize=9, wrap=True)
                post_line = ''
                if result == 'SL':
                    with db_lock:
                        ps = db.execute(
                            'SELECT post_stop_tp1_hit,post_stop_tp2_hit,post_stop_tp3_hit,post_stop_best_move_pct,post_stop_checked_at FROM signals WHERE id=?',
                            (sid,)
                        ).fetchone()
                    if ps and ps[4]:
                        post_line = f' · После SL: TP1/TP2/TP3 {ps[0]}/{ps[1]}/{ps[2]} · max favorable {float(ps[3] or 0):.2f}%'
                    else:
                        post_line = ' · Анализ движения после SL: ожидает полного 12H окна'
                fig.text(0.08, 0.065, f'Валовый результат до комиссий: {float(r[13] or 0):+.2f}R · Время входа: {datetime.fromtimestamp(float(r[10]), ZoneInfo(TIMEZONE)).strftime("%d.%m %H:%M") if r[10] else "нет входа"}{post_line}', fontsize=8.5)
                pdf.savefig(fig, bbox_inches='tight')
                plt.close(fig)

        pdf_created = True

    except Exception:
        log.exception('DAILY PDF REPORT FAILED')
        try:
            if os.path.exists(pdf_path):
                os.remove(pdf_path)
        except OSError:
            pass

    if not pdf_created:
        log.error('DAILY REPORT NOT SENT | PDF generation failed')
        return

    best_line = ''
    if ranked:
        best_name, best = ranked[0]
        best_line = f'\n🏆 Лучшая стратегия: <b>{escape(str(best_name))}</b> · {best["r"]:+.2f}R'

    caption = (
        f'📊 <b>QUANTUM — ДНЕВНОЙ ОТЧЁТ</b>\n'
        f'🕘 {since_dt:%d.%m.%Y %H:%M} — {n:%d.%m.%Y %H:%M}\n\n'
        f'📨 Сигналов: <b>{total}</b>\n'
        f'🎯 Входов: <b>{entries}</b>\n'
        f'🟢 Финальных TP3: <b>{positive_count}</b>\n'
        f'🛡 Защищённых после частичной фиксации: <b>{protected_count}</b>\n'
        f'⚖️ Чистый BE: <b>{be_count}</b>\n'
        f'🔴 Стопов: <b>{stop_count}</b>\n'
        f'⚪ TIMEOUT: <b>{timeout_count}</b>\n'
        f'⌛ Без входа: <b>{no_entry_count}</b>\n'
        f'🔄 В работе: <b>{open_count}</b>\n'
        f'📈 Win rate закрытых: <b>{win_rate:.1f}%</b>\n'
        f'💰 Валовый результат до комиссий: <b>{total_r:+.2f}R</b>\n'
        f'🔎 После стопов проверено: {post_stop_stats["checked"]} · '
        f'позже TP1/TP2/TP3: {post_stop_stats["tp1"]}/{post_stop_stats["tp2"]}/{post_stop_stats["tp3"]}'
        f'{best_line}\n\n'
        f'📎 PDF: сводка + отдельная страница с графиком и уровнями по каждому сигналу. TP2 — промежуточная цель, TP3 — финальная.'
    )

    try:
        with open(pdf_path, 'rb') as f:
            bot.send_document(CHANNEL_ID, f, caption=caption, parse_mode='HTML')

        with db_lock:
            db.execute(
                "INSERT OR REPLACE INTO bot_state(key,value) VALUES(?,?)",
                (state_key, day_key)
            )
            db.commit()

        log.info(
            'DAILY REPORT SENT | date=%s | signals=%d | entries=%d | positive=%d | '
            'be=%d | stop=%d | timeout=%d | no_entry=%d | open=%d | R=%.2f | pdf=%s',
            day_key, total, entries, positive_count, be_count, stop_count,
            timeout_count, no_entry_count, open_count, total_r, pdf_path
        )

    except Exception:
        log.exception('DAILY REPORT SEND FAILED')
    finally:
        try:
            if os.path.exists(pdf_path):
                os.remove(pdf_path)
        except OSError:
            pass


# ============================================================
# HEALTH / STARTUP
# ============================================================

def startup_healthcheck():
    """Fail closed: do not start scanning/polling if a required service is down."""
    payload = okx_get('/api/v5/public/time', {})
    if str(payload.get('code')) != '0' or not payload.get('data'):
        raise RuntimeError('OKX health check failed; startup aborted')
    log.info('HEALTH | OKX API OK')
    me = bot.get_me()
    if not me or not getattr(me, 'id', None):
        raise RuntimeError('Telegram health check failed; startup aborted')
    log.info('HEALTH | TELEGRAM OK | @%s', me.username)


# ============================================================
# MAIN
# ============================================================

def main():
    log.info('============================================================')
    log.info('QUANTUM INTRADAY SWING ENGINE V2.3 STARTED')
    log.info('Strategies: Breakout+Retest | Trend Pullback | Mean Reversion (Extreme Reversal disabled)')
    log.info('Risk model: structural stops; TP2 >= %.2f%% and >= 1:%.2f RR', MIN_TARGET_MOVE_PCT, MIN_TARGET_RR)
    log.info('Learning: bounded 30D strategy adaptation after raw setup generation')
    log.info('Context: batched OI snapshot + soft BTC 1H regime adjustment')
    log.info('Partial exits: TP1 %.0f%% | TP2 %.0f%% | TP3 %.0f%% (gross R before fees)',
             TP1_CLOSE_PCT, TP2_CLOSE_PCT, TP3_CLOSE_PCT)
    log.info('Reports: retryable daily 24H audit; TP2 is milestone, TP3 final')
    log.info('Universe: gainers | losers | volatile | new active | high turnover; full deduplicated pool')
    log.info('============================================================')
    acquire_instance_lock()
    init_db()
    startup_healthcheck()
    restore_live_signals()
    start_telegram_polling()

    while True:
        try:
            update_signal_results()
            daily_report()
            scan_market()
        except KeyboardInterrupt:
            log.info('STOPPED')
            break
        except Exception:
            log.exception('MAIN LOOP ERROR')
        time.sleep(SCAN_INTERVAL_SECONDS)


if __name__=='__main__':
    main()
