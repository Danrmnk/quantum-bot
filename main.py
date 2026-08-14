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
            resistance_levels.append(float(c[2]))  # High свечи
            support_levels.append(float(c[3]))     # Low свечи
        return {"support": support_levels, "resistance": resistance_levels}
    except Exception:
        return {"support": [], "resistance": []}

# =====================================================================
# ФИНАЛЬНЫЙ СКАЛЬПИНГ-ДВИЖОК
# =====================================================================
def bot_loop():
    print("МОНОЛИТ QUANTUM V6.9 ENTERPRISE УСПЕШНО ЗАПУЩЕН!")
    while True:
        try:
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
                
                atr = current_price * 0.0025
                near_res = [r for r in all_resistance if 0.0015 <= (r - current_price) / current_price <= 0.0045]
                near_sup = [s for s in all_support if 0.0015 <= (current_price - s) / current_price <= 0.0045]
                
                signal_data = None
                
                if near_res and large_bid_usd > 150000:
                    target_level = near_res[0]
                    entry_price = target_level
                    tp1 = entry_price + (atr * 1.5)
                    tp2 = entry_price + (atr * 3.0)
                    tp3 = entry_price + (atr * 5.0)
                    sl = entry_price * 0.996  
                    
                    signal_data = {
                        "type": "ПРОБОЙ УРОВНЯ / РАЗЪЕДАНИЕ ПЛОТНОСТИ", "dir": "LONG", "entry": entry_price,
                        "tp1": tp1, "tp2": tp2, "tp3": tp3, "sl": sl, "level_vol": large_ask_usd,
                        "trigger": f"Цена поджимается к сильному сопротивлению {format_price(target_level)}. Ожидается выкуп плотности."
                    }
                    
                elif near_sup and large_ask_usd > 150000:
                    target_level = near_sup[0]
                    entry_price = target_level
                    tp1 = entry_price - (atr * 1.5)
                    tp2 = entry_price - (atr * 3.0)
                    tp3 = entry_price - (atr * 5.0)
                    sl = entry_price * 1.004  
                    
                    signal_data = {
                        "type": "ПРОБОЙ УРОВНЯ / РАЗЪЕДАНИЕ ПЛОТНОСТИ", "dir": "SHORT", "entry": entry_price,
                        "tp1": tp1, "tp2": tp2, "tp3": tp3, "sl": sl, "level_vol": large_bid_usd,
                        "trigger": f"Цена приблизилась к уровню поддержки {format_price(target_level)}. Ожидается пробой вниз."
                    }
                    
                if signal_data:
                    coin_clean = inst_id.split("-")[0]
                    profit_pct = round((abs(signal_data["tp1"] - signal_data["entry"]) / signal_data["entry"]) * 100, 2)
                    tv_chart_url = f"https://tradingview.com{coin_clean.lower()[:2]}/{coin_clean.lower()}usdt.png"
                    
                    signal_msg = (
                        f"📊 **QUANTUM | VIP ENTERPRISE SIGNAL 💎**\n\n"
                        f"🪙 Пара: #{coin_clean}/USDT\n"
                        f"Паттерн: **{signal_data['type']}**\n"
                        f"Направление: 🔥 **{signal_data['dir']}** 🔥\n\n"
                        f"📊 **ОБЪЕМНЫЙ АНАЛИЗ (SMART MONEY):**\n"
                        f"• Суточный объём монеты: `${round(vol_24h_usd / 1000000, 1)} Млн $`\n"
                        f"• Плотность капитала на уровне: `${signal_data['level_vol']:,.0f} USD` 💵\n\n"
                        f"⚠️ **ИНСТРУКЦИЯ ДЛЯ ВХОДА:**\n"
                        f"_{signal_data['trigger']}_\n"
                        f"👉 *Выставляйте ОТЛОЖЕННЫЙ ОРДЕР заранее по указанной цене!*\n\n"
                        f"📥 **ПЛАНИРУЕМАЯ ЦЕНА ВХОДА:** `{format_price(signal_data['entry'])}`\n\n"
                        f"🎯 **ЛЕСЕНКА ЗАКРЫТИЯ ЦЕЛЕЙ:**\n"
                        f"🎯 Цель 1: `{format_price(signal_data['tp1'])}` (+{profit_pct}%)\n"
                        f"🎯 Цель 2: `{format_price(signal_data['tp2'])}` (+{round(profit_pct*2, 2)}%)\n"
                        f"🎯 Цель 3: `{format_price(signal_data['tp3'])}` (+{round(profit_pct*3.3, 2)}%)\n\n"
                        f"🛡 **КАЧЕСТВЕННЫЙ СТОП-ЛОСС (СТРОГО -0.4% РИСКА):** `{format_price(signal_data['sl'])}`\n\n"
                        f"📈 **ЖИВОЙ ГРАФИК TRADINGVIEW:** [ОТКРЫТЬ В БРАУЗЕРЕ]({tv_chart_url})\n\n"
                        f"💡 *Ребята, строго соблюдайте правила риск-менеджмента! Заходите только в подтвержденные сделки и забирайте профит лесенкой!*"
                    )
                    
                    bot.send_message(CHANNEL_ID, signal_msg, parse_mode="Markdown")
                    cooldowns[inst_id] = time.time()
                    time.sleep(2)
            time.sleep(10)
        except Exception:
            time.sleep(10)

# =====================================================================
# ФИНАЛЬНЫЙ ОБМАН ПОРТА ДЛЯ RENDER (БЕСПЛАТНЫЙ WEB SERVICE)
# =====================================================================
class WebServerHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()
        self.wfile.write(b"QUANTUM BOT IS ALIVE!")

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), WebServerHandler)
    print(f"Заглушка веб-порта успешно открыта на порту {port}")
    server.serve_forever()

if __name__ == "__main__":
    # Запускаем фоновый веб-сервер, чтобы Render увидел открытый порт
    threading.Thread(target=run_web_server, daemon=True).start()
    # Запускаем основной движок сканера
    bot_loop()
