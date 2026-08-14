import os
import time
import requests
from datetime import datetime
import telebot

# ==========================================
# КОНФИГУРАЦИЯ И БЕЗОПАСНОСТЬ
# ==========================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "ТВОЙ_ТЕЛЕГРАМ_ТОКЕН")
CHANNEL_ID = os.getenv("CHANNEL_ID", "ID_ТВОЕГО_КАНАЛА_ИЛИ_ЧАТА")

bot = telebot.TeleBot(TELEGRAM_TOKEN)
OKX_BASE_URL = "https://okx.com"

active_tracks = {}

def get_high_volume_markets():
    """Фильтр Smart-Money: ТОЛЬКО топ-монеты с объемом от 100 млн $ за сутки"""
    try:
        url = f"{OKX_BASE_URL}/api/v5/market/tickers?instType=SWAP"
        response = requests.get(url, timeout=5).json()
        if response.get("code") != "0" or "data" not in response: 
            return []
        
        valid_instruments = []
        for ticker in response["data"]:
            inst_id = ticker["instId"]
            if not inst_id.endswith("-USDT-SWAP"): 
                continue
            
            vol_usd = float(ticker.get("volCcy24h", 0))
            if vol_usd >= 100000000:  # Строго от 100 000 000 долларов
                valid_instruments.append({
                    "id": inst_id,
                    "last_price": float(ticker["last"]),
                    "vol_24h": vol_usd
                })
        return valid_instruments
    except:
        return []

def analyze_scapling_signals(market):
    """ЯДРО СКАЛЬПИНГА: Чистый Python без внешних тяжелых библиотек"""
    inst_id = market["id"]
    current_price = market["last_price"]
    coin_clean = inst_id.split("-")[0]
    
    try:
        # Запрашиваем стакан (глубина 50 ордеров)
        books_url = f"{OKX_BASE_URL}/api/v5/market/books?instId={inst_id}&sz=50"
        books = requests.get(books_url, timeout=3).json()
        
        # Запрашиваем последние 20 свечей 5м
        candles_url = f"{OKX_BASE_URL}/api/v5/market/candles?instId={inst_id}&bar=5m&limit=20"
        candles = requests.get(candles_url, timeout=3).json()
        
        if books.get("code") != "0" or "data" not in books or not books["data"]: return None
        if candles.get("code") != "0" or "data" not in candles or len(candles["data"]) < 5: return None

        bids = books["data"][0]["bids"]
        asks = books["data"][0]["asks"]
        if not bids or not asks: return None
            
        # Считаем средние объемы вручную (без numpy)
        avg_bid_size = sum([float(b[1]) for b in bids]) / len(bids)
        avg_ask_size = sum([float(a[1]) for a in asks]) / len(asks)
        
        # Поиск крупных заявок в стакане
        bid_sizes = [float(b[1]) for b in bids]
        large_bid = max(bid_sizes)
        large_bid_price = float(bids[bid_sizes.index(large_bid)][0])
        
        ask_sizes = [float(a[1]) for a in asks]
        large_ask = max(ask_sizes)
        large_ask_price = float(asks[ask_sizes.index(large_ask)][0])

        # Анализ уровней по свечам
        highs = [float(c[2]) for c in candles["data"]]
        lows = [float(c[3]) for c in candles["data"]]
        volumes = [float(c[5]) for c in candles["data"]]
        
        max_high = max(highs[1:])
        min_low = min(lows[1:])
        avg_volume = sum(volumes[1:]) / len(volumes[1:])
        current_volume = volumes[0]

        # --- СТРАТЕГИИ СКАЛЬПИНГА ---
        
        # 1. ОТСКОК ОТ ПЛОТНОСТИ
        if large_bid > avg_bid_size * 5 and abs(current_price - large_bid_price) / current_price < 0.003:
            return {"type": "ОТСКОК ОТ ПЛОТНОСТИ (LIMIT)", "dir": "LONG", "trigger": f"Крупная лимитная заявка на покупку ({int(large_bid)} лотов) возле цены на уровне {large_bid_price}"}
        if large_ask > avg_ask_size * 5 and abs(large_ask_price - current_price) / current_price < 0.003:
            return {"type": "ОТСКОК ОТ ПЛОТНОСТИ (LIMIT)", "dir": "SHORT", "trigger": f"Крупная лимитная заявка на продажу ({int(large_ask)} лотов) возле цены на уровне {large_ask_price}"}

        # 2. ПРОБОЙ ЛОКАЛЬНОГО УРОВНЯ
        if current_price >= max_high and current_volume > avg_volume * 2.5:
            return {"type": "ПРОБОЙ ЛОКАЛЬНОГО ХАЯ (BREAKOUT)", "dir": "LONG", "trigger": f"Импульсный прорыв горизонтального сопротивления {max_high} на повышенных объемах"}
        if current_price <= min_low and current_volume > avg_volume * 2.5:
            return {"type": "ПРОБОЙ ЛОКАЛЬНОГО ЛОЯ (BREAKOUT)", "dir": "SHORT", "trigger": f"Импульсный прорыв горизонтальной поддержки {min_low} на повышенных объемах"}

        # 3. ПОДБОР НОЖЕЙ (СКВИЗЫ)
        c_open = float(candles["data"][0][1])
        c_close = float(candles["data"][0][4])
        last_candle_change = abs(c_close - c_open) / c_open
        
        if last_candle_change > 0.015 and current_volume > avg_volume * 3:
            if c_close > c_open:
                return {"type": "ЛОПНУВШИЙ ИМПУЛЬС (СКВИЗ)", "dir": "SHORT", "trigger": f"Резкий вертикальный взлет цены на {round(last_candle_change*100, 1)}% за 5 минут. Входим на откат."}
            else:
                return {"type": "ПОДБОР НОЖА (СКВИЗ)", "dir": "LONG", "trigger": f"Панический вертикальный пролив цены на {round(last_candle_change*100, 1)}% за 5 минут. Ловим отскок."}

        return None
    except:
        return None

def check_active_trades():
    global active_tracks
    for inst_id, trade in list(active_tracks.items()):
        try:
            ticker_url = f"{OKX_BASE_URL}/api/v5/market/ticker?instId={inst_id}"
            res = requests.get(ticker_url, timeout=3).json()
            if res.get("code") != "0" or "data" not in res: continue
            
            current_price = float(res["data"][0]["last"])
            direction = trade["direction"]
            coin_clean = inst_id.split("-")[0]
            
            is_tp = current_price >= trade["tp"] if direction == "LONG" else current_price <= trade["tp"]
            is_sl = current_price <= trade["sl"] if direction == "LONG" else current_price >= trade["sl"]
                
            if is_tp:
                msg = f"🎯 **QUANTUM | ОТЧЕТ ПО СДЕЛКЕ**\n\n✅ **СДЕЛКА ЗАКРЫЛАСЬ В ПЛЮС (ТЕЙК ВЗЯТ)!**\n🪙 Монета: #{coin_clean}/USDT\n📈 Направление: {direction}\n💵 Чистая прибыль: +{trade['profit_pct']}%"
                bot.send_message(CHANNEL_ID, msg, parse_mode="Markdown")
                del active_tracks[inst_id]
            elif is_sl:
                msg = f"🛑 **QUANTUM | ОТЧЕТ ПО СДЕЛКЕ**\n\n❌ **СДЕЛКА ЗАКРЫЛАСЬ В МИНУС (СТОП-ЛОСС).**\n🪙 Монета: #{coin_clean}/USDT\n📉 Направление: {direction}\n📋 Риски соблюдены. Робот ищет новую ТВХ."
                bot.send_message(CHANNEL_ID, msg, parse_mode="Markdown")
                del active_tracks[inst_id]
        except:
            pass

def main():
    print("СКАЛЬПИНГ-ЯДРО QUANTUM V5.5 LIGHT УСПЕШНО ЗАПУЩЕНО!")
    while True:
        try:
            check_active_trades()
            markets = get_high_volume_markets()
            for market in markets:
                inst_id = market["id"]
                if inst_id in active_tracks: continue
                
                signal = analyze_scapling_signals(market)
                if signal:
                    current_price = market["last_price"]
                    coin_clean = inst_id.split("-")[0]
                    
                    if signal["dir"] == "LONG":
                        tp, sl = current_price * 1.008, current_price * 0.996
                    else:
                        tp, sl = current_price * 0.992, current_price * 1.004
                        
                    signal_msg = (
                        f"⚡️ **QUANTUM | VIP SCALPER SIGNAL 💎**\n\n"
                        f"🪙 Пара: #{coin_clean}/USDT\n"
                        f"Тип сигнала: **{signal['type']}**\n"
                        f"Направление: 🔥 **{signal['dir']}** 🔥\n\n"
                        f"🚨 **Причина входа (Триггер):**\n_{signal['trigger']}_\n\n"
                        f"📥 **ТОЧКА ВХОДА (МАРКЕТ):** `{current_price}`\n"
                        f"🎯 Тейк-Профит: `{round(tp, 4)}` (+0.8%)\n"
                        f"🛡 Стоп-Лосс: `{round(sl, 4)}` (-0.4%)"
                    )
                    
                    bot.send_message(CHANNEL_ID, signal_msg, parse_mode="Markdown")
                    active_tracks[inst_id] = {"direction": signal["dir"], "entry": current_price, "tp": tp, "sl": sl, "profit_pct": 0.8}
                    time.sleep(1)
            time.sleep(5)
        except Exception as e:
            time.sleep(5)

if __name__ == "__main__":
    main()
