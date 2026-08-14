import os
import time
import requests
from datetime import datetime, timezone, timedelta
import matplotlib
matplotlib.use('Agg')  # Фоновый режим отрисовки без экрана
import matplotlib.pyplot as plt
import telebot

# =====================================================================
# ИНИЦИАЛИЗАЦИЯ И СИСТЕМНЫЕ НАСТРОЙКИ
# =====================================================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "ТВОЙ_ТЕЛЕГРАМ_ТОКЕН")
CHANNEL_ID = os.getenv("CHANNEL_ID", "ID_ТВОЕГО_КАНАЛА_ИЛИ_ЧАТА")

bot = telebot.TeleBot(TELEGRAM_TOKEN)
OKX_BASE_URL = "https://okx.com"

active_tracks = {}   
cooldowns = {}       # 45 минут защиты от спама по одной монете
last_morning_greeting = None

def format_price(price):
    price_float = float(price)
    if price_float == 0: return "0.0"
    if price_float >= 100: return f"{price_float:.2f}"
    if price_float >= 1: return f"{price_float:.4f}".rstrip('0').rstrip('.')
    return f"{price_float:.8f}".rstrip('0').rstrip('.')

def get_high_volume_markets():
    try:
        url = f"{OKX_BASE_URL}/api/v5/market/tickers?instType=SWAP"
        response = requests.get(url, timeout=5).json()
        if response.get("code") != "0" or "data" not in response: return []
        
        valid_instruments = []
        for ticker in response["data"]:
            inst_id = ticker["instId"]
            if not inst_id.endswith("-USDT-SWAP"): continue
            vol_usd = float(ticker.get("volCcy24h", 0))
            if vol_usd >= 100000000:
                valid_instruments.append({
                    "id": inst_id,
                    "last_price": float(ticker["last"]),
                    "vol_24h": vol_usd
                })
        return valid_instruments
    except:
        return []

def get_deep_historical_levels(inst_id, bar, limit=150):
    try:
        url = f"{OKX_BASE_URL}/api/v5/market/candles?instId={inst_id}&bar={bar}&limit={limit}"
        res = requests.get(url, timeout=4).json()
        if res.get("code") != "0" or "data" not in res or len(res["data"]) < 20:
            return {"support": [], "resistance": []}
            
        highs = [float(c)[2] for c in res["data"]]
        lows = [float(c)[3] for c in res["data"]]
        
        support_levels = []
        resistance_levels = []
        
        for h in set(highs):
            if highs.count(h) >= 2 or any(abs(h - x) / h < 0.0008 for x in highs if x != h):
                resistance_levels.append(h)
        for l in set(lows):
            if lows.count(l) >= 2 or any(abs(l - x) / l < 0.0008 for x in lows if x != l):
                support_levels.append(l)
                
        return {"support": support_levels, "resistance": resistance_levels}
    except:
        return {"support": [], "resistance": []}

def generate_chart_photo(inst_id, entry, tp1, tp2, tp3, sl, direction, type_label):
    """Генерирует качественное фото графика с центрированными уровнями"""
    try:
        url = f"{OKX_BASE_URL}/api/v5/market/candles?instId={inst_id}&bar=5m&limit=35"
        res = requests.get(url, timeout=5).json()
        if res.get("code") != "0" or "data" not in res: return None
        
        # Переворачиваем свечи от старых к новым
        candles = res["data"][::-1]
        closes = [float(c)[4] for c in candles]
        times = [datetime.fromtimestamp(int(c)[0]/1000).strftime('%H:%M') for c in candles]
        
        plt.style.use('dark_background')
        fig, ax = plt.subplots(figsize=(10, 5), dpi=130)
        
        # Рисуем плавный неоновый график цены монеты
        ax.plot(times, closes, color='#00f0ff', linewidth=2, label='Текущий тренд')
        
        # Чертим горизонтальные линии уровней
        ax.axhline(y=entry, color='#3b82f6', linestyle='--', linewidth=1.5, label=f'ВХОД: {format_price(entry)}')
        ax.axhline(y=tp1, color='#10b981', linestyle='-', linewidth=1.5, label=f'ЦЕЛЬ 1: {format_price(tp1)}')
        ax.axhline(y=tp2, color='#10b981', linestyle='-', linewidth=1.5, label=f'ЦЕЛЬ 2: {format_price(tp2)}')
        ax.axhline(y=tp3, color='#059669', linestyle='-', linewidth=2, label=f'ЦЕЛЬ 3: {format_price(tp3)}')
        ax.axhline(y=sl, color='#ef4444', linestyle='-', linewidth=1.5, label=f'СТОП: {format_price(sl)}')
        
        coin_clean = inst_id.split("-")[0]
        ax.set_title(f"QUANTUM VIP SCANNER | {coin_clean}/USDT (5m) | {direction}", fontsize=11, fontweight='bold', color='#ffffff', pad=12)
        ax.grid(True, color='#262626', linestyle=':', linewidth=0.5)
        ax.legend(loc='upper left', framealpha=0.2)
        
        # Прореживаем шаги времени снизу
        plt.xticks(range(0, len(times), 6), times[::6])
        
        file_path = f"{inst_id}_scalp.png"
        plt.tight_layout()
        plt.savefig(file_path, facecolor='#121212', bbox_inches='tight')
        plt.close()
        return file_path
    except Exception as e:
        print(f"Ошибка прорисовки фото: {e}")
        return None

def check_active_trades():
    global active_tracks
    for inst_id, trade in list(active_tracks.items()):
        try:
            url = f"{OKX_BASE_URL}/api/v5/market/ticker?instId={inst_id}"
            res = requests.get(url, timeout=3).json()
            if res.get("code") != "0" or "data" not in res: continue
            
            current_price = float(res["data"]["last"])
            direction = trade["direction"]
            coin = inst_id.split("-")[0]
            
            if not trade["activated"]:
                if (direction == "LONG" and current_price >= trade["entry"]) or (direction == "SHORT" and current_price <= trade["entry"]):
                    trade["activated"] = True
                    bot.send_message(CHANNEL_ID, f"🔔 **QUANTUM | ОРДЕР АКТИВИРОВАН**\n\n🟢 Отложенный ордер по #{coin}/USDT набран в позицию по цене `{format_price(trade['entry'])}`!\n🎯 Робот начинает ведение сделки по лесенке целей.", parse_mode="Markdown")
                continue

            if direction == "LONG":
                if current_price >= trade["tp1"] and not trade["tp1_hit"]:
                    trade["tp1_hit"] = True
                    bot.send_message(CHANNEL_ID, f"🎯 **QUANTUM | ЦЕЛЬ №1 ВЗЯТА**\n\n✅ **Первая цель достигнута по #{coin}/USDT!**\n💵 Фиксируем часть прибыли.\n💼 Переносим Стоп-Лосс в **БЕЗУБЫТОК** (на цену входа).", parse_mode="Markdown")
                if current_price >= trade["tp2"] and not trade["tp2_hit"]:
                    trade["tp2_hit"] = True
                    bot.send_message(CHANNEL_ID, f"🚀 **QUANTUM | ЦЕЛЬ №2 ВЗЯТА**\n\n✅ **Основная цель достигнута по #{coin}/USDT!**\n💵 Фиксируем еще +30% позиции в плюс!", parse_mode="Markdown")
                if current_price >= trade["tp3"]:
                    bot.send_message(CHANNEL_ID, f"🏆 **QUANTUM | ПОЛНЫЙ ТЕЙК-ПРОФИТ**\n\n✅ **Финальная Цель №3 закрыта по #{coin}/USDT!**\nСделка отработала идеально на 100%! 🔥", parse_mode="Markdown")
                    del active_tracks[inst_id]
                    continue
                if current_price <= trade["sl"]:
                    status = "в БЕЗУБЫТОК" if trade["tp1_hit"] else "по СТОП-ЛОССУ (Риск сохранен)"
                    bot.send_message(CHANNEL_ID, f"🛑 **QUANTUM | СДЕЛКА ЗАКРЫТА**\n\n📋 Позиция #{coin}/USDT закрылась {status}. Риск-менеджмент соблюден.", parse_mode="Markdown")
                    del active_tracks[inst_id]
            else: # SHORT
                if current_price <= trade["tp1"] and not trade["tp1_hit"]:
                    trade["tp1_hit"] = True
                    bot.send_message(CHANNEL_ID, f"🎯 **QUANTUM | ЦЕЛЬ №1 ВЗЯТА (SHORT)**\n\n✅ **Первая цель достигнута по #{coin}/USDT!**\n💼 Переносим Стоп-Лосс в **БЕЗУБЫТОК**.", parse_mode="Markdown")
                if current_price <= trade["tp2"] and not trade["tp2_hit"]:
                    trade["tp2_hit"] = True
                    bot.send_message(CHANNEL_ID, f"🚀 **QUANTUM | ЦЕЛЬ №2 ВЗЯТА (SHORT)**\n\n✅ **Основная цель достигнута по #{coin}/USDT!**\n💵 Фиксируем прибыль!", parse_mode="Markdown")
                if current_price <= trade["tp3"]:
                    bot.send_message(CHANNEL_ID, f"🏆 **QUANTUM | ПОЛНЫЙ ТЕЙК-ПРОФИТ (SHORT)**\n\n✅ **Финальная Цель №3 закрыта по #{coin}/USDT!**\nПозиция полностью закрыта по целям! 🔥", parse_mode="Markdown")
                    del active_tracks[inst_id]
                    continue
                if current_price >= trade["sl"]:
                    status = "в БЕЗУБЫТОК" if trade["tp1_hit"] else "по СТОП-ЛОССУ"
                    bot.send_message(CHANNEL_ID, f"🛑 **QUANTUM | СДЕЛКА ЗАКРЫТА**\n\n📋 Позиция #{coin}/USDT закрылась {status}.", parse_mode="Markdown")
                    del active_tracks[inst_id]
        except:
            pass

def main():
    print("МОНОЛИТ QUANTUM V6.5 ULTRA ENTERPRISE С ГРАФИКАМИ СТАРТОВАЛ!")
    while True:
        try:
            check_active_trades()
            markets = get_high_volume_markets()
            for market in markets:
                inst_id = market["id"]
                current_price = market["last_price"]
                vol_24h_usd = market["vol_24h"]
                
                if inst_id in active_tracks: continue
                if inst_id in cooldowns and (time.time() - cooldowns[inst_id]) < 2700: continue 
                
                levels_1D = get_deep_historical_levels(inst_id, "1D", limit=20)
                levels_1H = get_deep_historical_levels(inst_id, "1H", limit=48)
                levels_15m = get_deep_historical_levels(inst_id, "15m", limit=60)
                
                all_resistance = levels_1D["resistance"] + levels_1H["resistance"] + levels_15m["resistance"]
                all_support = levels_1D["support"] + levels_1H["support"] + levels_15m["support"]
                
                if not all_resistance and not all_support: continue
                
