import asyncio
import requests
import os
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

# ================== НАСТРОЙКИ ==================

TOKEN = os.getenv("8234883658:AAEmjnP-bEskeGMhs7CvSXow2iAxZPZWhm0")
ALLOWED_USERS = {1128293345}

BINANCE = "https://fapi.binance.com"

cfg = {
    "oi_period": 10,     # минут
    "oi_percent": 5.0,   # %
    "enabled": False,
    "chat_id": None,
}

OI_POLL_INTERVAL = 15  # секунд (частота опроса Binance)

oi_history = {}        # symbol -> [(timestamp, oi)]
scanner_running = False

# ================== BINANCE ==================

def get_symbols():
    r = requests.get(f"{BINANCE}/fapi/v1/exchangeInfo", timeout=10).json()
    return [
        s["symbol"]
        for s in r["symbols"]
        if s["quoteAsset"] == "USDT" and s["status"] == "TRADING"
    ]

def get_open_interest(symbol):
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
    now = (datetime.utcnow() + timedelta(hours=3)).strftime("%H:%M:%S")
    return (
        "🤖 <b>OI Screener Binance</b>\n\n"
        "Мониторинг роста <b>Open Interest</b>\n\n"
        "<b>Текущие настройки:</b>\n"
        f"▶️ Включен: <b>{cfg['enabled']}</b>\n\n"
        f"📊 OI период: {cfg['oi_period']} мин\n"
        f"📈 OI рост: {cfg['oi_percent']}%\n\n"
        f"⏱ <i>Обновлено: {now} (UTC+3)</i>"
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

# ================== CALLBACKS ==================

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
            f"Введи значение для: <b>{action}</b>",
            parse_mode="HTML",
        )
        return

    try:
        await q.message.edit_text(
            status_text(),
            parse_mode="HTML",
            reply_markup=keyboard(),
        )
    except:
        pass

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

    cfg[key] = int(value) if "period" in key else value
    context.user_data["edit"] = None

    await update.message.reply_text(
        "✅ Сохранено",
        reply_markup=keyboard(),
    )

# ================== SCANNER ==================

async def scanner():
    global scanner_running

    if scanner_running or not cfg["enabled"] or not cfg["chat_id"]:
        return

    scanner_running = True

    try:
        symbols = get_symbols()
        window = timedelta(minutes=cfg["oi_period"])

        for symbol in symbols:
            if not cfg["enabled"]:
                break

            oi = get_open_interest(symbol)
            now = datetime.utcnow()

            history = oi_history.setdefault(symbol, [])
            history.append((now, oi))

            # чистим старые данные
            cutoff = now - window
            history[:] = [(t, v) for t, v in history if t >= cutoff]

            if len(history) < 2:
                continue

            old_time, old_oi = history[0]
            pct = (oi - old_oi) / old_oi * 100

            if pct >= cfg["oi_percent"]:
                await send_signal(symbol, pct)
                history.clear()  # защита от спама

            await asyncio.sleep(0.05)

    finally:
        scanner_running = False

# ================== SIGNAL ==================

async def send_signal(symbol, pct):
    if not cfg["enabled"]:
        return

    link = f"https://www.coinglass.com/tv/Binance_{symbol}"

    msg = (
        f"🟣 <b>OI СИГНАЛ</b>\n"
        f"🪙 <b><a href='{link}'>{symbol}</a></b>\n"
        f"📈 Рост OI: {pct:.2f}%\n"
        f"⏱ За {cfg['oi_period']} мин"
    )

    await app.bot.send_message(
        chat_id=cfg["chat_id"],
        text=msg,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )

# ================== MAIN ==================

async def loop_job(context):
    await scanner()

app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CallbackQueryHandler(button))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

app.job_queue.run_repeating(loop_job, interval=OI_POLL_INTERVAL, first=10)

print(">>> OI SCREENER RUNNING <<<")
app.run_polling()
