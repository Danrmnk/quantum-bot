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
# QUANTUM INTRADAY SWING ENGINE V3
# ------------------------------------------------------------
# Purpose:
#   Find high-momentum crypto futures setups for 1-8 hour holds.
#   5M = trigger/entry, 15M = structure, 1H = context.
#
# Strategies:
#   1) Trend Pullback          1H trend -> 15M pullback -> 5M continuation trigger
#   2) Breakout + Retest       distinct break -> retest -> continuation
#   3) Extreme Reversal        rare, strongly confirmed exhaustion reversal
#   4) Mean Reversion          rare opportunistic exhaustion setup
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
MIN_SCORE = int(os.getenv('MIN_SCORE', '82'))

# ---------------- runtime ----------------
SCAN_INTERVAL_SECONDS = int(os.getenv('SCAN_INTERVAL_SECONDS', '30'))
HTTP_TIMEOUT = int(os.getenv('HTTP_TIMEOUT', '10'))
REQUEST_RETRIES = int(os.getenv('REQUEST_RETRIES', '3'))
CANDLE_CACHE_SECONDS = int(os.getenv('CANDLE_CACHE_SECONDS', '15'))
READY_TTL_MINUTES = int(os.getenv('READY_TTL_MINUTES', '20'))
ACTIVE_MAX_HOURS = int(os.getenv('ACTIVE_MAX_HOURS', '12'))
COOLDOWN_MINUTES = int(os.getenv('COOLDOWN_MINUTES', '45'))

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
    activated_candle_ts: int = 0
    last_processed_candle_ts: int = 0

pending: Dict[str, PendingSignal] = {}
active: Dict[str, ActiveSignal] = {}
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
    'r_multiple':'REAL DEFAULT 0', 'message_id':'INTEGER DEFAULT 0'
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


def confirmed_candles(cs: List[Candle]) -> List[Candle]:
    # Technical analysis must never use the still-forming candle.
    return [c for c in cs if c.confirmed]


def load_symbol(inst_id: str) -> Dict[str, List[Candle]]:
    # Strategies operate only on closed candles. The raw 5M stream is fetched
    # separately by the lifecycle engine when it needs live entry/exit touches.
    return {
        '1h': confirmed_candles(get_candles(inst_id, '1H', 160)),
        '15m': confirmed_candles(get_candles(inst_id, '15m', 180)),
        '5m': confirmed_candles(get_candles(inst_id, '5m', 200)),
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
    """Market quality only. Entry quality is scored separately and heavily."""
    score = 42
    ch = abs(ticker['change24h_pct'])
    rng = ticker['range24h_pct']
    if ch >= 3: score += 4
    if ch >= 8: score += 5
    if ch >= 15: score += 4
    if rng >= 6: score += 4
    if rng >= 10: score += 4
    if atr_pct(c5) >= MIN_ATR_5M_PCT: score += 4
    if atr_pct(c15) >= MIN_ATR_15M_PCT: score += 4
    if vr >= 1.15: score += 3
    if vr >= 1.5: score += 3
    if vr >= 2.0: score += 3
    if ticker['vol24h_usd'] >= PREFERRED_TURNOVER_USD: score += 3
    return min(score, 70)


def risk_pct_for_zone(direction: str, entry_low: float, entry_high: float, sl: float) -> float:
    # Worst-case fill inside the published zone.
    if direction == 'LONG':
        entry = entry_low if sl >= entry_low else entry_low
        # For a long, the lower edge is farther from a lower SL.
        entry = entry_low
        return (entry - sl) / max(entry, 1e-12) * 100
    entry = entry_high
    return (sl - entry) / max(entry, 1e-12) * 100


def validate_zone_risk(direction: str, entry_low: float, entry_high: float, sl: float) -> bool:
    if not (entry_low > sl if direction == 'LONG' else entry_high < sl):
        return False
    risk_pct = risk_pct_for_zone(direction, entry_low, entry_high, sl)
    return MIN_RISK_PCT <= risk_pct <= MAX_RISK_PCT


def target_levels(direction: str, entry: float, sl: float, c15: List[Candle]):
    """Structure-first targets with R-multiple fallback and strict ordering."""
    risk = abs(entry - sl)
    if risk <= 0:
        return None
    levels = cluster_levels(c15, 0.30)
    if direction == 'LONG':
        structural = sorted({p for p, k, t in levels if p > entry + risk * 0.65})
        sign = 1
    else:
        structural = sorted({p for p, k, t in levels if p < entry - risk * 0.65}, reverse=True)
        sign = -1

    r_targets = [entry + sign * risk * m for m in (TP1_R, TP2_R, TP3_R)]
    chosen = []
    for p in structural:
        if sign * (p - entry) >= risk * 1.0:
            chosen.append(p)
        if len(chosen) == 3:
            break

    targets = r_targets[:]
    if len(chosen) >= 1: targets[0] = chosen[0]
    if len(chosen) >= 2 and sign * (chosen[1] - targets[0]) >= risk * 0.55: targets[1] = chosen[1]
    if len(chosen) >= 3 and sign * (chosen[2] - targets[1]) >= risk * 0.55: targets[2] = chosen[2]

    tp1, tp2, tp3 = targets
    if direction == 'LONG':
        tp1 = max(tp1, entry + risk * 1.0)
        tp2 = max(tp2, tp1 + risk * 0.45)
        tp3 = max(tp3, tp2 + risk * 0.45)
    else:
        tp1 = min(tp1, entry - risk * 1.0)
        tp2 = min(tp2, tp1 - risk * 0.45)
        tp3 = min(tp3, tp2 - risk * 0.45)
    return tp1, tp2, tp3


def entry_zone_from_level(level: float, atr5: float, direction: str):
    # Zone is deliberately near the structure, not centered on the current price.
    width = max(atr5 * 0.20, level * 0.00035)
    if direction == 'LONG':
        return level - width, level + width
    return level - width, level + width


def current_distance_atr(price: float, level: float, atr5: float) -> float:
    return abs(price - level) / max(atr5, 1e-12)


def entry_quality(direction: str, price: float, level: float, atr5: float,
                  impulse_pct: float, pullback_pct: float, vr: float,
                  candle: Candle, structure_touches: int = 0) -> int:
    """Score the location of the trade, not merely how exciting the coin is."""
    q = 0
    dist = current_distance_atr(price, level, atr5)
    if dist <= 0.25: q += 22
    elif dist <= 0.45: q += 18
    elif dist <= 0.70: q += 12
    elif dist <= 0.95: q += 5
    else: q -= 12

    if 0.20 <= abs(pullback_pct) <= 1.8: q += 14
    elif abs(pullback_pct) <= 3.0: q += 8
    else: q -= 4

    if abs(impulse_pct) >= 1.5: q += 7
    if abs(impulse_pct) >= 3.0: q += 5
    if vr >= 1.2: q += 5
    if vr >= 1.8: q += 4
    if body_ratio(candle) >= 0.45: q += 5
    if (direction == 'LONG' and close_location(candle) >= 0.60) or (direction == 'SHORT' and close_location(candle) <= 0.40):
        q += 5
    q += min(structure_touches * 3, 9)
    return int(clamp(q, 0, 80))


def final_score(base: int, entry_q: int, bonuses: List[int], penalties: List[int]) -> int:
    # Entry location is the dominant component. A late entry cannot hide behind volume.
    return int(clamp(base + entry_q + sum(bonuses) - sum(penalties), 0, 99))


def build_setup_common(iid, ticker, direction, strategy, pattern, level, level_kind,
                       sl, c15, c5, vr, score, reason, points, hold='1–8 ч'):
    entry = level
    lo, hi = entry_zone_from_level(level, atr(c5), direction)
    if not validate_zone_risk(direction, lo, hi, sl):
        return None
    tps = target_levels(direction, (lo + hi) / 2, sl, c15)
    if not tps:
        return None
    chart_start = max(0, len(c5) - 96)
    chart_points = [(max(0, min(95, int(idx) - chart_start)), price, label) for idx, price, label in points]
    return Setup(iid, get_coin(iid), direction, strategy, pattern, level, level_kind,
                 lo, hi, sl, *tps, int(score), reason,
                 ticker['vol24h_usd'], ticker['change24h_pct'], ticker['range24h_pct'],
                 atr_pct(c5), atr_pct(c15), vr, c5[-96:], chart_points, hold)

# ============================================================
# STRATEGY 1 — EXTREME REVERSAL (RARE)
# ============================================================

def build_reversal(iid, ticker, data) -> Optional[Setup]:
    c5, c15, c1h = data['5m'], data['15m'], data['1h']
    if min(len(c5), len(c15), len(c1h)) < 70:
        return None
    ch = ticker['change24h_pct']
    if abs(ch) < MIN_24H_MOVE_FOR_REVERSAL:
        return None
    vr = vol_ratio(c5)
    if vr < MIN_REVERSAL_VOLUME_RATIO:
        return None
    m1h, m2h, _ = impulse_stats(c5)
    direction = 'SHORT' if ch > 0 else 'LONG'
    if direction == 'SHORT' and m2h < MIN_2H_IMPULSE:
        return None
    if direction == 'LONG' and m2h > -MIN_2H_IMPULSE:
        return None
    extreme = local_extreme(c5, direction, 60)
    cur, prev = c5[-1], c5[-2]
    if direction == 'SHORT':
        upper, _ = wick_ratio(cur)
        if not (cur.close < cur.open and cur.close < prev.low and upper >= 0.12 and body_ratio(cur) >= 0.42):
            return None
        level = extreme.high
        sl = max(level, max(c.high for c in c5[-4:])) + atr(c5) * 0.25
        reason = f'сильный рост 24H {ch:+.1f}% → экстремум → подтверждённый разворот; объём x{vr:.2f}'
    else:
        _, lower = wick_ratio(cur)
        if not (cur.close > cur.open and cur.close > prev.high and lower >= 0.12 and body_ratio(cur) >= 0.42):
            return None
        level = extreme.low
        sl = min(level, min(c.low for c in c5[-4:])) - atr(c5) * 0.25
        reason = f'сильное падение 24H {ch:+.1f}% → экстремум → подтверждённый разворот; объём x{vr:.2f}'

    # Reversal is intentionally much harder to publish than continuation.
    dist = current_distance_atr(cur.close, level, atr(c5))
    if dist > 0.65:
        return None
    lo, hi = entry_zone_from_level(level, atr(c5), direction)
    if not validate_zone_risk(direction, lo, hi, sl):
        return None
    eq = entry_quality(direction, cur.close, level, atr(c5), m2h, 0, vr, cur)
    score = final_score(base_score(ticker, c5, c15, vr), eq, [5 if abs(ch) >= STRONG_24H_MOVE else 0], [])
    if score < MIN_SCORE:
        return None
    return build_setup_common(iid, ticker, direction, 'EXTREME REVERSAL',
        'ЭКСТРЕМУМ → ПОДТВЕРЖДЁННЫЙ РАЗВОРОТ', level, 'EXTREME', sl, c15, c5, vr,
        score, reason, [(len(c5[-96:])-1, level, 'EXTREME'), (len(c5[-96:])-1, cur.close, 'CONFIRM')], '1–6 ч')


# ============================================================
# STRATEGY 2 — BREAKOUT + RETEST + CONTINUATION
# ============================================================

def _find_recent_break(c5: List[Candle], level: float, direction: str, lookback: int = 10):
    start = max(1, len(c5) - lookback - 1)
    for i in range(len(c5) - 2, start - 1, -1):
        c = c5[i]
        prev = c5[i-1]
        if direction == 'LONG' and c.close > level and prev.close <= level:
            return i
        if direction == 'SHORT' and c.close < level and prev.close >= level:
            return i
    return None


def build_breakout(iid, ticker, data) -> Optional[Setup]:
    c5, c15, c1h = data['5m'], data['15m'], data['1h']
    if min(len(c5), len(c15), len(c1h)) < 80:
        return None
    vr = vol_ratio(c5)
    if vr < MIN_BREAKOUT_VOLUME:
        return None
    levels = cluster_levels(c15, 0.30)
    if not levels:
        return None

    cur = c5[-1]
    candidates = [(p, t) for p, k, t in levels if t >= 2 and p < cur.close and k == 'HIGH']
    for level, touches in sorted(candidates, key=lambda x: x[0], reverse=True):
        bi = _find_recent_break(c5, level, 'LONG', 12)
        if bi is None or bi >= len(c5)-1:
            continue
        retest = c5[bi+1:-1]
        if not retest:
            continue
        # Price must actually come back to the broken level after the breakout.
        if not any(c.low <= level * 1.003 for c in retest):
            continue
        if cur.close <= level or cur.close <= c5[-2].close:
            continue
        if body_ratio(cur) < 0.40 or close_location(cur) < 0.58:
            continue
        # Avoid buying after a vertical continuation away from the retest.
        a5 = atr(c5)
        if current_distance_atr(cur.close, level, a5) > 0.80:
            continue
        sl = min(level, min(c.low for c in retest[-4:])) - a5 * 0.22
        lo, hi = entry_zone_from_level(level, a5, 'LONG')
        if not validate_zone_risk('LONG', lo, hi, sl):
            continue
        pullback = pct_move(min(c.low for c in retest), max(c.high for c in c5[bi:bi+1]))
        impulse = pct_move(c5[bi].close, c5[max(0, bi-8)].close)
        eq = entry_quality('LONG', cur.close, level, a5, impulse, pullback, vr, cur, touches)
        score = final_score(base_score(ticker, c5, c15, vr), eq, [8, min(6, touches)], [])
        if score < MIN_SCORE:
            continue
        reason = f'15M сопротивление {touches}× → отдельный пробой → откат к уровню → 5M continuation; объём x{vr:.2f}'
        return build_setup_common(iid, ticker, 'LONG', 'BREAKOUT + RETEST', 'ПРОБОЙ → РЕТЕСТ → ПРОДОЛЖЕНИЕ',
            level, 'BROKEN RESISTANCE', sl, c15, c5, vr, score, reason,
            [(bi, level, 'BREAK'), (len(c5[-96:])-2, level, 'RETEST'), (len(c5[-96:])-1, cur.close, 'CONFIRM')], '1–6 ч')

    candidates = [(p, t) for p, k, t in levels if t >= 2 and p > cur.close and k == 'LOW']
    for level, touches in sorted(candidates, key=lambda x: x[0]):
        bi = _find_recent_break(c5, level, 'SHORT', 12)
        if bi is None or bi >= len(c5)-1:
            continue
        retest = c5[bi+1:-1]
        if not retest:
            continue
        if not any(c.high >= level * 0.997 for c in retest):
            continue
        if cur.close >= level or cur.close >= c5[-2].close:
            continue
        if body_ratio(cur) < 0.40 or close_location(cur) > 0.42:
            continue
        a5 = atr(c5)
        if current_distance_atr(cur.close, level, a5) > 0.80:
            continue
        sl = max(level, max(c.high for c in retest[-4:])) + a5 * 0.22
        lo, hi = entry_zone_from_level(level, a5, 'SHORT')
        if not validate_zone_risk('SHORT', lo, hi, sl):
            continue
        pullback = pct_move(max(c.high for c in retest), min(c.low for c in c5[bi:bi+1]))
        impulse = pct_move(c5[bi].close, c5[max(0, bi-8)].close)
        eq = entry_quality('SHORT', cur.close, level, a5, impulse, pullback, vr, cur, touches)
        score = final_score(base_score(ticker, c5, c15, vr), eq, [8, min(6, touches)], [])
        if score < MIN_SCORE:
            continue
        reason = f'15M поддержка {touches}× → отдельный пробой → откат к уровню → 5M continuation; объём x{vr:.2f}'
        return build_setup_common(iid, ticker, 'SHORT', 'BREAKOUT + RETEST', 'ПРОБОЙ → РЕТЕСТ → ПРОДОЛЖЕНИЕ',
            level, 'BROKEN SUPPORT', sl, c15, c5, vr, score, reason,
            [(bi, level, 'BREAK'), (len(c5[-96:])-2, level, 'RETEST'), (len(c5[-96:])-1, cur.close, 'CONFIRM')], '1–6 ч')
    return None


# ============================================================
# STRATEGY 3 — TREND PULLBACK (PRIMARY)
# ============================================================

def build_pullback(iid, ticker, data) -> Optional[Setup]:
    c5, c15, c1h = data['5m'], data['15m'], data['1h']
    if min(len(c5), len(c15), len(c1h)) < 80:
        return None
    trend = trend_state(c1h)
    if not trend:
        return None
    e20 = ema([c.close for c in c15], 20)
    e50 = ema([c.close for c in c15], 50)
    if len(e20) < 10 or len(e50) < 10:
        return None
    vr = vol_ratio(c5)
    cur, prev = c5[-1], c5[-2]
    a5 = atr(c5)

    recent = c15[-9:-1]
    if len(recent) < 6:
        return None

    if trend == 'LONG':
        if not (e20[-1] > e50[-1] and e20[-1] > e20[-4]):
            return None
        impulse_start = c15[-13].close
        impulse = pct_move(c15[-2].close, impulse_start)
        if impulse < 2.0:
            return None
        pull_low = min(c.low for c in recent)
        # Pullback must enter the moving-average/structure zone.
        if pull_low > e20[-2] * 1.004:
            return None
        # But trend must remain structurally intact.
        if pull_low < e50[-2] * 0.995:
            return None
        level = e20[-1]
        # 5M trigger: reclaim local pullback micro-resistance, not a huge chase candle.
        micro_high = max(c.high for c in c5[-6:-1])
        if not (cur.close > micro_high and cur.close > cur.open):
            return None
        if current_distance_atr(cur.close, level, a5) > 0.85:
            return None
        pullback_pct = (pull_low - e20[-2]) / max(e20[-2], 1e-12) * 100
        sl = min(pull_low, min(c.low for c in c5[-8:])) - a5 * 0.20
        reason = f'1H LONG trend → 15M impulse {impulse:+.1f}% → откат к EMA20 → 5M reclaim; объём x{vr:.2f}'
        points_label = 'PULLBACK LOW'
    else:
        if not (e20[-1] < e50[-1] and e20[-1] < e20[-4]):
            return None
        impulse_start = c15[-13].close
        impulse = pct_move(c15[-2].close, impulse_start)
        if impulse > -2.0:
            return None
        pull_high = max(c.high for c in recent)
        if pull_high < e20[-2] * 0.996:
            return None
        if pull_high > e50[-2] * 1.005:
            return None
        level = e20[-1]
        micro_low = min(c.low for c in c5[-6:-1])
        if not (cur.close < micro_low and cur.close < cur.open):
            return None
        if current_distance_atr(cur.close, level, a5) > 0.85:
            return None
        pullback_pct = (pull_high - e20[-2]) / max(e20[-2], 1e-12) * 100
        sl = max(pull_high, max(c.high for c in c5[-8:])) + a5 * 0.20
        reason = f'1H SHORT trend → 15M impulse {impulse:+.1f}% → откат к EMA20 → 5M reclaim; объём x{vr:.2f}'
        points_label = 'PULLBACK HIGH'

    lo, hi = entry_zone_from_level(level, a5, trend)
    if not validate_zone_risk(trend, lo, hi, sl):
        return None
    eq = entry_quality(trend, cur.close, level, a5, impulse, pullback_pct, vr, cur)
    score = final_score(base_score(ticker, c5, c15, vr), eq, [10, 5 if vr >= 1.2 else 0], [])
    if score < MIN_SCORE:
        return None
    return build_setup_common(iid, ticker, trend, 'TREND PULLBACK', 'ИМПУЛЬС → ОТКАТ → ПРОДОЛЖЕНИЕ',
        level, 'EMA20 PULLBACK', sl, c15, c5, vr, score, reason,
        [(len(c5[-96:])-6, level, points_label), (len(c5[-96:])-1, cur.close, 'TRIGGER')], '1–8 ч')


# ============================================================
# STRATEGY 4 — MEAN REVERSION (OPPORTUNISTIC, VERY RARE)
# ============================================================

def build_mean_reversion(iid, ticker, data) -> Optional[Setup]:
    c5, c15 = data['5m'], data['15m']
    if len(c5) < 90 or len(c15) < 70:
        return None
    e20 = ema([c.close for c in c15], 20)[-1]
    cur, prev = c5[-1], c5[-2]
    deviation = (cur.close - e20) / max(e20, 1e-12) * 100
    direction = 'LONG' if deviation <= -3.5 else 'SHORT' if deviation >= 3.5 else None
    if not direction:
        return None
    vr = vol_ratio(c5)
    if vr < MIN_VOLUME_RATIO:
        return None
    # Do not fight a clean trend. Reversion is only for an exhaustion move.
    if direction == 'LONG' and trend_state(c15) == 'SHORT':
        return None
    if direction == 'SHORT' and trend_state(c15) == 'LONG':
        return None
    if direction == 'LONG':
        if not (cur.close > cur.open and cur.close > prev.high and close_location(cur) >= 0.65):
            return None
        level = e20
        sl = min(c.low for c in c5[-8:]) - atr(c5) * 0.22
    else:
        if not (cur.close < cur.open and cur.close < prev.low and close_location(cur) <= 0.35):
            return None
        level = e20
        sl = max(c.high for c in c5[-8:]) + atr(c5) * 0.22
    lo, hi = entry_zone_from_level(level, atr(c5), direction)
    if not validate_zone_risk(direction, lo, hi, sl):
        return None
    eq = entry_quality(direction, cur.close, level, atr(c5), deviation, 0, vr, cur)
    score = final_score(base_score(ticker, c5, c15, vr), eq, [2], [])
    if score < MIN_SCORE + 3:
        return None
    return build_setup_common(iid, ticker, direction, 'MEAN REVERSION', 'ЭКСТРЕМУМ → ВОЗВРАТ К EMA20',
        level, '15M EMA20', sl, c15, c5, vr, score,
        f'отклонение от EMA20 {deviation:+.2f}% → exhaustion → 5M reversal',
        [(len(c5[-96:])-1, cur.close, 'REVERSAL')], '1–6 ч')


STRATEGIES=(build_pullback,build_breakout,build_reversal,build_mean_reversion)

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
    risk=abs(entry-s.sl)/max(entry,1e-12)*100
    worst_risk=risk_pct_for_zone(s.direction,s.entry_low,s.entry_high,s.sl)
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
        f'🛑 <b>Стоп:</b> <code>{fmt_price(s.sl)}</code> · риск до {worst_risk:.2f}%\n'
        f'🎯 <b>TP1:</b> <code>{fmt_price(s.tp1)}</code>\n'
        f'🎯 <b>TP2:</b> <code>{fmt_price(s.tp2)}</code> · RR 1:{rr2:.1f}\n'
        f'🎯 <b>TP3:</b> <code>{fmt_price(s.tp3)}</code>\n\n'
        f'🧠 <b>Почему:</b> {s.reason}.\n'
        f'⏱ <b>Ожидаемое удержание:</b> {s.hold_hours}\n\n'
        f'⚠️ Внимательно проверьте сделку перед входом.\n'
        f'🛡 Риск на одну сделку — <b>не более 1% депозита</b>.\n'
        f'🚫 Не догоняйте цену после ухода от зоны входа.'
    )


def insert_signal(s:Setup) -> int:
    created = now_ts()
    with db_lock:
        cur = db.execute("""INSERT INTO signals(
            inst_id,direction,strategy,level,entry_low,entry_high,sl,tp1,tp2,tp3,
            score,status,created_at,expires_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (s.inst_id,s.direction,s.strategy,s.level,s.entry_low,s.entry_high,s.sl,
         s.tp1,s.tp2,s.tp3,s.score,'READY',created,created+READY_TTL_MINUTES*60))
        sid = cur.lastrowid
        db.commit()
    return sid


def send_signal(s:Setup) -> bool:
    path = None
    sid = None
    try:
        path = make_chart(s)
        sid = insert_signal(s)
        with open(path, 'rb') as f:
            msg = bot.send_photo(CHANNEL_ID, f, caption=build_signal_text(s), parse_mode='HTML',
                                 show_caption_above_media=True, reply_markup=vote_keyboard(sid))
        with db_lock:
            db.execute('UPDATE signals SET message_id=? WHERE id=?', (msg.message_id, sid))
            db.commit()
        created = now_ts()
        pending[s.inst_id] = PendingSignal(s, sid, created, created+READY_TTL_MINUTES*60, msg.message_id)
        log.info('SIGNAL SENT | %s | %s | %s | score=%s | id=%s', s.coin, s.direction, s.strategy, s.score, sid)
        return True
    except Exception:
        log.exception('SIGNAL SEND FAILED | %s', s.inst_id)
        if sid is not None:
            with db_lock:
                db.execute("UPDATE signals SET status='SEND_FAILED' WHERE id=?", (sid,)); db.commit()
        return False
    finally:
        if path:
            try: os.remove(path)
            except OSError: pass

# ============================================================
# RUNTIME RECOVERY
# ============================================================

def setup_from_row(row) -> Setup:
    (sid, iid, direction, strategy, level, entry_low, entry_high, sl, tp1, tp2, tp3,
     score, status, created_at, activated_at, expires_at, tp1_hit, tp2_hit, tp3_hit, message_id) = row
    return Setup(iid, get_coin(iid), direction, strategy, '', float(level or 0), '',
                 float(entry_low), float(entry_high), float(sl), float(tp1), float(tp2), float(tp3),
                 int(score or 0), 'recovered after restart', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, [])


def load_runtime_state():
    with db_lock:
        rows = db.execute("""SELECT id,inst_id,direction,strategy,level,entry_low,entry_high,sl,tp1,tp2,tp3,
            score,status,created_at,activated_at,expires_at,tp1_hit,tp2_hit,tp3_hit,message_id
            FROM signals WHERE status IN ('READY','ACTIVE') ORDER BY created_at""").fetchall()
    now = now_ts()
    restored = 0
    for row in rows:
        setup = setup_from_row(row)
        sid, iid, direction, strategy, level, entry_low, entry_high, sl, tp1, tp2, tp3, score, status, created_at, activated_at, expires_at, h1, h2, h3, message_id = row
        if status == 'READY':
            if expires_at and float(expires_at) <= now:
                with db_lock:
                    db.execute("UPDATE signals SET status='EXPIRED',result='EXPIRED',closed_at=? WHERE id=? AND status='READY'", (now, sid)); db.commit()
                continue
            pending[iid] = PendingSignal(setup, sid, float(created_at), float(expires_at), int(message_id or 0))
        else:
            active[iid] = ActiveSignal(setup, sid, float(activated_at or created_at), int(message_id or 0),
                                       bool(h1), bool(h2), bool(h3), 0, 0)
        restored += 1
    log.info('RECOVERY | restored=%d pending=%d active=%d', restored, len(pending), len(active))


def send_update(text: str):
    try:
        bot.send_message(CHANNEL_ID, text, parse_mode='HTML')
    except Exception:
        log.exception('TELEGRAM UPDATE FAILED')


def price_in_entry(s:Setup, price:float)->bool:
    return s.entry_low <= price <= s.entry_high


def _close_signal(signal_id:int, status:str, result:str, r_multiple:float=0.0):
    with db_lock:
        db.execute("UPDATE signals SET status=?,result=?,closed_at=?,r_multiple=? WHERE id=? AND status IN ('READY','ACTIVE')",
                   (status,result,now_ts(),r_multiple,signal_id)); db.commit()


def lifecycle_for_symbol(iid:str, cs:List[Candle]):
    if len(cs) < 3:
        return
    last = cs[-1]
    p = pending.get(iid)
    if p:
        if now_ts() > p.expires_at:
            _close_signal(p.signal_id,'EXPIRED','EXPIRED',0.0); pending.pop(iid,None); p=None
        elif price_in_entry(p.setup,last.close) or (last.low <= p.setup.entry_high and last.high >= p.setup.entry_low):
            activated=now_ts()
            with db_lock:
                db.execute("UPDATE signals SET status='ACTIVE',activated_at=? WHERE id=? AND status='READY'",(activated,p.signal_id)); db.commit()
            # Never evaluate the activation candle for TP/SL: intrabar order is unknowable.
            active[iid]=ActiveSignal(p.setup,p.signal_id,activated,p.message_id,False,False,False,last.ts,last.ts)
            pending.pop(iid,None)
    a=active.get(iid)
    if not a or not last.confirmed:
        return
    eligible=[c for c in cs if c.confirmed and c.ts>a.activated_candle_ts]
    if not eligible:
        return
    last=eligible[-1]
    if last.ts<=a.last_processed_candle_ts:
        return
    a.last_processed_candle_ts=last.ts
    s=a.setup; long=s.direction=='LONG'
    hit1=last.high>=s.tp1 if long else last.low<=s.tp1
    hit2=last.high>=s.tp2 if long else last.low<=s.tp2
    hit3=last.high>=s.tp3 if long else last.low<=s.tp3
    stop=last.low<=s.sl if long else last.high>=s.sl
    with db_lock:
        row=db.execute('SELECT tp1_hit,tp2_hit,tp3_hit FROM signals WHERE id=?',(a.signal_id,)).fetchone()
    if not row: return
    h1,h2,h3=[bool(x) for x in row]
    # Any TP and SL on the same candle is ambiguous; conservative result is SL.
    if stop and (hit1 or hit2 or hit3):
        _close_signal(a.signal_id,'CLOSED_LOSS','SL',-1.0)
        send_update(f'🔴 <b>STOP — {s.coin}USDT</b>\nTP и SL были затронуты одной свечой. Порядок неизвестен — результат консервативно засчитан по SL.')
        active.pop(iid,None); return
    if hit1 and not h1:
        with db_lock: db.execute('UPDATE signals SET tp1_hit=1 WHERE id=?',(a.signal_id,)); db.commit()
        a.tp1_sent=True
        send_update(f'🟢 <b>TP1 — {s.coin}USDT</b>\nЦена достигла <code>{fmt_price(s.tp1)}</code>.\nЧасть позиции можно зафиксировать по своему плану.')
    if hit2 and not h2:
        with db_lock: db.execute('UPDATE signals SET tp2_hit=1 WHERE id=?',(a.signal_id,)); db.commit()
        a.tp2_sent=True
        send_update(f'🚀 <b>TP2 — {s.coin}USDT</b>\nЦена достигла <code>{fmt_price(s.tp2)}</code>.\nОстаток сопровождаем по плану.')
    if hit3 and not h3:
        entry=(s.entry_low+s.entry_high)/2
        r=abs(s.tp3-entry)/max(abs(entry-s.sl),1e-12)
        with db_lock:
            db.execute("UPDATE signals SET tp1_hit=1,tp2_hit=1,tp3_hit=1,status='CLOSED_WIN',result='TP3',closed_at=?,r_multiple=? WHERE id=?",(now_ts(),r,a.signal_id)); db.commit()
        send_update(f'🏁 <b>TP3 — {s.coin}USDT</b>\nЦена достигла <code>{fmt_price(s.tp3)}</code>.\nРезультат зафиксирован: TP3.')
        active.pop(iid,None); return
    if stop:
        _close_signal(a.signal_id,'CLOSED_LOSS','SL',-1.0)
        send_update(f'🔴 <b>STOP — {s.coin}USDT</b>\nЦена достигла стопа <code>{fmt_price(s.sl)}</code>.\nСделка закрыта по плановому риску.')
        active.pop(iid,None); return
    if now_ts()-a.activated_at>ACTIVE_MAX_HOURS*3600:
        _close_signal(a.signal_id,'TIMEOUT','TIMEOUT',0.0)
        send_update(f'⚪ <b>TIMEOUT — {s.coin}USDT</b>\nСделка не дошла до TP3/SL в установленное время. Результат закрыт как TIMEOUT.')
        active.pop(iid,None)


def update_signal_results():
    if not pending and not active:
        load_runtime_state()
    symbols=set(pending)|set(active)
    for iid in list(symbols):
        try:
            lifecycle_for_symbol(iid,get_candles(iid,'5m',100))
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

def scan_market():
    instruments=get_instruments()
    tickers=get_tickers()
    candidates=build_universe(instruments,tickers)
    raw=[]

    for iid,t,_ in candidates:
        if not can_send(iid):
            continue
        try:
            data=load_symbol(iid)
            c5=data['5m']; c15=data['15m']
            if len(c5)<70 or len(c15)<70:
                continue
            if atr_pct(c5)<MIN_ATR_5M_PCT or atr_pct(c15)<MIN_ATR_15M_PCT:
                continue
            for builder in STRATEGIES:
                try:
                    s=builder(iid,t,data)
                    if s:
                        raw.append(s)
                except Exception as exc:
                    log.debug('STRATEGY FAILED | %s | %s | %s',iid,builder.__name__,exc)
        except Exception as exc:
            log.warning('CANDIDATE FAILED | %s | %s',iid,exc)

    # Only one setup per instrument. This avoids sending four competing signals.
    best={}
    for s in raw:
        if s.inst_id not in best or s.score>best[s.inst_id].score:
            best[s.inst_id]=s
    final=sorted(best.values(),key=lambda x:x.score,reverse=True)
    log.info('SCAN | market=%d candidates=%d raw=%d final=%d',len(tickers),len(candidates),len(raw),len(final))

    for s in final:
        if not can_send(s.inst_id):
            continue
        send_signal(s)

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
        timeouts=db.execute("SELECT COUNT(*) FROM signals WHERE created_at>=? AND result='TIMEOUT'",(since,)).fetchone()[0]
        expired=db.execute("SELECT COUNT(*) FROM signals WHERE created_at>=? AND result='EXPIRED'",(since,)).fetchone()[0]
        avg_r=db.execute("SELECT AVG(r_multiple) FROM signals WHERE created_at>=? AND result IN ('TP3','SL')",(since,)).fetchone()[0] or 0.0
    try:
        bot.send_message(CHANNEL_ID,
            f'📊 <b>QUANTUM — неделя</b>\n\nСигналов: <b>{total}</b>\nTP3: <b>{wins}</b>\nSL: <b>{losses}</b>\nTIMEOUT: <b>{timeouts}</b>\nНе активировались: <b>{expired}</b>\nСредний R по TP3/SL: <b>{avg_r:+.2f}</b>',
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
    log.info('QUANTUM INTRADAY SWING ENGINE V3 STARTED')
    log.info('Strategies: Trend Pullback | Breakout+Retest | Rare Reversal | Rare Mean Reversion')
    log.info('Universe: gainers | losers | volatile | new active | small alts')
    log.info('============================================================')
    startup_healthcheck()
    load_runtime_state()
    start_telegram_polling()

    while True:
        try:
            update_signal_results()
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
