import os
import time
import math
import logging
import sqlite3
from html import escape
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
#   4) Mean Reversion          stretched 15M price -> 5M reversal
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


def validate_risk(entry: float, sl: float) -> bool:
    risk_pct = abs(entry - sl) / max(entry, 1e-12) * 100
    return MIN_RISK_PCT <= risk_pct <= MAX_RISK_PCT


def target_levels(direction: str, entry: float, sl: float, c15: List[Candle]):
    risk = abs(entry - sl)
    if risk <= 0:
        return None
    levels = cluster_levels(c15)
    if direction == 'LONG':
        structural = sorted([p for p, k, t in levels if p > entry])
        sign = 1
    else:
        structural = sorted([p for p, k, t in levels if p < entry], reverse=True)
        sign = -1

    def r_target(mult):
        return entry + sign * risk * mult

    candidates = [p for p in structural if sign * (p - entry) >= risk * 0.9]
    tp1 = r_target(TP1_R)
    tp2 = r_target(TP2_R)
    tp3 = r_target(TP3_R)
    if candidates:
        if sign * (candidates[0] - entry) >= risk * 0.95:
            tp1 = candidates[0]
        if len(candidates) >= 2 and sign * (candidates[1] - entry) >= risk * 1.5:
            tp2 = candidates[1]
        if len(candidates) >= 3 and sign * (candidates[2] - entry) >= risk * 2.4:
            tp3 = candidates[2]

    # Force monotonic targets.
    if direction == 'LONG':
        tp1 = max(tp1, entry + risk)
        tp2 = max(tp2, tp1 + risk * 0.4)
        tp3 = max(tp3, tp2 + risk * 0.4)
    else:
        tp1 = min(tp1, entry - risk)
        tp2 = min(tp2, tp1 - risk * 0.4)
        tp3 = min(tp3, tp2 - risk * 0.4)
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
    if not validate_risk(entry, sl):
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
    cur, prev = c5[-1], c5[-2]
    support, resistance = nearest_support_resistance(cur.close, levels)
    direction = None
    level = None
    touches = 0

    # Find a meaningful nearby structure level from prior candles, then require
    # a break and a retest/hold rather than a raw one-candle breakout.
    highs = [p for p, k, t in levels if k == 'HIGH' and t >= 2 and p < cur.close * 1.012]
    lows = [p for p, k, t in levels if k == 'LOW' and t >= 2 and p > cur.close * 0.988]
    if highs:
        candidate = max(highs)
        if cur.close > candidate and prev.high >= candidate * 0.997:
            direction, level = 'LONG', candidate
            touches = max(t for p, k, t in levels if k == 'HIGH' and abs(p-candidate)/candidate < 0.003)
    if lows and direction is None:
        candidate = min(lows)
        if cur.close < candidate and prev.low <= candidate * 1.003:
            direction, level = 'SHORT', candidate
            touches = max(t for p, k, t in levels if k == 'LOW' and abs(p-candidate)/candidate < 0.003)
    if not direction:
        return None

    # Retest: current candle must interact with broken level and close away from it.
    if direction == 'LONG':
        if cur.low > level * 1.006:
            return None
        if cur.close <= level or cur.close <= prev.close:
            return None
        sl = min(level, cur.low) - atr(c5) * 0.30
        reason = f'15M сопротивление {touches}× → пробой → ретест → 5M подтверждение; объём x{vr:.2f}'
    else:
        if cur.high < level * 0.994:
            return None
        if cur.close >= level or cur.close >= prev.close:
            return None
        sl = max(level, cur.high) + atr(c5) * 0.30
        reason = f'15M поддержка {touches}× → пробой → ретест → 5M подтверждение; объём x{vr:.2f}'

    entry = cur.close
    if abs(entry-level)/max(entry,1e-12)*100 > MAX_ENTRY_CHASE_PCT:
        return None
    if not validate_risk(entry, sl):
        return None
    tps = target_levels(direction, entry, sl, c15)
    if not tps:
        return None
    score = final_score(base_score(ticker,c5,c15,vr), [8, min(6, touches), 5 if vr >= 1.8 else 0], [])
    if score < MIN_SCORE:
        return None
    lo, hi = entry_zone(entry, atr(c5), direction)
    return Setup(iid,get_coin(iid),direction,'BREAKOUT + RETEST','ПРОБОЙ + РЕТЕСТ',level,'BROKEN LEVEL',lo,hi,sl,*tps,score,reason,
                 ticker['vol24h_usd'],ticker['change24h_pct'],ticker['range24h_pct'],atr_pct(c5),atr_pct(c15),vr,c5[-96:],
                 [(len(c5[-96:])-2,level,'RETEST'),(len(c5[-96:])-1,entry,'CONFIRM')],'1–6 ч')

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
        sl=pull_low-atr(c5)*0.30
        reason=f'1H восходящий тренд → 15M откат к EMA20 → 5M возврат выше уровня; объём x{vr:.2f}'
    else:
        pull_high=max(c.high for c in recent)
        if not any(c.high >= e20_15[-3]*0.997 for c in recent):
            return None
        if not (cur.close < e20_15[-1] and cur.close < prev.low and cur.close < cur.open):
            return None
        level=pull_high
        sl=pull_high+atr(c5)*0.30
        reason=f'1H нисходящий тренд → 15M откат к EMA20 → 5M возврат ниже уровня; объём x{vr:.2f}'

    entry=cur.close
    if not validate_risk(entry,sl):
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
    c5,c15=data['5m'],data['15m']
    if len(c5)<90 or len(c15)<70:
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
    if direction=='LONG':
        if not(cur.close>cur.open and cur.close>prev.high and close_location(cur)>=0.62):
            return None
        sl=min(c.low for c in c5[-8:])-atr(c5)*0.25
    else:
        if not(cur.close<cur.open and cur.close<prev.low and close_location(cur)<=0.38):
            return None
        sl=max(c.high for c in c5[-8:])+atr(c5)*0.25
    entry=cur.close
    if not validate_risk(entry,sl):
        return None
    tps=target_levels(direction,entry,sl,c15)
    if not tps:
        return None
    # EMA is useful as a first mean-reversion target only if it is at least 1R away.
    risk=abs(entry-sl)
    ema_target=e20
    if direction=='LONG' and ema_target>entry+risk:
        tp1=ema_target
    elif direction=='SHORT' and ema_target<entry-risk:
        tp1=ema_target
    else:
        tp1=tps[0]
    score=final_score(base_score(ticker,c5,c15,vr),[6,5 if abs(deviation)>=4 else 0],[])
    if score<MIN_SCORE:
        return None
    lo,hi=entry_zone(entry,atr(c5),direction)
    return Setup(iid,get_coin(iid),direction,'MEAN REVERSION','СИЛЬНОЕ ОТКЛОНЕНИЕ → ВОЗВРАТ',e20,'15M EMA20',lo,hi,sl,tp1,tps[1],tps[2],score,
                 f'цена отклонена от 15M EMA20 на {deviation:+.2f}% → 5M подтверждение возврата',ticker['vol24h_usd'],ticker['change24h_pct'],ticker['range24h_pct'],atr_pct(c5),atr_pct(c15),vr,c5[-96:],
                 [(len(c5[-96:])-1,entry,'REVERSAL')],'1–6 ч')


STRATEGIES=(build_reversal,build_breakout,build_pullback,build_mean_reversion)

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
        f'🛑 <b>Стоп:</b> <code>{fmt_price(s.sl)}</code> · риск {risk:.2f}%\n'
        f'🎯 <b>TP1:</b> <code>{fmt_price(s.tp1)}</code>\n'
        f'🎯 <b>TP2:</b> <code>{fmt_price(s.tp2)}</code> · RR 1:{rr2:.1f}\n'
        f'🎯 <b>TP3:</b> <code>{fmt_price(s.tp3)}</code>\n\n'
        f'🧠 <b>Почему:</b> {s.reason}.\n'
        f'⏱ <b>Ожидаемое удержание:</b> {s.hold_hours}\n\n'
        f'⚠️ Внимательно проверьте сделку перед входом.\n'
        f'🛡 Риск на одну сделку — <b>не более 1% депозита</b>.\n'
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


def lifecycle_for_symbol(iid:str, cs:List[Candle]):
    if len(cs)<3:
        return
    last=cs[-1]
    # 1) Pending: wait for actual entry-zone touch.
    p=pending.get(iid)
    if p:
        s=p.setup
        if now_ts()>p.expires_at:
            with db_lock:
                db.execute("UPDATE signals SET status='EXPIRED' WHERE id=? AND status='READY'",(p.signal_id,)); db.commit()
            pending.pop(iid,None)
        elif price_in_entry(s,last.close) or (last.low<=s.entry_high and last.high>=s.entry_low):
            with db_lock:
                db.execute("UPDATE signals SET status='ACTIVE',activated_at=? WHERE id=? AND status='READY'",(now_ts(),p.signal_id)); db.commit()
            active[iid]=ActiveSignal(s,p.signal_id,now_ts(),p.message_id)
            pending.pop(iid,None)

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
    stop=last.low<=s.sl if long else last.high>=s.sl
    both=stop and hit1

    with db_lock:
        row=db.execute('SELECT tp1_hit,tp2_hit,tp3_hit FROM signals WHERE id=?',(a.signal_id,)).fetchone()
    if not row:
        return
    h1,h2,h3=row

    if both and not h1:
        # Conservative handling for an ambiguous candle.
        with db_lock:
            db.execute("UPDATE signals SET status='CLOSED_LOSS',result='SL',closed_at=?,r_multiple=-1 WHERE id=?",(now_ts(),a.signal_id)); db.commit()
        send_update(f'🔴 <b>STOP — {s.coin}USDT</b>\nСтоп затронут в свече с неоднозначным пересечением целей. Результат засчитан консервативно по SL.')
        active.pop(iid,None)
        return

    if hit1 and not h1:
        with db_lock:
            db.execute("UPDATE signals SET tp1_hit=1 WHERE id=?",(a.signal_id,)); db.commit()
        a.tp1_sent=True
        send_update(f'🟢 <b>TP1 — {s.coin}USDT</b>\nЦена достигла <code>{fmt_price(s.tp1)}</code>.\nЧасть позиции можно зафиксировать по своему плану.')
    if hit2 and not h2:
        with db_lock:
            db.execute("UPDATE signals SET tp2_hit=1 WHERE id=?",(a.signal_id,)); db.commit()
        a.tp2_sent=True
        send_update(f'🚀 <b>TP2 — {s.coin}USDT</b>\nЦена достигла <code>{fmt_price(s.tp2)}</code>.\nОстаток сопровождаем по плану.')
    if hit3 and not h3:
        entry=(s.entry_low+s.entry_high)/2
        r=abs(s.tp3-entry)/max(abs(entry-s.sl),1e-12)
        with db_lock:
            db.execute("UPDATE signals SET tp3_hit=1,status='CLOSED_WIN',result='TP3',closed_at=?,r_multiple=? WHERE id=?",(now_ts(),r,a.signal_id)); db.commit()
        send_update(f'🏁 <b>TP3 — {s.coin}USDT</b>\nЦена достигла <code>{fmt_price(s.tp3)}</code>.\nРезультат зафиксирован: TP3.')
        active.pop(iid,None)
        return
    if stop:
        with db_lock:
            db.execute("UPDATE signals SET status='CLOSED_LOSS',result='SL',closed_at=?,r_multiple=-1 WHERE id=?",(now_ts(),a.signal_id)); db.commit()
        send_update(f'🔴 <b>STOP — {s.coin}USDT</b>\nЦена достигла стопа <code>{fmt_price(s.sl)}</code>.\nСделка закрыта по плановому риску.')
        active.pop(iid,None)
        return

    if now_ts()-a.activated_at > ACTIVE_MAX_HOURS*3600:
        with db_lock:
            db.execute("UPDATE signals SET status='TIMEOUT',result='TIMEOUT',closed_at=? WHERE id=?",(now_ts(),a.signal_id)); db.commit()
        send_update(f'⚪ <b>TIMEOUT — {s.coin}USDT</b>\nСделка не дошла до TP3/SL в установленное время. Результат закрыт как TIMEOUT.')
        active.pop(iid,None)


def update_signal_results():
    symbols=set(pending)|set(active)
    for iid in list(symbols):
        try:
            cs=get_candles(iid,'5m',80)
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
# DAILY REPORT — once a day at 21:00, rolling last 24 hours
# ============================================================

def daily_report():
    """
    Единственный ежедневный отчёт канала.

    ВАЖНО: этот блок отвечает только за дневную статистику и аудит
    уже опубликованных сигналов. Поиск монет, стратегии, scoring,
    cooldown и отправка новых сигналов находятся вне него.

    В 21:00 Europe/Kyiv формируется ОДИН PDF-файл за последние 24 часа:
      - сводка по всем опубликованным сигналам;
      - фактический вход / без входа;
      - TP1 / TP2 / TP3;
      - безубыток после TP1;
      - стоп / timeout;
      - результат в R;
      - статистика по каждой стратегии;
      - отдельная страница с графиком для каждого сигнала.

    Перед формированием отчёта незакрытые записи повторно сверяются
    с подтверждёнными 5M свечами OKX, чтобы после рестарта не получать
    ложное "всё в работе".
    """
    n = local_now()

    # Один отчёт на календарный день. Если процесс был перезапущен,
    # отчёт после 21:00 будет отправлен один раз после восстановления.
    if n.hour < 21:
        return

    day_key = n.date().isoformat()
    state_key = 'daily_report_21'

    with db_lock:
        row = db.execute(
            "SELECT value FROM bot_state WHERE key=?",
            (state_key,)
        ).fetchone()

    if row and row[0] == day_key:
        return

    since_dt = n - timedelta(hours=24)
    since = since_dt.timestamp()
    until = n.timestamp()

    # Только сигналы, опубликованные за последние 24 часа.
    with db_lock:
        rows = db.execute(
            """SELECT id, inst_id, direction, strategy, status, result,
                      tp1_hit, tp2_hit, tp3_hit, created_at, activated_at,
                      expires_at, closed_at, r_multiple,
                      entry_low, entry_high, sl, tp1, tp2, tp3
               FROM signals
               WHERE created_at >= ? AND created_at <= ?
                 AND status != 'SEND_FAILED'
               ORDER BY created_at ASC""",
            (since, until)
        ).fetchall()

    # ------------------------------------------------------------
    # REPORT-ONLY RECONCILIATION
    # ------------------------------------------------------------
    # Ничего из поиска новых сигналов здесь не вызывается.
    # Мы только проверяем уже существующие записи signals.
    def reconcile_row(r):
        (sid, inst_id, direction, strategy, status, result,
         h1, h2, h3, created_at, activated_at, expires_at,
         closed_at, r_multiple, entry_low, entry_high, sl,
         tp1, tp2, tp3) = r

        terminal_statuses = {
            'CLOSED_WIN', 'CLOSED_LOSS', 'CLOSED_BE', 'TIMEOUT', 'EXPIRED'
        }
        terminal_results = {'TP2', 'TP3', 'BE', 'SL', 'TIMEOUT', 'NO_ENTRY'}

        if status in terminal_statuses or result in terminal_results:
            return r

        try:
            # 300 x 5M = до 25 часов — достаточно для rolling 24h отчёта
            # плюс небольшой запас.
            candles = get_candles(inst_id, '5m', 300)
            if len(candles) < 3:
                return r

            long = direction == 'LONG'
            entry = (float(entry_low) + float(entry_high)) / 2.0

            # Сначала ищем фактическое касание зоны входа.
            entered = bool(activated_at)
            activation_ts = float(activated_at) if activated_at else None

            signal_candles = [
                c for c in candles
                if c.confirmed and c.ts / 1000.0 >= float(created_at)
            ]

            if not entered:
                for c in signal_candles:
                    touched = (
                        float(entry_low) <= c.close <= float(entry_high)
                        or (c.low <= float(entry_high) and c.high >= float(entry_low))
                    )
                    if touched:
                        entered = True
                        activation_ts = c.ts / 1000.0
                        break

                if not entered:
                    expiry = float(
                        expires_at or
                        (float(created_at) + READY_TTL_MINUTES * 60)
                    )
                    if until >= expiry:
                        return (
                            sid, inst_id, direction, strategy,
                            'EXPIRED', 'NO_ENTRY',
                            int(h1 or 0), int(h2 or 0), int(h3 or 0),
                            created_at, activated_at, expires_at, closed_at,
                            r_multiple, entry_low, entry_high, sl, tp1, tp2, tp3
                        )
                    return r

            trade_candles = [
                c for c in candles
                if c.confirmed and c.ts / 1000.0 >= float(activation_ts)
            ]

            hit1 = bool(h1)
            hit2 = bool(h2)
            hit3 = bool(h3)
            terminal_status = status
            terminal_result = result or ''
            terminal_closed_at = closed_at
            terminal_r = float(r_multiple or 0.0)

            for c in trade_candles:
                hit_tp1 = c.high >= float(tp1) if long else c.low <= float(tp1)
                hit_tp2 = c.high >= float(tp2) if long else c.low <= float(tp2)
                hit_tp3 = c.high >= float(tp3) if long else c.low <= float(tp3)
                hit_sl = c.low <= float(sl) if long else c.high >= float(sl)

                # Если TP1 и SL находятся в одной подтверждённой свече,
                # порядок внутри свечи неизвестен — считаем SL первым.
                if not hit1 and hit_tp1 and hit_sl:
                    terminal_status = 'CLOSED_LOSS'
                    terminal_result = 'SL'
                    terminal_closed_at = c.ts / 1000.0
                    terminal_r = -1.0
                    break

                if not hit1 and hit_sl:
                    terminal_status = 'CLOSED_LOSS'
                    terminal_result = 'SL'
                    terminal_closed_at = c.ts / 1000.0
                    terminal_r = -1.0
                    break

                if hit_tp1:
                    hit1 = True

                if hit1 and hit_tp2:
                    hit2 = True

                if hit2 and hit_tp3:
                    hit3 = True

                if hit3:
                    risk = abs(entry - float(sl))
                    terminal_status = 'CLOSED_WIN'
                    terminal_result = 'TP3'
                    terminal_closed_at = c.ts / 1000.0
                    terminal_r = abs(float(tp3) - entry) / max(risk, 1e-12)
                    break

                # После TP1 стоп считается перенесённым в безубыток.
                if hit1 and not hit2:
                    returned_to_entry = c.low <= entry if long else c.high >= entry
                    if returned_to_entry:
                        terminal_status = 'CLOSED_BE'
                        terminal_result = 'BE'
                        terminal_closed_at = c.ts / 1000.0
                        terminal_r = 0.0
                        break

                # TP2 — положительное закрытие по плану.
                if hit2:
                    risk = abs(entry - float(sl))
                    terminal_status = 'CLOSED_WIN'
                    terminal_result = 'TP2'
                    terminal_closed_at = c.ts / 1000.0
                    terminal_r = abs(float(tp2) - entry) / max(risk, 1e-12)
                    break

            # Если закрывающего события нет — результат не придумываем.
            if terminal_result not in ('TP2', 'TP3', 'BE', 'SL', 'TIMEOUT'):
                terminal_status = 'ACTIVE'
                terminal_result = ''
                terminal_closed_at = None
                terminal_r = 0.0

                if activation_ts and until - activation_ts > ACTIVE_MAX_HOURS * 3600:
                    terminal_status = 'TIMEOUT'
                    terminal_result = 'TIMEOUT'
                    terminal_closed_at = until

            with db_lock:
                db.execute(
                    """UPDATE signals
                       SET status=?, result=?, activated_at=COALESCE(activated_at,?),
                           tp1_hit=?, tp2_hit=?, tp3_hit=?,
                           closed_at=?, r_multiple=?
                       WHERE id=?""",
                    (
                        terminal_status, terminal_result, activation_ts,
                        int(hit1), int(hit2), int(hit3),
                        terminal_closed_at, float(terminal_r or 0.0), sid
                    )
                )
                db.commit()

            return (
                sid, inst_id, direction, strategy,
                terminal_status, terminal_result,
                int(hit1), int(hit2), int(hit3),
                created_at, activation_ts, expires_at,
                terminal_closed_at, terminal_r,
                entry_low, entry_high, sl, tp1, tp2, tp3
            )

        except Exception:
            log.exception('DAILY REPORT RECONCILIATION FAILED | id=%s | %s', sid, inst_id)
            return r

    reconciled_rows = [reconcile_row(r) for r in rows]

    # ------------------------------------------------------------
    # FINAL STATISTICS
    # ------------------------------------------------------------
    total = len(reconciled_rows)

    def is_entry(r):
        return (
            r[10] is not None
            or r[4] in ('ACTIVE', 'CLOSED_WIN', 'CLOSED_LOSS', 'CLOSED_BE', 'TIMEOUT')
        )

    entries = sum(1 for r in reconciled_rows if is_entry(r))
    tp1_count = sum(1 for r in reconciled_rows if int(r[6] or 0))
    tp2_count = sum(1 for r in reconciled_rows if r[5] == 'TP2' or int(r[7] or 0))
    tp3_count = sum(1 for r in reconciled_rows if r[5] == 'TP3' or int(r[8] or 0))
    be_count = sum(1 for r in reconciled_rows if r[5] == 'BE')
    stop_count = sum(1 for r in reconciled_rows if r[5] == 'SL' or r[4] == 'CLOSED_LOSS')
    timeout_count = sum(1 for r in reconciled_rows if r[4] == 'TIMEOUT' or r[5] == 'TIMEOUT')
    no_entry_count = sum(1 for r in reconciled_rows if r[4] == 'EXPIRED' or r[5] == 'NO_ENTRY')
    open_count = sum(1 for r in reconciled_rows if r[4] in ('READY', 'ACTIVE'))

    positive_count = tp2_count + tp3_count
    closed_count = positive_count + be_count + stop_count + timeout_count
    win_rate = positive_count / closed_count * 100 if closed_count else 0.0
    total_r = sum(
        float(r[13] or 0.0)
        for r in reconciled_rows
        if r[5] in ('TP2', 'TP3', 'BE', 'SL')
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
        if r[5] in ('TP2', 'TP3'):
            st['positive'] += 1
        if r[5] == 'BE':
            st['be'] += 1
        if r[5] == 'SL' or r[4] == 'CLOSED_LOSS':
            st['sl'] += 1
        if r[4] == 'TIMEOUT' or r[5] == 'TIMEOUT':
            st['timeout'] += 1
        if r[4] == 'EXPIRED' or r[5] == 'NO_ENTRY':
            st['no_entry'] += 1
        if r[5] in ('TP2', 'TP3', 'BE', 'SL'):
            st['r'] += float(r[13] or 0.0)

    ranked = sorted(
        strategy_stats.items(),
        key=lambda x: (x[1]['r'], x[1]['positive'], -x[1]['sl']),
        reverse=True
    )

    # ------------------------------------------------------------
    # PDF REPORT WITH ONE CHART PAGE PER SIGNAL
    # ------------------------------------------------------------
    pdf_path = f'/tmp/quantum_daily_{day_key}.pdf'
    pdf_created = False

    try:
        from matplotlib.backends.backend_pdf import PdfPages

        with PdfPages(pdf_path) as pdf:
            # Summary page.
            fig = plt.figure(figsize=(11.7, 8.3), dpi=110)
            fig.patch.set_facecolor('white')
            fig.text(0.06, 0.93, 'QUANTUM | VIP SCANNER', fontsize=22, fontweight='bold')
            fig.text(0.06, 0.885, 'ДНЕВНОЙ АУДИТ СИГНАЛОВ · 24 ЧАСА', fontsize=14, fontweight='bold')
            fig.text(0.06, 0.845, f'{since_dt:%d.%m.%Y %H:%M} — {n:%d.%m.%Y %H:%M} · {TIMEZONE}', fontsize=9.5)

            summary = (
                f'Сигналов опубликовано: {total}\n'
                f'Фактических входов: {entries}\n'
                f'Плюсовых закрытий (TP2/TP3): {positive_count}\n'
                f'Безубыток после TP1: {be_count}\n'
                f'Стоп: {stop_count}\n'
                f'TIMEOUT: {timeout_count}\n'
                f'Без входа: {no_entry_count}\n'
                f'Остались в работе: {open_count}\n\n'
                f'TP1 достигнут: {tp1_count}\nTP2 достигнут: {tp2_count}\nTP3 достигнут: {tp3_count}\n'
                f'Win rate закрытых: {win_rate:.1f}%\nСуммарный результат: {total_r:+.2f}R'
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

            fig.text(0.06, 0.055, 'Аудит закрытия: подтверждённые 5M свечи OKX. Если TP и SL попали в одну свечу до TP1 — консервативно засчитывается SL.', fontsize=8.5)
            pdf.savefig(fig, bbox_inches='tight')
            plt.close(fig)

            # One page per signal. The candles are taken around the signal time,
            # not from the current chart, so the report preserves the market context
            # that existed when the signal was published.
            for r in reconciled_rows:
                (sid, inst_id, direction, strategy, status, result,
                 h1, h2, h3, created_at, activated_at, expires_at,
                 closed_at, r_multiple, entry_low, entry_high,
                 sl, tp1, tp2, tp3) = r

                coin = get_coin(inst_id or '')
                entry = (float(entry_low) + float(entry_high)) / 2.0
                created_ts = float(created_at)
                close_ts = float(closed_at) if closed_at else None

                try:
                    candles = get_candles(inst_id, '5m', 300)
                    idx = min(
                        range(len(candles)),
                        key=lambda i: abs(candles[i].ts / 1000.0 - created_ts)
                    ) if candles else 0
                    lo_i = max(0, idx - 60)
                    hi_i = min(len(candles), idx + 36)
                    chart_candles = candles[lo_i:hi_i]
                except Exception:
                    chart_candles = []

                fig, ax = plt.subplots(figsize=(11.7, 7.2), dpi=110)
                fig.patch.set_facecolor('white')
                ax.set_facecolor('white')

                if chart_candles:
                    width = 0.58
                    for i, c in enumerate(chart_candles):
                        rising = c.close >= c.open
                        candle_color = '#16a34a' if rising else '#dc2626'
                        ax.vlines(i, c.low, c.high, color=candle_color, linewidth=0.9)
                        y0 = min(c.open, c.close)
                        hh = max(abs(c.close-c.open), c.close*0.00002)
                        ax.add_patch(Rectangle((i-width/2, y0), width, hh,
                                               facecolor=candle_color, edgecolor=candle_color, linewidth=.4))

                    def report_hline(price, label, color, ls='--', lw=1.5):
                        ax.axhline(float(price), color=color, linewidth=lw, linestyle=ls, zorder=4)
                        ax.text(len(chart_candles)-1.0, float(price), f' {label} {fmt_price(float(price))}',
                                ha='right', va='bottom', fontsize=8.5, fontweight='bold', color=color,
                                bbox=dict(facecolor='white', alpha=.82, edgecolor='none', pad=1.2))

                    report_hline(entry, 'ВХОД', '#2563eb', '-', 2.0)
                    report_hline(sl, 'СТОП', '#dc2626', '--', 1.8)
                    report_hline(tp1, 'TP1', '#16a34a', ':', 1.3)
                    report_hline(tp2, 'TP2', '#16a34a', ':', 1.5)
                    report_hline(tp3, 'TP3', '#15803d', '--', 1.7)
                    ax.axhspan(float(entry_low), float(entry_high), alpha=.08, color='#2563eb', zorder=1)

                    signal_idx = min(
                        range(len(chart_candles)),
                        key=lambda i: abs(chart_candles[i].ts / 1000.0 - created_ts)
                    )
                    ax.scatter([signal_idx], [entry], s=48, marker='o', zorder=7, color='#111827')
                    ax.annotate('СИГНАЛ', (signal_idx, entry), xytext=(0, 16),
                                textcoords='offset points', ha='center', fontsize=8.5, fontweight='bold')

                    if close_ts:
                        close_idx = min(
                            range(len(chart_candles)),
                            key=lambda i: abs(chart_candles[i].ts / 1000.0 - close_ts)
                        )
                        close_price = (
                            float(tp3) if result == 'TP3' else
                            float(tp2) if result == 'TP2' else
                            entry if result == 'BE' else
                            float(sl) if result == 'SL' else
                            entry
                        )
                        ax.scatter([close_idx], [close_price], s=58, marker='X', zorder=8, color='#111827')
                        ax.annotate('ЗАКРЫТИЕ', (close_idx, close_price), xytext=(0, -20),
                                    textcoords='offset points', ha='center', fontsize=8.5, fontweight='bold')

                    ax.set_xlim(-1, len(chart_candles))
                    ax.grid(True, alpha=.14)
                    ax.tick_params(labelsize=8)
                else:
                    ax.text(0.5, 0.5, 'Не удалось получить 5M свечи для графика',
                            transform=ax.transAxes, ha='center', va='center', fontsize=13)
                    ax.set_axis_off()

                side = 'LONG' if direction == 'LONG' else 'SHORT'
                if result == 'TP3' or int(h3 or 0):
                    outcome = 'TP3 · ПЛЮС'
                elif result == 'TP2':
                    outcome = 'TP2 · ПЛЮС'
                elif result == 'BE':
                    outcome = 'BE · БЕЗУБЫТОК'
                elif result == 'SL' or status == 'CLOSED_LOSS':
                    outcome = 'SL · СТОП'
                elif result == 'TIMEOUT' or status == 'TIMEOUT':
                    outcome = 'TIMEOUT'
                elif status == 'EXPIRED':
                    outcome = 'NO ENTRY · БЕЗ ВХОДА'
                elif status == 'ACTIVE':
                    outcome = 'В РАБОТЕ'
                elif status == 'READY':
                    outcome = 'ОЖИДАЕТ ВХОДА'
                else:
                    outcome = result or status or 'НЕ ОПРЕДЕЛЕНО'

                duration = '—'
                if activated_at:
                    end_ts = close_ts if close_ts else until
                    mins = max(0, int((end_ts - float(activated_at)) / 60))
                    duration = f'{mins//60}ч {mins%60:02d}м'

                r_text = f'{float(r_multiple or 0):+.2f}R' if result in ('TP2','TP3','BE','SL') else '—'

                ax.set_title(f'#{sid} · {coin}USDT · {side} · {strategy}', fontsize=14, fontweight='bold', pad=10)
                fig.text(0.08, 0.955, f'Сигнал {datetime.fromtimestamp(created_ts, ZoneInfo(TIMEZONE)):%d.%m.%Y %H:%M} · {outcome}', fontsize=10.5, fontweight='bold')
                fig.text(0.08, 0.025,
                         f'Вход {fmt_price(entry_low)}–{fmt_price(entry_high)} · Стоп {fmt_price(sl)} · '
                         f'TP1 {fmt_price(tp1)} · TP2 {fmt_price(tp2)} · TP3 {fmt_price(tp3)} · '
                         f'Удержание {duration} · Результат {r_text}', fontsize=8.5)
                plt.tight_layout(rect=[0.03, 0.06, 0.97, 0.92])
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

    # ------------------------------------------------------------
    # SEND ONE DAILY REPORT MESSAGE
    # ------------------------------------------------------------
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
        f'🟢 Плюсовых: <b>{positive_count}</b>\n'
        f'⚖️ Безубыток: <b>{be_count}</b>\n'
        f'🔴 Стопов: <b>{stop_count}</b>\n'
        f'⚪ TIMEOUT: <b>{timeout_count}</b>\n'
        f'⌛ Без входа: <b>{no_entry_count}</b>\n'
        f'🔄 В работе: <b>{open_count}</b>\n'
        f'📈 Win rate закрытых: <b>{win_rate:.1f}%</b>\n'
        f'💰 Результат: <b>{total_r:+.2f}R</b>'
        f'{best_line}\n\n'
        f'📎 Вложение содержит полный аудит и график каждого сигнала за 24 часа.'
    )

    try:
        with open(pdf_path, 'rb') as f:
            bot.send_document(
                CHANNEL_ID,
                f,
                caption=caption,
                parse_mode='HTML'
            )

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
    log.info('Strategies: Extreme Reversal | Breakout+Retest | Pullback | Mean Reversion')
    log.info('Universe: gainers | losers | volatile | new active | small alts')
    log.info('============================================================')
    startup_healthcheck()
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
