import os
import time
import logging
import sqlite3
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
# QUANTUM INTRADAY SWING ENGINE V2 — AUDIT FIXES
# OKX USDT perpetuals | 5M entry | 15M structure | 1H context
# Strategies: Extreme Reversal, Breakout+Retest, Trend Pullback,
# Mean Reversion. This bot analyzes and publishes signals only.
# ============================================================

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', os.getenv('BOT_TOKEN', '')).strip()
CHANNEL_ID = os.getenv('CHANNEL_ID', '').strip()
OKX_BASE_URL = os.getenv('OKX_BASE_URL', 'https://www.okx.com').rstrip('/')
TIMEZONE = os.getenv('BOT_TIMEZONE', 'Europe/Kyiv')
DB_PATH = os.getenv('DB_PATH', 'quantum_state.db')

if not TELEGRAM_TOKEN:
    raise RuntimeError('TELEGRAM_TOKEN is missing.')
if not CHANNEL_ID:
    raise RuntimeError('CHANNEL_ID is missing.')

# ---------- market universe ----------
MIN_24H_TURNOVER_USD = float(os.getenv('MIN_24H_TURNOVER_USD', '5000000'))
PREFERRED_TURNOVER_USD = float(os.getenv('PREFERRED_TURNOVER_USD', '15000000'))
TOP_GAINERS = int(os.getenv('TOP_GAINERS', '15'))
TOP_LOSERS = int(os.getenv('TOP_LOSERS', '15'))
# Candidate discovery is intentionally based on movers + live momentum.
# The four strategy builders below remain unchanged.
TOP_VOLATILE = int(os.getenv('TOP_VOLATILE', '25'))
NEW_ACTIVE_DAYS = int(os.getenv('NEW_ACTIVE_DAYS', '30'))
NEW_ACTIVE_COUNT = int(os.getenv('NEW_ACTIVE_COUNT', '15'))
DEEP_SCAN_LIMIT = int(os.getenv('DEEP_SCAN_LIMIT', '30'))
MIN_24H_MOVE_FOR_REVERSAL = float(os.getenv('MIN_24H_MOVE_FOR_REVERSAL', '15'))
MIN_ATR_5M_PCT = float(os.getenv('MIN_ATR_5M_PCT', '0.18'))
MIN_ATR_15M_PCT = float(os.getenv('MIN_ATR_15M_PCT', '0.45'))
MIN_ACTIVE_24H_RANGE_PCT = float(os.getenv('MIN_ACTIVE_24H_RANGE_PCT', '5.0'))

# -------- candidate-quality filters (universe only) --------
MIN_24H_MOVE_FOR_CANDIDATE = float(os.getenv('MIN_24H_MOVE_FOR_CANDIDATE', '8.0'))
MIN_2H_MOVE_FOR_CANDIDATE = float(os.getenv('MIN_2H_MOVE_FOR_CANDIDATE', '3.0'))
MIN_1H_MOVE_FOR_CANDIDATE = float(os.getenv('MIN_1H_MOVE_FOR_CANDIDATE', '1.5'))
MIN_CANDIDATE_VOLUME_RATIO = float(os.getenv('MIN_CANDIDATE_VOLUME_RATIO', '1.05'))
CANDIDATE_MIN_ATR_5M_PCT = float(os.getenv('CANDIDATE_MIN_ATR_5M_PCT', '0.20'))
UNIVERSE_CANDLE_LIMIT = int(os.getenv('UNIVERSE_CANDLE_LIMIT', '40'))
UNIVERSE_REFRESH_SECONDS = int(os.getenv('UNIVERSE_REFRESH_SECONDS', '45'))

MORNING_GREETING_HOUR = int(os.getenv('MORNING_GREETING_HOUR', '9'))
MORNING_GREETING_MINUTE = int(os.getenv('MORNING_GREETING_MINUTE', '0'))

# ---------- strategy ----------
MIN_VOLUME_RATIO = float(os.getenv('MIN_VOLUME_RATIO', '1.20'))
MIN_BREAKOUT_VOLUME = float(os.getenv('MIN_BREAKOUT_VOLUME', '1.35'))
MAX_ENTRY_CHASE_PCT = float(os.getenv('MAX_ENTRY_CHASE_PCT', '1.00'))
MAX_RISK_PCT = float(os.getenv('MAX_RISK_PCT', '1.00'))
MIN_RISK_PCT = float(os.getenv('MIN_RISK_PCT', '0.20'))
TP1_R = float(os.getenv('TP1_R', '1.20'))
TP2_R = float(os.getenv('TP2_R', '2.00'))
TP3_R = float(os.getenv('TP3_R', '3.00'))
MIN_SCORE = int(os.getenv('MIN_SCORE', '82'))

# ---------- runtime ----------
SCAN_INTERVAL_SECONDS = int(os.getenv('SCAN_INTERVAL_SECONDS', '30'))
HTTP_TIMEOUT = int(os.getenv('HTTP_TIMEOUT', '10'))
REQUEST_RETRIES = int(os.getenv('REQUEST_RETRIES', '3'))
CANDLE_CACHE_SECONDS = int(os.getenv('CANDLE_CACHE_SECONDS', '12'))
MAX_SIGNALS_PER_HOUR = int(os.getenv('MAX_SIGNALS_PER_HOUR', '4'))
MAX_SIGNALS_PER_DAY = int(os.getenv('MAX_SIGNALS_PER_DAY', '15'))
READY_TTL_MINUTES = int(os.getenv('READY_TTL_MINUTES', '20'))
COOLDOWN_MINUTES = int(os.getenv('COOLDOWN_MINUTES', '120'))

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
log = logging.getLogger('QUANTUM')
bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode='HTML')

# ============================================================
# TELEGRAM POLLING SAFETY
# ============================================================
# Telegram allows only one active getUpdates consumer for a bot token.
# If another instance is already polling, Telegram returns HTTP 409.
# We run polling in one dedicated daemon thread and stop that thread
# cleanly on a 409 instead of letting the polling loop fight forever.

_telegram_polling_started = False

def start_telegram_polling():
    global _telegram_polling_started

    if _telegram_polling_started:
        log.warning('TELEGRAM POLLING | already started, skip duplicate start')
        return

    _telegram_polling_started = True
    log.info('TELEGRAM POLLING START')

    try:
        bot.infinity_polling(
            skip_pending=True,
            timeout=20,
            long_polling_timeout=20,
            allowed_updates=None,
        )
    except Exception as exc:
        # pyTelegramBotAPI exposes Telegram API errors with error_code.
        # Do not endlessly reconnect when another instance owns getUpdates.
        if getattr(exc, 'error_code', None) == 409 or '409' in str(exc) and 'Conflict' in str(exc):
            log.error(
                'TELEGRAM 409 CONFLICT | another bot instance is using getUpdates. '
                'Polling stopped for this process.'
            )
        else:
            log.exception('TELEGRAM POLLING STOPPED | %s', exc)
    finally:
        _telegram_polling_started = False

session = requests.Session()
session.headers.update({'User-Agent': 'QuantumIntradaySwing/1.0', 'Accept': 'application/json'})

# ============================================================
# DATA
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
    lines: List[Tuple[int, float, int, float, str]] = field(default_factory=list)
    hold_hours: str = '1–8 ч'

@dataclass
class ActiveReady:
    setup: Setup
    created_at: float
    expires_at: float
    message_id: Optional[int] = None
    tp1_sent: bool = False
    tp2_sent: bool = False
    tp3_sent: bool = False
    sl_sent: bool = False

ready_setups: Dict[str, ActiveReady] = {}
signals_hour: List[float] = []
signals_today = 0
candle_cache: Dict[Tuple[str, str], Tuple[float, List[Candle]]] = {}

# ============================================================
# DATABASE
# ============================================================
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute('''CREATE TABLE IF NOT EXISTS signals (
 id INTEGER PRIMARY KEY AUTOINCREMENT, inst_id TEXT, direction TEXT, strategy TEXT,
 level REAL, entry_low REAL, entry_high REAL, sl REAL, tp1 REAL, tp2 REAL, tp3 REAL,
 score INTEGER, status TEXT, created_at REAL, activated_at REAL, expires_at REAL,
 tp1_hit INTEGER DEFAULT 0, tp2_hit INTEGER DEFAULT 0, tp3_hit INTEGER DEFAULT 0,
 result TEXT DEFAULT '', closed_at REAL, r_multiple REAL DEFAULT 0)''')
db.execute('''CREATE TABLE IF NOT EXISTS signal_votes (
 signal_id INTEGER NOT NULL, user_id INTEGER NOT NULL, vote TEXT NOT NULL,
 created_at REAL NOT NULL, PRIMARY KEY(signal_id, user_id))''')
db.execute('''CREATE TABLE IF NOT EXISTS bot_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)''')
db.commit()

# ============================================================
# HELPERS
# ============================================================
def now_ts(): return time.time()
def local_now(): return datetime.now(ZoneInfo(TIMEZONE))
def get_coin(inst_id): return inst_id.replace('-USDT-SWAP', '')

def fmt_price(p):
    if p >= 1000: return f'{p:,.2f}'.replace(',', ' ')
    if p >= 100: return f'{p:,.2f}'.replace(',', ' ')
    if p >= 1: return f'{p:,.4f}'.replace(',', ' ')
    if p >= 0.01: return f'{p:.6f}'.rstrip('0').rstrip('.')
    return f'{p:.10f}'.rstrip('0').rstrip('.')

def pct_move(a, b): return (a - b) / max(abs(b), 1e-12) * 100

def ema(vals, period):
    if not vals: return []
    k = 2 / (period + 1)
    out = [vals[0]]
    for x in vals[1:]: out.append(x * k + out[-1] * (1-k))
    return out

def atr(cs, period=14):
    if len(cs) < period + 1: return 0
    trs = []
    for i in range(1, len(cs)):
        c, p = cs[i], cs[i-1]
        trs.append(max(c.high-c.low, abs(c.high-p.close), abs(c.low-p.close)))
    return sum(trs[-period:]) / period if len(trs) >= period else 0

def atr_pct(cs):
    return atr(cs) / max(cs[-1].close, 1e-12) * 100 if cs else 0

def vol_ratio(cs, lookback=20):
    """Compare current 5M quote-volume pace with completed 5M candles.

    OKX returns the currently forming candle with confirm=0. Comparing its
    partial volume directly with five-minute completed candles caused false
    readings such as x0.04-x0.30. Normalize that partial candle by elapsed
    time, with a conservative floor to avoid extreme early-candle spikes.
    """
    if len(cs) < lookback + 1:
        return 0.0
    cur = cs[-1]
    hist_candles = cs[-lookback-1:-1]
    hist = [c.quote_volume for c in hist_candles if c.confirmed and c.quote_volume > 0]
    # If history has too few confirmed values, fall back to positive values.
    if len(hist) < max(5, lookback // 2):
        hist = [c.quote_volume for c in hist_candles if c.quote_volume > 0]
    if not hist:
        return 0.0
    baseline = sum(hist) / len(hist)
    if baseline <= 0 or cur.quote_volume <= 0:
        return 0.0
    ratio = cur.quote_volume / baseline
    if not cur.confirmed:
        elapsed = (now_ts() * 1000 - cur.ts) / 300_000.0
        elapsed = clamp(elapsed, 0.25, 1.0)
        ratio /= elapsed
    return min(ratio, 5.0)

def body_ratio(c):
    r = c.high-c.low
    return abs(c.close-c.open)/r if r > 0 else 0

def recent_range(cs, n):
    x = cs[-n:]
    return (max(c.high for c in x)-min(c.low for c in x))/max(x[-1].close,1e-12)*100 if x else 0

def highest(cs, n): return max(c.high for c in cs[-n:])
def lowest(cs, n): return min(c.low for c in cs[-n:])

def clamp(x, a, b): return max(a, min(b, x))

# ============================================================
# OKX DATA
# ============================================================
def okx_get(path, params):
    err = None
    for attempt in range(1, REQUEST_RETRIES+1):
        try:
            r = session.get(OKX_BASE_URL + path, params=params, timeout=HTTP_TIMEOUT)
            r.raise_for_status(); payload = r.json()
            if str(payload.get('code','')) != '0': raise RuntimeError(payload.get('msg','OKX error'))
            return payload
        except Exception as exc:
            err = exc; log.warning('OKX FAILED | %s | %s/%s | %s', path, attempt, REQUEST_RETRIES, exc)
            if attempt < REQUEST_RETRIES: time.sleep(attempt * 0.8)
    raise RuntimeError(f'OKX request failed: {err}')

def get_instruments():
    data = okx_get('/api/v5/public/instruments', {'instType':'SWAP'}).get('data', [])
    out = {}
    for x in data:
        iid = str(x.get('instId',''))
        if not iid.endswith('-USDT-SWAP') or x.get('state') != 'live': continue
        try:
            out[iid] = {'tickSz': float(x.get('tickSz') or 0), 'lotSz': float(x.get('lotSz') or 0),
                        'listTime': int(x.get('listTime') or 0)}
        except (TypeError, ValueError): out[iid] = {}
    return out

def get_tickers():
    data = okx_get('/api/v5/market/tickers', {'instType':'SWAP'}).get('data', [])
    out = {}
    for x in data:
        iid = str(x.get('instId',''))
        if not iid.endswith('-USDT-SWAP'): continue
        try:
            last=float(x.get('last') or 0); op=float(x.get('open24h') or 0)
            hi=float(x.get('high24h') or 0); lo=float(x.get('low24h') or 0)
            v=float(x.get('vol24h') or 0); vc=float(x.get('volCcy24h') or 0)
            if last <= 0 or op <= 0: continue
            turnover = vc * last
            fallback = v * last
            if turnover <= 0:
                turnover = fallback
            elif fallback > 0 and turnover > fallback * 1000:
                turnover = fallback
            change = (last/op - 1) * 100
            rng = (hi-lo)/last*100 if hi>lo else 0
            out[iid]={'last':last,'open24h':op,'high24h':hi,'low24h':lo,'vol24h_usd':turnover,
                      'change24h_pct':change,'range24h_pct':rng,'ts':int(x.get('ts') or 0)}
        except (TypeError,ValueError): continue
    return out

def get_candles(inst_id, bar, limit=160):
    key=(inst_id,bar); cached=candle_cache.get(key)
    if cached and now_ts()-cached[0] < CANDLE_CACHE_SECONDS: return cached[1]
    rows=okx_get('/api/v5/market/candles', {'instId':inst_id,'bar':bar,'limit':str(min(limit,300))}).get('data',[])
    out=[]
    for r in reversed(rows):
        try:
            out.append(Candle(int(r[0]),float(r[1]),float(r[2]),float(r[3]),float(r[4]),float(r[5] or 0),float(r[7] or 0),str(r[8])=='1'))
        except (IndexError,TypeError,ValueError): pass
    candle_cache[key]=(now_ts(),out); return out

def load_symbol(iid):
    return {'1h':get_candles(iid,'1H',140),'15m':get_candles(iid,'15m',160),'5m':get_candles(iid,'5m',180)}

# ============================================================
# UNIVERSE / CANDIDATE RANKING
# ============================================================
def _candidate_momentum(iid):
    """Return live 5M momentum metrics used ONLY to rank the universe."""
    cs = get_candles(iid, '5m', UNIVERSE_CANDLE_LIMIT)
    if len(cs) < 25:
        return None

    # Use the last confirmed candle as the source for candidate ranking.
    confirmed = [c for c in cs if c.confirmed]
    if len(confirmed) < 25:
        return None
    x = confirmed
    cur = x[-1]
    p12 = x[-13].close      # ~1H
    p24 = x[-25].close      # ~2H
    move_1h = pct_move(cur.close, p12)
    move_2h = pct_move(cur.close, p24)
    atr5 = atr_pct(x)
    vr = vol_ratio(x)
    range_1h = recent_range(x, 12)

    # Acceleration: the last hour should not be completely dead compared
    # with the preceding hour. This is a ranking feature, not a signal gate.
    if len(x) >= 25:
        prev_hour = pct_move(x[-13].close, x[-25].close)
        last_hour = move_1h
        acceleration = abs(last_hour) - abs(prev_hour)
    else:
        acceleration = 0.0

    return {
        'move_1h': move_1h,
        'move_2h': move_2h,
        'atr5': atr5,
        'volume_ratio': vr,
        'range_1h': range_1h,
        'acceleration': acceleration,
        'last': cur.close,
    }

def build_universe(instruments, tickers):
    """
    Build a small, high-quality candidate universe for the four strategies.

    Important: this function does NOT change any strategy or signal logic.
    It only decides which coins are worth sending into the existing builders.

    Pipeline:
      1) liquidity + 24H activity
      2) top gainers / top losers
      3) minimum 24H move
      4) live 1H/2H momentum from confirmed 5M candles
      5) volatility + volume quality
      6) momentum score and top-N ranking
    """
    base = []
    for iid, t in tickers.items():
        if iid not in instruments:
            continue
        vol = t['vol24h_usd']
        rng = t['range24h_pct']
        move = abs(t['change24h_pct'])
        if vol < MIN_24H_TURNOVER_USD:
            continue
        if rng < MIN_ACTIVE_24H_RANGE_PCT:
            continue
        if move < MIN_24H_MOVE_FOR_CANDIDATE:
            continue
        base.append((iid, t))

    gainers = sorted(
        [z for z in base if z[1]['change24h_pct'] > 0],
        key=lambda z: z[1]['change24h_pct'], reverse=True
    )[:TOP_GAINERS]
    losers = sorted(
        [z for z in base if z[1]['change24h_pct'] < 0],
        key=lambda z: z[1]['change24h_pct']
    )[:TOP_LOSERS]

    # Only top movers are allowed into the expensive live-momentum ranking.
    mover_pool = {}
    for iid, t in gainers + losers:
        mover_pool[iid] = (iid, t)

    ranked = []
    for iid, t in mover_pool.values():
        try:
            m = _candidate_momentum(iid)
            if not m:
                continue

            # Direction is deliberately NOT forced here. Each of the four
            # strategies keeps its own LONG/SHORT logic unchanged.
            same_direction = (
                t['change24h_pct'] * m['move_2h'] > 0
            )

            # Reject coins that are only top movers on old 24H data but have
            # gone completely flat recently.
            if abs(m['move_2h']) < MIN_2H_MOVE_FOR_CANDIDATE:
                continue
            if abs(m['move_1h']) < MIN_1H_MOVE_FOR_CANDIDATE:
                continue
            if m['atr5'] < CANDIDATE_MIN_ATR_5M_PCT:
                continue
            if m['volume_ratio'] < MIN_CANDIDATE_VOLUME_RATIO:
                continue

            # Score is only for candidate ranking. It does not alter the
            # score of a generated signal.
            score = 0.0
            score += min(abs(t['change24h_pct']), 40.0) * 1.10
            score += min(abs(m['move_2h']), 15.0) * 2.00
            score += min(abs(m['move_1h']), 8.0) * 2.25
            score += min(m['atr5'], 1.5) * 10.0
            score += min(max(m['volume_ratio'] - 1.0, 0.0), 3.0) * 7.0
            score += min(m['range_1h'], 8.0) * 1.25
            if same_direction:
                score += 8.0
            if m['acceleration'] > 0:
                score += min(m['acceleration'], 5.0) * 2.0
            if t['vol24h_usd'] >= PREFERRED_TURNOVER_USD:
                score += 5.0

            ranked.append((iid, t, score, m))
        except Exception as exc:
            log.debug('UNIVERSE MOMENTUM FAILED | %s | %s', iid, exc)

    ranked.sort(key=lambda z: z[2], reverse=True)
    final = [(iid, t, score) for iid, t, score, _ in ranked[:DEEP_SCAN_LIMIT]]

    log.info(
        'UNIVERSE | gainers=%d losers=%d mover_pool=%d qualified=%d deep=%d',
        len(gainers), len(losers), len(mover_pool), len(ranked), len(final)
    )

    if ranked:
        preview = ', '.join(
            f"{get_coin(iid)} {t['change24h_pct']:+.1f}%/{m['move_2h']:+.1f}%/score{score:.0f}"
            for iid, t, score, m in ranked[:8]
        )
        log.info('UNIVERSE TOP | %s', preview)

    return final

# ============================================================
# STRUCTURE / LEVELS
# ============================================================
def level_candidates(c15):
    if len(c15)<30: return []
    levels=[]
    for n,kind in ((12,'S/R 1H'),(24,'S/R 6H'),(48,'S/R 12H')):
        if len(c15)>=n:
            x=c15[-n:]
            levels += [(highest(x, min(12,len(x))),'RESISTANCE'),(lowest(x,min(12,len(x))),'SUPPORT')]
    # recent pivot-like extremes
    for i in range(2,len(c15)-2):
        c=c15[i]
        if c.high>=max(z.high for z in c15[i-2:i+3]): levels.append((c.high,'RESISTANCE'))
        if c.low<=min(z.low for z in c15[i-2:i+3]): levels.append((c.low,'SUPPORT'))
    return levels

def nearest_level(price, levels, direction=None):
    candidates=[]
    for p,k in levels:
        if direction=='LONG' and p <= price: candidates.append((price-p,p,k))
        elif direction=='SHORT' and p >= price: candidates.append((p-price,p,k))
        elif direction is None: candidates.append((abs(p-price),p,k))
    return min(candidates,key=lambda x:x[0]) if candidates else None

def swing_points(c5, direction):
    n=min(60,len(c5)); x=c5[-n:]
    if direction=='LONG':
        idx=min(range(3,len(x)-3),key=lambda i:x[i].low)
        return len(c5)-n+idx,x[idx].low
    idx=max(range(3,len(x)-3),key=lambda i:x[i].high)
    return len(c5)-n+idx,x[idx].high

# ============================================================
# RISK / TARGETS
# ============================================================
def make_targets(direction, entry, sl, levels):
    risk=abs(entry-sl)
    if risk<=0: return None
    structural=[]
    if direction=='LONG':
        structural=sorted([p for p,k in levels if p>entry])
    else:
        structural=sorted([p for p,k in levels if p<entry], reverse=True)
    def target(rmult, idx):
        raw=entry + risk*rmult*(1 if direction=='LONG' else -1)
        if structural:
            # Prefer a structural target when it still gives at least 1R.
            candidates=[p for p in structural if (p-entry)*(1 if direction=='LONG' else -1) >= risk*0.9]
            if candidates:
                p=candidates[min(idx,len(candidates)-1)]
                if (p-entry)*(1 if direction=='LONG' else -1) >= risk*rmult*0.75: return p
        return raw
    tp1=target(TP1_R,0); tp2=target(TP2_R,1); tp3=target(TP3_R,2)
    if direction=='LONG': tp1=max(tp1,entry+risk); tp2=max(tp2,tp1+risk*0.4); tp3=max(tp3,tp2+risk*0.4)
    else: tp1=min(tp1,entry-risk); tp2=min(tp2,tp1-risk*0.4); tp3=min(tp3,tp2-risk*0.4)
    return tp1,tp2,tp3

def validate_setup(entry, sl, direction):
    risk=abs(entry-sl)/max(entry,1e-12)*100
    return MIN_RISK_PCT <= risk <= MAX_RISK_PCT


def validate_target_order(setup):
    """Reject inverted/too-close targets and require a usable risk/reward."""
    entry=(setup.entry_low+setup.entry_high)/2
    risk=abs(entry-setup.sl)
    if entry <= 0 or risk <= 0:
        return False, 'invalid entry/stop'
    sign=1 if setup.direction=='LONG' else -1
    distances=[(tp-entry)*sign for tp in (setup.tp1,setup.tp2,setup.tp3)]
    if not (0 < distances[0] < distances[1] < distances[2]):
        return False, 'TP order is invalid'
    rr=[d/risk for d in distances]
    # Structural targets may replace the theoretical R targets, but should
    # still leave enough room to justify publishing the setup.
    if rr[0] < 1.0 or rr[1] < 1.6 or rr[2] < 2.2:
        return False, f'insufficient RR ({rr[0]:.2f}/{rr[1]:.2f}/{rr[2]:.2f})'
    return True, 'ok'


def early_entry_check(setup, c5):
    """Avoid entering after most of the recent directional impulse is spent.

    This is deliberately a timing filter, not a new strategy. It applies to
    all existing strategies and rejects only a large, already-extended move
    when price is still sitting close to its local extreme.
    """
    if len(c5) < 25:
        return True, 'insufficient history; timing filter skipped'
    entry=(setup.entry_low+setup.entry_high)/2
    window=c5[-25:-1]  # prior ~2 hours; exclude the trigger candle
    start=window[0].close
    if start <= 0 or entry <= 0:
        return False, 'invalid prices'
    sign=1 if setup.direction=='LONG' else -1
    directional_move=((entry/start)-1)*100*sign
    if setup.direction=='LONG':
        extreme=max(c.high for c in window)
        total=(extreme/start-1)*100
        distance=(extreme-entry)/entry*100
        progress=(entry-start)/max(extreme-start, 1e-12)
    else:
        extreme=min(c.low for c in window)
        total=(start-extreme)/start*100
        distance=(entry-extreme)/entry*100
        progress=(start-entry)/max(start-extreme, 1e-12)
    # A move >=3.5% over ~2h with >=72% of the move already travelled and
    # price within 0.55% of the extreme is considered late/exhausted.
    if directional_move >= 3.5 and total >= 3.5 and progress >= 0.72 and distance <= 0.55:
        return False, (f'late entry: impulse {directional_move:.2f}%, '
                       f'{progress*100:.0f}% travelled, {distance:.2f}% from extreme')
    # Each strategy already validates its own trigger distance. Do not compare
    # reversal/mean-reversion entries to their extreme/EMA level here: that
    # would incorrectly reject the very setups those strategies are designed for.
    return True, 'timing accepted'

def score_base(ticker,c5,c15,volume_ratio_value):
    s=60
    if abs(ticker['change24h_pct'])>=25: s+=8
    if abs(ticker['change24h_pct'])>=35: s+=5
    if ticker['range24h_pct']>=10: s+=6
    if atr_pct(c5)>=MIN_ATR_5M_PCT: s+=4
    if atr_pct(c15)>=MIN_ATR_15M_PCT: s+=4
    if volume_ratio_value>=1.5: s+=7
    elif volume_ratio_value>=1.2: s+=4
    if ticker['vol24h_usd']>=PREFERRED_TURNOVER_USD: s+=4
    return min(99,s)

# ============================================================
# STRATEGIES
# ============================================================
def build_reversal(iid,ticker,c):
    c5,c15,c1h=c['5m'],c['15m'],c['1h']
    if min(map(len,(c5,c15,c1h))) < 50: return None
    ch=ticker['change24h_pct']; direction='LONG' if ch <= -MIN_24H_MOVE_FOR_REVERSAL else 'SHORT' if ch >= MIN_24H_MOVE_FOR_REVERSAL else None
    if not direction or atr_pct(c5)<MIN_ATR_5M_PCT or atr_pct(c15)<MIN_ATR_15M_PCT: return None
    move2h=pct_move(c5[-1].close,c5[-25].close)
    if direction=='LONG' and move2h>-4: return None
    if direction=='SHORT' and move2h<4: return None
    # Exclude the current candle from the extreme and demand a recent failed continuation.
    ext_i,ext=swing_points(c5[:-1],direction)
    cur=c5[-1]; prev=c5[-2]; vr=vol_ratio(c5)
    if direction=='LONG':
        reversal=pct_move(cur.close,ext)
        if not (reversal>=0.55 and cur.close>prev.high*0.999 and cur.close>cur.open and body_ratio(cur)>=0.45): return None
        recent_low=min(x.low for x in c5[-8:-1])
        if cur.low > recent_low*1.025: return None
        level=ext; sl=ext-atr(c5,14)*0.45
        pattern='EXTREME REVERSAL · LONG'
        reason=f'падение 24H {ch:+.1f}% → 2H {move2h:+.1f}% → экстремум → первый подтверждённый разворот вверх'
        point_label='EXTREME LOW'
    else:
        reversal=pct_move(ext,cur.close)
        if not (reversal>=0.55 and cur.close<prev.low*1.001 and cur.close<cur.open and body_ratio(cur)>=0.45): return None
        recent_high=max(x.high for x in c5[-8:-1])
        if cur.high < recent_high*0.975: return None
        level=ext; sl=ext+atr(c5,14)*0.45
        pattern='EXTREME REVERSAL · SHORT'
        reason=f'рост 24H {ch:+.1f}% → 2H {move2h:+.1f}% → экстремум → первый подтверждённый разворот вниз'
        point_label='EXTREME HIGH'
    entry=cur.close
    if abs(entry-ext)/entry*100>MAX_ENTRY_CHASE_PCT*4 or not validate_setup(entry,sl,direction): return None
    levels=level_candidates(c15); tps=make_targets(direction,entry,sl,levels)
    if not tps: return None
    vr=vol_ratio(c5); score=score_base(ticker,c5,c15,vr)
    score+=8 if reversal>=1.2 else 4
    score+=6 if vr>=1.5 else 2 if vr>=1.2 else 0
    if score<MIN_SCORE:return None
    return Setup(iid,get_coin(iid),direction,'EXTREME REVERSAL',pattern,level,point_label,entry*0.998,entry*1.002,sl,*tps,min(99,score),reason,ticker['vol24h_usd'],ch,ticker['range24h_pct'],atr_pct(c5),atr_pct(c15),vr,c5[-96:],[(max(0,ext_i-(len(c5)-96)),ext,point_label),(len(c5[-96:])-1,entry,'CONFIRMATION')],[], '1–8 ч')

def build_breakout(iid,ticker,c):
    c5,c15=c['5m'],c['15m']
    if len(c5)<50 or len(c15)<50:return None
    vr=vol_ratio(c5)
    if vr<MIN_BREAKOUT_VOLUME:return None
    x=c15[-24:-2]; res=max(z.high for z in x); sup=min(z.low for z in x); cur=c5[-1]; prev=c5[-2]
    direction=None; level=None
    if cur.close>res and prev.close<=res*1.003: direction='LONG'; level=res
    elif cur.close<sup and prev.close>=sup*0.997: direction='SHORT'; level=sup
    else:return None
    # Require a retest wick or a very tight distance from the broken level.
    if direction=='LONG':
        if cur.low>level*1.008:return None
        sl=min(level,cur.low)-atr(c5)*0.35
    else:
        if cur.high<level*0.992:return None
        sl=max(level,cur.high)+atr(c5)*0.35
    entry=cur.close
    if not validate_setup(entry,sl,direction):return None
    if abs(entry-level)/entry*100>MAX_ENTRY_CHASE_PCT:return None
    levels=level_candidates(c15); tps=make_targets(direction,entry,sl,levels)
    if not tps:return None
    score=score_base(ticker,c5,c15,vr)+8+min(7,int(vr*2))
    if score<MIN_SCORE:return None
    return Setup(iid,get_coin(iid),direction,'BREAKOUT + RETEST','BREAKOUT + RETEST',level,'BROKEN LEVEL',entry*0.999,entry*1.001,sl,*tps,min(99,score),f'15M уровень пробит → ретест уровня → 5M подтверждение; объём x{vr:.2f}',ticker['vol24h_usd'],ticker['change24h_pct'],ticker['range24h_pct'],atr_pct(c5),atr_pct(c15),vr,c5[-96:],[(len(c5[-96:])-1,entry,'BREAKOUT')],[], '1–6 ч')

def build_pullback(iid,ticker,c):
    c5,c15,c1h=c['5m'],c['15m'],c['1h']
    if min(map(len,(c5,c15,c1h)))<50:return None
    e1=ema([z.close for z in c1h],20)[-1]; e2=ema([z.close for z in c1h],50)[-1]
    trend='LONG' if e1>e2 else 'SHORT' if e1<e2 else None
    if not trend:return None
    cur=c5[-1]; prev=c5[-2]; e15=ema([z.close for z in c15],20)[-1]; vr=vol_ratio(c5)
    if trend=='LONG':
        if cur.close<e15 or cur.close<=prev.high or cur.close<=cur.open:return None
        pull=min(z.low for z in c15[-8:]); level=pull; sl=pull-atr(c5)*0.45
    else:
        if cur.close>e15 or cur.close>=prev.low or cur.close>=cur.open:return None
        pull=max(z.high for z in c15[-8:]); level=pull; sl=pull+atr(c5)*0.45
    entry=cur.close
    if not validate_setup(entry,sl,trend):return None
    levels=level_candidates(c15); tps=make_targets(trend,entry,sl,levels)
    if not tps:return None
    score=score_base(ticker,c5,c15,vr)+8
    if vr>=1.2:score+=4
    if score<MIN_SCORE:return None
    return Setup(iid,get_coin(iid),trend,'TREND PULLBACK','TREND PULLBACK',level,'PULLBACK LEVEL',entry*0.998,entry*1.002,sl,*tps,min(99,score),f'1H тренд {trend} → 15M откат → 5M подтверждение продолжения',ticker['vol24h_usd'],ticker['change24h_pct'],ticker['range24h_pct'],atr_pct(c5),atr_pct(c15),vr,c5[-96:],[(len(c5[-96:])-1,entry,'TRIGGER')],[], '1–8 ч')

def build_mean_reversion(iid,ticker,c):
    c5,c15=c['5m'],c['15m']
    if len(c5)<80 or len(c15)<50:return None
    vals=[z.close for z in c15]; e20=ema(vals,20)[-1]; cur=c5[-1]; prev=c5[-2]; deviation=(cur.close-e20)/e20*100
    direction='LONG' if deviation<=-2.5 else 'SHORT' if deviation>=2.5 else None
    if not direction:return None
    vr=vol_ratio(c5)
    if direction=='LONG':
        if not(cur.close>prev.high and cur.close>cur.open):return None
        sl=min(z.low for z in c5[-10:])-atr(c5)*0.3
    else:
        if not(cur.close<prev.low and cur.close<cur.open):return None
        sl=max(z.high for z in c5[-10:])+atr(c5)*0.3
    entry=cur.close
    if not validate_setup(entry,sl,direction):return None
    # Mean-reversion targets are EMA and then structural levels.
    target1=e20
    risk=abs(entry-sl)
    if direction=='LONG' and target1<=entry+risk:return None
    if direction=='SHORT' and target1>=entry-risk:return None
    levels=level_candidates(c15); tps=make_targets(direction,entry,sl,levels)
    if not tps:return None
    tp1=target1
    if direction=='LONG': tp1=min(max(tp1,entry+risk),tps[0]) if tp1>entry else tps[0]
    else: tp1=max(min(tp1,entry-risk),tps[0]) if tp1<entry else tps[0]
    score=score_base(ticker,c5,c15,vr)+5
    if abs(deviation)>=4:score+=5
    if score<MIN_SCORE:return None
    return Setup(iid,get_coin(iid),direction,'MEAN REVERSION','MEAN REVERSION',e20,'EMA20 15M',entry*0.998,entry*1.002,sl,tp1,tps[1],tps[2],min(99,score),f'цена отклонена от 15M EMA20 на {deviation:+.2f}% → подтверждён возврат к структуре',ticker['vol24h_usd'],ticker['change24h_pct'],ticker['range24h_pct'],atr_pct(c5),atr_pct(c15),vr,c5[-96:],[(len(c5[-96:])-1,entry,'REVERSAL')],[], '1–6 ч')

STRATEGY_BUILDERS=(build_reversal,build_breakout,build_pullback,build_mean_reversion)

# ============================================================
# TELEGRAM VOTING
# ============================================================
VOTES={'strong':'🔥','good':'👍','weak':'👎','miss':'❌','watch':'👀'}

def vote_keyboard(signal_id):
    rows=[]
    for key,label in [('strong','🔥 Сильный'),('good','👍 Норм'),('weak','👎 Слабый'),('miss','❌ Мимо'),('watch','👀 Наблюдаю')]:
        n=db.execute('SELECT COUNT(*) FROM signal_votes WHERE signal_id=? AND vote=?',(signal_id,key)).fetchone()[0]
        rows.append(InlineKeyboardButton(f'{label} {n}',callback_data=f'vote:{signal_id}:{key}'))
    return InlineKeyboardMarkup([rows[:2],rows[2:4],rows[4:]])

def vote_counts(signal_id):
    rows=db.execute('SELECT vote,COUNT(*) FROM signal_votes WHERE signal_id=? GROUP BY vote',(signal_id,)).fetchall()
    return {v:n for v,n in rows}

@bot.callback_query_handler(func=lambda call: bool(call.data and call.data.startswith('vote:')))
def on_vote(call):
    try:
        _,sid_s,vote=call.data.split(':',2); sid=int(sid_s)
        if vote not in VOTES: raise ValueError('bad vote')
        db.execute('INSERT OR REPLACE INTO signal_votes(signal_id,user_id,vote,created_at) VALUES(?,?,?,?)',(sid,call.from_user.id,vote,now_ts()))
        db.commit()
        try: bot.answer_callback_query(call.id,'Ваш голос учтён. Его можно изменить.')
        except Exception: pass
        try: bot.edit_message_reply_markup(CHANNEL_ID,call.message.message_id,reply_markup=vote_keyboard(sid))
        except Exception: pass
    except Exception:
        try: bot.answer_callback_query(call.id,'Не удалось сохранить реакцию.')
        except Exception: pass

# ============================================================
# CHART
# ============================================================
def make_chart(setup):
    cs=setup.candles_5m[-96:]
    if len(cs)<35: raise RuntimeError('Not enough candles for chart')
    path=f'/tmp/quantum_{setup.coin}_{int(time.time()*1000)}.png'
    fig,ax=plt.subplots(figsize=(15,8.5),dpi=150)
    fig.patch.set_facecolor('white'); ax.set_facecolor('white')
    w=.58
    for i,c in enumerate(cs):
        color='#16a34a' if c.close>=c.open else '#dc2626'
        ax.vlines(i,c.low,c.high,color=color,linewidth=1.1,zorder=2)
        y=min(c.open,c.close); h=max(abs(c.close-c.open),c.close*0.00002)
        ax.add_patch(Rectangle((i-w/2,y),w,h,facecolor=color,edgecolor=color,linewidth=.5,zorder=3))
    def hline(price,color,label,lw=1.7,ls='--'):
        ax.axhline(price,color=color,linewidth=lw,linestyle=ls,zorder=4)
        ax.text(len(cs)-1.2,price,f'  {label} {fmt_price(price)}',ha='right',va='bottom',fontsize=9,fontweight='bold',color=color,bbox=dict(facecolor='white',alpha=.78,edgecolor='none',pad=1.5))
    hline(setup.level,'#7c3aed',setup.level_kind,1.8,'-')
    hline((setup.entry_low+setup.entry_high)/2,'#2563eb','ВХОД',2.2,'-')
    hline(setup.sl,'#dc2626','СТОП',2.0,'--')
    hline(setup.tp1,'#16a34a','TP1',1.4,':'); hline(setup.tp2,'#16a34a','TP2',1.6,':'); hline(setup.tp3,'#15803d','TP3',1.9,'--')
    for idx,p,label in setup.points:
        idx=clamp(idx,0,len(cs)-1); ax.scatter([idx],[p],s=45,marker='o',zorder=6,color='#111827')
        ax.annotate(label,(idx,p),xytext=(0,14 if 'LOW' in label else -20),textcoords='offset points',ha='center',fontsize=9,fontweight='bold',color='#111827')
    ax.set_title(f'{setup.coin}USDT · {setup.strategy} · {setup.direction} · 5M',fontsize=18,fontweight='bold',pad=14,color='#111827')
    ax.text(.01,.97,f'15M/1H context · {setup.pattern_name}',transform=ax.transAxes,va='top',fontsize=11,fontweight='bold',color='#111827')
    ax.grid(True,alpha=.14,color='#94a3b8'); ax.tick_params(labelsize=9,colors='#475569')
    ax.set_xlim(-1,len(cs)); plt.tight_layout(); fig.savefig(path,facecolor='white',bbox_inches='tight'); plt.close(fig)
    return path

# ============================================================
# SIGNAL TEXT / SEND
# ============================================================
def build_signal_text(setup):
    entry=(setup.entry_low+setup.entry_high)/2
    risk=abs(entry-setup.sl)/entry*100
    rr2=abs(setup.tp2-entry)/max(abs(entry-setup.sl),1e-12)
    emoji='🟢 LONG' if setup.direction=='LONG' else '🔴 SHORT'
    move=f'{setup.change24:+.1f}%'
    return (f'<b>{emoji}  {setup.coin}USDT</b>\n'
            f'<b>{setup.strategy}</b> · 5M / 15M / 1H\n\n'
            f'📌 <b>Сетап:</b> {setup.pattern_name}\n'
            f'📈 <b>24H:</b> {move} · Range {setup.range24:.1f}%\n'
            f'⚡ <b>ATR:</b> 5M {setup.atr5_pct:.2f}% · 15M {setup.atr15_pct:.2f}%\n'
            f'💧 <b>Volume:</b> x{setup.volume_ratio:.2f} · 24H turnover ${setup.volume_24h/1_000_000:.1f}M\n\n'
            f'🎯 <b>Вход:</b> <code>{fmt_price(setup.entry_low)} – {fmt_price(setup.entry_high)}</code>\n'
            f'🛑 <b>Стоп:</b> <code>{fmt_price(setup.sl)}</code> · риск {risk:.2f}%\n'
            f'🎯 <b>TP1:</b> <code>{fmt_price(setup.tp1)}</code>\n'
            f'🎯 <b>TP2:</b> <code>{fmt_price(setup.tp2)}</code> · RR 1:{rr2:.1f}\n'
            f'🎯 <b>TP3:</b> <code>{fmt_price(setup.tp3)}</code>\n\n'
            f'🧠 <b>Почему:</b> {setup.reason}.\n'
            f'⏱ <b>Ожидаемое удержание:</b> {setup.hold_hours}\n\n'
            f'⚠️ Внимательно проверьте сделку перед входом.\n'
            f'🛡 Риск на одну сделку — <b>не более 1% депозита</b>.\n'
            f'🚫 Не догоняйте цену после ухода от зоны входа.')

def send_signal(setup):
    path=None
    try:
        path=make_chart(setup); signal_text=build_signal_text(setup)
        # Insert first so the callback has a stable DB id.
        created=now_ts(); cur=db.execute('''INSERT INTO signals(inst_id,direction,strategy,level,entry_low,entry_high,sl,tp1,tp2,tp3,score,status,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (setup.inst_id,setup.direction,setup.strategy,setup.level,setup.entry_low,setup.entry_high,setup.sl,setup.tp1,setup.tp2,setup.tp3,setup.score,'READY',created,created+READY_TTL_MINUTES*60))
        sid=cur.lastrowid; db.commit()
        with open(path,'rb') as f:
            msg=bot.send_photo(CHANNEL_ID,f,caption=signal_text,parse_mode='HTML',show_caption_above_media=True,reply_markup=vote_keyboard(sid))
        ready_setups[setup.inst_id]=ActiveReady(setup,created,created+READY_TTL_MINUTES*60,msg.message_id)
        log.info('SIGNAL SENT | %s | %s | %s | score=%s | id=%s',setup.coin,setup.direction,setup.strategy,setup.score,sid)
        return True
    except Exception:
        log.exception('SIGNAL SEND FAILED | %s',setup.inst_id); return False
    finally:
        if path:
            try: os.remove(path)
            except OSError: pass

# ============================================================
# RESULT TRACKING
# ============================================================
def send_update(ar,text):
    # Per-signal Telegram result messages are disabled.
    # Results are collected silently and published once per day at 21:00.
    return

def update_signal_results():
    for iid,ar in list(ready_setups.items()):
        try:
            cs=get_candles(iid,'5m',60)
            if len(cs)<2: continue
            candle=cs[-1]; price=candle.close; s=ar.setup; entry=(s.entry_low+s.entry_high)/2
            long=s.direction=='LONG'
            # Use traded candle extremes, not close-only checks. If one candle
            # touches both stop and a target, resolve conservatively as SL.
            hit1=(candle.high>=s.tp1 if long else candle.low<=s.tp1)
            hit2=(candle.high>=s.tp2 if long else candle.low<=s.tp2)
            hit3=(candle.high>=s.tp3 if long else candle.low<=s.tp3)
            stop=(candle.low<=s.sl if long else candle.high>=s.sl)
            if stop:
                hit1=hit2=hit3=False
            row=db.execute('SELECT id,tp1_hit,tp2_hit,tp3_hit FROM signals WHERE inst_id=? AND status IN (\'READY\',\'ACTIVE\') ORDER BY id DESC LIMIT 1',(iid,)).fetchone()
            if not row: continue
            sid,h1,h2,h3=row
            if hit1 and not h1:
                db.execute('UPDATE signals SET tp1_hit=1,status=\'ACTIVE\' WHERE id=?',(sid,)); ar.tp1_sent=True; send_update(ar,f'🟢 <b>TP1 — {s.coin}USDT</b>\nЦена достигла TP1: <code>{fmt_price(s.tp1)}</code>\nЧасть позиции можно зафиксировать по своему плану; остаток сопровождаем.')
            if hit2 and not h2:
                db.execute('UPDATE signals SET tp2_hit=1,status=\'ACTIVE\' WHERE id=?',(sid,)); ar.tp2_sent=True; send_update(ar,f'🚀 <b>TP2 — {s.coin}USDT</b>\nДвижение продолжилось до <code>{fmt_price(s.tp2)}</code>.\nОстаток позиции сопровождаем по плану.')
            if hit3 and not h3:
                db.execute('UPDATE signals SET tp3_hit=1,status=\'CLOSED_WIN\',result=\'TP3\',closed_at=?,r_multiple=? WHERE id=?',(now_ts(),abs(s.tp3-entry)/max(abs(entry-s.sl),1e-12),sid)); ar.tp3_sent=True; send_update(ar,f'🏁 <b>TP3 — {s.coin}USDT</b>\nЦена достигла <code>{fmt_price(s.tp3)}</code>.\nСделка закрыта по TP3; результат зафиксирован.')
                ready_setups.pop(iid,None)
            elif stop:
                db.execute('UPDATE signals SET status=\'CLOSED_LOSS\',result=\'SL\',closed_at=?,r_multiple=-1 WHERE id=?',(now_ts(),sid)); ar.sl_sent=True; send_update(ar,f'🔴 <b>STOP — {s.coin}USDT</b>\nЦена достигла стопа <code>{fmt_price(s.sl)}</code>.\nСделка закрыта по плановому риску.')
                ready_setups.pop(iid,None)
            db.commit()
        except Exception: log.exception('RESULT CHECK FAILED | %s',iid)

def expire_ready():
    now=now_ts()
    for iid,ar in list(ready_setups.items()):
        if now < ar.expires_at: continue
        db.execute('UPDATE signals SET status=\'EXPIRED\' WHERE inst_id=? AND status IN (\'READY\',\'ACTIVE\')',(iid,)); db.commit(); ready_setups.pop(iid,None)
        send_update(ar,f'⚪ <b>SETUP EXPIRED — {ar.setup.coin}USDT</b>\nВход не активировался в отведённое окно. Рынок не догоняем.')

# ============================================================
# LIMITS / SCAN
# ============================================================
def can_send(iid):
    if iid in ready_setups:return False
    if signals_today>=MAX_SIGNALS_PER_DAY:return False
    cutoff=now_ts()-3600; signals_hour[:]=[x for x in signals_hour if x>=cutoff]
    if len(signals_hour)>=MAX_SIGNALS_PER_HOUR:return False
    row=db.execute('SELECT created_at FROM signals WHERE inst_id=? ORDER BY created_at DESC LIMIT 1',(iid,)).fetchone()
    return not row or now_ts()-float(row[0])>=COOLDOWN_MINUTES*60

def scan_market():
    global signals_today
    instruments=get_instruments(); tickers=get_tickers(); candidates=build_universe(instruments,tickers)
    setups=[]
    for iid,t,_ in candidates:
        if not can_send(iid): continue
        try:
            c=load_symbol(iid)
            # Hard activity gate before expensive strategy scoring.
            if c['5m'] and atr_pct(c['5m']) < MIN_ATR_5M_PCT: continue
            for builder in STRATEGY_BUILDERS:
                try:
                    s=builder(iid,t,c)
                    if s:
                        timing_ok, timing_reason = early_entry_check(s, c['5m'])
                        targets_ok, targets_reason = validate_target_order(s)
                        if not timing_ok:
                            log.info('REJECT TIMING | %s | %s | %s', iid, s.strategy, timing_reason)
                            continue
                        if not targets_ok:
                            log.info('REJECT TARGETS | %s | %s | %s', iid, s.strategy, targets_reason)
                            continue
                        setups.append(s)
                except Exception as exc: log.debug('STRATEGY FAILED | %s | %s',iid,exc)
        except Exception as exc: log.warning('CANDIDATE FAILED | %s | %s',iid,exc)
    # One best setup per coin; prioritize score, not a fixed strategy.
    best={}
    for s in setups:
        if s.inst_id not in best or s.score>best[s.inst_id].score: best[s.inst_id]=s
    final=sorted(best.values(),key=lambda s:s.score,reverse=True)
    log.info('QUANTUM SCAN | market=%d | candidates=%d | raw_setups=%d | final=%d',len(tickers),len(candidates),len(setups),len(final))
    for s in final:
        if signals_today>=MAX_SIGNALS_PER_DAY:break
        if not can_send(s.inst_id):continue
        if send_signal(s): signals_today+=1; signals_hour.append(now_ts())

def daily_report():
    """One professional 24-hour report at 21:00 Europe/Kyiv.

    Reporting-only section: universe selection and all strategy builders are
    intentionally untouched. Unresolved signals are audited against recent
    5M candles so a restart cannot make the report show everything as active.
    """
    n = local_now()
    if n.hour != 21 or n.minute > 1:
        return

    key = n.date().isoformat()
    row = db.execute("SELECT value FROM bot_state WHERE key='daily_report'").fetchone()
    if row and row[0] == key:
        return

    since = n.timestamp() - 24 * 60 * 60
    now = n.timestamp()

    rows = db.execute('''
        SELECT id,inst_id,direction,strategy,entry_low,entry_high,sl,tp1,tp2,tp3,
               score,status,created_at,expires_at,tp1_hit,tp2_hit,tp3_hit,result,closed_at,r_multiple
        FROM signals WHERE created_at >= ? ORDER BY created_at ASC
    ''', (since,)).fetchall()

    # Reconcile unresolved signals from the market. This does not create or
    # reject signals; it only determines their reporting outcome.
    for row in rows:
        (sid,iid,direction,strategy,entry_low,entry_high,sl,tp1,tp2,tp3,
         score,status,created_at,expires_at,tp1_hit,tp2_hit,tp3_hit,result,closed_at,r_multiple) = row
        if status not in ('READY','ACTIVE','EXPIRED'):
            continue
        try:
            cs = get_candles(iid, '5m', 300)
            history = [c for c in cs if c.ts / 1000.0 >= created_at and c.ts / 1000.0 <= now]
            if not history:
                continue

            long = direction == 'LONG'
            entered = False
            reached_tp1 = False
            reached_tp2 = False
            reached_tp3 = False
            stopped = False

            for c in history:
                ctime = c.ts / 1000.0
                # Entry is valid only inside the original READY window.
                if not entered:
                    if ctime <= expires_at and c.high >= entry_low and c.low <= entry_high:
                        entered = True
                    else:
                        continue

                hit1 = c.high >= tp1 if long else c.low <= tp1
                hit2 = c.high >= tp2 if long else c.low <= tp2
                hit3 = c.high >= tp3 if long else c.low <= tp3
                stop = c.low <= sl if long else c.high >= sl

                # Same-candle TP/SL is conservatively treated as stop.
                if stop and (hit1 or hit2 or hit3):
                    stopped = True
                    break
                if hit3:
                    reached_tp1 = reached_tp2 = reached_tp3 = True
                    break
                if hit2:
                    reached_tp1 = reached_tp2 = True
                elif hit1:
                    reached_tp1 = True
                # Once TP1 was reached, the planned management is BE on the
                # remainder; a later stop is therefore reported as BE.
                if stop:
                    stopped = True
                    break

            if reached_tp3:
                entry = (entry_low + entry_high) / 2
                risk = abs(entry - sl)
                db.execute("UPDATE signals SET tp1_hit=1,tp2_hit=1,tp3_hit=1,status='CLOSED_WIN',result='TP3',closed_at=?,r_multiple=? WHERE id=?", (now, abs(tp3-entry)/max(risk,1e-12), sid))
            elif stopped and reached_tp1:
                db.execute("UPDATE signals SET tp1_hit=1,tp2_hit=?,status='CLOSED_BE',result='BE',closed_at=?,r_multiple=0 WHERE id=?", (1 if reached_tp2 else 0, now, sid))
            elif stopped:
                db.execute("UPDATE signals SET status='CLOSED_LOSS',result='SL',closed_at=?,r_multiple=-1 WHERE id=?", (now, sid))
            elif reached_tp2:
                db.execute("UPDATE signals SET tp1_hit=1,tp2_hit=1,status='ACTIVE' WHERE id=?", (sid,))
            elif reached_tp1:
                db.execute("UPDATE signals SET tp1_hit=1,status='ACTIVE' WHERE id=?", (sid,))
            elif entered:
                # The signal entered but has not reached an exit yet.
                db.execute("UPDATE signals SET status='ACTIVE' WHERE id=?", (sid,))
            elif now >= expires_at:
                db.execute("UPDATE signals SET status='EXPIRED',result='NO_ENTRY',closed_at=? WHERE id=?", (now, sid))
        except Exception:
            log.exception('DAILY REPORT MARKET AUDIT FAILED | %s | id=%s', iid, sid)

    db.commit()

    rows = db.execute('''
        SELECT id,inst_id,direction,strategy,score,created_at,status,result,
               tp1_hit,tp2_hit,tp3_hit,closed_at,r_multiple
        FROM signals WHERE created_at >= ? ORDER BY created_at ASC
    ''', (since,)).fetchall()

    total = len(rows)
    tp3 = sum(1 for r in rows if r[7] == 'TP3')
    be = sum(1 for r in rows if r[7] == 'BE' or r[6] == 'CLOSED_BE')
    sl = sum(1 for r in rows if r[7] == 'SL')
    no_entry = sum(1 for r in rows if r[7] == 'NO_ENTRY' or r[6] == 'EXPIRED')
    active = sum(1 for r in rows if r[6] in ('READY','ACTIVE'))
    closed = tp3 + be + sl
    wins = tp3

    # Strategy leaderboard: closed trades only, ranked by total R first.
    stats = {}
    for r in rows:
        strategy = r[3]
        st = stats.setdefault(strategy, {'tp3':0,'be':0,'sl':0,'r':0.0})
        if r[7] == 'TP3': st['tp3'] += 1
        elif r[7] == 'BE': st['be'] += 1
        elif r[7] == 'SL': st['sl'] += 1
        if r[7] in ('TP3','BE','SL'):
            st['r'] += float(r[12] or 0)

    leaderboard=[]
    for strategy,st in stats.items():
        c=st['tp3']+st['be']+st['sl']
        if c:
            leaderboard.append((st['r'], st['tp3']/c*100, strategy, st, c))
    leaderboard.sort(key=lambda x:(x[0],x[1]), reverse=True)

    lines=[
        '📊 <b>QUANTUM — ДНЕВНОЙ ОТЧЁТ</b>',
        f'🕘 <b>Последние 24 часа:</b> {(n-timedelta(days=1)).strftime("%d.%m %H:%M")} — {n.strftime("%d.%m %H:%M")} (Kyiv)',
        '',
        f'📡 <b>Сигналов:</b> {total}',
        f'🟢 <b>Плюс (TP3):</b> {wins}',
        f'🟡 <b>Безубыток:</b> {be}',
        f'🔴 <b>Стоп:</b> {sl}',
        f'⚪ <b>Не вошли / истекли:</b> {no_entry}',
        f'⏳ <b>В работе:</b> {active}',
        f'📈 <b>Win rate:</b> {(wins/closed*100):.1f}%' if closed else '📈 <b>Win rate:</b> —',
        '',
        '🏆 <b>СТРАТЕГИИ — РЕЗУЛЬТАТ</b>'
    ]

    if leaderboard:
        for i,(_,wr,strategy,st,c) in enumerate(leaderboard,1):
            lines.append(f'{i}. <b>{strategy}</b> — {c} закр. | 🟢 {st["tp3"]} | 🟡 {st["be"]} | 🔴 {st["sl"]} | Win {wr:.0f}% | {st["r"]:+.2f}R')
    else:
        lines.append('Нет закрытых сделок для сравнения стратегий.')

    lines += ['', '🧾 <b>ДЕТАЛИ СИГНАЛОВ</b>']
    for r in rows:
        sid,iid,direction,strategy,score,created,status,result,h1,h2,h3,closed_at,rmult = r
        coin=get_coin(iid)
        created_local=datetime.fromtimestamp(created, ZoneInfo(TIMEZONE)).strftime('%H:%M')
        if result == 'TP3': outcome='🟢 TP3'
        elif result == 'BE': outcome='🟡 BE'
        elif result == 'SL': outcome='🔴 STOP'
        elif result == 'NO_ENTRY' or status == 'EXPIRED': outcome='⚪ NO ENTRY'
        else: outcome='⏳ В РАБОТЕ'
        lines.append(f'#{sid} {created_local} <b>{coin}</b> {direction} · {strategy} · {outcome}')

    try:
        bot.send_message(CHANNEL_ID, '\n'.join(lines), parse_mode='HTML')
        log.info('DAILY REPORT SENT | total=%d closed=%d wins=%d be=%d sl=%d active=%d', total, closed, wins, be, sl, active)
        db.execute("INSERT OR REPLACE INTO bot_state(key,value) VALUES('daily_report',?)", (key,))
        db.commit()
    except Exception:
        log.exception('DAILY REPORT FAILED')

# ============================================================
# MORNING GREETING
# ============================================================
def morning_greeting():
    n = local_now()
    if n.hour != MORNING_GREETING_HOUR or n.minute != MORNING_GREETING_MINUTE:
        return

    key = n.date().isoformat()
    row = db.execute("SELECT value FROM bot_state WHERE key='morning_greeting'").fetchone()
    if row and row[0] == key:
        return

    text = (
        '☀️ <b>Доброе утро!</b>\n\n'
        '💎 <b>QUANTUM | VIP Scanner</b> снова в работе.\n'
        '🔎 Сегодня ищем только активные монеты с сильным движением, объёмом и хорошим текущим импульсом.\n\n'
        '⚠️ Внимательно проверьте сделку перед входом.\n'
        '🛡 Риск на одну сделку — <b>не более 1% депозита</b>.\n'
        '🚫 Не догоняйте цену после ухода от зоны входа.'
    )

    try:
        bot.send_message(CHANNEL_ID, text, parse_mode='HTML')
        db.execute("INSERT OR REPLACE INTO bot_state(key,value) VALUES('morning_greeting',?)", (key,))
        db.commit()
        log.info('MORNING GREETING SENT | %s', key)
    except Exception:
        log.exception('MORNING GREETING FAILED')

# ============================================================
# MAIN
# ============================================================
def main():
    global signals_today

    # Telegram polling is isolated from the market scanner.
    # This prevents a polling exception from killing the trading engine.
    import threading

    polling_thread = threading.Thread(
        target=start_telegram_polling,
        name='telegram-polling',
        daemon=True,
    )
    polling_thread.start()

    log.info('QUANTUM INTRADAY SWING ENGINE V2 — AUDIT FIXES STARTED')

    while True:
        try:
            day=local_now().date().isoformat()
            row=db.execute(
                'SELECT value FROM bot_state WHERE key=\'signals_day\''
            ).fetchone()

            if not row or row[0] != day:
                signals_today=0
                db.execute(
                    'INSERT OR REPLACE INTO bot_state(key,value) VALUES(\'signals_day\',?)',
                    (day,)
                )
                db.commit()

            expire_ready()
            update_signal_results()
            morning_greeting()
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
