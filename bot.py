import asyncio
import requests
import os
from datetime import datetime, timedelta, timezone

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ================== CONFIG ==================

TOKEN = os.getenv("8234883658:AAEmjnP-bEskeGMhs7CvSXow2iAxZPZWhm0")
ALLOWED_USERS = {1128293345}

BINANCE = "https://fapi.binance.com"

UTC_PLUS_3 = timezone(timedelta(hours=3))

cfg = {
    "oi_period": 10,      # minutes
    "oi_percent": 5.0,    # %
    "enabled": False,
    "chat_id": None,
}

# symbol -> list[(timestamp, oi)]
oi_history = {}

scanner_running = False

SYMBOLS_CACHE = []
LAST_SYMBOL_UPDATE = None

# ================== BINANCE ==================

def get_symbols():
    global SYMBOLS_CACHE, LAST_SYMBOL_UPDATE

    if SYMBOLS_CACHE and LAST_SYMBOL_UPDATE:
        if datetime.now() - LAST_SYMBOL_UPDATE < timedelta(hours=1):
            return SYMBOLS_CACHE

    r = requests.get(f"{BINANCE}/fapi/v1/exchangeInfo", timeout=10).json()
    SYMBOLS_CACHE = [
        s["symbol"]
        for s in r["symbols"]
        if s["quoteAsset"] == "USDT" and s["status"] == "TRADING"
    ]
    LAST_SYMBOL_UPDATE = datetime.now()
    return SYMBOLS_CACHE


def get_open_interest(symbol: str) -> float:
    r = requests.get(
        f"{BINANCE}/fapi/v1/openInterest",
        params={"symbol": symbol},
        timeout=5,
    ).json()
    return float(r["openInterest"])

# ================== UI ==================

def keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⏱ OI период", callback_data="oi_period"),
            InlineKeyboardButton("📈 OI %", callback_data="oi_percent"),
        ],
        [
            InlineKeyboardButton("📊 Статус", callback_data="status"),
        ],
        [
            InlineKeyboardButton("▶️ ВКЛ", callback_data="on"),
            InlineKeyboardButton("⛔ ВЫКЛ", callback_data="off"),
        ],
    ])


def status_text():
    now = datetime.now(UTC_PLUS_3).strftime("%H:%M:%S")
    return (
        "📊 <b>Binance Open Interest Screener</b>\n\n"
        f"▶️ Включен: <b>{cfg['enabled']}</b>\n\n"
        "📈 <b>Рост OI</b>\n"
        f"• Период: {cfg['oi_period']} мин\n"
        f"• Процент: {cfg['oi_percent']}%\n\n"
        f"🕒 Рынок обновлен: <i>{now} (UTC+3)</i>"
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

# ================== BUTTONS ==================

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    action = q.data

    if action == "on":
        cfg["enabled"] = True

    elif action == "off":
        cfg["enabled"] = False

    elif action == "status":
        pass

    else:
        context.user_data["edit"] = action
        await q.message.reply_text(
            f"Введи значение для <b>{action}</b>",
            parse_mode="HTML",
        )
        return

    text = status_text()
    if q.message.text != text:
        await q.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard(),
        )

# ================== TEXT INPUT ==================

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = context.user_data.get("edit")
    if not key:
        return

    try:
        value = float(update.message.text)
    except ValueError:
        await update.message.reply_text("❌ Введи число")
        return

    if "period" in key:
        cfg[key] = int(value)
    else:
        cfg[key] = value

    context.user_data["edit"] = None

    await update.message.reply_text(
        "✅ Сохранено",
        reply_markup=keyboard(),
    )

# ================== SCANNER ==================

async def scanner_loop():
    global scanner_running

    if scanner_running:
        return

    scanner_running = True

    try:
        while True:
            if not cfg["enabled"] or not cfg["chat_id"]:
                await asyncio.sleep(1)
                continue

            symbols = get_symbols()
            now = datetime.now()

            window = timedelta(minutes=cfg["oi_period"])

            for symbol in symbols:
                oi = get_open_interest(symbol)

                history = oi_history.setdefault(symbol, [])
                history.append((now, oi))

                # clean old data
                history[:] = [(t, v) for t, v in history if now - t <= window]

                if len(history) < 2:
                    continue

                old_time, old_oi = history[0]
                pct = (oi - old_oi) / old_oi * 100

                if pct >= cfg["oi_percent"]:
                    await send_signal(symbol, pct, cfg["oi_period"])
                    history.clear()

                await asyncio.sleep(0.05)

            await asyncio.sleep(10)  # минимальный опрос Binance

    finally:
        scanner_running = False

# ================== SIGNAL ==================

async def send_signal(symbol: str, pct: float, period: int):
    msg = (
        "📈 <b>OPEN INTEREST РАСТЕТ</b>\n\n"
        f"🪙 <b>{symbol}</b>\n"
        f"📊 Рост OI: <b>{pct:.2f}%</b>\n"
        f"⏱ Период: {period} мин"
    )

    await app.bot.send_message(
        chat_id=cfg["chat_id"],
        text=msg,
        parse_mode="HTML",
    )

# ================== MAIN ==================

async def on_startup(app):
    asyncio.create_task(scanner_loop())

app = ApplicationBuilder().token(TOKEN).post_init(on_startup).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CallbackQueryHandler(button))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

print(">>> BINANCE OI SCREENER RUNNING <<<")
app.run_polling()
