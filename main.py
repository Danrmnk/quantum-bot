import os
import time
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, List, Dict, Tuple

import requests
import telebot

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

# ============================================================
# REVERSAL SCALPER V1
#
# Только одна стратегия:
#
# TOP GAINERS (15) -> первый подтверждённый разворот -> SHORT
# TOP LOSERS  (10) -> первый подтверждённый отскок   -> LONG
#
# 24H % используется ТОЛЬКО для отбора лидеров/аутсайдеров.
# Сам вход определяется текущим 5M движением.
#
# Бот НЕ торгует. Он публикует сигнал + график в Telegram.
# ============================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", os.getenv("BOT_TOKEN", "")).strip()
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()
OKX_BASE_URL = os.getenv("OKX_BASE_URL", "https://www.okx.com").rstrip("/")
TIMEZONE = os.getenv("BOT_TIMEZONE", "Europe/Kyiv")
DB_PATH = os.getenv("DB_PATH", "reversal_scalper.db")

# Только TOP movers.
TOP_GAINERS = int(os.getenv("TOP_GAINERS", "15"))
TOP_LOSERS = int(os.getenv("TOP_LOSERS", "10"))
MIN_24H_VOLUME_USD = float(os.getenv("MIN_24H_VOLUME_USD", "60000000"))

# Первый разворот должен быть ранним, а не после большого отката.
MIN_GAINER_24H_PCT = float(os.getenv("MIN_GAINER_24H_PCT", "30"))
MIN_LOSER_24H_PCT = float(os.getenv("MIN_LOSER_24H_PCT", "-30"))
MIN_INITIAL_REVERSAL_PCT = float(os.getenv("MIN_INITIAL_REVERSAL_PCT", "0.45"))
MAX_INITIAL_REVERSAL_PCT = float(os.getenv("MAX_INITIAL_REVERSAL_PCT", "4.5"))
MIN_5M_RANGE_PCT = float(os.getenv("MIN_5M_RANGE_PCT", "0.65"))
MIN_BODY_ATR = float(os.getenv("MIN_BODY_ATR", "1.15"))
MIN_VOLUME_RATIO = float(os.getenv("MIN_VOLUME_RATIO", "1.30"))
MIN_ROOM_PCT = float(os.getenv("MIN_ROOM_PCT", "3.0"))
MAX_RISK_PCT = float(os.getenv("MAX_RISK_PCT", "1.0"))
MIN_RISK_PCT = float(os.getenv("MIN_RISK_PCT", "0.15"))
LOOKBACK_5M = int(os.getenv("LOOKBACK_5M", "36"))

SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "20"))
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "10"))
REQUEST_RETRIES = int(os.getenv("REQUEST_RETRIES", "3"))
CANDLE_CACHE_SECONDS = int(os.getenv("CANDLE_CACHE_SECONDS", "10"))
READY_TTL_MINUTES = int(os.getenv("READY_TTL_MINUTES", "15"))
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "40"))
MAX_SIGNALS_PER_DAY = int(os.getenv("MAX_SIGNALS_PER_DAY", "18"))
REPORT_HOUR = int(os.getenv("REPORT_HOUR", "23"))
REPORT_MINUTE = int(os.getenv("REPORT_MINUTE", "55"))

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is missing.")
if not CHANNEL_ID:
    raise RuntimeError("CHANNEL_ID is missing.")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("REVERSAL_SCALPER")

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML")
session = requests.Session()
session.headers.update({"User-Agent": "ReversalScalper/1.0", "Accept": "application/json"})

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
    current_price: float
    extreme_price: float
    level: float
    entry_low: float
    entry_high: float
    sl: float
    tp1: float
    tp2: float
    tp3: float
    score: int
    change24: float
    volume_ratio: float
    risk_pct: float
    room_pct: float
    volume_24h: float
    candles_5m: List[Candle]
    extreme_index: int
    trigger_index: int
    pattern_lines: List[Tuple[int, float, int, float, str]]
    pattern_points: List[Tuple[int, float, str]]

@dataclass
class ActiveReady:
    setup: Setup
    created_at: float
    expires_at: float

# ============================================================
# DB
# ============================================================

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("""
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    inst_id TEXT NOT NULL,
    direction TEXT NOT NULL,
    strategy TEXT NOT NULL,
    entry_low REAL NOT NULL,
    entry_high REAL NOT NULL,
    sl REAL NOT NULL,
    tp1 REAL NOT NULL,
    tp2 REAL NOT NULL,
    tp3 REAL NOT NULL,
    score INTEGER NOT NULL,
    status TEXT NOT NULL,
    result TEXT DEFAULT '',
    created_at REAL NOT NULL,
    activated_at REAL,
    expires_at REAL NOT NULL,
    closed_at REAL
)
""")
db.execute("CREATE TABLE IF NOT EXISTS bot_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
db.commit()

ready_setups: Dict[str, ActiveReady] = {}
candle_cache: Dict[Tuple[str, str], Tuple[float, List[Candle]]] = {}
signals_today = 0

# ============================================================
# HELPERS
# ============================================================

def now_ts() -> float:
    return time.time()


def local_now() -> datetime:
    return datetime.now(ZoneInfo(TIMEZONE))


def get_coin(inst_id: str) -> str:
    return inst_id.replace("-USDT-SWAP", "")


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


def grade_volume(ratio: float) -> str:
    if ratio >= 2.5:
        return "EXTREME"
    if ratio >= 2.0:
        return "VERY HIGH"
    if ratio >= 1.5:
        return "HIGH"
    if ratio >= 1.3:
        return "GOOD"
    return "NORMAL"


def grade_liquidity(volume: float) -> str:
    if volume >= 1_000_000_000:
        return "HIGH"
    if volume >= 250_000_000:
        return "GOOD"
    if volume >= 60_000_000:
        return "MEDIUM"
    return "LOW"

# ============================================================
# OKX
# ============================================================

def okx_get(path: str, params: dict, retries: int = REQUEST_RETRIES) -> dict:
    url = OKX_BASE_URL + path
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            response = session.get(url, params=params, timeout=HTTP_TIMEOUT)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise RuntimeError("OKX returned invalid JSON")
            if str(payload.get("code", "")) != "0":
                raise RuntimeError(f"OKX code={payload.get('code')} msg={payload.get('msg', '')}")
            return payload
        except Exception as exc:
            last_error = exc
            log.warning("OKX REQUEST FAILED | %s | attempt=%s/%s | %s", path, attempt, retries, exc)
            if attempt < retries:
                time.sleep(min(attempt * 1.2, 4))
    raise RuntimeError(f"OKX request failed: {last_error}")


def get_instruments() -> set[str]:
    payload = okx_get("/api/v5/public/instruments", {"instType": "SWAP"})
    result = set()
    for item in payload.get("data", []):
        inst_id = str(item.get("instId", ""))
        if inst_id.endswith("-USDT-SWAP") and str(item.get("state", "")) == "live":
            result.add(inst_id)
    return result


def get_tickers() -> Dict[str, dict]:
    payload = okx_get("/api/v5/market/tickers", {"instType": "SWAP"})
    result = {}
    for item in payload.get("data", []):
        inst_id = str(item.get("instId", ""))
        if not inst_id.endswith("-USDT-SWAP"):
            continue
        try:
            last = float(item.get("last", 0) or 0)
            open24h = float(item.get("open24h", 0) or 0)
            high24h = float(item.get("high24h", 0) or 0)
            low24h = float(item.get("low24h", 0) or 0)
            vol24h = float(item.get("vol24h", 0) or 0)
            vol_ccy_24h = float(item.get("volCcy24h", 0) or 0)
            if last <= 0:
                continue
            turnover = vol_ccy_24h * last
            fallback = vol24h * last
            if turnover <= 0:
                turnover = fallback
            if fallback > 0 and turnover > fallback * 1000:
                turnover = fallback
            change24 = ((last - open24h) / open24h * 100.0) if open24h > 0 else 0.0
            result[inst_id] = {
                "last": last,
                "open24h": open24h,
                "high24h": high24h,
                "low24h": low24h,
                "vol24h_usd": turnover,
                "change24h_pct": change24,
            }
        except (TypeError, ValueError):
            continue
    return result


def get_candles(inst_id: str, bar: str, limit: int = 120) -> List[Candle]:
    key = (inst_id, bar)
    cached = candle_cache.get(key)
    if cached and now_ts() - cached[0] < CANDLE_CACHE_SECONDS:
        return cached[1]

    payload = okx_get("/api/v5/market/candles", {
        "instId": inst_id,
        "bar": bar,
        "limit": str(min(limit, 300)),
    })
    candles = []
    for row in reversed(payload.get("data", [])):
        try:
            candles.append(Candle(
                ts=int(row[0]), open=float(row[1]), high=float(row[2]), low=float(row[3]),
                close=float(row[4]), volume=float(row[5] or 0), quote_volume=float(row[7] or 0),
                confirmed=str(row[8]) == "1",
            ))
        except (IndexError, TypeError, ValueError):
            continue
    candle_cache[key] = (now_ts(), candles)
    return candles

# ============================================================
# INDICATORS
# ============================================================

def atr(candles: List[Candle], period: int = 14) -> float:
    if len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]
        trs.append(max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)))
    sample = trs[-period:]
    return sum(sample) / len(sample) if sample else 0.0


def avg_range(candles: List[Candle], n: int = 20) -> float:
    sample = candles[-n:]
    return sum(max(c.high - c.low, 0.0) for c in sample) / len(sample) if sample else 0.0


def volume_ratio(candles: List[Candle], n: int = 20) -> float:
    if len(candles) < n + 1:
        return 0.0
    base = sum(max(c.volume, 0.0) for c in candles[-n-1:-1]) / n
    return candles[-1].volume / base if base > 0 else 0.0

# ============================================================
# REVERSAL ENGINE
# ============================================================

def build_reversal_setup(inst_id: str, ticker: dict, candles: Dict[str, List[Candle]], side: str) -> Optional[Setup]:
    # Берём и текущую 5M свечу: для reversal важно поймать начало движения,
    # а не ждать закрытия свечи через несколько минут.
    c5 = candles.get("5m", [])
    c15 = [c for c in candles.get("15m", []) if c.confirmed]
    if len(c5) < 40 or len(c15) < 20:
        return None

    last = c5[-1].close
    if last <= 0:
        return None

    if side not in ("SHORT", "LONG"):
        return None

    look = c5[-min(LOOKBACK_5M, len(c5)):]
    a5 = atr(c5, 14)
    if a5 <= 0:
        return None

    # Для раннего входа экстремум ищем до текущей свечи.
    history = look[:-1]
    if len(history) < 8:
        return None

    extreme_i = (max(range(len(history)), key=lambda i: history[i].high)
                 if side == "SHORT" else
                 min(range(len(history)), key=lambda i: history[i].low))
    extreme = history[extreme_i].high if side == "SHORT" else history[extreme_i].low

    # Экстремум должен быть сформирован не на самой последней свече.
    if extreme_i >= len(history) - 2:
        return None

    post = history[extreme_i + 1:]
    if len(post) < 2:
        return None

    current = c5[-1]
    prev = c5[-2]
    body = abs(current.close - current.open)
    current_range_pct = (current.high - current.low) / max(current.close, 1e-12) * 100

    # Сильная свеча / активность сейчас. Объём текущей свечи может быть ещё
    # незакрытым, поэтому используем минимум либо текущий объём, либо предыдущую свечу.
    vratio = volume_ratio(c5, 20)
    prev_vratio = volume_ratio(c5[:-1], 20) if len(c5) > 22 else 0.0
    active_volume = max(vratio, prev_vratio)
    if current_range_pct < MIN_5M_RANGE_PCT and body < a5 * 0.75:
        return None
    if active_volume < MIN_VOLUME_RATIO and body < a5 * 1.25:
        return None

    if side == "SHORT":
        reversal_pct = (extreme - last) / extreme * 100
        if not (MIN_INITIAL_REVERSAL_PCT <= reversal_pct <= MAX_INITIAL_REVERSAL_PCT):
            return None

        # Первый медвежий импульс: красная свеча + пробой минимума
        # предыдущей свечи либо достаточно большой bearish-body.
        bearish = current.close < current.open
        trigger = (bearish and current.close < prev.low) or (bearish and body >= a5 * 0.90)
        if not trigger:
            return None

        local_high = max(c.high for c in post[-6:] + [current])
        sl = max(local_high, extreme) + a5 * 0.08
        risk = sl - last
        risk_pct = risk / last * 100
        if risk <= 0 or risk_pct < MIN_RISK_PCT or risk_pct > MAX_RISK_PCT:
            return None

        support5 = min(c.low for c in c5[-24:-1])
        support15 = min(c.low for c in c15[-20:])
        support_candidates = [x for x in (support5, support15) if x < last]
        if not support_candidates:
            return None
        support = max(support_candidates)
        room_pct = (last - support) / last * 100
        if room_pct < MIN_ROOM_PCT:
            return None

        tp1 = last - min(risk * 2.0, last * min(4.0, room_pct * 0.35) / 100)
        tp2 = last - min(risk * 3.5, last * min(8.0, room_pct * 0.70) / 100)
        tp3 = support
        if not (tp3 < tp2 < tp1 < last):
            return None

        strategy = "TOP GAINER · ПЕРВЫЙ РАЗВОРОТ"
        direction = "SHORT"
        reason_score = 82
        if abs(float(ticker.get("change24h_pct", 0) or 0)) >= 50: reason_score += 5
        if reversal_pct <= 2.5: reason_score += 3
        if active_volume >= 1.5: reason_score += 3
        if body >= a5: reason_score += 3
        score = min(98, reason_score)
        extreme_label = "EXTREME HIGH"
        trigger_label = "FIRST BEARISH BREAK"
        level = support

    else:
        reversal_pct = (last - extreme) / extreme * 100
        if not (MIN_INITIAL_REVERSAL_PCT <= reversal_pct <= MAX_INITIAL_REVERSAL_PCT):
            return None

        bullish = current.close > current.open
        trigger = (bullish and current.close > prev.high) or (bullish and body >= a5 * 0.90)
        if not trigger:
            return None

        local_low = min(c.low for c in post[-6:] + [current])
        sl = min(local_low, extreme) - a5 * 0.08
        risk = last - sl
        risk_pct = risk / last * 100
        if risk <= 0 or risk_pct < MIN_RISK_PCT or risk_pct > MAX_RISK_PCT:
            return None

        resistance5 = max(c.high for c in c5[-24:-1])
        resistance15 = max(c.high for c in c15[-20:])
        resistance_candidates = [x for x in (resistance5, resistance15) if x > last]
        if not resistance_candidates:
            return None
        resistance = min(resistance_candidates)
        room_pct = (resistance - last) / last * 100
        if room_pct < MIN_ROOM_PCT:
            return None

        tp1 = last + min(risk * 2.0, last * min(4.0, room_pct * 0.35) / 100)
        tp2 = last + min(risk * 3.5, last * min(8.0, room_pct * 0.70) / 100)
        tp3 = resistance
        if not (last < tp1 < tp2 < tp3):
            return None

        strategy = "TOP LOSER · ПЕРВЫЙ ОТСКОК"
        direction = "LONG"
        reason_score = 82
        if abs(float(ticker.get("change24h_pct", 0) or 0)) >= 50: reason_score += 5
        if reversal_pct <= 2.5: reason_score += 3
        if active_volume >= 1.5: reason_score += 3
        if body >= a5: reason_score += 3
        score = min(98, reason_score)
        extreme_label = "EXTREME LOW"
        trigger_label = "FIRST BULLISH BREAK"
        level = resistance

    chart_c = c5[-100:]
    offset = max(0, len(c5) - 100)
    extreme_global = max(0, len(c5) - len(look) + extreme_i)
    trigger_global = len(c5) - 1
    extreme_x = extreme_global - offset
    trigger_x = trigger_global - offset
    level_x = max(0, len(chart_c) - 28)

    lines = [(level_x, level, len(chart_c) - 1, level, "level")]
    points = [(extreme_x, extreme, extreme_label), (trigger_x, last, trigger_label)]

    return Setup(
        inst_id=inst_id, coin=get_coin(inst_id), direction=direction, strategy=strategy,
        current_price=last, extreme_price=extreme, level=level,
        entry_low=last * 0.9994, entry_high=last * 1.0004,
        sl=sl, tp1=tp1, tp2=tp2, tp3=tp3, score=score,
        change24=float(ticker.get("change24h_pct", 0) or 0),
        volume_ratio=active_volume, risk_pct=risk_pct, room_pct=room_pct,
        volume_24h=float(ticker.get("vol24h_usd", 0) or 0),
        candles_5m=c5[-100:], extreme_index=extreme_x, trigger_index=trigger_x,
        pattern_lines=lines, pattern_points=points,
    )

# ============================================================
# CHART — стиль сохраняем, содержание меняем под reversal
# ============================================================

def make_chart(setup: Setup) -> str:
    candles = setup.candles_5m
    if len(candles) < 30:
        raise RuntimeError("Not enough 5M candles for chart")

    path = f"/tmp/reversal_{setup.coin}_{int(time.time() * 1000)}.png"
    fig, ax = plt.subplots(figsize=(14, 8), dpi=160)
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")

    width = 0.58
    for i, c in enumerate(candles):
        color = "#16a34a" if c.close >= c.open else "#dc2626"
        ax.vlines(i, c.low, c.high, color=color, linewidth=1.15, zorder=2)
        body_low = min(c.open, c.close)
        body_h = max(abs(c.close - c.open), c.close * 0.000015)
        ax.add_patch(Rectangle((i - width / 2, body_low), width, body_h,
                               facecolor=color, edgecolor=color, linewidth=0.6, zorder=3))

    # Геометрия именно первого разворота.
    for x1, y1, x2, y2, kind in setup.pattern_lines:
        style = "--" if kind == "level" else "-"
        color = "#111827" if kind == "level" else "#2563eb"
        ax.plot([x1, x2], [y1, y2], color=color, linewidth=2.4, linestyle=style, zorder=4)

    for px, py, label in setup.pattern_points:
        if 0 <= px < len(candles):
            ax.scatter([px], [py], s=38, color="#111827", zorder=5)
            ax.annotate(label, (px, py), xytext=(0, 10), textcoords="offset points",
                        ha="center", fontsize=9, fontweight="bold", color="#111827")

    entry = (setup.entry_low + setup.entry_high) / 2
    ax.axhline(entry, color="#2563eb", linewidth=2.3, zorder=4)
    ax.axhline(setup.sl, color="#dc2626", linewidth=2.0, linestyle="--", zorder=4)
    ax.axhline(setup.tp1, color="#16a34a", linewidth=1.4, linestyle=":", zorder=4)
    ax.axhline(setup.tp2, color="#16a34a", linewidth=1.6, linestyle=":", zorder=4)
    ax.axhline(setup.tp3, color="#16a34a", linewidth=1.8, linestyle="--", zorder=4)

    x = len(candles) - 1
    ax.text(x, entry, f"  ВХОД {fmt_price(entry)}", va="bottom", ha="right", color="#1d4ed8", fontsize=11, fontweight="bold")
    ax.text(x, setup.sl, f"  СТОП {fmt_price(setup.sl)}", va="bottom", ha="right", color="#b91c1c", fontsize=11, fontweight="bold")
    ax.text(x, setup.tp1, f"  TP1 {fmt_price(setup.tp1)}", va="bottom", ha="right", color="#15803d", fontsize=9, fontweight="bold")
    ax.text(x, setup.tp2, f"  TP2 {fmt_price(setup.tp2)}", va="bottom", ha="right", color="#15803d", fontsize=9, fontweight="bold")
    ax.text(x, setup.tp3, f"  TP3 {fmt_price(setup.tp3)}", va="bottom", ha="right", color="#15803d", fontsize=10, fontweight="bold")

    ax.set_title(f"{setup.coin}USDT · {setup.strategy} · {setup.direction} · 5M",
                 fontsize=19, fontweight="bold", color="#111827", pad=14)
    ax.text(0.01, 0.98, f"Стратегия: {setup.strategy}", transform=ax.transAxes,
            va="top", fontsize=12, fontweight="bold", color="#111827")
    ax.grid(True, alpha=0.16, color="#94a3b8")
    ax.tick_params(colors="#475569", labelsize=9)
    for spine in ax.spines.values():
        spine.set_color("#cbd5e1")
    ax.set_xlim(-1, len(candles))
    plt.tight_layout()
    fig.savefig(path, facecolor="white", bbox_inches="tight")
    plt.close(fig)
    return path

# ============================================================
# TELEGRAM SIGNAL
# ============================================================

def build_signal_text(setup: Setup) -> str:
    entry = (setup.entry_low + setup.entry_high) / 2
    rr2 = abs(setup.tp2 - entry) / max(abs(entry - setup.sl), 1e-12)
    move = f"+{setup.change24:.1f}%" if setup.change24 >= 0 else f"{setup.change24:.1f}%"

    if setup.direction == "SHORT":
        headline = "🔻 TOP GAINER → SHORT"
        action = "Монета после сильного роста дала первый подтверждённый разворот вниз."
    else:
        headline = "🔺 TOP LOSER → LONG"
        action = "Монета после сильного падения дала первый подтверждённый отскок вверх."

    return (
        f"<b>{headline}</b>\n"
        f"<b>{setup.coin}USDT</b> · 5M\n\n"
        f"🧭 <b>Сетап:</b> {setup.strategy}\n"
        f"📈 <b>Движение 24H:</b> <code>{move}</code>\n"
        f"⚡ <b>Сейчас:</b> первый импульс разворота · объём <code>x{setup.volume_ratio:.2f}</code>\n\n"
        f"📝 <b>Что произошло:</b> {action}\n"
        f"🎯 <b>Вход:</b> <code>{fmt_price(entry)}</code>\n"
        f"🛑 <b>Стоп:</b> <code>{fmt_price(setup.sl)}</code> · риск <code>{setup.risk_pct:.2f}%</code>\n"
        f"🎯 <b>TP1:</b> <code>{fmt_price(setup.tp1)}</code>\n"
        f"🎯 <b>TP2:</b> <code>{fmt_price(setup.tp2)}</code>\n"
        f"🎯 <b>TP3:</b> <code>{fmt_price(setup.tp3)}</code>\n"
        f"📊 <b>RR до TP2:</b> <code>1:{rr2:.1f}</code>\n"
        f"💧 <b>Ликвидность:</b> <code>{grade_liquidity(setup.volume_24h)}</code>\n\n"
        f"⚠️ <b>РИСКИ</b>\n"
        f"• Риск на одну сделку — <b>не более 1% депозита</b>.\n"
        f"• Стоп обязателен. Кредитное плечо не отменяет риск.\n"
        f"• Не входите, если цена уже ушла далеко от зоны входа.\n"
        f"• Разворотная стратегия может давать ложные пробои и резкие выбросы цены.\n\n"
        f"❤️ 🔥 👍 👎 <b>Реакция на сигнал</b>"
    )


def send_photo_and_text(setup: Setup) -> bool:
    chart_path = None
    try:
        chart_path = make_chart(setup)
        caption = build_signal_text(setup)
        with open(chart_path, "rb") as photo:
            sent = bot.send_photo(CHANNEL_ID, photo, caption=caption,
                                  parse_mode="HTML", show_caption_above_media=True)
        log.info("SIGNAL SENT | %s | %s | score=%s", setup.coin, setup.direction, setup.score)
        return bool(sent)
    except Exception as exc:
        log.exception("TELEGRAM SEND FAILED | %s | %s", setup.inst_id, exc)
        return False
    finally:
        if chart_path:
            try:
                os.remove(chart_path)
            except OSError:
                pass

# ============================================================
# SIGNAL STORAGE / RESULTS
# ============================================================

def save_signal(setup: Setup) -> None:
    created = now_ts()
    db.execute("""
        INSERT INTO signals (
            inst_id, direction, strategy, entry_low, entry_high,
            sl, tp1, tp2, tp3, score, status, created_at, expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'READY', ?, ?)
    """, (
        setup.inst_id, setup.direction, setup.strategy,
        setup.entry_low, setup.entry_high, setup.sl,
        setup.tp1, setup.tp2, setup.tp3, setup.score,
        created, created + READY_TTL_MINUTES * 60,
    ))
    db.commit()


def can_send_new_signal(inst_id: str) -> bool:
    cooldown = now_ts() - COOLDOWN_MINUTES * 60
    row = db.execute("SELECT 1 FROM signals WHERE inst_id=? AND created_at>=? LIMIT 1", (inst_id, cooldown)).fetchone()
    return row is None


def update_signal_results() -> None:
    rows = db.execute("""
        SELECT id, inst_id, direction, entry_low, entry_high, sl, tp1, tp2, tp3,
               status, created_at, expires_at
        FROM signals
        WHERE status IN ('READY','ACTIVE')
        ORDER BY id ASC
    """).fetchall()

    for row in rows:
        sid, inst_id, direction = row[0], row[1], row[2]
        entry_low, entry_high = float(row[3]), float(row[4])
        sl, tp1, tp2, tp3 = map(float, row[5:9])
        status, expires_at = row[9], float(row[11])
        try:
            cs = [c for c in get_candles(inst_id, "5m", 80) if c.confirmed]
            if len(cs) < 2:
                continue
            c = cs[-1]
        except Exception:
            continue

        price = c.close
        if status == "READY":
            if entry_low <= price <= entry_high:
                db.execute("UPDATE signals SET status='ACTIVE', activated_at=? WHERE id=?", (now_ts(), sid))
                status = "ACTIVE"
            elif now_ts() > expires_at:
                db.execute("UPDATE signals SET status='EXPIRED', result='EXPIRED', closed_at=? WHERE id=?", (now_ts(), sid))
                continue

        if status != "ACTIVE":
            continue

        sl_hit = c.low <= sl if direction == "LONG" else c.high >= sl
        tp1_hit = c.high >= tp1 if direction == "LONG" else c.low <= tp1
        tp2_hit = c.high >= tp2 if direction == "LONG" else c.low <= tp2
        tp3_hit = c.high >= tp3 if direction == "LONG" else c.low <= tp3

        # Консервативно: если внутри одной свечи задеты и стоп, и TP до фиксации,
        # считаем сначала стоп.
        if sl_hit:
            db.execute("UPDATE signals SET status='CLOSED', result='SL', closed_at=? WHERE id=?", (now_ts(), sid))
            continue
        if tp3_hit:
            db.execute("UPDATE signals SET status='CLOSED', result='TP3', closed_at=? WHERE id=?", (now_ts(), sid))
        elif tp2_hit:
            db.execute("UPDATE signals SET status='ACTIVE', result='TP2' WHERE id=?", (sid,))
        elif tp1_hit:
            db.execute("UPDATE signals SET status='ACTIVE', result='TP1' WHERE id=?", (sid,))

    db.commit()

# ============================================================
# WEEKLY REPORT ONLY
# ============================================================

def previous_week_boundaries():
    n = local_now()
    today = datetime(n.year, n.month, n.day, tzinfo=ZoneInfo(TIMEZONE))
    this_week = today - timedelta(days=today.weekday())
    previous_week = this_week - timedelta(days=7)
    return previous_week.timestamp(), this_week.timestamp(), previous_week.date().isoformat()


def send_weekly_report_once() -> None:
    n = local_now()
    if n.weekday() != 0 or (n.hour, n.minute) < (REPORT_HOUR, REPORT_MINUTE):
        return

    start_ts, end_ts, week_key = previous_week_boundaries()
    key = f"weekly_report_{week_key}"
    if db.execute("SELECT 1 FROM bot_state WHERE key=?", (key,)).fetchone():
        return

    rows = db.execute("SELECT result FROM signals WHERE created_at>=? AND created_at<?", (start_ts, end_ts)).fetchall()
    total = len(rows)
    wins = sum(1 for (r,) in rows if r in ("TP1", "TP2", "TP3"))
    losses = sum(1 for (r,) in rows if r == "SL")

    text = (
        "📊 <b>ОТЧЁТ ЗА НЕДЕЛЮ</b>\n\n"
        f"Сигналов пришло: <b>{total}</b>\n"
        f"Закрылось в плюс: <b>{wins}</b>\n"
        f"Закрылось в минус: <b>{losses}</b>"
    )
    try:
        bot.send_message(CHANNEL_ID, text, parse_mode="HTML")
        db.execute("INSERT OR REPLACE INTO bot_state(key,value) VALUES(?,?)", (key, "1"))
        db.commit()
        log.info("WEEKLY REPORT SENT | %s", week_key)
    except Exception:
        log.exception("WEEKLY REPORT FAILED")

# ============================================================
# SCANNER
# ============================================================

def load_candles_for_symbol(inst_id: str) -> Dict[str, List[Candle]]:
    return {
        "15m": get_candles(inst_id, "15m", 100),
        "5m": get_candles(inst_id, "5m", 120),
    }


def scan_market() -> None:
    global signals_today

    n = local_now()
    day_key = n.date().isoformat()
    saved = db.execute("SELECT value FROM bot_state WHERE key='signals_day'").fetchone()
    if not saved or saved[0] != day_key:
        signals_today = 0
        db.execute("INSERT OR REPLACE INTO bot_state(key,value) VALUES('signals_day',?)", (day_key,))
        db.commit()

    if signals_today >= MAX_SIGNALS_PER_DAY:
        return

    instruments = get_instruments()
    tickers = get_tickers()
    liquid = [
        (inst_id, ticker) for inst_id, ticker in tickers.items()
        if inst_id in instruments and float(ticker.get("vol24h_usd", 0) or 0) >= MIN_24H_VOLUME_USD
    ]

    gainers = sorted(liquid, key=lambda x: float(x[1].get("change24h_pct", 0)), reverse=True)[:TOP_GAINERS]
    losers = sorted(liquid, key=lambda x: float(x[1].get("change24h_pct", 0)))[:TOP_LOSERS]
    candidates = [(inst_id, ticker, "SHORT") for inst_id, ticker in gainers] + [(inst_id, ticker, "LONG") for inst_id, ticker in losers]

    log.info("TOP MOVERS | gainers=%s losers=%s unique=%s", TOP_GAINERS, TOP_LOSERS, len(candidates))

    setups = []
    for inst_id, ticker, side in candidates:
        if not can_send_new_signal(inst_id):
            continue
        try:
            candles = load_candles_for_symbol(inst_id)
            setup = build_reversal_setup(inst_id, ticker, candles, side)
            if setup:
                setups.append(setup)
        except Exception as exc:
            log.warning("ANALYZE FAILED | %s | %s", inst_id, exc)

    setups.sort(key=lambda s: (s.score, s.volume_ratio), reverse=True)
    for setup in setups:
        if signals_today >= MAX_SIGNALS_PER_DAY:
            break
        if not can_send_new_signal(setup.inst_id):
            continue
        if send_photo_and_text(setup):
            save_signal(setup)
            created = now_ts()
            ready_setups[setup.inst_id] = ActiveReady(setup, created, created + READY_TTL_MINUTES * 60)
            signals_today += 1
            log.info("REVERSAL SIGNAL | %s | %s | 24H=%+.2f%% | risk=%.2f%%", setup.coin, setup.direction, setup.change24, setup.risk_pct)


def main() -> None:
    log.info("REVERSAL SCALPER V1 STARTED")
    log.info(
        "TOP GAINERS=%s (>=%.1f%%) | TOP LOSERS=%s (<=%.1f%%) | MAX RISK=%.2f%%",
        TOP_GAINERS, MIN_GAINER_24H_PCT, TOP_LOSERS, MIN_LOSER_24H_PCT, MAX_RISK_PCT,
    )
    while True:
        try:
            update_signal_results()
            send_weekly_report_once()
            scan_market()
        except KeyboardInterrupt:
            log.info("STOPPED")
            break
        except Exception:
            log.exception("MAIN LOOP ERROR")
        time.sleep(SCAN_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
