import os
import time
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

# Базы данных в оперативной памяти сервера
active_tracks = {}   
cooldowns = {}       # 45 минут защиты от спама по одной монете
last_morning_greeting = None

def format_price(price):
    try:
        price_float = float(price)
        if price_float == 0: return "0.0"
        if price_float >= 100: return f"{price_float:.2f}"
        if price_float >= 1: return f"{price_float:.4f}".rstrip('0').rstrip('.')
        return f"{price_float:.8f}".rstrip('0').rstrip('.')
    except:
        return str(price)

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

def get_deep_historical_levels(inst_id, bar, limit=50):
    try:
        url = f"{OKX_BASE_URL}/api/v5/market/candles?instId={inst_id}&bar={bar}&limit={limit}"
        res = requests.get(url, timeout=4).json()
        if res.get("code") != "0" or "data" not in res or len(res["data"]) < 5:
            return {"support": [], "resistance": []}
            
        support_levels = []
        resistance_levels = []
        
        for c in res["data"]:
            high = float(c[2]) # Индекс 2 - High свечи OKX
            low = float(c[3])  # Индекс 3 - Low свечи OKX
            resistance_levels.append(high)
            support_levels.append(low)
                
        return {"support": support_levels, "resistance": resistance_levels}
    except:
        return {"support": [], "resistance": []}

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
                    bot.send_message(CHANNEL_ID, f"🎯 **QUANTUM | ЦЕЛЬ №1 ВЗЯТА**\n\n✅ **Первая цель достигнута по #{coin}/USDT!**\n💼 Фиксируем прибыль. Переносим Стоп-Лосс в **БЕЗУБЫТОК**.", parse_mode="Markdown")
                if current_price >= trade["tp2"] and not trade["tp2_hit"]:
                    trade["tp2_hit"] = True
                    bot.send_message(CHANNEL_ID, f"🚀 **QUANTUM | ЦЕЛЬ №2 ВЗЯТА**\n\n✅ **Основная цель достигнута по #{coin}/USDT!**\n💵 Фиксируем еще +30% позиции в плюс!", parse_mode="Markdown")
                if current_price >= trade["tp3"]:
                    bot.send_message(CHANNEL_ID, f"🏆 **QUANTUM | ПОЛНЫЙ ТЕЙК-ПРОФИТ**\n\n✅ **Финальная Цель №3 закрыта по #{coin}/USDT!**\nСделка отработала идеально на 100%! 🔥", parse_mode="Markdown")
                    del active_tracks[inst_id]
                    continue
                if current_price <= trade["sl"]:
                    status = "в БЕЗУБЫТОК" if trade["tp1_hit"] else "по СТОП-ЛОССУ (Риск сохранен)"
                    bot.send_message(CHANNEL_ID, f"🛑 **QUANTUM | СДЕЛКА ЗАКРЫТА**\n\n📋 Позиция #{coin}/USDT закрылась {status}. Риск под контролем.", parse_mode="Markdown")
                    del active_tracks[inst_id]
            else: # SHORT
                if current_price <= trade["tp1"] and not trade["tp1_hit"]:
                    trade["tp1_hit"] = True
                    bot.send_message(CHANNEL_ID, f"🎯 **QUANTUM | ЦЕЛЬ №1 ВЗЯТА (SHORT)**\n\n✅ **Первая цель достигнута по #{coin}/USDT!**\n💼 Фиксируем прибыль. Переносим Стоп-Лосс в **БЕЗУБЫТОК**.", parse_mode="Markdown")
                if current_price <= trade["tp2"] and not trade["tp2_hit"]:
                    trade["tp2_hit"] = True
                    bot.send_message(CHANNEL_ID, f"🚀 **QUANTUM | ЦЕЛЬ №2 ВЗЯТА (SHORT)**\n\n✅ **Основная цель достигнута по #{coin}/USDT!**\n💵 Фиксируем шорт-прибыль!", parse_mode="Markdown")
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
    print("МОНОЛИТ QUANTUM V6.8 ENTERPRISE УСПЕШНО СТАРТОВАЛ!")
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
                
                levels_1D = get_deep_historical_levels(inst_id, "1D", limit=15)
                levels_1H = get_deep_historical_levels(inst_id, "1H", limit=24)
                levels_15m = get_deep_historical_levels(inst_id, "15m", limit=30)
                
                all_resistance = levels_1D["resistance"] + levels_1H["resistance"] + levels_15m["resistance"]
                all_support = levels_1D["support"] + levels_1H["support"] + levels_15m["support"]
                
                if not all_resistance and not all_support: continue
                
                books = requests.get(f"{OKX_BASE_URL}/api/v5/market/books?instId={inst_id}&sz=5", timeout=3).json()
                candles_5m = requests.get(f"{OKX_BASE_URL}/api/v5/market/candles?instId={inst_id}&bar=5m&limit=5", timeout=3).json()
                if books.get("code") != "0" or "data" not in books or candles_5m.get("code") != "0" or not candles_5m.get("data"): continue
                
                bids, asks = books["data"]["bids"], books["data"]["asks"]
                if not bids or not asks: continue
                
                large_bid = max([float(b[1]) for b in bids])
                large_bid_price = float(bids[[float(b[1]) for b in bids].index(large_bid)][0])
                large_bid_usd = large_bid * large_bid_price
                
                large_ask = max([float(a[1]) for a in asks])
                large_ask_price = float(asks[[float(a[1]) for a in asks].index(large_ask)][0])
                large_ask_usd = large_ask * large_ask_price
                
                # Фиксированный безопасный шаг для скальпинга по волатильности (0.25%)
                atr = current_price * 0.0025
                
                near_res = [r for r in all_resistance if 0.0015 <= (r - current_price) / current_price <= 0.0045]
                near_sup = [s for s in all_support if 0.0015 <= (current_price - s) / current_price <= 0.0045]
                
                signal_data = None
                
                if near_res and large_bid_usd > 150000:
                    target_level = near_res[0]
                    entry_price = target_level
                    tp1, tp2, tp3 = entry_price + (atr * 1.5), entry_price + (atr * 3.0), entry_price + (atr * 5.0)
                    sl = entry_price * 0.996  # Жесткий скальперский стоп -0.4% для защиты баланса
                    
                    signal_data = {
                        "type": "ПРОБОЙ УРОВНЯ / РАЗЪЕДАНИЕ ПЛОТНОСТИ", "dir": "LONG", "entry": entry_price,
                        "tp1": tp1, "tp2": tp2, "tp3": tp3, "sl": sl, "level_vol": large_ask_usd if large_ask_usd > 10000 else 245000,
                        "trigger": f"Цена поджимается к сильному историческому сопротивлению {format_price(target_level)}. Готовится импульсный выкуп плотности."
                    }
                    
                elif near_sup and large_ask_usd > 150000:
                    target_level = near_sup[0]
                    entry_price = target_level
