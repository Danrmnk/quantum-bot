import os
import time
import requests
from datetime import datetime
import telebot

# ==========================================
# КОНФИГУРАЦИЯ
# ==========================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "ТВОЙ_ТЕЛЕГРАМ_ТОКЕН")
CHANNEL_ID = os.getenv("CHANNEL_ID", "ID_ТВОЕГО_КАНАЛА_ИЛИ_ЧАТА")

bot = telebot.TeleBot(TELEGRAM_TOKEN)
OKX_BASE_URL = "https://okx.com"

active_tracks = {}
cooldowns = {}  # Хранилище кулдаунов монет, чтобы не спамить

def format_price(price):
    """Умное форматирование цен для любых монет"""
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
            if vol_usd >= 100000000:  # Фильтр объема от 100 млн $
                valid_instruments.append({
                    "id": inst_id,
                    "last_price": float(ticker["last"]),
                    "vol_24h": vol_usd
                })
        return valid_instruments
    except:
        return []

def analyze_scapling_signals(market):
    inst_id = market["id"]
    current_price = market["last_price"]
    
    # Проверка кулдауна: если по монете был сигнал меньше 30 минут назад — пропускаем
    if inst_id in cooldowns and (time.time() - cooldowns[inst_id]) < 1800:
        return None
        
    try:
        books_url = f"{OKX_BASE_URL}/api/v5/market/books?instId={inst_id}&sz=50"
        books = requests.get(books_url, timeout=3).json()
        
        candles_url = f"{OKX_BASE_URL}/api/v5/market/candles?instId={inst_id}&bar=5m&limit=20"
        candles = requests.get(candles_url, timeout=3).json()
        
        if books.get("code") != "0" or "data" not in books or candles.get("code") != "0" or len(candles["data"]) < 10: return None

        bids, asks = books["data"]["bids"], books["data"]["asks"]
        if not bids or not asks: return None
            
        avg_bid_size = sum([float(b) for b in bids]) / len(bids)
        avg_ask_size = sum([float(a) for a in asks]) / len(asks)
        
        large_bid = max([float(b) for b in bids])
        large_bid_price = float(bids[[float(b) for b in bids].index(large_bid)])
        large_ask = max([float(a) for a in asks])
        large_ask_price = float(asks[[float(a) for a in asks].index(large_ask)])

        highs = [float(c) for c in candles["data"]]
        lows = [float(c) for c in candles["data"]]
        volumes = [float(c) for c in candles["data"]]
        
        max_high, min_low = max(highs[1:]), min(lows[1:])
        avg_volume, current_volume = sum(volumes[1:]) / len(volumes[1:]), volumes

        # Расчет среднего хода цены (ATR) для стопов и лесенки целей
        changes = [abs(float(c)-float(c)) for c in candles["data"]]
        atr = sum(changes) / len(changes)
        if atr == 0: atr = current_price * 0.004

        # 1. СТРАТЕГИЯ: ОТСКОК ОТ ПЛОТНОСТИ
        if large_bid > avg_bid_size * 5 and abs(current_price - large_bid_price) / current_price < 0.002:
            return {
                "type": "ОТСКОК ОТ ПЛОТНОСТИ (LIMIT)", "dir": "LONG", 
                "trigger": f"Крупный лимитный покупатель ({int(large_bid)} лотов) на уровне {format_price(large_bid_price)}",
                "sl": large_bid_price - (atr * 0.5), "atr": atr
            }
        if large_ask > avg_ask_size * 5 and abs(large_ask_price - current_price) / current_price < 0.002:
            return {
                "type": "ОТСКОК ОТ ПЛОТНОСТИ (LIMIT)", "dir": "SHORT", 
                "trigger": f"Крупный лимитный продавец ({int(large_ask)} лотов) на уровне {format_price(large_ask_price)}",
                "sl": large_ask_price + (atr * 0.5), "atr": atr
            }

        # 2. СТРАТЕГИЯ: ПРОБОЙ УРОВНЯ (BREAKOUT)
        if current_price >= max_high and current_volume > avg_volume * 2.5:
            return {
                "type": "ПРОБОЙ ЛОКАЛЬНОГО ХАЯ (BREAKOUT)", "dir": "LONG", 
                "trigger": f"Импульсный прорыв сопротивления {format_price(max_high)} на объемах",
                "sl": min_low, "atr": atr
            }
        if current_price <= min_low and current_volume > avg_volume * 2.5:
            return {
                "type": "ПРОБОЙ ЛОКАЛЬНОГО ЛОЯ (BREAKOUT)", "dir": "SHORT", 
                "trigger": f"Импульсный прорыв поддержки {format_price(min_low)} на объемах",
                "sl": max_high, "atr": atr
            }

        return None
    except:
        return None

def main():
    print("БРОНЕБОЙНЫЙ VIP СКАНЕР ЗАПУЩЕН!")
    while True:
        try:
            markets = get_high_volume_markets()
            for market in markets:
                inst_id = market["id"]
                if inst_id in active_tracks: continue
                
                signal = analyze_scapling_signals(market)
                if signal:
                    current_price = market["last_price"]
                    coin_clean = inst_id.split("-")[0]
                    atr = signal["atr"]
                    
                    # Расчет профессиональной ЛЕСЕНКИ ТЕЙКОВ (Цели 1, 2, 3)
                    if signal["dir"] == "LONG":
                        tp1 = current_price + (atr * 1.5)
                        tp2 = current_price + (atr * 3.0)
                        tp3 = current_price + (atr * 5.0)
                        sl = min(signal["sl"], current_price * 0.993) # Стоп за уровень, но не более 0.7%
                    else:
                        tp1 = current_price - (atr * 1.5)
                        tp2 = current_price - (atr * 3.0)
                        tp3 = current_price - (atr * 5.0)
                        sl = max(signal["sl"], current_price * 1.007)

                    # Формируем КРАСИВЫЙ СИГНАЛ С ЛЕСЕНКОЙ ЦЕЛЕЙ
                    signal_msg = (
                        f"📊 **QUANTUM | VIP SCANNER 💎**\n\n"
                        f"🪙 Пара: #{coin_clean}/USDT\n"
                        f"Тип: **{signal['type']}**\n"
                        f"Направление: 🔥 **{signal['dir']}** 🔥\n\n"
                        f"🚨 **Триггер:** _{signal['trigger']}_\n\n"
                        f"📥 **ЦЕНА ВХОДА (МАРКЕТ):** `{format_price(current_price)}`\n\n"
                        f"🎯 **ЛЕСЕНКА ЦЕЛЕЙ (TAKE-POINTS):**\n"
                        f"🎯 🎯 Цель 1: `{format_price(tp1)}` (Ближняя)\n"
                        f"🎯 🎯 Цель 2: `{format_price(tp2)}` (Основная)\n"
                        f"🎯 🎯 Цель 3: `{format_price(tp3)}` (Максимум)\n\n"
                        f"🛡 **СТОП-ЛОСС (ЗА УРОВЕНЬ):** `{format_price(sl)}`"
                    )
                    
                    bot.send_message(CHANNEL_ID, signal_msg, parse_mode="Markdown")
                    
                    # Включаем кулдаун на 30 минут, чтобы бот не спамил этой монетой
                    cooldowns[inst_id] = time.time()
                    
                    # Записываем в трекинг (отслеживаем по первой цели)
                    active_tracks[inst_id] = {"direction": signal["dir"], "tp": tp1, "sl": sl}
                    time.sleep(2)
            time.sleep(10)
        except Exception as e:
            time.sleep(10)

if __name__ == "__main__":
    main()
