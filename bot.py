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
oi_signals_today = defaultdict(int)

scanner_running = False
ALL_SYMBOLS = []
BATCH_SIZE = 25
batch_index = 0


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

def get_open_interest(symbol: str):
    try:
        r = requests.get(
            f"{BINANCE}/fapi/v1/openInterest",
            params={"symbol": symbol},
            timeout=5,
        ).json()
        return float(r["openInterest"])
    except Exception:
        return None


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
    global scanner_running, ALL_SYMBOLS, batch_index

    if scanner_running:
        return

    scanner_running = True
    print(">>> OI scanner loop started <<<")

    try:
        # получаем список всех пар один раз
        ALL_SYMBOLS = await asyncio.to_thread(get_all_usdt_symbols)
        print("Total USDT perpetual pairs:", len(ALL_SYMBOLS))

        while True:
            try:
                if not cfg["chat_id"]:
                    await asyncio.sleep(1)
                    continue

                if not ALL_SYMBOLS:
                    await asyncio.sleep(5)
                    continue

                now = datetime.now(UTC_PLUS_3)
                window = timedelta(minutes=cfg["oi_period"])

                # формируем батч
                start = batch_index * BATCH_SIZE
                end = start + BATCH_SIZE
                batch = ALL_SYMBOLS[start:end]

                if not batch:
                    batch_index = 0
                    continue

                prices = await asyncio.to_thread(get_all_prices)

                for symbol in batch:

                    oi = await asyncio.to_thread(get_open_interest, symbol)
                    if oi is None:
                        continue

                    price = prices.get(symbol)
                    if not price:
                        continue

                    history = oi_history.setdefault(symbol, [])
                    history.append((now, oi, price))
                    history[:] = [
                        (t, o, p)
                        for t, o, p in history
                        if now - t <= window
                    ]

                    if len(history) >= 2:
                        old_oi = history[0][1]
                        old_price = history[0][2]

                        if old_oi == 0 or old_price == 0:
                            continue

                        oi_pct = (oi - old_oi) / old_oi * 100
                        price_pct = (price - old_price) / old_price * 100

                        if oi_pct >= cfg["oi_percent"]:
                            await send_signal(
                                symbol,
                                oi_pct,
                                price_pct,
                                cfg["oi_period"],
                            )
                            history.clear()

                batch_index += 1

                print(f"[OI] Проверен батч {batch_index}")

                await asyncio.sleep(10)

            except Exception as e:
                print("SCANNER LOOP ERROR:", e)
                await asyncio.sleep(5)

    finally:
        scanner_running = False


# ================== SIGNAL ==================

async def send_signal(symbol: str, oi_pct: float, price_pct: float, period: int):
    today = datetime.now(UTC_PLUS_3).date()
    oi_signals_today[(symbol, today)] += 1
    count = oi_signals_today[(symbol, today)]

    link = f"https://www.coinglass.com/tv/Binance_{symbol}"
    price_sign = "+" if price_pct >= 0 else ""

    msg = (
        f"🪙 <b><a href='{link}'>{symbol}</a></b>\n"
        f"📊 Рост OI: <b>+{oi_pct:.2f}%</b>\n"
        f"📈 Цена: <b>{price_sign}{price_pct:.2f}%</b>\n"
        f"⏱ Период: {period} мин\n"
        f"🔁 <b>Сигнал 24h:</b> {count}"
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



