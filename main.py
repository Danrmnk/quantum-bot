import os
import time
import requests
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')  # Переводим matplotlib в фоновый режим без экрана
import matplotlib.pyplot as plt
from datetime import datetime
import telebot

# ==========================================
# КОНФИГУРАЦИЯ И НАСТРОЙКИ
# ==========================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "ТВОЙ_ТЕЛЕГРАМ_ТОКЕН")
CHANNEL_ID = os.getenv("CHANNEL_ID", "ID_ТВОЕГО_КАНАЛА_ИЛИ_ЧАТА")

bot = telebot.TeleBot(TELEGRAM_TOKEN)
OKX_BASE_URL = "https://okx.com"

active_tracks = {}
last_morning_greeting = None

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

def generate_chart_photo(inst_id, current_price, tp, sl, strategy_type):
    """Генерирует профессиональное фото графика с уровнями прямо по центру"""
    try:
        # Запрашиваем 40 свечей для красивого отображения тренда
        url = f"{OKX_BASE_URL}/api/v5/market/candles?instId={inst_id}&bar=5m&limit=40"
        res = requests.get(url, timeout=5).json()
        if res.get("code") != "0" or "data" not in res: return None
        
        # Собираем данные в таблицу pandas и переворачиваем от старых к новым
        df = pd.DataFrame(res["data"], columns=['ts', 'open', 'high', 'low', 'close', 'vol', 'volCcy', 'volCcyQuote', 'confirm'])
        df = df.iloc[::-1].reset_index(drop=True)
        
        closes = df['close'].astype(float).tolist()
        times = [datetime.fromtimestamp(int(t)/1000).strftime('%H:%M') for t in df['ts'].tolist()]
        
        # Настройка стиля графика (Темная стильная тема как в TradingView)
        plt.style.use('dark_background')
        fig, ax = plt.subplots(figsize=(10, 5), dpi=150)
        
        # Рисуем основную линию цены монеты (плавная неоновая линия)
        ax.plot(times, closes, color='#00f0ff', label='Цена цены', linewidth=2, zorder=2)
        
        # --- ОТРИСОВКА ЧЁТКИХ УРОВНЕЙ КАСАНИЯ ---
        # Линия Входа (Маркет) — синяя пунктирная линия ровно по центру актуального движения
        ax.axhline(y=current_price, color='#3b82f6', linestyle='--', linewidth=1.5, label=f'ВХОД: {current_price}')
        
        # Линия Тейк-Профита (Зеленая жирная линия)
        ax.axhline(y=tp, color='#10b981', linestyle='-', linewidth=2.0, label=f'ЦЕЛЬ (TP): {round(tp, 4)}')
        
        # Линия Стоп-Лосса (Красная жирная линия)
        ax.axhline(y=sl, color='#ef4444', linestyle='-', linewidth=2.0, label=f'СТОП (SL): {round(sl, 4)}')
        
        # Текстовые подписи прямо на самом графике для максимальной четкости
        ax.text(len(times)-1, current_price, ' ВХОД', color='#3b82f6', fontsize=9, fontweight='bold', va='center')
        ax.text(len(times)-1, tp, ' TAKE PROFIT', color='#10b981', fontsize=10, fontweight='bold', va='center')
        ax.text(len(times)-1, sl, ' STOP LOSS', color='#ef4444', fontsize=10, fontweight='bold', va='center')
        
        # Красивое оформление сетки и осей
        coin_title = inst_id.split("-")[0]
        ax.set_title(f"QUANTUM SCANNER | {coin_title}/USDT (5m) | {strategy_type}", fontsize=12, fontweight='bold', color='#ffffff', pad=15)
        ax.grid(True, color='#262626', linestyle=':', linewidth=0.5)
        ax.legend(loc='upper left', framealpha=0.3)
        
        # Ограничиваем количество подписей времени снизу для читаемости
        plt.xticks(range(0, len(times), 5), times[::5], rotation=0)
        
        # Сохраняем получившийся рисунок в файл-фотографию
        file_path = f"{inst_id}_chart.png"
        plt.tight_layout()
        plt.savefig(file_path, bbox_inches='tight', facecolor='#171717')
        plt.close()
        
        return file_path
    except Exception as e:
        print(f"Ошибка генерации графика для {inst_id}: {e}")
        return None

def analyze_scapling_signals(market):
    inst_id = market["id"]
    current_price = market["last_price"]
    try:
        books_url = f"{OKX_BASE_URL}/api/v5/market/books?instId={inst_id}&sz=50"
        books = requests.get(books_url, timeout=3).json()
        candles_url = f"{OKX_BASE_URL}/api/v5/market/candles?instId={inst_id}&bar=5m&limit=20"
        candles = requests.get(candles_url, timeout=3).json()
        
        if books.get("code") != "0" or "data" not in books or candles.get("code") != "0" or "data" not in candles: return None

        bids, asks = books["data"]["bids"], books["data"]["asks"]
        if not bids or not asks: return None
            
        avg_bid_size = np.mean([float(b) for b in bids])
        avg_ask_size = np.mean([float(a) for a in asks])
        
        large_bid = max([float(b) for b in bids])
        large_bid_price = float(bids[np.argmax([float(b) for b in bids])])
        large_ask = max([float(a) for a in asks])
        large_ask_price = float(asks[np.argmax([float(a) for a in asks])])

        highs = [float(c) for c in candles["data"]]
        lows = [float(c) for c in candles["data"]]
        volumes = [float(c) for c in candles["data"]]
        
        max_high, min_low = max(highs[1:]), min(lows[1:])
        avg_volume, current_volume = np.mean(volumes[1:]), volumes

        # 1. ОТСКОК ОТ ПЛОТНОСТИ
        if large_bid > avg_bid_size * 5 and abs(current_price - large_bid_price) / current_price < 0.003:
            return {"type": "ОТСКОК ОТ ПЛОТНОСТИ (LIMIT)", "dir": "LONG", "trigger": f"Крупный лимитный покупатель ({int(large_bid)} лотов) на {large_bid_price}"}
        if large_ask > avg_ask_size * 5 and abs(large_ask_price - current_price) / current_price < 0.003:
            return {"type": "ОТСКОК ОТ ПЛОТНОСТИ (LIMIT)", "dir": "SHORT", "trigger": f"Крупный лимитный продавец ({int(large_ask)} лотов) на {large_ask_price}"}

        # 2. ПРОБОЙ УРОВНЯ
        if current_price >= max_high and current_volume > avg_volume * 2.5:
            return {"type": "ПРОБОЙ ЛОКАЛЬНОГО ХАЯ (BREAKOUT)", "dir": "LONG", "trigger": f"Импульсный прорыв сопротивления {max_high} на объемах"}
        if current_price <= min_low and current_volume > avg_volume * 2.5:
            return {"type": "ПРОБОЙ ЛОКАЛЬНОГО ЛОЯ (BREAKOUT)", "dir": "SHORT", "trigger": f"Импульсный прорыв поддержки {min_low} на объемах"}

        # 3. ИМПУЛЬСНЫЙ СКВИЗ (ПОДБОР НОЖЕЙ)
        c_open, c_close = float(candles["data"]), float(candles["data"])
        last_candle_change = abs(c_close - c_open) / c_open
        if last_candle_change > 0.015 and current_volume > avg_volume * 3:
            if c_close > c_open:
                return {"type": "ЛОПНУВШИЙ ИМПУЛЬС (СКВИЗ)", "dir": "SHORT", "trigger": f"Взлет цены на {round(last_candle_change*100, 1)}% за 5 минут. Ловим откат."}
            else:
                return {"type": "ПОДБОР НОЖА (СКВИЗ)", "dir": "LONG", "trigger": f"Пролив цены на {round(last_candle_change*100, 1)}% за 5 минут. Ловим технический отскок."}
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
            
            current_price = float(res["data"]["last"])
            direction = trade["direction"]
            coin_clean = inst_id.split("-")[0]
            
            is_tp = current_price >= trade["tp"] if direction == "LONG" else current_price <= trade["tp"]
            is_sl = current_price <= trade["sl"] if direction == "LONG" else current_price >= trade["sl"]
                
            if is_tp:
                msg = f"🎯 **QUANTUM | ОТЧЕТ ПО СДЕЛКЕ**\n\n✅ **СДЕЛКА ЗАКРЫЛАСЬ В ПЛЮС (ТЕЙК ВЗЯТ)!**\n🪙 Монета: #{coin_clean}/USDT\n📈 Направление: {direction}\n💵 Прибыль: +{trade['profit_pct']}%"
                bot.send_message(CHANNEL_ID, msg, parse_mode="Markdown")
                del active_tracks[inst_id]
            elif is_sl:
                msg = f"🛑 **QUANTUM | ОТЧЕТ ПО СДЕЛКЕ**\n\n❌ **СДЕЛКА ЗАКРЫЛАСЬ В МИНУС (СТОП-ЛОСС).**\n🪙 Монета: #{coin_clean}/USDT\n📉 Направление: {direction}\n📋 Риски соблюдены."
                bot.send_message(CHANNEL_ID, msg, parse_mode="Markdown")
                del active_tracks[inst_id]
        except:
            pass

def main():
    print("БРОНЕБОЙНЫЙ СКАЛЬПЕР QUANTUM V5.4 ULTRA ЗАПУЩЕН!")
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
                        
                    # 🚀 ШАГ 1: Генерируем изображение графика
                    photo_file = generate_chart_photo(inst_id, current_price, tp, sl, signal["type"])
