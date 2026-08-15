import os
import time
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import requests
import telebot

# =====================================================================
# НАСТРОЙКИ И ИНИЦИАЛИЗАЦИЯ
# =====================================================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "ТВОЙ_ТЕЛЕГРАМ_ТОКЕН")
CHANNEL_ID = os.getenv("CHANNEL_ID", "ID_ТВОЕГО_КАНАЛА_ИЛИ_ЧАТА")

bot = telebot.TeleBot(TELEGRAM_TOKEN)
OKX_BASE_URL = "https://okx.com"
cooldowns = {}

def format_price(price):
    try:
        p = float(price)
        if p == 0: return "0.0"
        if p >= 100: return f"{p:.2f}"
        if p >= 1: return f"{p:.4f}".rstrip('0').rstrip('.')
        return f"{p:.8f}".rstrip('0').rstrip('.')
    except:
        return str(price)

# =====================================================================
# ГЛАВНЫЙ СКАЛЬПИНГ-ДВИЖОК (БЕЗ СЛОЖНЫХ МАССИВОВ)
# =====================================================================
def bot_loop():
    print("МОНОЛИТ QUANTUM V10.0 СТАРТОВАЛ УСПЕШНО!")
    while True:
        try:
            url = f"{OKX_BASE_URL}/api/v5/market/tickers?instType=SWAP"
            res = requests.get(url, timeout=5).json()
            if res.get("code") != "0" or "data" not in res:
                time.sleep(10)
                continue
                
            for m in res["data"]:
                inst_id = m["instId"]
                if not inst_id.endswith("-USDT-SWAP"): continue
                if inst_id in cooldowns and (time.time() - cooldowns[inst_id]) < 2700: continue
                
                vol_usd = float(m.get("volCcy24h", 0))
                if vol_usd < 60000000: continue  # Фильтр объема строго от 60 млн $
                
                price = float(m["last"])
                high = float(m["high24h"])
                low = float(m["low24h"])
                
                signal_type = None
                direction = None
                target = 0
                
                # Поиск Pre-Entry поджатия к экстремумам дня (дистанция от 0.2% до 0.8%)
                if 0.0020 <= (high - price) / price <= 0.0080:
                    signal_type = "ПОДЖАТИЕ К УРОВНЮ СОПРОТИВЛЕНИЯ (ХАЙ ДНЯ)"
                    direction = "LONG"
                    target = high
                elif 0.0020 <= (price - low) / price <= 0.0080:
                    signal_type = "ПОДЖАТИЕ К УРОВНЮ ПОДДЕРЖКИ (ЛОУ ДНЯ)"
                    direction = "SHORT"
                    target = low
                    
                if signal_type and target > 0:
                    coin = inst_id.split("-")[0]
                    tv_chart = f"https://tradingview.com{coin.lower()[:2]}/{coin.lower()}usdt.png"
                    
                    if direction == "LONG":
                        tp1, tp2, tp3 = target * 1.015, target * 1.030, target * 1.050
                        sl = target * 0.990  # Стоп ровно 1%
                        trig = f"Цена сформировала плотное поджатие к максимуму суток {format_price(target)}. Ожидается пробой сопротивления вверх."
                    else:
                        tp1, tp2, tp3 = target * 0.985, target * 0.970, target * 0.950
                        sl = target * 1.010  # Стоп ровно 1%
                        trig = f"Цена тестирует пробой главного минимума суток {format_price(target)} вниз на объемах."
                        
                    signal_msg = (
                        f"📊 **QUANTUM | PREMIUM VIP SIGNAL** 💎\n\n"
                        f"🪙 **Пара:** #{coin}/USDT\n"
                        f"📊 **Паттерн:** {signal_type}\n"
                        f"🧭 **Направление:** 🔥 **{direction}** 🔥\n\n"
                        f"📊 **ОБЪЕМНЫЙ АНАЛИЗ (SMART MONEY):**\n"
                        f"• Суточный объём монеты: `${round(vol_usd / 1000000, 1)} Млн $`\n"
                        f"• Плотность капитала на уровне: НАБРАНА ПО РЫНКУ 💵\n\n"
                        f"⚠️ **ИНСТРУКЦИЯ ДЛЯ ВХОДА (УСПЕЮТ ВСЕ):**\n"
                        f"_{trig}_\n"
                        f"👉 *Зайдите на биржу и выставьте ОТЛОЖЕННЫЙ ОРДЕР (Stop-Market или Лимит) заранее по указанной цене входа!*\n\n"
                        f"📥 **ПЛАНИРУЕМАЯ ЦЕНА ВХОДА:** `{format_price(target)}`\n\n"
                        f"🎯 **ЛЕСЕНКА ЗАКРЫТИЯ ЦЕЛЕЙ (TAKE-POINTS):**\n"
                        f"├ 🎯 Цель 1: `{format_price(tp1)}` (+1.50%)\n"
                        f"├ 🚀 Цель 2: `{format_price(tp2)}` (+3.00%)\n"
                        f"└ 🏆 Цель 3: `{format_price(tp3)}` (+5.00%)\n\n"
                        f"🛡 **ТЕХНИЧЕСКИЙ СТОП-ЛОСС (ОТ ЗАКОЛОВ):** `{format_price(sl)}` (**СТРОГО -1.0% РИСКА**)\n\n"
                        f"📈 **ЖИВОЙ ГРАФИК TRADINGVIEW:** [ОТКРЫТЬ В БРАУЗЕРЕ]({tv_chart})\n\n"
                        f"💡 *Ребята, строго соблюдайте правила риск-менеджмента! Заходите только в подтвержденные сделки и забирайте профит лесенкой!*"
                    )
                    
                    bot.send_message(CHANNEL_ID, signal_msg, parse_mode="Markdown")
                    cooldowns[inst_id] = time.time()
                    time.sleep(2)
            time.sleep(15)
        except:
            time.sleep(10)

# =====================================================================
# ВЕБ-ЗАГЛУШКА ДЛЯ БЕСПЛАТНОГО ТАРИФА RENDER
# =====================================================================
class SimpleWebHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"QUANTUM V10 ACTIVE")
    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), SimpleWebHandler).serve_forever()

if __name__ == "__main__":
    threading.Thread(target=run_web_server, daemon=True).start()
    bot_loop()
