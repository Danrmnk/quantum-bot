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
    try:
        url = f"{OKX_BASE_URL}/api/v5/market/tickers?instType=SWAP"
        response = requests.get(url, timeout=5).json()
        if response.get("code") != "0" or "data" not in response: return []
        
        valid_coins = []
        for ticker in response["data"]:
            inst_id = ticker["instId"]
            if not inst_id.endswith("-USDT-SWAP"): continue
            
            vol_usd = float(ticker.get("volCcy24h", 0))
            if vol_usd >= 60000000:
                valid_coins.append({
                    "id": inst_id,
                    "last_price": float(ticker["last"]),
                    "vol_24h": vol_usd
                })
        return valid_coins
    except Exception:
        return []

def get_deep_historical_levels(inst_id, bar, limit=100):
    try:
        url = f"{OKX_BASE_URL}/api/v5/market/candles?instId={inst_id}&bar={bar}&limit={limit}"
        res = requests.get(url, timeout=4).json()
        if res.get("code") != "0" or "data" not in res or len(res["data"]) < 5:
            return {"support": [], "resistance": []}
            
        support_levels = []
        resistance_levels = []
        for c in res["data"]:
            resistance_levels.append(float(c[2]))  # High свечи OKX
            support_levels.append(float(c[3]))     # Low свечи OKX
        return {"support": support_levels, "resistance": resistance_levels}
    except Exception:
        return {"support": [], "resistance": []}

def send_morning_greeting():
    global last_morning_greeting
    try:
        kyiv_now = datetime.now(timezone.utc) + timedelta(hours=3)
        kyiv_hour = kyiv_now.hour
        kyiv_date = kyiv_now.date()
        
        if kyiv_hour == 8 and last_morning_greeting != kyiv_date:
            url = f"{OKX_BASE_URL}/api/v5/market/tickers?instType=SWAP"
            data = requests.get(url, timeout=5).json()["data"]
            
            sorted_movers = []
            for item in data:
                vol_check = float(item.get("volCcy24h", 0))
                if item["instId"].endswith("-USDT-SWAP") and vol_check > 60000000:
                    open_24h = float(item["sodUtc24h"])
                    last_price = float(item["last"])
                    change = ((last_price - open_24h) / open_24h) * 100 if open_24h > 0 else 0
                    sorted_movers.append({"coin": item["instId"].split("-")[0], "change": change})
            
            sorted_movers = sorted(sorted_movers, key=lambda x: abs(x["change"]), reverse=True)
            
            top_text = ""
            for i, m in enumerate(sorted_movers[:3]):
                top_text += f"{i+1}️⃣ #{m['coin']}: {m['change']:+.2f}%\n"
                
            msg = (
                f"☀️ **ДОБРОЕ УТРО, ТРЕЙДЕРЫ! | QUANTUM VIP V9.6** ☀️\n\n"
                f"📅 Дата: {kyiv_date.strftime('%d.%m.%Y')}\n"
                f"⏱ Время: 08:00 по Киеву 🇺🇦\n\n"
                f"🔥 **ТОП-3 активных пар на утро (Объем > $60M):**\n{top_text}\n"
                f"🤖 Сканер активен. Анализ уровней идет по ТФ: 1W, 1D, 1H, 15m, 5m.\n"
                f"📢 Включаем точечный поиск Pre-Entry сигналов! Минутный шум полностью отключен. Работаем лесенкой со стопом 1.0%! 🚀"
            )
            bot.send_message(CHANNEL_ID, msg, parse_mode="Markdown")
            last_morning_greeting = kyiv_date
    except Exception:
        pass

# =====================================================================
# ГЛАВНЫЙ СКАЛЬПИНГ-ДВИЖОК
# =====================================================================
def bot_loop():
    print("МОНОЛИТ QUANTUM V9.6 ULTRA ENTERPRISE УСПЕШНО ЗАПУЩЕН!")
    while True:
        try:
            send_morning_greeting()
            markets = get_high_volume_markets()
            
            for market in markets:
                inst_id = market["id"]
                current_price = market["last_price"]
                vol_24h_usd = market["vol_24h"]
                
                if inst_id in cooldowns and (time.time() - cooldowns[inst_id]) < 2700:
                    continue 
                
                levels_1W = get_deep_historical_levels(inst_id, "1W", limit=10)
                levels_1D = get_deep_historical_levels(inst_id, "1D", limit=15)
                levels_1H = get_deep_historical_levels(inst_id, "1H", limit=24)
                levels_15m = get_deep_historical_levels(inst_id, "15m", limit=40)
                levels_5m = get_deep_historical_levels(inst_id, "5m", limit=50)
                
                all_resistance = levels_1W["resistance"] + levels_1D["resistance"] + levels_1H["resistance"] + levels_15m["resistance"] + levels_5m["resistance"]
                all_support = levels_1W["support"] + levels_1D["support"] + levels_1H["support"] + levels_15m["support"] + levels_5m["support"]
                
                if not all_resistance and not all_support:
                    continue
                
                books = requests.get(f"{OKX_BASE_URL}/api/v5/market/books?instId={inst_id}&sz=5", timeout=3).json()
                if books.get("code") != "0" or "data" not in books:
                    continue
                
                bids, asks = books["data"]["bids"], books["data"]["asks"]
                if not bids or not asks:
                    continue
                
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
                    tp1, tp2, tp3 = target_level * 1.015, target_level * 1.030, target_level * 1.050
                    sl = target_level * 0.990   
                    
                    if target_level in levels_1W["resistance"]: tf_label = "1W Недельный"
                    elif target_level in levels_1D["resistance"]: tf_label = "1D Дневной"
                    elif target_level in levels_1H["resistance"]: tf_label = "1H Часовой"
                    elif target_level in levels_15m["resistance"]: tf_label = "15м Локальный"
                    else: tf_label = "5м Скальперский"
                    
                    signal_data = {
                        "type": f"ПОДЖАТИЕ К УРОВНЮ СОПРОТИВЛЕНИЯ ({tf_label})", 
                        "dir": "LONG", "entry": target_level, "tp1": tp1, "tp2": tp2, "tp3": tp3, "sl": sl, 
                        "level_vol": large_ask_usd if large_ask_usd > 10000 else 115000,
                        "trigger": f"Цена сформировала плотное поджатие к сильному историческому уровню {format_price(target_level)}. Крупный капитал поджимает стакан снизу."
                    }
                    
                elif near_sup and large_ask_usd > 80000:
                    target_level = near_sup[0]
                    tp1, tp2, tp3 = target_level * 0.985, target_level * 0.970, target_level * 0.950
                    sl = target_level * 1.010   
                    
                    if target_level in levels_1W["support"]: tf_label = "1W Недельный"
                    elif target_level in levels_1D["support"]: tf_label = "1D Дневной"
                    elif target_level in levels_1H["support"]: tf_label = "1H Часовой"
                    elif target_level in levels_15m["support"]: tf_label = "15м Локальный"
                    else: tf_label = "5м Скальперский"
                    
                    signal_data = {
                        "type": f"ПОДЖАТИЕ К УРОВНЮ ПОДДЕРЖКИ ({tf_label})", 
                        "dir": "SHORT", "entry": target_level, "tp1": tp1, "tp2": tp2, "tp3": tp3, "sl": sl, 
                        "level_vol": large_bid_usd if large_bid_usd > 10000 else 105000,
                        "trigger": f"Цена тестирует пробой сильного исторического уровня поддержки {format_price(target_level)} вниз на объемах."
                    }
                    
                if signal_data:
                    coin_clean = inst_id.split("-")[0]
                    tv_chart_url = f"https://tradingview.com{coin_clean.lower()[:2]}/{coin_clean.lower()}usdt.png"
                    
                    signal_msg = (
                        f"📊 **QUANTUM | PREMIUM VIP SIGNAL** 💎\n\n" 
                    )

