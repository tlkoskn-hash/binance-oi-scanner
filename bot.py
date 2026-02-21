import asyncio
import requests
import os
from datetime import datetime, timedelta, timezone
from collections import defaultdict

from telegram import (
    Update,
    ReplyKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
import websockets
import json
# ================== CONFIG ==================

print("### THIS IS WHILE TRUE VERSION ###")

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("BOT_TOKEN not set")

ALLOWED_USERS = set(
    int(x) for x in os.getenv("ALLOWED_USERS", "").split(",") if x.strip()
)

BINANCE = "https://fapi.binance.com"
UTC_PLUS_3 = timezone(timedelta(hours=3))

cfg = {
    "oi_period": 10,
    "oi_percent": 5.0,
    "chat_id": None,
}

oi_history = {}
market_data = {}

oi_signals_today = defaultdict(int)

scanner_running = False
ALL_SYMBOLS = []

# ================== BINANCE ==================

def get_all_usdt_symbols():
    try:
        r = requests.get(f"{BINANCE}/fapi/v1/exchangeInfo", timeout=10).json()
        if "symbols" not in r:
            print("exchangeInfo error:", r)
            return []

        symbols = [
            s["symbol"]
            for s in r["symbols"]
            if s["contractType"] == "PERPETUAL"
            and s["quoteAsset"] == "USDT"
            and s["status"] == "TRADING"
        ]

        return symbols

    except Exception as e:
        print("exchangeInfo failed:", e)
        return []


# ================== UI ==================

def keyboard():
    return ReplyKeyboardMarkup(
        [
            ["⏱ OI период", "📈 OI %"],
            ["📊 Статус"],
        ],
        resize_keyboard=True,
        is_persistent=True
    )


def status_text():
    now = datetime.now(UTC_PLUS_3).strftime("%H:%M:%S")
    return (
        "📊 <b>Binance Open Interest Screener</b>\n\n"
        "📈 <b>Рост OI</b>\n"
        f"• Период: {cfg['oi_period']} мин\n"
        f"• Процент: {cfg['oi_percent']}%\n\n"
        f"⏱ Обновлено: <i>{now} (UTC+3)</i>"
    )

# ================== COMMANDS ==================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS:
        return

    cfg["chat_id"] = update.effective_chat.id

    await update.message.reply_text(
        status_text(),
        parse_mode="HTML",
        reply_markup=keyboard(),
    )

# ================== TEXT INPUT ==================

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS:
        return

    text = update.message.text

    if text == "📊 Статус":
        await update.message.reply_text(status_text(), parse_mode="HTML")
        return

    mapping = {
        "⏱ OI период": "oi_period",
        "📈 OI %": "oi_percent",
    }

    if text in mapping:
        context.user_data["edit"] = mapping[text]
        await update.message.reply_text("Введите число:")
        return

    key = context.user_data.get("edit")
    if key:
        try:
            value = float(text)
            cfg[key] = int(value) if "period" in key else value
            context.user_data["edit"] = None
            await update.message.reply_text("✅ Сохранено", reply_markup=keyboard())
        except:
            await update.message.reply_text("❌ Введите число")


# ================== SCANNER LOOP ==================
async def scanner_loop():
    global scanner_running, ALL_SYMBOLS

    if scanner_running:
        return

    scanner_running = True
    print(">>> HYBRID SCANNER STARTED <<<")

    try:
        ALL_SYMBOLS = await asyncio.to_thread(get_all_usdt_symbols)
        print("Total USDT perpetual pairs:", len(ALL_SYMBOLS))

        if not ALL_SYMBOLS:
            return

        # ==========================================
        # 1️⃣ MARKET WEBSOCKETS (разбивка на чанки)
        # ==========================================

        SYMBOLS_PER_SOCKET = 250  # безопасно < 1024 stream

        symbol_chunks = [
            ALL_SYMBOLS[i:i + SYMBOLS_PER_SOCKET]
            for i in range(0, len(ALL_SYMBOLS), SYMBOLS_PER_SOCKET)
        ]

        async def run_market_socket(symbol_list):

            url = "wss://fstream.binance.com/ws"

            while True:
                try:
                    async with websockets.connect(url, ping_interval=20) as ws:
                        print(f"WS market connected ({len(symbol_list)} symbols)")

                        params = []
                        for s in symbol_list:
                            s = s.lower()
                            params.append(f"{s}@ticker")
                            params.append(f"{s}@markPrice")

                        # подписка батчами по 100
                        for i in range(0, len(params), 100):
                            chunk = params[i:i + 100]

                            await ws.send(json.dumps({
                                "method": "SUBSCRIBE",
                                "params": chunk,
                                "id": i
                            }))

                            await asyncio.sleep(0.2)

                        print("WS market subscribed chunk")

                        async for message in ws:
                            payload = json.loads(message)

                            event_type = payload.get("e")
                            symbol = payload.get("s")

                            if not symbol or not event_type:
                                continue

                            info = market_data.setdefault(symbol, {})

                            if event_type == "24hrTicker":
                                info["price"] = float(payload["c"])
                                info["volume"] = float(payload["q"])

                            elif event_type == "markPriceUpdate":
                                info["funding"] = float(payload["r"])

                except Exception as e:
                    print("WS MARKET ERROR:", e)
                    await asyncio.sleep(5)

        # ==========================================
        # 2️⃣ OPEN INTEREST LOOP (REST)
        # ==========================================

        async def oi_loop():

            while True:
                try:
                    now = datetime.now(UTC_PLUS_3)
                    window = timedelta(minutes=cfg["oi_period"])

                    for symbol in ALL_SYMBOLS:

                        r = await asyncio.to_thread(
                            requests.get,
                            f"{BINANCE}/fapi/v1/openInterest",
                            params={"symbol": symbol},
                            timeout=5
                        )

                        data = r.json()

                        if "openInterest" not in data:
                            continue

                        oi = float(data["openInterest"])

                        history = oi_history.setdefault(symbol, [])
                        history.append((now, oi))

                        # чистим историю по окну
                        history[:] = [
                            (t, o)
                            for t, o in history
                            if now - t <= window
                        ]

                        if len(history) >= 2:

                            old_oi = history[0][1]
                            if old_oi == 0:
                                continue

                            oi_pct = (oi - old_oi) / old_oi * 100

                            print(f"{symbol} OI change: {oi_pct:.3f}%")

                            if oi_pct >= cfg["oi_percent"] and cfg["chat_id"]:

                                info = market_data.get(symbol, {})

                                await send_signal_ws(
                                    symbol,
                                    oi_pct,
                                    info.get("price", 0),
                                    info.get("volume", 0),
                                    info.get("funding", 0),
                                    cfg["oi_period"],
                                )

                                history.clear()

                        await asyncio.sleep(0.05)  # защита от лимита

                    await asyncio.sleep(5)

                except Exception as e:
                    print("OI LOOP ERROR:", e)
                    await asyncio.sleep(5)

        # запуск нескольких market websocket + oi loop
        await asyncio.gather(
            *[asyncio.create_task(run_market_socket(chunk)) for chunk in symbol_chunks],
            oi_loop()
        )

    finally:
        scanner_running = False
# ================== SIGNAL ==================
async def send_signal_ws(symbol, oi_pct, price, volume, funding, period):
    today = datetime.now(UTC_PLUS_3).date()
    oi_signals_today[(symbol, today)] += 1
    count = oi_signals_today[(symbol, today)]

    link = f"https://www.coinglass.com/tv/Binance_{symbol}"

    funding_sign = "+" if funding >= 0 else ""

    msg = (
        f"🪙 <b><a href='{link}'>{symbol}</a></b>\n"
        f"📊 OI: <b>+{oi_pct:.2f}%</b>\n"
        f"💰 Цена: <b>{price}</b>\n"
        f"📦 Объём 24h: <b>{volume:,.0f}</b>\n"
        f"💸 Funding: <b>{funding_sign}{funding:.4%}</b>\n"
        f"⏱ Период: {period} мин\n"
        f"🔁 Сигнал 24h: {count}"
    )

    await app.bot.send_message(
        chat_id=cfg["chat_id"],
        text=msg,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )

# ================== MAIN ==================

async def on_startup(app):
    asyncio.create_task(scanner_loop())

app = ApplicationBuilder().token(TOKEN).post_init(on_startup).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

print(">>> BINANCE OI SCREENER RUNNING <<<")
app.run_polling()




















