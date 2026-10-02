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
# QUANTUM INTRADAY SWING ENGINE V1
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
TOP_GAINERS = int(os.getenv('TOP_GAINERS', '20'))
TOP_LOSERS = int(os.getenv('TOP_LOSERS', '20'))
TOP_VOLATILE = int(os.getenv('TOP_VOLATILE', '25'))
NEW_ACTIVE_DAYS = int(os.getenv('NEW_ACTIVE_DAYS', '30'))
NEW_ACTIVE_COUNT = int(os.getenv('NEW_ACTIVE_COUNT', '15'))
DEEP_SCAN_LIMIT = int(os.getenv('DEEP_SCAN_LIMIT', '70'))
MIN_24H_MOVE_FOR_REVERSAL = float(os.getenv('MIN_24H_MOVE_FOR_REVERSAL', '15'))
MIN_ATR_5M_PCT = float(os.getenv('MIN_ATR_5M_PCT', '0.18'))
MIN_ATR_15M_PCT = float(os.getenv('MIN_ATR_15M_PCT', '0.45'))
MIN_ACTIVE_24H_RANGE_PCT = float(os.getenv('MIN_ACTIVE_24H_RANGE_PCT', '5.0'))

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
    if len(cs) < lookback + 1: return 0
    cur = cs[-1].quote_volume
    hist = [c.quote_volume for c in cs[-lookback-1:-1] if c.quote_volume > 0]
    return cur / (sum(hist)/len(hist)) if hist else 0

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
def build_universe(instruments, tickers):
    base=[]
    for iid,t in tickers.items():
        if iid not in instruments: continue
        vol=t['vol24h_usd']; rng=t['range24h_pct']
        if vol < MIN_24H_TURNOVER_USD or rng < MIN_ACTIVE_24H_RANGE_PCT: continue
        activity = clamp((vol/PREFERRED_TURNOVER_USD)*20,0,20)
        vol_score = clamp(rng*2.2,0,35)
        move_score = clamp(abs(t['change24h_pct'])*1.2,0,35)
        base.append((iid,t,activity+vol_score+move_score))
    gainers=sorted(base,key=lambda z:z[1]['change24h_pct'],reverse=True)[:TOP_GAINERS]
    losers=sorted(base,key=lambda z:z[1]['change24h_pct'])[:TOP_LOSERS]
    volatile=sorted(base,key=lambda z:z[1]['range24h_pct'],reverse=True)[:TOP_VOLATILE]
    cutoff=int((local_now()-timedelta(days=NEW_ACTIVE_DAYS)).timestamp()*1000)
    new_active=[z for z in base if instruments[z[0]].get('listTime',0) >= cutoff]
    new_active=sorted(new_active,key=lambda z:z[2],reverse=True)[:NEW_ACTIVE_COUNT]
    pool={}
    for group in (gainers,losers,volatile,new_active):
        for iid,t,s in group: pool[iid]=(iid,t,s)
    ranked=sorted(pool.values(),key=lambda z:z[2],reverse=True)[:DEEP_SCAN_LIMIT]
    log.info('UNIVERSE | gainers=%d losers=%d volatile=%d new_active=%d deep=%d',len(gainers),len(losers),len(volatile),len(new_active),len(ranked))
    return ranked

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
    try: bot.send_message(CHANNEL_ID,text,parse_mode='HTML')
    except Exception: log.exception('UPDATE SEND FAILED | %s',ar.setup.inst_id)

def update_signal_results():
    for iid,ar in list(ready_setups.items()):
        try:
            cs=get_candles(iid,'5m',60)
            if len(cs)<2: continue
            price=cs[-1].close; s=ar.setup; entry=(s.entry_low+s.entry_high)/2
            long=s.direction=='LONG'; hit1=(price>=s.tp1 if long else price<=s.tp1); hit2=(price>=s.tp2 if long else price<=s.tp2); hit3=(price>=s.tp3 if long else price<=s.tp3); stop=(price<=s.sl if long else price>=s.sl)
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
                    if s: setups.append(s)
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

def weekly_report():
    # Monday report only; totals for the previous 7 days.
    n=local_now()
    if n.weekday()!=0 or n.hour!=9 or n.minute>1:return
    key=n.date().isoformat(); row=db.execute('SELECT value FROM bot_state WHERE key=\'weekly_report\'').fetchone()
    if row and row[0]==key:return
    since=(n-timedelta(days=7)).timestamp(); total=db.execute('SELECT COUNT(*) FROM signals WHERE created_at>=?',(since,)).fetchone()[0]
    wins=db.execute("SELECT COUNT(*) FROM signals WHERE created_at>=? AND result='TP3'",(since,)).fetchone()[0]
    losses=db.execute("SELECT COUNT(*) FROM signals WHERE created_at>=? AND result='SL'",(since,)).fetchone()[0]
    try: bot.send_message(CHANNEL_ID,f'📊 <b>QUANTUM — неделя</b>\n\nСигналов: <b>{total}</b>\nПрибыльных закрытых: <b>{wins}</b>\nУбыточных закрытых: <b>{losses}</b>',parse_mode='HTML')
    except Exception: log.exception('WEEKLY REPORT FAILED')
    db.execute('INSERT OR REPLACE INTO bot_state(key,value) VALUES(\'weekly_report\',?)',(key,)); db.commit()

# ============================================================
# MAIN
# ============================================================
def main():
    global signals_today
    log.info('QUANTUM INTRADAY SWING ENGINE V1 STARTED')
    while True:
        try:
            day=local_now().date().isoformat(); row=db.execute('SELECT value FROM bot_state WHERE key=\'signals_day\'').fetchone()
            if not row or row[0]!=day:
                signals_today=0; db.execute('INSERT OR REPLACE INTO bot_state(key,value) VALUES(\'signals_day\',?)',(day,)); db.commit()
            expire_ready(); update_signal_results(); weekly_report(); scan_market()
        except KeyboardInterrupt:
            log.info('STOPPED'); break
        except Exception: log.exception('MAIN LOOP ERROR')
        time.sleep(SCAN_INTERVAL_SECONDS)

if __name__=='__main__': main()
