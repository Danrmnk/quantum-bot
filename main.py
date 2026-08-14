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
    """Умное динамическое форматирование цен для любых монет"""
    try:
        price_float = float(price)
        if price_float == 0: return "0.0"
        if price_float >= 100: return f"{price_float:.2f}"
        if price_float >= 1: return f"{price_float:.4f}".rstrip('0').rstrip('.')
        return f"{price_float:.8f}".rstrip('0').rstrip('.')
    except:
        return str(price)

def get_high_volume_markets():
    """Фильтр монет с суточным объемом строго более 100,000,000 USD"""
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
    """Сквозной анализ структуры рынка на глубину до 150 свечей"""
    try:
        url = f"{OKX_BASE_URL}/api/v5/market/candles?instId={inst_id}&bar={bar}&limit={limit}"
        res = requests.get(url, timeout=4).json()
        if res.get("code") != "0" or "data" not in res or len(res["data"]) < 20:
            return {"support": [], "resistance": []}
            
        highs = [float(c[2]) for c in res["data"]] # High свечи
        lows = [float(c[3]) for c in res["data"]]  # Low свечи
        
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

def check_active_trades():
    """Пошаговый трекинг результатов отложенных ордеров и лесенки целей"""
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
                    bot.send_message(CHANNEL_ID, f"🎯 **QUANTUM | ЦЕЛЬ №1 ВЗЯТА**\n\n✅ **Первая цель достигнута по #{coin}/USDT!**\n💼 Часть профита зафиксирована. Переносим Стоп-Лосс в **БЕЗУБЫТОК**.", parse_mode="Markdown")
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

def send_morning_greeting():
    """Утренний дайджест строго в 08:00 по Киеву с ТОП-3 movers рынка"""
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
                if item["instId"].endswith("-USDT-SWAP") and float(item.get("volCcy24h", 0)) > 100000000:
                    open_24h = float(item["sodUtc24h"])
                    last_price = float(item["last"])
                    change = ((last_price - open_24h) / open_24h) * 100 if open_24h > 0 else 0
                    sorted_movers.append({"coin": item["instId"].split("-")[0], "change": change})
            
            sorted_movers = sorted(sorted_movers, key=lambda x: abs(x["change"]), reverse=True)
            
            top_text = ""
            for i, m in enumerate(sorted_movers[:3]):
                top_text += f"{i+1}️⃣ #{m['coin']}: {m['change']:+.2f}%\n"
                
            msg = (
                f"☀️ **ДОБРОЕ УТРО, ТРЕЙДЕРЫ! | QUANTUM PRO V6.6** ☀️\n\n"
                f"📅 Дата: {kyiv_date.strftime('%d.%m.%Y')}\n"
                f"⏱ Время: 08:00 по Киеву 🇺🇦\n\n"
                f"🔥 **ТОП-3 активных волатильных пар на утро (Объем > $100M):**\n{top_text}\n"
                f"🤖 Сканер активен. Модули МТФ прочесали графики 1D/1H/15m.\n"
                f"📢 База отложенных ордеров (Pre-Entry) обновлена. Выставляйте лимитки заранее! Профитного дня! 🚀"
            )
            bot.send_message(CHANNEL_ID, msg, parse_mode="Markdown")
            last_morning_greeting = kyiv_date
    except:
        pass

def main():
    print("МОНОЛИТ QUANTUM V6.6 TRADINGVIEW EDITION УСПЕШНО СТАРТОВАЛ!")
    while True:
        try:
            send_morning_greeting()
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
                
