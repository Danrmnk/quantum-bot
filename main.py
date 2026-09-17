import os
import time
import sqlite3
import logging
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import telebot

# Quantum Scalper — compact structure/pattern scanner for OKX USDT perpetuals.
# Public market data only. The bot DOES NOT place orders.
#
# 4 strategies:
# 1) Breakout + Retest
# 2) Triangle / Compression Breakout
# 3) Liquidity Sweep + Reversal
# 4) Trend Momentum Pullback
#
# Signals are sent only after confirmation. Results are tracked from
# subsequent 5m candles. Daily/weekly/monthly reports use real DB results.

TOKEN = os.getenv("TELEGRAM_TOKEN", os.getenv("BOT_TOKEN", "")).strip()
CHANNEL = os.getenv("CHANNEL_ID", "").strip()
OKX = os.getenv("OKX_BASE_URL", "https://www.okx.com").rstrip("/")
TZ = ZoneInfo(os.getenv("BOT_TIMEZONE", "Europe/Kyiv"))
DB_PATH = os.getenv("DB_PATH", "quantum_state.db")

MIN_VOL = float(os.getenv("MIN_24H_VOLUME_USD", "30000000"))
MAX_COINS = int(os.getenv("MAX_SYMBOLS", "50"))
SCAN_SEC = int(os.getenv("SCAN_INTERVAL_SECONDS", "30"))
MIN_SCORE = int(os.getenv("MIN_SCORE", "78"))
MAX_DAY = int(os.getenv("MAX_SIGNALS_PER_DAY", "20"))
MAX_HOUR = int(os.getenv("MAX_SIGNALS_PER_HOUR", "5"))
COOLDOWN_MIN = int(os.getenv("COOLDOWN_MINUTES", "60"))
MAX_RISK_PCT = float(os.getenv("MAX_RISK_PCT", "2.2"))
REPORT_HOUR = int(os.getenv("REPORT_HOUR", "23"))
REPORT_MINUTE = int(os.getenv("REPORT_MINUTE", "55"))
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "10"))

if not TOKEN or not CHANNEL:
    raise RuntimeError("Set TELEGRAM_TOKEN and CHANNEL_ID")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("quantum")
bot = telebot.TeleBot(TOKEN, parse_mode="HTML")

http = requests.Session()
http.headers.update({"User-Agent": "QuantumScalper/5.0", "Accept": "application/json"})

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("""
CREATE TABLE IF NOT EXISTS signals(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    strategy TEXT NOT NULL,
    entry REAL NOT NULL,
    sl REAL NOT NULL,
    tp1 REAL NOT NULL,
    tp2 REAL NOT NULL,
    tp3 REAL NOT NULL,
    score INTEGER NOT NULL,
    created REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'OPEN',
    tp1_hit INTEGER NOT NULL DEFAULT 0,
    tp2_hit INTEGER NOT NULL DEFAULT 0,
    tp3_hit INTEGER NOT NULL DEFAULT 0,
    result TEXT,
    closed REAL,
    r REAL
)
""")
db.execute("CREATE TABLE IF NOT EXISTS bot_state(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
db.commit()


@dataclass
class Candle:
    ts: int
    o: float
    h: float
    l: float
    c: float
    v: float
    qv: float
    confirmed: bool


@dataclass
class Setup:
    symbol: str
    direction: str
    strategy: str
    entry: float
    sl: float
    tp1: float
    tp2: float
    tp3: float
    score: int
    reason: str
    volume: float
    vol_ratio: float
    atr_pct: float


cache = {}


def now():
    return time.time()


def local_now():
    return datetime.now(TZ)


def price(x):
    if x >= 1000:
        return f"{x:,.2f}".replace(",", " ")
    if x >= 1:
        return f"{x:,.4f}"
    if x >= 0.01:
        return f"{x:.6f}".rstrip("0").rstrip(".")
    return f"{x:.10f}".rstrip("0").rstrip(".")


def okx(path, params):
    last = None
    for n in range(3):
        try:
            r = http.get(OKX + path, params=params, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            data = r.json()
            if str(data.get("code")) != "0":
                raise RuntimeError(data.get("msg", "OKX error"))
            return data.get("data", [])
        except Exception as exc:
            last = exc
            time.sleep(1 + n)
    raise RuntimeError(last)


def candles(symbol, bar="5m", limit=180):
    key = (symbol, bar)
    hit = cache.get(key)
    if hit and now() - hit[0] < 15:
        return hit[1]

    rows = okx("/api/v5/market/candles", {
        "instId": symbol,
        "bar": bar,
        "limit": min(limit, 300),
    })

    out = []
    for x in reversed(rows):
        try:
            out.append(Candle(
                int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]),
                float(x[5] or 0), float(x[7] or 0), str(x[8]) == "1"
            ))
        except (ValueError, TypeError, IndexError):
            continue

    cache[key] = (now(), out)
    return out


def tickers():
    rows = okx("/api/v5/market/tickers", {"instType": "SWAP"})
    out = []

    for x in rows:
        symbol = x.get("instId", "")
        if not symbol.endswith("-USDT-SWAP"):
            continue

        try:
            last = float(x["last"])
            volume = float(x.get("volCcy24h") or 0)
            if volume <= 0:
                volume = float(x.get("vol24h") or 0) * last

            if last > 0 and volume >= MIN_VOL:
                out.append((symbol, last, volume))
        except (ValueError, TypeError):
            continue

    out.sort(key=lambda item: item[2], reverse=True)
    return out[:MAX_COINS]


def atr(cs, n=14):
    if len(cs) < n + 1:
        return 0.0

    trs = []
    for i in range(-n, 0):
        previous = cs[i - 1].c
        trs.append(max(
            cs[i].h - cs[i].l,
            abs(cs[i].h - previous),
            abs(cs[i].l - previous),
        ))
    return sum(trs) / len(trs)


def ema(values, n):
    if len(values) < n:
        return None

    k = 2 / (n + 1)
    result = sum(values[:n]) / n
    for value in values[n:]:
        result = value * k + result * (1 - k)
    return result


def volume_ratio(cs, n=20):
    if len(cs) < n + 1:
        return 1.0

    average = sum(x.v for x in cs[-n - 1:-1]) / n
    return cs[-1].v / average if average else 1.0


def trend(cs):
    closes = [x.c for x in cs]
    e20, e50 = ema(closes, 20), ema(closes, 50)

    if not e20 or not e50:
        return 0

    if e20 > e50 and closes[-1] > e20:
        return 1
    if e20 < e50 and closes[-1] < e20:
        return -1
    return 0


def make_trade(symbol, direction, strategy, entry, sl, score, reason,
               volume, vr, atr_value):
    risk = abs(entry - sl)
    if entry <= 0 or risk <= 0:
        return None

    risk_pct = risk / entry * 100
    if risk_pct > MAX_RISK_PCT or risk_pct < 0.05:
        return None

    if direction == "LONG":
        tp1, tp2, tp3 = entry + risk, entry + 2 * risk, entry + 3 * risk
    else:
        tp1, tp2, tp3 = entry - risk, entry - 2 * risk, entry - 3 * risk

    return Setup(
        symbol, direction, strategy, entry, sl, tp1, tp2, tp3,
        max(0, min(100, int(score))), reason, volume, vr,
        atr_value / entry * 100 if entry else 0,
    )


def breakout_retest(cs, symbol, volume):
    if len(cs) < 45:
        return None

    a = atr(cs)
    last, previous = cs[-1], cs[-2]
    high = max(x.h for x in cs[-31:-5])
    low = min(x.l for x in cs[-31:-5])
    vr = volume_ratio(cs)
    t = trend(cs)

    if last.c > high and previous.c > high and last.l <= high * 1.003 and t >= 0:
        score = 70 + min(12, int(vr * 4)) + (8 if t == 1 else 3)
        sl = min(high, last.l) - a * 0.35
        return make_trade(
            symbol, "LONG", "Breakout + Retest", last.c, sl, score,
            f"Пробой сопротивления {price(high)} + удержание уровня; объём x{vr:.1f}",
            volume, vr, a
        )

    if last.c < low and previous.c < low and last.h >= low * 0.997 and t <= 0:
        score = 70 + min(12, int(vr * 4)) + (8 if t == -1 else 3)
        sl = max(low, last.h) + a * 0.35
        return make_trade(
            symbol, "SHORT", "Breakout + Retest", last.c, sl, score,
            f"Пробой поддержки {price(low)} + удержание уровня; объём x{vr:.1f}",
            volume, vr, a
        )

    return None


def compression_breakout(cs, symbol, volume):
    if len(cs) < 40:
        return None

    a = atr(cs)
    recent = cs[-12:]
    older = cs[-32:-12]

    recent_range = (max(x.h for x in recent) - min(x.l for x in recent)) / max(x.c for x in recent)
    old_range = (max(x.h for x in older) - min(x.l for x in older)) / max(x.c for x in older)

    if old_range <= 0 or recent_range > old_range * 0.72:
        return None

    last = cs[-1]
    vr = volume_ratio(cs)
    high = max(x.h for x in recent[:-1])
    low = min(x.l for x in recent[:-1])

    if last.c > high and vr >= 1.15:
        return make_trade(
            symbol, "LONG", "Triangle / Compression Breakout", last.c,
            last.l - a * 0.25, 73 + min(10, int(vr * 4)) + 7,
            f"Сжатие диапазона {recent_range * 100:.2f}% → пробой вверх; объём x{vr:.1f}",
            volume, vr, a
        )

    if last.c < low and vr >= 1.15:
        return make_trade(
            symbol, "SHORT", "Triangle / Compression Breakout", last.c,
            last.h + a * 0.25, 73 + min(10, int(vr * 4)) + 7,
            f"Сжатие диапазона {recent_range * 100:.2f}% → пробой вниз; объём x{vr:.1f}",
            volume, vr, a
        )

    return None


def liquidity_sweep(cs, symbol, volume):
    if len(cs) < 45:
        return None

    a = atr(cs)
    last = cs[-1]
    high = max(x.h for x in cs[-31:-2])
    low = min(x.l for x in cs[-31:-2])
    vr = volume_ratio(cs)

    if last.l < low and last.c > low and last.c > last.o:
        return make_trade(
            symbol, "LONG", "Liquidity Sweep + Reversal", last.c,
            last.l - a * 0.20, 76 + min(12, int(vr * 4)),
            f"Снятие ликвидности ниже {price(low)} + возврат выше уровня",
            volume, vr, a
        )

    if last.h > high and last.c < high and last.c < last.o:
        return make_trade(
            symbol, "SHORT", "Liquidity Sweep + Reversal", last.c,
            last.h + a * 0.20, 76 + min(12, int(vr * 4)),
            f"Снятие ликвидности выше {price(high)} + возврат ниже уровня",
            volume, vr, a
        )

    return None


def trend_pullback(cs, symbol, volume):
    if len(cs) < 60:
        return None

    a = atr(cs)
    closes = [x.c for x in cs]
    e20, e50 = ema(closes, 20), ema(closes, 50)
    last = cs[-1]
    vr = volume_ratio(cs)

    if not e20 or not e50:
        return None

    if e20 > e50 and last.l <= e20 * 1.002 and last.c > e20 and last.c > last.o:
        return make_trade(
            symbol, "LONG", "Trend Momentum Pullback", last.c,
            min(last.l, e20) - a * 0.30, 75 + min(10, int(vr * 3)) + 8,
            "EMA20 выше EMA50; откат к EMA20 и бычье подтверждение",
            volume, vr, a
        )

    if e20 < e50 and last.h >= e20 * 0.998 and last.c < e20 and last.c < last.o:
        return make_trade(
            symbol, "SHORT", "Trend Momentum Pullback", last.c,
            max(last.h, e20) + a * 0.30, 75 + min(10, int(vr * 3)) + 8,
            "EMA20 ниже EMA50; откат к EMA20 и медвежье подтверждение",
            volume, vr, a
        )

    return None


STRATEGIES = (
    breakout_retest,
    compression_breakout,
    liquidity_sweep,
    trend_pullback,
)


def best_setup(symbol, volume):
    try:
        c5 = [x for x in candles(symbol, "5m", 180) if x.confirmed]
        c1 = [x for x in candles(symbol, "1H", 120) if x.confirmed]

        if len(c5) < 60 or len(c1) < 55:
            return None

        higher_trend = trend(c1)
        candidates = [f(c5, symbol, volume) for f in STRATEGIES]
        candidates = [x for x in candidates if x and x.score >= MIN_SCORE]

        if not candidates:
            return None

        candidates.sort(key=lambda x: x.score, reverse=True)
        setup = candidates[0]

        # HTF conflict is a quality penalty. Reversal strategy can ignore it.
        if higher_trend and (
            (setup.direction == "LONG" and higher_trend < 0)
            or (setup.direction == "SHORT" and higher_trend > 0)
        ):
            if "Liquidity Sweep" not in setup.strategy:
                setup.score -= 8

        return setup if setup.score >= MIN_SCORE else None

    except Exception as exc:
        log.warning("%s analysis: %s", symbol, exc)
        return None


def recent_signal(symbol):
    row = db.execute(
        "SELECT created FROM signals WHERE symbol=? ORDER BY id DESC LIMIT 1",
        (symbol,),
    ).fetchone()
    return bool(row and now() - row[0] < COOLDOWN_MIN * 60)


def limits_ok():
    t = now()
    hour = db.execute("SELECT COUNT(*) FROM signals WHERE created>=?", (t - 3600,)).fetchone()[0]
    day = db.execute("SELECT COUNT(*) FROM signals WHERE created>=?", (t - 86400,)).fetchone()[0]
    return hour < MAX_HOUR and day < MAX_DAY


def send_signal(setup):
    risk = abs(setup.entry - setup.sl)
    text = (
        f"🔥 <b>{setup.symbol.replace('-USDT-SWAP', '')} {setup.direction}</b>\n"
        f"Стратегия: <b>{setup.strategy}</b>\n"
        f"Качество: <b>{setup.score}/100</b>\n\n"
        f"Вход: <b>{price(setup.entry)}</b>\n"
        f"SL: <b>{price(setup.sl)}</b>\n"
        f"TP1: <b>{price(setup.tp1)}</b>\n"
        f"TP2: <b>{price(setup.tp2)}</b>\n"
        f"TP3: <b>{price(setup.tp3)}</b>\n"
        f"R:R до TP1: <b>1:1</b>\n\n"
        f"Объём 24h: <b>${setup.volume:,.0f}</b>\n"
        f"Объём свечи: <b>x{setup.vol_ratio:.1f}</b>\n"
        f"ATR: <b>{setup.atr_pct:.2f}%</b>\n"
        f"Риск до SL: <b>{risk / setup.entry * 100:.2f}%</b>\n"
        f"Причина: {setup.reason}\n\n"
        f"⚠️ Аналитический сигнал, не гарантия результата."
    )
    bot.send_message(CHANNEL, text)


def save_signal(setup):
    db.execute("""
        INSERT INTO signals(
            symbol,direction,strategy,entry,sl,tp1,tp2,tp3,score,created,status
        ) VALUES(?,?,?,?,?,?,?,?,?,?, 'OPEN')
    """, (
        setup.symbol, setup.direction, setup.strategy,
        setup.entry, setup.sl, setup.tp1, setup.tp2, setup.tp3,
        setup.score, now(),
    ))
    db.commit()


def update_results():
    rows = db.execute("""
        SELECT id,symbol,direction,entry,sl,tp1,tp2,tp3,tp1_hit,tp2_hit
        FROM signals
        WHERE status='OPEN'
        ORDER BY id DESC
        LIMIT 100
    """).fetchall()

    for row in rows:
        sid, symbol, direction, entry, sl, tp1, tp2, tp3, h1, h2 = row

        try:
            cs = [x for x in candles(symbol, "5m", 8) if x.confirmed]
            if not cs:
                continue

            new_h1, new_h2, h3 = bool(h1), bool(h2), False
            close_result = None
            close_price = None

            for c in cs[-4:]:
                if direction == "LONG":
                    # Conservative rule: if one candle touches both SL and TP,
                    # count SL first because intrabar order is unknown.
                    if c.l <= sl and not (h1 or h2):
                        close_result, close_price = "SL", sl
                        break
                    if c.h >= tp1:
                        new_h1 = True
                    if c.h >= tp2:
                        new_h2 = True
                    if c.h >= tp3:
                        h3 = True
                        close_result, close_price = "TP3", tp3
                        break
                    if new_h1 and c.l <= entry:
                        close_result, close_price = "BE", entry
                        break
                else:
                    if c.h >= sl and not (h1 or h2):
                        close_result, close_price = "SL", sl
                        break
                    if c.l <= tp1:
                        new_h1 = True
                    if c.l <= tp2:
                        new_h2 = True
                    if c.l <= tp3:
                        h3 = True
                        close_result, close_price = "TP3", tp3
                        break
                    if new_h1 and c.h >= entry:
                        close_result, close_price = "BE", entry
                        break

            if close_result is None:
                # Keep monitoring after TP1/TP2 instead of declaring them final.
                if new_h2 and not h3:
                    db.execute(
                        "UPDATE signals SET tp1_hit=1,tp2_hit=1 WHERE id=?",
                        (sid,),
                    )
                elif new_h1:
                    db.execute(
                        "UPDATE signals SET tp1_hit=1 WHERE id=?",
                        (sid,),
                    )
                db.commit()
                continue

            risk = abs(entry - sl)
            r_value = (
                (close_price - entry) / risk
                if direction == "LONG"
                else (entry - close_price) / risk
            )

            db.execute("""
                UPDATE signals
                SET status='CLOSED', result=?, closed=?, r=?,
                    tp1_hit=?, tp2_hit=?, tp3_hit=?
                WHERE id=?
            """, (
                close_result, now(), r_value,
                int(new_h1), int(new_h2), int(h3), sid,
            ))
            db.commit()
            log.info("RESULT %s %s id=%s R=%.2f", symbol, close_result, sid, r_value)

        except Exception as exc:
            log.warning("result update %s: %s", symbol, exc)


def report(period_name, start_ts):
    end_ts = now()

    q = db.execute("""
        SELECT COUNT(*),
               SUM(result='TP3'),
               SUM(result='SL'),
               SUM(result='BE'),
               AVG(r),
               SUM(r)
        FROM signals
        WHERE status='CLOSED' AND closed>=? AND closed<?
    """, (start_ts, end_ts)).fetchone()

    total = q[0] or 0
    tp3 = q[1] or 0
    sl = q[2] or 0
    be = q[3] or 0
    average_r = q[4] or 0
    total_r = q[5] or 0

    # "TP1/TP2 reached" is separate from final outcome.
    reached = db.execute("""
        SELECT SUM(tp1_hit),SUM(tp2_hit),SUM(tp3_hit)
        FROM signals
        WHERE status='CLOSED' AND closed>=? AND closed<?
    """, (start_ts, end_ts)).fetchone()

    tp1_hit, tp2_hit, tp3_hit = (reached[0] or 0, reached[1] or 0, reached[2] or 0)

    text = (
        f"📊 <b>ОТЧЁТ — {period_name}</b>\n\n"
        f"Закрыто сигналов: <b>{total}</b>\n"
        f"Дошли до TP1: <b>{tp1_hit}</b>\n"
        f"Дошли до TP2: <b>{tp2_hit}</b>\n"
        f"Дошли до TP3: <b>{tp3_hit}</b>\n"
        f"SL: <b>{sl}</b> | BE: <b>{be}</b>\n"
        f"Средний R: <b>{average_r:.2f}</b>\n"
        f"Суммарный R: <b>{total_r:.2f}</b>\n\n"
        f"ℹ️ Статистика строится только по фактически закрытым сигналам."
    )

    by_strategy = db.execute("""
        SELECT strategy,COUNT(*),SUM(result='TP3'),SUM(result='SL'),AVG(r),SUM(r)
        FROM signals
        WHERE status='CLOSED' AND closed>=? AND closed<?
        GROUP BY strategy
    """, (start_ts, end_ts)).fetchall()

    if by_strategy:
        text += "\n\n<b>Стратегии:</b>"
        for strategy, n, wins, losses, avg_r, sum_r in by_strategy:
            text += (
                f"\n• {strategy}: {n} сигналов | "
                f"TP3 {wins or 0} | SL {losses or 0} | R {sum_r or 0:.2f}"
            )

    bot.send_message(CHANNEL, text)


def morning_message():
    key = local_now().strftime("%Y-%m-%d")
    old = db.execute("SELECT value FROM bot_state WHERE key='morning'").fetchone()

    if old and old[0] == key:
        return

    n = local_now()
    if n.hour >= 9:
        bot.send_message(
            CHANNEL,
            "🌅 <b>Доброе утро!</b>\n"
            "Quantum Scalper начинает новый торговый день. "
            "Ищем подтверждённые формации, пробои, ретесты и качественные входы."
        )
        db.execute(
            "INSERT OR REPLACE INTO bot_state(key,value) VALUES('morning',?)",
            (key,),
        )
        db.commit()


def daily_reports():
    n = local_now()
    key = n.strftime("%Y-%m-%d")
    old = db.execute("SELECT value FROM bot_state WHERE key='reports'").fetchone()

    if old and old[0] == key:
        return

    if n.hour < REPORT_HOUR:
        return

    report("ДЕНЬ", now() - 86400)
    report("НЕДЕЛЯ", now() - 7 * 86400)
    report("МЕСЯЦ", now() - 30 * 86400)

    db.execute(
        "INSERT OR REPLACE INTO bot_state(key,value) VALUES('reports',?)",
        (key,),
    )
    db.commit()


def main():
    log.info("Quantum Scalper started")

    while True:
        try:
            morning_message()
            update_results()
            daily_reports()

            if limits_ok():
                for symbol, _, volume in tickers():
                    if recent_signal(symbol):
                        continue

                    setup = best_setup(symbol, volume)
                    if not setup:
                        continue

                    send_signal(setup)
                    save_signal(setup)
                    log.info(
                        "SIGNAL %s %s %s score=%s",
                        symbol, setup.direction, setup.strategy, setup.score
                    )

            time.sleep(SCAN_SEC)

        except KeyboardInterrupt:
            break
        except Exception as exc:
            log.exception("MAIN LOOP: %s", exc)
            time.sleep(10)


if __name__ == "__main__":
    main()
