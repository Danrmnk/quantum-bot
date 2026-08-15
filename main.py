import os
import time
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import requests
from datetime import datetime, timezone, timedelta
import telebot

# =====================================================================
# ИНИЦИАЛИЗАЦИЯ И СИСТЕМНЫЕ НАСТРОЙКИ
# =====================================================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "ТВОЙ_ТЕЛЕГРАМ_ТОКЕН")
CHANNEL_ID = os.getenv("CHANNEL_ID", "ID_ТВОЕГО_КАНАЛА_ИЛИ_ЧАТА")

bot = telebot.TeleBot(TELEGRAM_TOKEN)
OKX_BASE_URL = "https://okx.com"

active_tracks = {}   
cooldowns = {}       # 45 минут защиты от повторного спама монеты
last_morning_greeting = None

def format_price(price):
    try:
        price_float = float(price)
        if price_float == 0: return "0.0"
        if price_float >= 100: return f"{price_float:.2f}"
        if price_float >= 1: return f"{price_float:.4f}".rstrip('0').rstrip('.')
        return f"{price_float:.8f}".rstrip('0').rstrip('.')
    except Exception:
        return str(price)

def get_high_volume_markets():
    """Фильтр Smart-Money: Находит монеты с объемом строго от 80,000,000 USD"""
    try:
        url = f"{OKX_BASE_URL}/api/v5/market/tickers?instType=SWAP"
        response = requests.get(url, timeout=5).json()
        if response.get("code") != "0" or "data" not in response: return []
        
        valid_instruments = []
        for ticker in response["data"]:
            inst_id = ticker["instId"]
            if not inst_id.endswith("-USDT-SWAP"): continue
            
            vol_usd = float(ticker.get("volCcy24h", 0))
            if vol_usd >= 80000000: # Снижено до 80 миллионов долларов для повышения активности
                valid_instruments.append({
                    "id": inst_id,
                    "last_price": float(ticker["last"]),
                    "vol_24h": vol_usd
                })
        return valid_instruments
    except Exception:
        return []

def get_deep_historical_levels(inst_id, bar):
    try:
        url = f"{OKX_BASE_URL}/api/v5/market/candles?instId={inst_id}&bar={bar}&limit=30"
        res = requests.get(url, timeout=4).json()
        if res.get("code") != "0" or "data" not in res or len(res["data"]) < 5:
            return {"support": [], "resistance": []}
            
        support_levels = []
        resistance_levels = []
        for c in res["data"]:
            # Безопасное извлечение High/Low по индексам OKX
            resistance_levels.append(float(c[2]))  
            support_levels.append(float(c[3]))     
        return {"support": support_levels, "resistance": resistance_levels}
    except Exception:
        return {"support": [], "resistance": []}

def send_morning_greeting():
    """Утренний дайджест: срабатывает СРАЗУ при первом запуске бота"""
    global last_morning_greeting
    try:
        kyiv_now = datetime.now(timezone.utc) + timedelta(hours=3)
        kyiv_date = kyiv_now.date()
        
        if last_morning_greeting != kyiv_date:
            url = f"{OKX_BASE_URL}/api/v5/market/tickers?instType=SWAP"
            data = requests.get(url, timeout=5).json()["data"]
            
            sorted_movers = []
            for item in data:
                vol_check = float(item.get("volCcy24h", 0))
                if item["instId"].endswith("-USDT-SWAP") and vol_check > 80000000:
                    open_24h = float(item["sodUtc24h"])
                    last_price = float(item["last"])
                    change = ((last_price - open_24h) / open_24h) * 100 if open_24h > 0 else 0
                    sorted_movers.append({"coin": item["instId"].split("-")[0], "change": change})
            
            sorted_movers = sorted(sorted_movers, key=lambda x: abs(x["change"]), reverse=True)
            
            top_text = ""
            for i, m in enumerate(sorted_movers[:3]):
                top_text += f"{i+1}️⃣ #{m['coin']}: {m['change']:+.2f}%\n"
                
            msg = (
                f"☀️ **ДОБРОЕ УТРО, ТРЕЙДЕРЫ! | QUANTUM VIP V7.4** ☀️\n\n"
                f"📅 Дата: {kyiv_date.strftime('%d.%m.%Y')}\n"
                f"⏱ Время: {kyiv_now.strftime('%H:%M')} по Киеву 🇺🇦\n\n"
                f"🔥 **ТОП-3 активных пар на утро (Объем > $80M):**\n{top_text}\n"
                f"🤖 Сканер запущен. Порог фильтрации снижен до $80 млн для поиска лучших ТВХ.\n"
                f"📢 Включаем точечный поиск Pre-Entry сигналов! Работаем лесенкой целей со стопом 1.0%! Профитного дня! 🚀"
            )
            bot.send_message(CHANNEL_ID, msg, parse_mode="Markdown")
            last_morning_greeting = kyiv_date
    except Exception as e:
        print(f"Ошибка в приветствии: {e}")

# =====================================================================
# ГЛАВНЫЙ ДВИЖОК СКАЛЬПИНГА
# =====================================================================
def bot_loop():
    print("МОНОЛИТ QUANTUM V7.4 VIP PRO УСПЕШНО СТАРТОВАЛ!")
    while True:
        try:
            send_morning_greeting()
            markets = get_high_volume_markets()
            for market in markets:
                inst_id = market["id"]
                current_price = market["last_price"]
                vol_24h_usd = market["vol_24h"]
                
                if inst_id in active_tracks: continue
                if inst_id in cooldowns and (time.time() - cooldowns[inst_id]) < 2700: continue 
                
                levels_1D = get_deep_historical_levels(inst_id, "1D")
                levels_1H = get_deep_historical_levels(inst_id, "1H")
                
                all_resistance = levels_1D["resistance"] + levels_1H["resistance"]
                all_support = levels_1D["support"] + levels_1H["support"]
                
                if not all_resistance and not all_support: continue
                
                books = requests.get(f"{OKX_BASE_URL}/api/v5/market/books?instId={inst_id}&sz=5", timeout=3).json()
                if books.get("code") != "0" or "data" not in books: continue
                
                bids, asks = books["data"]["bids"], books["data"]["asks"]
                if not bids or not asks: continue
                
                large_bid = max([float(b[1]) for b in bids])
                large_bid_price = float(bids[[float(b[1]) for b in bids].index(large_bid)][0])
                large_bid_usd = large_bid * large_bid_price
                
                large_ask = max([float(a[1]) for a in asks])
                large_ask_price = float(asks[[float(a[1]) for a in asks].index(large_ask)][0])
                large_ask_usd = large_ask * large_ask_price
                
                near_res = [r for r in all_resistance if 0.0020 <= (r - current_price) / current_price <= 0.0080]
                near_sup = [s for s in all_support if 0.0020 <= (current_price - s) / current_price <= 0.0080]
                
                signal_data = None
                
                if near_res and large_bid_usd > 80000:
                    target_level = near_res[0]
                    entry_price = target_level
                    
                    tp1 = entry_price * 1.015  # +1.5%
                    tp2 = entry_price * 1.030  # +3.0%
                    tp3 = entry_price * 1.050  # +5.0%
                    sl = entry_price * 0.990   # ТЕХНИЧЕСКИЙ СТОП СТРОГО -1.0%
                    
                    signal_data = {
                        "type": "ПРОБОЙ УРОВНЯ / РАЗЪЕДАНИЕ ПЛОТНОСТИ", "dir": "LONG", "entry": entry_price,
                        "tp1": tp1, "tp2": tp2, "tp3": tp3, "sl": sl, "level_vol": large_ask_usd,
                        "trigger": f"Цена поджимается к сопротивлению {format_price(target_level)}. Ожидается пробой вверх."
                    }
                    
                elif near_sup and large_ask_usd > 80000:
                    target_level = near_sup[0]
                    entry_price = target_level
                    
                    tp1 = entry_price * 0.985  # +1.5%
                    tp2 = entry_price * 0.970  # +3.0%
                    tp3 = entry_price * 0.950  # +5.0%
                    sl = entry_price * 1.010   # ТЕХНИЧЕСКИЙ СТОП СТРОГО -1.0%
                    
                    signal_data = {
                        "type": "ПРОБОЙ УРОВНЯ / РАЗЪЕДАНИЕ ПЛОТНОСТИ", "dir": "SHORT", "entry": entry_price,
                        "tp1": tp1, "tp2": tp2, "tp3": tp3, "sl": sl, "level_vol": large_bid_usd,
                        "trigger": f"Цена приблизилась к уровню поддержки {format_price(target_level)}. Ожидается пробой вниз."
                    }
                    
                if signal_data:
                    coin_clean = inst_id.split("-")[0]
                    tv_chart_url = f"https://tradingview.com{coin_clean.lower()[:2]}/{coin_clean.lower()}usdt.png"
                    
                    signal_msg = (
                        f"📊 **QUANTUM | VIP ENTERPRISE SIGNAL 💎**\n\n"
                        f"🪙 Пара: #{coin_clean}/USDT\n"
                        f"Паттерн: **{signal_data['type']}**\n"
                        f"Направление: 🔥 **{signal_data['dir']}** 🔥\n\n"
                        f"📊 **ОБЪЕМНЫЙ АНАЛИЗ (SMART MONEY):**\n"
                        f"• Суточный объём монеты: `${round(vol_24h_usd / 1000000, 1)} Млн $`\n"
                        f"• Плотность капитала на уровне: `${signal_data['level_vol']:,.0f} USD` 💵\n\n"
                        f"⚠️ **ИНСТРУКЦИЯ ДЛЯ ВХОДА (УСПЕЮТ ВСЕ):**\n"
                        f"_{signal_data['trigger']}_\n"
                        f"👉 *Выставляйте ОТЛОЖЕННЫЙ ОРДЕР заранее по указанной цене!*\n\n"
                        f"📥 **ПЛАНИРУЕМАЯ ЦЕНА ВХОДА:** `{format_price(signal_data['entry'])}`\n\n"
                        f"🎯 **ЛЕСЕНКА ЗАКРЫТИЯ ЦЕЛЕЙ (TAKE-POINTS):**\n"
                        f"🎯 Цель 1: `{format_price(signal_data['tp1'])}` (+1.5%)\n"
