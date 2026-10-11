import os
import time
import math
import logging
import sqlite3
import threading
from dataclasses import dataclass, field
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
# QUANTUM INTRADAY SWING ENGINE V2
# ------------------------------------------------------------
# Purpose:
#   Find high-momentum crypto futures setups for 1-8 hour holds.
#   5M = trigger/entry, 15M = structure, 1H = context.
#
# Strategies:
#   1) Extreme Reversal       strong mover -> exhaustion -> reversal
#   2) Breakout + Retest      structure break -> retest -> continuation
#   3) Trend Pullback          1H trend -> 15M pullback -> 5M trigger
#   4) Pre-Breakout            horizontal level compression -> early entry
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
MIN_24H_TURNOVER_USD = float(os.getenv('MIN_24H_TURNOVER_USD', '5000000'))
PREFERRED_TURNOVER_USD = float(os.getenv('PREFERRED_TURNOVER_USD', '15000000'))
TOP_GAINERS = int(os.getenv('TOP_GAINERS', '20'))
TOP_LOSERS = int(os.getenv('TOP_LOSERS', '20'))
TOP_VOLATILE = int(os.getenv('TOP_VOLATILE', '25'))
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
MAX_RISK_PCT = float(os.getenv('MAX_RISK_PCT', '1.00'))
MIN_RISK_PCT = float(os.getenv('MIN_RISK_PCT', '0.20'))
TP1_R = float(os.getenv('TP1_R', '1.20'))
TP2_R = float(os.getenv('TP2_R', '2.00'))
TP3_R = float(os.getenv('TP3_R', '3.00'))
MIN_SCORE = int(os.getenv('MIN_SCORE', '85'))
MIN_PULLBACK_VOLUME_RATIO = float(os.getenv('MIN_PULLBACK_VOLUME_RATIO', '1.05'))
MAX_TP1_R = float(os.getenv('MAX_TP1_R', '2.5'))
MAX_TP2_R = float(os.getenv('MAX_TP2_R', '5.0'))
MAX_TP3_R = float(os.getenv('MAX_TP3_R', '6.0'))

# ---------------- runtime ----------------
SCAN_INTERVAL_SECONDS = int(os.getenv('SCAN_INTERVAL_SECONDS', '30'))
LIFECYCLE_POLL_SECONDS = max(2, int(os.getenv('LIFECYCLE_POLL_SECONDS', '5')))
HTTP_TIMEOUT = int(os.getenv('HTTP_TIMEOUT', '10'))
REQUEST_RETRIES = int(os.getenv('REQUEST_RETRIES', '3'))
CANDLE_CACHE_SECONDS = int(os.getenv('CANDLE_CACHE_SECONDS', '15'))
READY_TTL_MINUTES = int(os.getenv('READY_TTL_MINUTES', '20'))
ACTIVE_MAX_HOURS = int(os.getenv('ACTIVE_MAX_HOURS', '8'))
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
    timeout_notified: bool = False

pending: Dict[str, PendingSignal] = {}
active: Dict[str, ActiveSignal] = {}
state_lock = threading.RLock()
candle_cache: Dict[Tuple[str, str], Tuple[float, List[Candle]]] = {}

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

SIGNAL_COLUMNS = {
    'inst_id':'TEXT', 'direction':'TEXT', 'strategy':'TEXT', 'level':'REAL',
    'entry_low':'REAL', 'entry_high':'REAL', 'sl':'REAL', 'tp1':'REAL', 'tp2':'REAL', 'tp3':'REAL',
    'score':'INTEGER', 'status':'TEXT', 'created_at':'REAL', 'activated_at':'REAL',
    'expires_at':'REAL', 'tp1_hit':'INTEGER DEFAULT 0', 'tp2_hit':'INTEGER DEFAULT 0',
    'tp3_hit':'INTEGER DEFAULT 0', 'result':'TEXT DEFAULT \'\'', 'closed_at':'REAL',
    'r_multiple':'REAL DEFAULT 0'
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
            # OKX gives base/quote volume fields depending on instrument.
            # Use the larger valid estimate only after sanity checking.
            turnover_a = vol_ccy * last
            turnover_b = vol * last
            if turnover_a <= 0:
                turnover = turnover_b
            elif turnover_b > 0 and turnover_a > turnover_b * 1000:
                turnover = turnover_b
            else:
                turnover = turnover_a
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


def get_candles(inst_id: str, bar: str, limit: int = 180) -> List[Candle]:
    key = (inst_id, bar)
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
    return {
        '1h': get_candles(inst_id, '1H', 160),
        '15m': get_candles(inst_id, '15m', 180),
        '5m': get_candles(inst_id, '5m', 200),
    }

# ============================================================
# MARKET UNIVERSE
# ============================================================

def build_universe(instruments: Dict[str, dict], tickers: Dict[str, dict]):
    base = []
    for iid, t in tickers.items():
        if iid not in instruments:
            continue
        if t['vol24h_usd'] < MIN_24H_TURNOVER_USD:
            continue
        if t['range24h_pct'] < MIN_ACTIVE_24H_RANGE_PCT:
            continue
        activity = clamp((t['vol24h_usd'] / PREFERRED_TURNOVER_USD) * 20, 0, 20)
        movement = clamp(abs(t['change24h_pct']) * 1.15, 0, 40)
        volatility = clamp(t['range24h_pct'] * 1.8, 0, 40)
        score = activity + movement + volatility
        base.append((iid, t, score))

    gainers = sorted(base, key=lambda z: z[1]['change24h_pct'], reverse=True)[:TOP_GAINERS]
    losers = sorted(base, key=lambda z: z[1]['change24h_pct'])[:TOP_LOSERS]
    volatile = sorted(base, key=lambda z: z[1]['range24h_pct'], reverse=True)[:TOP_VOLATILE]
    cutoff = int((local_now() - timedelta(days=NEW_ACTIVE_DAYS)).timestamp() * 1000)
    new_active = [z for z in base if instruments[z[0]].get('listTime', 0) >= cutoff]
    new_active = sorted(new_active, key=lambda z: z[2], reverse=True)[:NEW_ACTIVE_COUNT]

    pool = {}
    for group in (gainers, losers, volatile, new_active):
        for item in group:
            pool[item[0]] = item
    ranked = sorted(pool.values(), key=lambda z: z[2], reverse=True)[:DEEP_SCAN_LIMIT]
    log.info('UNIVERSE | gainers=%d losers=%d volatile=%d new=%d deep=%d',
             len(gainers), len(losers), len(volatile), len(new_active), len(ranked))
    return ranked

# ============================================================
# STRUCTURE ENGINE
# ============================================================

def pivots(cs: List[Candle], left: int = 2, right: int = 2) -> List[Pivot]:
    out = []
    if len(cs) < left + right + 1:
        return out
    for i in range(left, len(cs) - right):
        window = cs[i-left:i+right+1]
        h = cs[i].high
        l = cs[i].low
        # Require a unique local extreme; flat plateaus must not inflate level touches.
        highs = [c.high for c in window]
        lows = [c.low for c in window]
        if h == max(highs) and highs.count(h) == 1:
            out.append(Pivot(i, h, 'HIGH'))
        if l == min(lows) and lows.count(l) == 1:
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


def count_level_touches(cs: List[Candle], level: float, kind: str,
                       lookback: int = 80, tolerance_pct: float = 0.22) -> int:
    """Count distinct candle interactions with a horizontal level, not just clustered pivots."""
    if level <= 0 or not cs:
        return 0
    sample = cs[-lookback:]
    tol = tolerance_pct / 100.0
    count = 0
    for c in sample:
        if kind == 'HIGH':
            touched = abs(c.high - level) / level <= tol
        else:
            touched = abs(c.low - level) / level <= tol
        if touched:
            count += 1
    return count


def monotonic_pressure(values: List[float], direction: str, min_steps: int = 2) -> bool:
    """A tolerant short sequence trend; ignores one small counter-step but needs structure."""
    if len(values) < 4:
        return False
    diffs = [values[i+1] - values[i] for i in range(len(values)-1)]
    if direction == 'UP':
        good = sum(d > 0 for d in diffs)
        return good >= min_steps and values[-1] > values[0]
    good = sum(d < 0 for d in diffs)
    return good >= min_steps and values[-1] < values[0]


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
    # Market context is deliberately capped: it must not outweigh the actual entry pattern.
    score = 35
    ch = abs(ticker['change24h_pct'])
    rng = ticker['range24h_pct']
    if ch >= 20: score += 3
    if ch >= STRONG_24H_MOVE: score += 3
    if ch >= 45: score += 2
    if rng >= 10: score += 3
    if rng >= 20: score += 2
    if atr_pct(c5) >= MIN_ATR_5M_PCT: score += 3
    if atr_pct(c15) >= MIN_ATR_15M_PCT: score += 3
    if vr >= 1.2: score += 3
    if vr >= 1.5: score += 2
    if vr >= 2.0: score += 2
    if ticker['vol24h_usd'] >= PREFERRED_TURNOVER_USD: score += 3
    return min(score, 65)


def validate_risk(entry: float, sl: float) -> bool:
    risk_pct = abs(entry - sl) / max(entry, 1e-12) * 100
    return MIN_RISK_PCT <= risk_pct <= MAX_RISK_PCT


def target_levels(direction: str, entry: float, sl: float, c15: List[Candle]):
    """Build bounded targets around R multiples; never use a far-away level as TP1."""
    risk = abs(entry - sl)
    if risk <= 0:
        return None
    sign = 1 if direction == 'LONG' else -1

    def r_target(mult):
        return entry + sign * risk * mult

    # Base targets are deterministic and bounded for the intended 1–8h holding window.
    levels = cluster_levels(c15)
    relevant_kind = 'HIGH' if direction == 'LONG' else 'LOW'
    structural = sorted(
        [sign * (p - entry) / risk for p, kind, _touches in levels
         if kind == relevant_kind and sign * (p - entry) > 0]
    )
    # If the nearest opposing structure is less than 0.9R away, there is not
    # enough clear room for a clean first target: skip the setup.
    if structural and structural[0] < 0.9:
        return None
    # Replace a base target only with a nearby, usable structural level in its own R band.
    bands = [(0.9, 1.7, TP1_R), (1.5, 3.2, TP2_R), (2.4, MAX_TP3_R, TP3_R)]
    chosen = []
    for low_r, high_r, preferred_r in bands:
        options = [r for r in structural if low_r <= r <= high_r]
        options = [r for r in options if not chosen or r > chosen[-1] + 0.25]
        chosen.append(min(options, key=lambda r: abs(r - preferred_r)) if options else preferred_r)

    # Enforce strictly increasing targets without stretching them arbitrarily.
    chosen[1] = max(chosen[1], chosen[0] + 0.35)
    chosen[2] = max(chosen[2], chosen[1] + 0.35)
    if chosen[2] > MAX_TP3_R:
        return None
    return tuple(r_target(r) for r in chosen)


def validate_setup_geometry(direction: str, entry_low: float, entry_high: float,
                            sl: float, tp1: float, tp2: float, tp3: float) -> bool:
    """Validate stop side, full entry-zone risk, target ordering and realistic RR."""
    if not all(math.isfinite(x) and x > 0 for x in
               (entry_low, entry_high, sl, tp1, tp2, tp3)):
        return False
    if entry_low >= entry_high:
        return False
    entry = (entry_low + entry_high) / 2.0
    if direction == 'LONG':
        if not (sl < entry_low < entry_high < tp1 < tp2 < tp3):
            return False
        worst_entry = entry_high
    elif direction == 'SHORT':
        if not (tp3 < tp2 < tp1 < entry_low < entry_high < sl):
            return False
        worst_entry = entry_low
    else:
        return False
    worst_risk_pct = abs(worst_entry - sl) / max(worst_entry, 1e-12) * 100
    if not (MIN_RISK_PCT <= worst_risk_pct <= MAX_RISK_PCT):
        return False
    risk = abs(entry - sl)
    rr1, rr2, rr3 = abs(tp1-entry)/risk, abs(tp2-entry)/risk, abs(tp3-entry)/risk
    return (0.8 <= rr1 <= MAX_TP1_R and
            1.4 <= rr2 <= MAX_TP2_R and
            2.2 <= rr3 <= MAX_TP3_R)


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
        return _diag_reject('build_reversal', 685)
    ch = ticker['change24h_pct']
    if abs(ch) < MIN_24H_MOVE_FOR_REVERSAL:
        return _diag_reject('build_reversal', 688)
    if atr_pct(c5) < MIN_ATR_5M_PCT or atr_pct(c15) < MIN_ATR_15M_PCT:
        return _diag_reject('build_reversal', 690)

    m1h, m2h, _ = impulse_stats(c5)
    direction = 'SHORT' if ch > 0 else 'LONG'
    if direction == 'SHORT' and m2h < MIN_2H_IMPULSE:
        return _diag_reject('build_reversal', 695)
    if direction == 'LONG' and m2h > -MIN_2H_IMPULSE:
        return _diag_reject('build_reversal', 697)

    extreme = local_extreme(c5, 'SHORT' if direction == 'SHORT' else 'LONG', 60)
    if extreme is None:
        return _diag_reject('build_reversal', 701)
    cur = c5[-1]
    prev = c5[-2]
    if true_range_pct(cur) > max(0.75, atr_pct(c5) * 2.5):
        return _diag_reject('build_reversal', 705)
    vr = vol_ratio(c5)
    if vr < MIN_REVERSAL_VOLUME_RATIO:
        return _diag_reject('build_reversal', 708)

    if direction == 'SHORT':
        retrace = pct_move(extreme.high, cur.close)
        upper, lower = wick_ratio(cur)
        # Strong rejection from the high + bearish confirmation.
        if retrace < 0.55:
            return _diag_reject('build_reversal', 715)
        if not (cur.close < cur.open and cur.close < prev.low * 1.001):
            return _diag_reject('build_reversal', 717)
        if body_ratio(cur) < 0.42 or close_location(cur) > 0.48:
            return _diag_reject('build_reversal', 719)
        if upper < 0.10 and cur.high < max(c.high for c in c5[-6:-1]):
            return _diag_reject('build_reversal', 721)
        sl = max(extreme.high, max(c.high for c in c5[-4:])) + atr(c5) * 0.28
        level = extreme.high
        point_label = 'EXTREME HIGH'
        pattern = 'ТОП МОВЕР → ПЕРВЫЙ РАЗВОРОТ SHORT'
        reason = f'сильный рост 24H {ch:+.1f}% → 2H {m2h:+.1f}% → экстремум → медвежье подтверждение'
        bonuses = [10 if ch >= STRONG_24H_MOVE else 5, 8 if m2h >= 8 else 4, 7 if vr >= 1.5 else 0]
    else:
        retrace = pct_move(cur.close, extreme.low)
        upper, lower = wick_ratio(cur)
        if retrace < 0.55:
            return _diag_reject('build_reversal', 732)
        if not (cur.close > cur.open and cur.close > prev.high * 0.999):
            return _diag_reject('build_reversal', 734)
        if body_ratio(cur) < 0.42 or close_location(cur) < 0.52:
            return _diag_reject('build_reversal', 736)
        if lower < 0.10 and cur.low > min(c.low for c in c5[-6:-1]):
            return _diag_reject('build_reversal', 738)
        sl = min(extreme.low, min(c.low for c in c5[-4:])) - atr(c5) * 0.28
        level = extreme.low
        point_label = 'EXTREME LOW'
        pattern = 'ТОП ЛУЗЕР → ПЕРВЫЙ РАЗВОРОТ LONG'
        reason = f'сильное падение 24H {ch:+.1f}% → 2H {m2h:+.1f}% → экстремум → бычье подтверждение'
        bonuses = [10 if ch <= -STRONG_24H_MOVE else 5, 8 if m2h <= -8 else 4, 7 if vr >= 1.5 else 0]

    entry = cur.close
    if abs(entry - level) / max(entry, 1e-12) * 100 > MAX_ENTRY_CHASE_PCT * 1.8:
        return _diag_reject('build_reversal', 748)
    if not validate_risk(entry, sl):
        return _diag_reject('build_reversal', 750)
    tps = target_levels(direction, entry, sl, c15)
    if not tps:
        return _diag_reject('build_reversal', 753)
    score = final_score(base_score(ticker, c5, c15, vr), bonuses, [])
    # Higher-timeframe agreement adds quality but does not create the setup.
    tstate = trend_state(c15)
    if (direction == 'LONG' and tstate == 'LONG') or (direction == 'SHORT' and tstate == 'SHORT'):
        score = min(99, score + 3)
    if score < MIN_SCORE:
        return _diag_reject('build_reversal', 760)
    lo, hi = entry_zone(entry, atr(c5), direction)
    if not validate_setup_geometry(direction, lo, hi, sl, *tps):
        return _diag_reject('build_reversal', 763)
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
        return _diag_reject('build_breakout', 780)
    vr = vol_ratio(c5)
    if vr < MIN_BREAKOUT_VOLUME:
        return _diag_reject('build_breakout', 783)

    levels = cluster_levels(c15, 0.30)
    if not levels:
        return _diag_reject('build_breakout', 787)
    cur, prev = c5[-1], c5[-2]
    if true_range_pct(cur) > max(0.75, atr_pct(c5) * 2.5):
        return _diag_reject('build_breakout', 790)
    direction = None
    level = None
    touches = 0

    # Require a completed breakout candle followed by a separate completed retest
    # candle. The previous candle must CLOSE beyond the level; a wick alone is not
    # accepted as a breakout.
    highs = [(p, t) for p, k, t in levels
             if k == 'HIGH' and t >= 2 and p < cur.close * 1.012]
    lows = [(p, t) for p, k, t in levels
            if k == 'LOW' and t >= 2 and p > cur.close * 0.988]

    long_candidates = [(p, t) for p, t in highs if prev.close > p * 1.001 and cur.close > p]
    short_candidates = [(p, t) for p, t in lows if prev.close < p * 0.999 and cur.close < p]

    if long_candidates:
        # Closest already-broken resistance below price.
        level, touches = max(long_candidates, key=lambda x: x[0])
        direction = 'LONG'
    elif short_candidates:
        # Closest already-broken support above price.
        level, touches = min(short_candidates, key=lambda x: x[0])
        direction = 'SHORT'
    else:
        return _diag_reject('build_breakout', 815)

    if direction == 'LONG':
        # Separate retest: candle trades into the level and closes bullish above it.
        if not (cur.low <= level * 1.003 and cur.low >= level * 0.992):
            return _diag_reject('build_breakout', 820)
        if not (cur.close > level and cur.close > cur.open and close_location(cur) >= 0.58):
            return _diag_reject('build_breakout', 822)
        if abs(cur.close - level) / level * 100 > MAX_ENTRY_CHASE_PCT:
            return _diag_reject('build_breakout', 824)
        sl = min(level, cur.low) - atr(c5) * 0.30
        reason = f'15M сопротивление {touches}× → отдельный закрытый пробой → ретест уровня → бычье 5M подтверждение; объём x{vr:.2f}'
    else:
        if not (cur.high >= level * 0.997 and cur.high <= level * 1.008):
            return _diag_reject('build_breakout', 829)
        if not (cur.close < level and cur.close < cur.open and close_location(cur) <= 0.42):
            return _diag_reject('build_breakout', 831)
        if abs(cur.close - level) / level * 100 > MAX_ENTRY_CHASE_PCT:
            return _diag_reject('build_breakout', 833)
        sl = max(level, cur.high) + atr(c5) * 0.30
        reason = f'15M поддержка {touches}× → отдельный закрытый пробой → ретест уровня → медвежье 5M подтверждение; объём x{vr:.2f}'

    # Do not trade a counter-trend retest against a clearly established 1H trend.
    htf = trend_state(c1h)
    if htf and htf != direction:
        return _diag_reject('build_breakout', 840)

    entry = cur.close
    if not validate_risk(entry, sl):
        return _diag_reject('build_breakout', 844)
    tps = target_levels(direction, entry, sl, c15)
    if not tps:
        return _diag_reject('build_breakout', 847)
    score = final_score(base_score(ticker, c5, c15, vr),
                        [10, min(8, touches * 2), 6 if vr >= 1.8 else 2,
                         3 if htf == direction else 0], [])
    if score < MIN_SCORE:
        return _diag_reject('build_breakout', 852)
    lo, hi = entry_zone(entry, atr(c5), direction)
    if not validate_setup_geometry(direction, lo, hi, sl, *tps):
        return _diag_reject('build_breakout', 855)
    return Setup(iid, get_coin(iid), direction, 'BREAKOUT + RETEST', 'ПРОБОЙ + РЕТЕСТ',
                 level, 'BROKEN LEVEL', lo, hi, sl, *tps, score, reason,
                 ticker['vol24h_usd'], ticker['change24h_pct'], ticker['range24h_pct'],
                 atr_pct(c5), atr_pct(c15), vr, c5[-96:],
                 [(len(c5[-96:])-2, level, 'RETEST'), (len(c5[-96:])-1, entry, 'CONFIRM')],
                 '1–6 ч')


# ============================================================
# STRATEGY 3 — TREND PULLBACK
# ============================================================

def build_pullback(iid, ticker, data) -> Optional[Setup]:
    c5,c15,c1h=data['5m'],data['15m'],data['1h']
    if min(len(c5),len(c15),len(c1h))<70:
        return _diag_reject('build_pullback', 871)
    trend=trend_state(c1h)
    if not trend:
        return _diag_reject('build_pullback', 874)
    e20_15=ema([c.close for c in c15],20)
    if len(e20_15)<10:
        return _diag_reject('build_pullback', 877)
    cur,prev=c5[-1],c5[-2]
    vr=vol_ratio(c5)
    if vr < MIN_PULLBACK_VOLUME_RATIO:
        return _diag_reject('build_pullback', 881)
    recent=c15[-10:-2]
    if len(recent)<5:
        return _diag_reject('build_pullback', 884)

    if trend=='LONG':
        pull_low=min(c.low for c in recent)
        # Pullback must test EMA20 but not become a deep structural breakdown.
        if not any(c.low <= e20_15[-3]*1.003 for c in recent):
            return _diag_reject('build_pullback', 890)
        pull_depth_pct = abs(pull_low - e20_15[-3]) / max(e20_15[-3], 1e-12) * 100
        max_pull_depth_pct = min(2.0, max(0.75, atr_pct(c15) * 1.75))
        if pull_depth_pct > max_pull_depth_pct:
            return _diag_reject('build_pullback', 894)
        if not (cur.close > e20_15[-1] and cur.close > prev.high and cur.close > cur.open):
            return _diag_reject('build_pullback', 896)
        # Reject an oversized trigger candle that would make the entry late.
        if true_range_pct(cur) > max(0.60, atr_pct(c5) * 2.2):
            return _diag_reject('build_pullback', 899)
        level=pull_low
        sl=pull_low-atr(c5)*0.30
        reason=f'1H восходящий тренд → контролируемый 15M откат к EMA20 → 5M reclaim/подтверждение; объём x{vr:.2f}'
    else:
        pull_high=max(c.high for c in recent)
        if not any(c.high >= e20_15[-3]*0.997 for c in recent):
            return _diag_reject('build_pullback', 906)
        pull_depth_pct = abs(pull_high - e20_15[-3]) / max(e20_15[-3], 1e-12) * 100
        max_pull_depth_pct = min(2.0, max(0.75, atr_pct(c15) * 1.75))
        if pull_depth_pct > max_pull_depth_pct:
            return _diag_reject('build_pullback', 910)
        if not (cur.close < e20_15[-1] and cur.close < prev.low and cur.close < cur.open):
            return _diag_reject('build_pullback', 912)
        if true_range_pct(cur) > max(0.60, atr_pct(c5) * 2.2):
            return _diag_reject('build_pullback', 914)
        level=pull_high
        sl=pull_high+atr(c5)*0.30
        reason=f'1H нисходящий тренд → контролируемый 15M откат к EMA20 → 5M reclaim/подтверждение; объём x{vr:.2f}'

    entry=cur.close
    if not validate_risk(entry,sl):
        return _diag_reject('build_pullback', 921)
    if abs(entry-level)/max(entry,1e-12)*100>MAX_ENTRY_CHASE_PCT*1.5:
        return _diag_reject('build_pullback', 923)
    tps=target_levels(trend,entry,sl,c15)
    if not tps:
        return _diag_reject('build_pullback', 926)
    score=final_score(base_score(ticker,c5,c15,vr),[10,7 if vr>=1.2 else 3,5,3],[])
    if score<MIN_SCORE:
        return _diag_reject('build_pullback', 929)
    lo,hi=entry_zone(entry,atr(c5),trend)
    if not validate_setup_geometry(trend, lo, hi, sl, *tps):
        return _diag_reject('build_pullback', 932)
    return Setup(iid,get_coin(iid),trend,'TREND PULLBACK','ТРЕНДОВЫЙ ОТКАТ',level,'PULLBACK LOW' if trend=='LONG' else 'PULLBACK HIGH',lo,hi,sl,*tps,score,reason,
                 ticker['vol24h_usd'],ticker['change24h_pct'],ticker['range24h_pct'],atr_pct(c5),atr_pct(c15),vr,c5[-96:],
                 [(len(c5[-96:])-1,entry,'TRIGGER')],'1–8 ч')

# ============================================================
# STRATEGY 4 — PRE-BREAKOUT (HORIZONTAL LEVEL COMPRESSION)
# ============================================================

def build_prebreakout(iid, ticker, data) -> Optional[Setup]:
    c5, c15, c1h = data['5m'], data['15m'], data['1h']
    if min(len(c5), len(c15), len(c1h)) < 70:
        return _diag_reject('build_prebreakout', 944)
    if atr_pct(c5) < MIN_ATR_5M_PCT or atr_pct(c15) < MIN_ATR_15M_PCT:
        return _diag_reject('build_prebreakout', 946)

    levels = cluster_levels(c15, 0.30)
    if not levels:
        return _diag_reject('build_prebreakout', 950)
    cur = c5[-1]
    vr = vol_ratio(c5)
    if vr < max(0.85, MIN_VOLUME_RATIO * 0.75):
        return _diag_reject('build_prebreakout', 954)

    # Use the nearest repeatedly tested horizontal level on the correct side.
    support, resistance = nearest_support_resistance(cur.close, levels)
    recent = c5[-6:]
    if len(recent) < 6:
        return _diag_reject('build_prebreakout', 960)

    # Compression means recent bars are not expanding into a late, chase-prone move.
    prior_ranges = [max(c.high-c.low, 0.0) for c in recent[:3]]
    recent_ranges = [max(c.high-c.low, 0.0) for c in recent[3:]]
    prior_avg = sum(prior_ranges) / max(len(prior_ranges), 1)
    recent_avg = sum(recent_ranges) / max(len(recent_ranges), 1)
    compressed = prior_avg > 0 and recent_avg <= prior_avg * 0.95

    direction = None
    level = None
    touches = 0
    if resistance and resistance[2] >= 2:
        candidate = resistance[0]
        actual_touches = count_level_touches(c15, candidate, 'HIGH', lookback=80, tolerance_pct=0.22)
        distance_pct = (candidate - cur.close) / candidate * 100
        # Pressure must build into the level while every recent close remains below it.
        higher_lows = monotonic_pressure([c.low for c in recent[-4:]], 'UP')
        pressure = cur.close > sum(c.close for c in recent[-3:]) / 3.0 and cur.close >= recent[-2].close
        below_level = all(c.close < candidate for c in recent[-4:])
        if (actual_touches >= 3 and 0.08 <= distance_pct <= 0.55 and
                higher_lows and pressure and below_level and compressed):
            direction, level, touches = 'LONG', candidate, actual_touches

    if direction is None and support and support[2] >= 2:
        candidate = support[0]
        actual_touches = count_level_touches(c15, candidate, 'LOW', lookback=80, tolerance_pct=0.22)
        distance_pct = (cur.close - candidate) / candidate * 100
        lower_highs = monotonic_pressure([c.high for c in recent[-4:]], 'DOWN')
        pressure = cur.close < sum(c.close for c in recent[-3:]) / 3.0 and cur.close <= recent[-2].close
        above_level = all(c.close > candidate for c in recent[-4:])
        if (actual_touches >= 3 and 0.08 <= distance_pct <= 0.55 and
                lower_highs and pressure and above_level and compressed):
            direction, level, touches = 'SHORT', candidate, actual_touches

    if not direction:
        return _diag_reject('build_prebreakout', 996)

    # Avoid pre-breakout entries directly against an established 1H trend.
    htf = trend_state(c1h)
    if htf and htf != direction:
        return _diag_reject('build_prebreakout', 1001)

    if direction == 'LONG':
        # Price must be pressing resistance, but no 5M close may have broken it.
        if cur.close >= level or cur.high >= level * 1.0015:
            return _diag_reject('build_prebreakout', 1006)
        sl = min(c.low for c in recent[-4:]) - atr(c5) * 0.22
        reason = (f'горизонтальное сопротивление {touches}× → сжатие под уровнем → '
                  f'повышающиеся минимумы → PRE-BREAKOUT до подтверждённого пробоя; объём x{vr:.2f}')
    else:
        if cur.close <= level or cur.low <= level * 0.9985:
            return _diag_reject('build_prebreakout', 1012)
        sl = max(c.high for c in recent[-4:]) + atr(c5) * 0.22
        reason = (f'горизонтальная поддержка {touches}× → сжатие над уровнем → '
                  f'понижающиеся максимумы → PRE-BREAKOUT до подтверждённого пробоя; объём x{vr:.2f}')

    entry = cur.close
    if abs(entry - level) / max(entry, 1e-12) * 100 > MAX_ENTRY_CHASE_PCT:
        return _diag_reject('build_prebreakout', 1019)
    if not validate_risk(entry, sl):
        return _diag_reject('build_prebreakout', 1021)

    # For PRE-BREAKOUT the horizontal level is the trigger barrier, not an
    # opposing obstacle to TP1. Targets must sit beyond that barrier.
    risk = abs(entry - sl)
    if risk <= 0:
        return _diag_reject('build_prebreakout', 1027)
    level_r = abs(level - entry) / risk
    tp1_r = max(TP1_R, level_r + 0.25)
    tp2_r = max(TP2_R, tp1_r + 0.45)
    tp3_r = max(TP3_R, tp2_r + 0.45)
    if tp1_r > MAX_TP1_R or tp2_r > MAX_TP2_R or tp3_r > MAX_TP3_R:
        return _diag_reject('build_prebreakout', 1033)
    sign = 1 if direction == 'LONG' else -1
    tps = (entry + sign * risk * tp1_r,
           entry + sign * risk * tp2_r,
           entry + sign * risk * tp3_r)

    score = final_score(
        base_score(ticker, c5, c15, vr),
        [10, min(8, touches * 2), 6 if vr >= 1.2 else 2,
         3 if htf == direction else 0],
        []
    )
    if score < MIN_SCORE:
        return _diag_reject('build_prebreakout', 1046)
    lo, hi = entry_zone(entry, atr(c5), direction)
    if direction == 'LONG' and hi >= level:
        return _diag_reject('build_prebreakout', 1049)
    if direction == 'SHORT' and lo <= level:
        return _diag_reject('build_prebreakout', 1051)
    if not validate_setup_geometry(direction, lo, hi, sl, *tps):
        return _diag_reject('build_prebreakout', 1053)

    return Setup(
        iid, get_coin(iid), direction, 'PRE-BREAKOUT', 'СЖАТИЕ У ГОРИЗОНТАЛЬНОГО УРОВНЯ',
        level, 'RESISTANCE' if direction == 'LONG' else 'SUPPORT',
        lo, hi, sl, *tps, score, reason,
        ticker['vol24h_usd'], ticker['change24h_pct'], ticker['range24h_pct'],
        atr_pct(c5), atr_pct(c15), vr, c5[-96:],
        [(len(c5[-96:])-3, level, 'LEVEL'), (len(c5[-96:])-1, entry, 'PRE-BREAKOUT')],
        '1–6 ч'
    )


STRATEGIES=(build_reversal,build_breakout,build_pullback,build_prebreakout)

# Counts exact strategy exit lines so persistent raw=0 can be diagnosed from logs.
strategy_reject_counts = {}

def _diag_reject(strategy_name: str, source_line: int):
    key = (strategy_name, source_line)
    strategy_reject_counts[key] = strategy_reject_counts.get(key, 0) + 1
    return None

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
        try:
            log.info('TELEGRAM POLLING START')
            bot.infinity_polling(skip_pending=True, timeout=20, long_polling_timeout=20)
        except Exception:
            log.exception('TELEGRAM POLLING STOPPED')
    t=threading.Thread(target=runner,name='telegram-polling',daemon=True)
    t.start()

# ============================================================
# CHART
# ============================================================

def make_chart(setup:Setup)->str:
    cs=setup.candles_5m[-96:]
    if len(cs)<35:
        raise RuntimeError('Not enough candles for chart')
    path=f'/tmp/quantum_{setup.coin}_{int(time.time()*1000)}.png'
    fig,ax=plt.subplots(figsize=(15,8.5),dpi=150)
    fig.patch.set_facecolor('white'); ax.set_facecolor('white')
    width=.58
    for i,c in enumerate(cs):
        rising=c.close>=c.open
        color='#16a34a' if rising else '#dc2626'
        ax.vlines(i,c.low,c.high,color=color,linewidth=1.1,zorder=2)
        y=min(c.open,c.close)
        h=max(abs(c.close-c.open),c.close*0.00002)
        ax.add_patch(Rectangle((i-width/2,y),width,h,facecolor=color,edgecolor=color,linewidth=.5,zorder=3))

    def hline(price,color,label,lw=1.8,ls='--'):
        ax.axhline(price,color=color,linewidth=lw,linestyle=ls,zorder=4)
        ax.text(len(cs)-1.2,price,f'  {label} {fmt_price(price)}',ha='right',va='bottom',fontsize=9,fontweight='bold',color=color,
                bbox=dict(facecolor='white',alpha=.82,edgecolor='none',pad=1.5))

    hline(setup.level,'#7c3aed',setup.level_kind,1.9,'-')
    hline((setup.entry_low+setup.entry_high)/2,'#2563eb','ВХОД',2.4,'-')
    ax.axhspan(setup.entry_low,setup.entry_high,alpha=.10,color='#2563eb',zorder=1)
    hline(setup.sl,'#dc2626','СТОП',2.1,'--')
    hline(setup.tp1,'#16a34a','TP1',1.5,':')
    hline(setup.tp2,'#16a34a','TP2',1.7,':')
    hline(setup.tp3,'#15803d','TP3',1.9,'--')

    for idx,p,label in setup.points:
        idx=int(clamp(idx,0,len(cs)-1))
        ax.scatter([idx],[p],s=48,marker='o',zorder=7,color='#111827')
        offset=16 if 'LOW' in label or 'TRIGGER' in label or 'REVERSAL' in label else -20
        ax.annotate(label,(idx,p),xytext=(0,offset),textcoords='offset points',ha='center',fontsize=9,fontweight='bold',color='#111827')

    title=f'{setup.coin}USDT · {setup.strategy} · {setup.direction} · 5M'
    ax.set_title(title,fontsize=18,fontweight='bold',pad=14,color='#111827')
    ax.text(.01,.965,f'15M structure · 1H context · {setup.pattern_name}',transform=ax.transAxes,va='top',fontsize=10.5,fontweight='bold',color='#111827')
    ax.grid(True,alpha=.14,color='#94a3b8')
    ax.tick_params(labelsize=9,colors='#475569')
    ax.set_xlim(-1,len(cs)); plt.tight_layout()
    fig.savefig(path,facecolor='white',bbox_inches='tight'); plt.close(fig)
    return path

# ============================================================
# SIGNAL MESSAGE
# ============================================================

def build_signal_text(s:Setup)->str:
    entry=(s.entry_low+s.entry_high)/2
    worst_entry = s.entry_high if s.direction == 'LONG' else s.entry_low
    risk=abs(worst_entry-s.sl)/max(worst_entry,1e-12)*100
    rr2=abs(s.tp2-entry)/max(abs(entry-s.sl),1e-12)
    side='🟢 LONG' if s.direction=='LONG' else '🔴 SHORT'
    return (
        f'<b>{side} · {s.coin}USDT</b>\n'
        f'<b>{s.strategy}</b> · 5M / 15M / 1H\n\n'
        f'📌 <b>Сетап:</b> {s.pattern_name}\n'
        f'📈 <b>24H:</b> {s.change24:+.1f}% · Range {s.range24:.1f}%\n'
        f'⚡ <b>ATR:</b> 5M {s.atr5_pct:.2f}% · 15M {s.atr15_pct:.2f}%\n'
        f'💧 <b>Volume:</b> x{s.volume_ratio:.2f} · Turnover ${s.volume_24h/1_000_000:.1f}M\n\n'
        f'🎯 <b>Вход:</b> <code>{fmt_price(s.entry_low)} – {fmt_price(s.entry_high)}</code>\n'
        f'🛑 <b>Стоп:</b> <code>{fmt_price(s.sl)}</code> · риск по цене от края зоны {risk:.2f}%\n'
        f'🎯 <b>TP1:</b> <code>{fmt_price(s.tp1)}</code>\n'
        f'🎯 <b>TP2:</b> <code>{fmt_price(s.tp2)}</code> · RR 1:{rr2:.1f}\n'
        f'🎯 <b>TP3:</b> <code>{fmt_price(s.tp3)}</code>\n\n'
        f'🧠 <b>Почему:</b> {s.reason}.\n'
        f'⏱ <b>Ожидаемое удержание:</b> {s.hold_hours}\n\n'
        f'⚠️ Внимательно проверьте сделку перед входом.\n'
        f'🛡 Рассчитайте размер позиции так, чтобы убыток при стопе не превышал <b>1% депозита</b>.\n'
        f'🚫 Не догоняйте цену после ухода от зоны входа.'
    )


def insert_signal(s:Setup)->int:
    created=now_ts()
    with db_lock:
        cur=db.execute('''INSERT INTO signals(inst_id,direction,strategy,level,entry_low,entry_high,sl,tp1,tp2,tp3,score,status,created_at,expires_at)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                       (s.inst_id,s.direction,s.strategy,s.level,s.entry_low,s.entry_high,s.sl,s.tp1,s.tp2,s.tp3,s.score,'READY',created,created+READY_TTL_MINUTES*60))
        sid=cur.lastrowid; db.commit()
    return sid


def send_signal(s:Setup)->bool:
    path=None; sid=None
    try:
        path=make_chart(s)
        sid=insert_signal(s)
        with open(path,'rb') as f:
            msg=bot.send_photo(CHANNEL_ID,f,caption=build_signal_text(s),parse_mode='HTML',show_caption_above_media=True,reply_markup=vote_keyboard(sid))
        with state_lock:
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
# SIGNAL LIFECYCLE
# ============================================================

def send_update(text:str):
    try:
        bot.send_message(CHANNEL_ID,text,parse_mode='HTML')
    except Exception:
        log.exception('TELEGRAM UPDATE FAILED')


def price_in_entry(s:Setup, price:float)->bool:
    return s.entry_low <= price <= s.entry_high


def _lifecycle_for_symbol_unlocked(iid: str, cs: List[Candle], live_price: Optional[float] = None):
    """Track a signal using live last price; candle data is not treated as a fill."""
    if live_price is None or not math.isfinite(live_price) or live_price <= 0:
        # Missing/invalid ticker data is not evidence of a stop, target or timeout.
        return

    p = pending.get(iid)
    if p:
        s = p.setup
        if now_ts() > p.expires_at:
            with db_lock:
                db.execute("UPDATE signals SET status='EXPIRED' WHERE id=? AND status='READY'", (p.signal_id,))
                db.commit()
            pending.pop(iid, None)
        elif price_in_entry(s, live_price):
            with db_lock:
                db.execute("UPDATE signals SET status='ACTIVE',activated_at=? WHERE id=? AND status='READY'",
                           (now_ts(), p.signal_id))
                db.commit()
            active[iid] = ActiveSignal(s, p.signal_id, now_ts(), p.message_id)
            pending.pop(iid, None)

    a = active.get(iid)
    if not a:
        return
    s = a.setup
    long = s.direction == 'LONG'
    entry = (s.entry_low + s.entry_high) / 2.0

    with db_lock:
        row = db.execute(
            'SELECT tp1_hit,tp2_hit,tp3_hit,status FROM signals WHERE id=?',
            (a.signal_id,)
        ).fetchone()
    if not row:
        active.pop(iid, None)
        return
    h1, h2, h3, status = row

    # Before TP1, use the original stop. After TP1, the virtual stop is moved to entry.
    effective_stop = entry if h1 else s.sl
    stop_hit = live_price <= effective_stop if long else live_price >= effective_stop

    # TP1 and stop cannot be simultaneously inferred from a single sampled last price.
    if stop_hit:
        if h1:
            with db_lock:
                db.execute("UPDATE signals SET status='CLOSED_BE',result='BE',closed_at=?,r_multiple=0 WHERE id=?",
                           (now_ts(), a.signal_id))
                db.commit()
            send_update(
                f'⚪ <b>БЕЗУБЫТОК — {s.coin}USDT</b>\n'
                f'После TP1 цена вернулась к виртуальному стопу на входе '
                f'<code>{fmt_price(entry)}</code>.\n'
                f'Это сопровождение сигнала; биржевой ордер бот не передвигает.'
            )
        else:
            with db_lock:
                db.execute("UPDATE signals SET status='CLOSED_LOSS',result='SL',closed_at=?,r_multiple=-1 WHERE id=?",
                           (now_ts(), a.signal_id))
                db.commit()
            send_update(
                f'🔴 <b>STOP — {s.coin}USDT</b>\n'
                f'Текущая цена достигла исходного стопа <code>{fmt_price(s.sl)}</code>.\n'
                f'Результат отслеживания сигнала зафиксирован по стопу.'
            )
        active.pop(iid, None)
        return

    # Targets are checked in order. Because last price is sampled every scan cycle,
    # notices are near-real-time but are not exchange-side order execution receipts.
    hit1 = live_price >= s.tp1 if long else live_price <= s.tp1
    hit2 = live_price >= s.tp2 if long else live_price <= s.tp2
    hit3 = live_price >= s.tp3 if long else live_price <= s.tp3

    if hit1 and not h1:
        with db_lock:
            db.execute("UPDATE signals SET tp1_hit=1 WHERE id=?", (a.signal_id,))
            db.commit()
        h1 = 1
        send_update(
            f'🟢 <b>TP1 — {s.coin}USDT</b>\n'
            f'Цена достигла <code>{fmt_price(s.tp1)}</code>.\n'
            f'В сопровождении сигнала стоп перенесён на безубыток '
            f'<code>{fmt_price(entry)}</code> (виртуальный стоп).'
        )

    if hit2 and not h2:
        with db_lock:
            db.execute("UPDATE signals SET tp2_hit=1 WHERE id=?", (a.signal_id,))
            db.commit()
        h2 = 1
        send_update(
            f'🚀 <b>TP2 — {s.coin}USDT</b>\n'
            f'Цена достигла <code>{fmt_price(s.tp2)}</code>.\n'
            f'Сопровождаем оставшуюся часть по торговому плану.'
        )

    if hit3 and not h3:
        risk = max(abs(entry - s.sl), 1e-12)
        r = abs(s.tp3 - entry) / risk
        with db_lock:
            db.execute("UPDATE signals SET tp3_hit=1,status='CLOSED_WIN',result='TP3',closed_at=?,r_multiple=? WHERE id=?",
                       (now_ts(), r, a.signal_id))
            db.commit()
        send_update(
            f'🏁 <b>TP3 — {s.coin}USDT</b>\n'
            f'Цена достигла <code>{fmt_price(s.tp3)}</code>.\n'
            f'Сигнал завершён по TP3; это не подтверждение фактического исполнения ордеров.'
        )
        active.pop(iid, None)
        return

    if now_ts() - a.activated_at > ACTIVE_MAX_HOURS * 3600 and not a.timeout_notified:
        with db_lock:
            db.execute("UPDATE signals SET status='TIME_REVIEW' WHERE id=? AND status='ACTIVE'", (a.signal_id,))
            db.commit()
        a.timeout_notified = True
        send_update(
            f'⏳ <b>ПРОВЕРКА ПО ВРЕМЕНИ — {s.coin}USDT</b>\n'
            f'Прошло более {ACTIVE_MAX_HOURS} ч. Цена не достигла стопа или TP3.\n'
            f'Сделка НЕ объявлена закрытой: проверьте её вручную. Бот продолжит отслеживание '
            f'и сообщит при достижении цели/виртуального стопа.'
        )


def lifecycle_for_symbol(iid: str, cs: List[Candle], live_price: Optional[float] = None):
    with state_lock:
        _lifecycle_for_symbol_unlocked(iid, cs, live_price)


def update_signal_results():
    with state_lock:
        symbols = set(pending) | set(active)
    if not symbols:
        return
    try:
        tickers = get_tickers()
    except Exception:
        # Do not infer outcomes from stale candles when the live price API failed.
        log.exception('LIFECYCLE TICKER SNAPSHOT FAILED')
        return
    for iid in list(symbols):
        try:
            ticker = tickers.get(iid)
            if not ticker:
                continue
            lifecycle_for_symbol(iid, [], ticker.get('last'))
        except Exception:
            log.exception('LIFECYCLE FAILED | %s', iid)


def start_lifecycle_monitor():
    def runner():
        log.info('LIFECYCLE MONITOR START | interval=%ss', LIFECYCLE_POLL_SECONDS)
        while True:
            try:
                update_signal_results()
            except Exception:
                log.exception('LIFECYCLE MONITOR ERROR')
            time.sleep(LIFECYCLE_POLL_SECONDS)
    threading.Thread(target=runner, name='lifecycle-monitor', daemon=True).start()


def recover_open_signals():
    """Restore READY/ACTIVE signal tracking after a process restart."""
    with db_lock:
        rows = db.execute(
            "SELECT id,inst_id,direction,strategy,level,entry_low,entry_high,sl,tp1,tp2,tp3,"
            "score,status,created_at,activated_at,expires_at FROM signals "
            "WHERE status IN ('READY','ACTIVE','TIME_REVIEW') ORDER BY created_at"
        ).fetchall()

    restored_pending = restored_active = 0
    for row in rows:
        (sid,iid,direction,strategy,level,entry_low,entry_high,sl,tp1,tp2,tp3,
         score,status,created_at,activated_at,expires_at) = row
        if not iid or direction not in ('LONG','SHORT'):
            continue
        setup = Setup(
            inst_id=iid, coin=get_coin(iid), direction=direction, strategy=strategy or 'RECOVERED',
            pattern_name='ВОССТАНОВЛЕННЫЙ СИГНАЛ', level=float(level or 0),
            level_kind='RECOVERED', entry_low=float(entry_low), entry_high=float(entry_high),
            sl=float(sl), tp1=float(tp1), tp2=float(tp2), tp3=float(tp3),
            score=int(score or 0), reason='Состояние восстановлено из SQLite после перезапуска.',
            volume_24h=0.0, change24=0.0, range24=0.0, atr5_pct=0.0, atr15_pct=0.0,
            volume_ratio=0.0, candles_5m=[], points=[]
        )
        if status == 'READY':
            if now_ts() > float(expires_at or 0):
                with db_lock:
                    db.execute("UPDATE signals SET status='EXPIRED' WHERE id=? AND status='READY'", (sid,))
                    db.commit()
                continue
            pending[iid] = PendingSignal(
                setup, int(sid), float(created_at or now_ts()), float(expires_at or now_ts()), 0
            )
            restored_pending += 1
        else:
            active[iid] = ActiveSignal(
                setup, int(sid), float(activated_at or created_at or now_ts()), 0,
                timeout_notified=(status == 'TIME_REVIEW')
            )
            restored_active += 1
    log.info('SIGNAL RECOVERY | pending=%d active=%d', restored_pending, restored_active)


def morning_greeting():
    """Send a different prepared greeting daily, once per Kyiv-local date."""
    n = local_now()
    # Permit a small startup/loop delay, but never send yesterday's greeting late.
    if n.hour != 9 or n.minute > 10:
        return
    key = n.date().isoformat()
    with db_lock:
        row = db.execute("SELECT value FROM bot_state WHERE key='morning_greeting'").fetchone()
    if row and row[0] == key:
        return

    greetings = [
        '☀️ <b>Доброе утро, QUANTUM-команда!</b>\n\n'
        '💎 Новый торговый день — ищем не просто движение, а подтверждённые сетапы.\n'
        '🎯 Вход только по плану, риск контролируем, ушедшую цену не догоняем.',
        '🌅 <b>Доброе утро!</b>\n\n'
        '📊 Сначала структура рынка, затем сигнал. Сегодня приоритет — понятный вход, логичный стоп и реальная цель.\n'
        '🛡 Берегите депозит: качество важнее количества.',
        '🚀 <b>QUANTUM на связи!</b>\n\n'
        '🔎 Ищем сильный импульс, ретест и подтверждение на младшем таймфрейме.\n'
        '⛔️ Без погони за свечой и без входа из-за FOMO.',
        '💎 <b>Доброе утро, трейдеры!</b>\n\n'
        '📈 Пусть сегодня решения будут спокойными и системными. Не каждый рыночный всплеск — возможность.\n'
        '🎯 Ждём сетап, где риск заранее понятен.',
        '☕️ <b>Утренний чек-лист QUANTUM</b>\n\n'
        '1️⃣ Направление и структура.\n2️⃣ Подтверждение входа.\n3️⃣ Стоп и цели до сделки.\n'
        '🛡 Если условия не совпали — пропускаем.',
        '🌄 <b>Доброе утро!</b>\n\n'
        '⚡️ Сегодня следим за активными контрактами, но не путаем волатильность с качеством.\n'
        '💎 Только понятные сценарии, без обещаний лёгкой прибыли.',
        '🧭 <b>Новый день — новый план.</b>\n\n'
        'QUANTUM отсеивает слабые входы и ждёт подтверждения.\n'
        '🎯 Не гонимся за количеством: дисциплина и контроль риска прежде всего.'
    ]
    # Date-based rotation guarantees no same-text repeats on consecutive days.
    text = greetings[n.date().toordinal() % len(greetings)]
    try:
        bot.send_message(CHANNEL_ID, text, parse_mode='HTML')
        with db_lock:
            db.execute("INSERT OR REPLACE INTO bot_state(key,value) VALUES('morning_greeting',?)", (key,))
            db.commit()
        log.info('MORNING GREETING SENT | %s', key)
    except Exception:
        log.exception('MORNING GREETING FAILED')

# ============================================================
# LIMITS / COOLDOWN
# ============================================================

def can_send(iid:str)->bool:
    # No global hourly/daily signal quota.
    # Keep only anti-duplicate protection for the same instrument/setup.
    with state_lock:
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

def live_entry_viable(s: Setup, live_price: float) -> bool:
    """Reject stale setups when the live market has already run away from the planned entry."""
    if not math.isfinite(live_price) or live_price <= 0:
        return False
    if s.direction == 'LONG':
        if live_price <= s.sl or live_price >= s.tp1:
            return False
    else:
        if live_price >= s.sl or live_price <= s.tp1:
            return False

    if s.entry_low <= live_price <= s.entry_high:
        return True
    if live_price < s.entry_low:
        gap_pct = (s.entry_low - live_price) / live_price * 100
    else:
        gap_pct = (live_price - s.entry_high) / live_price * 100

    # Allow only a small distance from the zone, scaled to volatility, to avoid late alerts.
    max_gap_pct = min(0.30, max(0.10, s.atr5_pct * 0.60))
    return gap_pct <= max_gap_pct


def scan_market():
    strategy_reject_counts.clear()
    instruments = get_instruments()
    tickers = get_tickers()
    candidates = build_universe(instruments, tickers)
    raw = []

    # Diagnostics are observational only: they do not change strategy rules.
    diag = {
        'cooldown_or_active': 0,
        'load_errors': 0,
        'short_history': 0,
        'empty_5m': 0,
        'stale_candles': 0,
        'low_atr': 0,
        'eligible': 0,
    }
    strategy_diag = {
        builder.__name__: {
            'evaluated': 0,
            'no_setup': 0,
            'built': 0,
            'live_reject': 0,
            'exceptions': 0,
        }
        for builder in STRATEGIES
    }

    for iid, ticker, _ in candidates:
        if not can_send(iid):
            diag['cooldown_or_active'] += 1
            continue

        try:
            loaded = load_symbol(iid)
            # Only closed candles are allowed to create setups.
            data = {
                tf: [c for c in candles if c.confirmed]
                for tf, candles in loaded.items()
            }
            c5, c15, c1h = data['5m'], data['15m'], data['1h']

            if min(len(c5), len(c15), len(c1h)) < 70:
                diag['short_history'] += 1
                continue
            if not c5:
                diag['empty_5m'] += 1
                continue

            # Candle timestamps are candle-open times. Allow the latest closed
            # 5M candle plus ordinary API/scanner latency.
            candle_age = now_ts() - (c5[-1].ts / 1000.0)
            if candle_age > 600 or candle_age < -30:
                diag['stale_candles'] += 1
                continue

            if atr_pct(c5) < MIN_ATR_5M_PCT or atr_pct(c15) < MIN_ATR_15M_PCT:
                diag['low_atr'] += 1
                continue

            diag['eligible'] += 1
            for builder in STRATEGIES:
                name = builder.__name__
                stats = strategy_diag[name]
                stats['evaluated'] += 1
                try:
                    setup = builder(iid, ticker, data)
                    if setup is None:
                        stats['no_setup'] += 1
                        continue

                    stats['built'] += 1
                    if live_entry_viable(setup, float(ticker.get('last') or 0)):
                        raw.append(setup)
                    else:
                        stats['live_reject'] += 1
                except Exception:
                    stats['exceptions'] += 1
                    log.exception('STRATEGY FAILED | %s | %s', iid, name)
        except Exception:
            diag['load_errors'] += 1
            log.exception('CANDIDATE FAILED | %s', iid)

    # One setup per instrument; no global hourly/daily signal cap is applied.
    best = {}
    for setup in raw:
        if setup.inst_id not in best or setup.score > best[setup.inst_id].score:
            best[setup.inst_id] = setup
    final = sorted(best.values(), key=lambda setup: setup.score, reverse=True)

    log.info(
        'SCAN | market=%d candidates=%d eligible=%d raw=%d final=%d',
        len(tickers), len(candidates), diag['eligible'], len(raw), len(final)
    )
    log.info(
        'SCAN DIAG | cooldown_or_active=%d short_history=%d empty_5m=%d '
        'stale_candles=%d low_atr=%d load_errors=%d',
        diag['cooldown_or_active'], diag['short_history'], diag['empty_5m'],
        diag['stale_candles'], diag['low_atr'], diag['load_errors']
    )
    for name, stats in strategy_diag.items():
        log.info(
            'STRATEGY DIAG | %s | evaluated=%d no_setup=%d built=%d '
            'live_reject=%d exceptions=%d',
            name, stats['evaluated'], stats['no_setup'], stats['built'],
            stats['live_reject'], stats['exceptions']
        )
        rejects = sorted(
            ((line_no, count) for (strategy, line_no), count in strategy_reject_counts.items()
             if strategy == name),
            key=lambda item: item[1], reverse=True
        )[:5]
        if rejects:
            summary = ','.join(f'L{line_no}:{count}' for line_no, count in rejects)
            log.info('STRATEGY REJECT LINES | %s | %s', name, summary)

    for setup in final:
        if not can_send(setup.inst_id):
            continue
        send_signal(setup)

# ============================================================
# WEEKLY REPORT
# ============================================================

def weekly_report():
    n=local_now()
    # Monday after 09:00, exactly once per week.
    if n.weekday()!=0 or n.hour<9:
        return
    key=n.date().isoformat()
    with db_lock:
        row=db.execute("SELECT value FROM bot_state WHERE key='weekly_report'").fetchone()
    if row and row[0]==key:
        return
    since=(n-timedelta(days=7)).timestamp()
    with db_lock:
        total=db.execute('SELECT COUNT(*) FROM signals WHERE created_at>=?',(since,)).fetchone()[0]
        wins=db.execute("SELECT COUNT(*) FROM signals WHERE created_at>=? AND result='TP3'",(since,)).fetchone()[0]
        losses=db.execute("SELECT COUNT(*) FROM signals WHERE created_at>=? AND result='SL'",(since,)).fetchone()[0]
    try:
        bot.send_message(CHANNEL_ID,
            f'📊 <b>QUANTUM — неделя</b>\n\nСигналов: <b>{total}</b>\nПрибыльных закрытых: <b>{wins}</b>\nУбыточных закрытых: <b>{losses}</b>',
            parse_mode='HTML')
        with db_lock:
            db.execute("INSERT OR REPLACE INTO bot_state(key,value) VALUES('weekly_report',?)",(key,)); db.commit()
    except Exception:
        log.exception('WEEKLY REPORT FAILED')

# ============================================================
# HEALTH / STARTUP
# ============================================================

def startup_healthcheck():
    try:
        payload=okx_get('/api/v5/public/time',{})
        if str(payload.get('code'))!='0':
            raise RuntimeError('OKX time endpoint failed')
        log.info('HEALTH | OKX API OK')
    except Exception:
        log.exception('HEALTH | OKX API FAILED')
    try:
        me=bot.get_me()
        log.info('HEALTH | TELEGRAM OK | @%s',me.username)
    except Exception:
        log.exception('HEALTH | TELEGRAM FAILED')

# ============================================================
# MAIN
# ============================================================

def main():
    log.info('============================================================')
    log.info('QUANTUM INTRADAY SWING ENGINE V2 STARTED')
    log.info('Strategies: Extreme Reversal | Breakout+Retest | Trend Pullback | Pre-Breakout')
    log.info('Universe: gainers | losers | volatile | new active | small alts')
    log.info('============================================================')
    startup_healthcheck()
    recover_open_signals()
    start_telegram_polling()
    start_lifecycle_monitor()

    while True:
        try:
            morning_greeting()
            weekly_report()
            scan_market()
        except KeyboardInterrupt:
            log.info('STOPPED')
            break
        except Exception:
            log.exception('MAIN LOOP ERROR')
        time.sleep(SCAN_INTERVAL_SECONDS)


if __name__=='__main__':
    main()
