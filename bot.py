import asyncio
import requests
import os
from datetime import datetime, timedelta, timezone
from collections import defaultdict, deque

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

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("❌ BOT_TOKEN not set")

ALLOWED_USERS = set(
    int(x) for x in os.getenv("ALLOWED_USERS", "").split(",") if x.strip()
)

BINANCE = "https://fapi.binance.com"
UTC_PLUS_3 = timezone(timedelta(hours=3))

cfg = {
    "enabled": False,
    "chat_id": None,

    "oi_period": 10,     # minutes
    "oi_percent": 5.0,   # %
}

# защита от наложения job
scanner_running = False

# symbol -> deque[(timestamp, oi)]
oi_history = defaultdict(deque)

# кеш символов
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
        f"⏱ Рынок обновлён: <i>{now} (UTC+3)</i>"
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

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

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
            f"Введи значение для: <b>{action}</b>",
            parse_mode="HTML",
        )
        return

    new_text = status_text()
    if q.message.text != new_text:
        await q.message.edit_text(
            new_text,
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

    cfg[key] = int(value) if "period" in key else value
    context.user_data["edit"] = None

    await update.message.reply_text(
        "✅ Сохранено",
        reply_markup=keyboard(),
    )

# ================== SCANNER (ONE PASS) ==================

async def scanner(context: ContextTypes.DEFAULT_TYPE):
    global scanner_running

    if scanner_running or not cfg["enabled"] or not cfg["chat_id"]:
        return

    scanner_running = True

    try:
        symbols = get_symbols()
        now = datetime.now(UTC_PLUS_3)
        window = timedelta(minutes=cfg["oi_period"])

        for symbol in symbols:
            if not cfg["enabled"]:
                break

            oi = await asyncio.to_thread(get_open_interest, symbol)

            history = oi_history[symbol]
            history.append((now, oi))

            # чистим окно
            while history and now - history[0][0] > window:
                history.popleft()

            if len(history) < 2:
                continue

            old_oi = history[0][1]
            pct = (oi - old_oi) / old_oi * 100

            if pct >= cfg["oi_percent"]:
                await send_signal(symbol, pct, cfg["oi_period"])
                history.clear()  # антиспам

            await asyncio.sleep(0.03)

    finally:
        scanner_running = False

# ================== SIGNAL ==================

async def send_signal(symbol: str, pct: float, period: int):
    link = f"https://www.coinglass.com/tv/Binance_{symbol}"

    msg = (
        "📈 <b>OPEN INTEREST РАСТЕТ</b>\n\n"
        f"🪙 <b><a href='{link}'>{symbol}</a></b>\n"
        f"📊 Рост OI: <b>{pct:.2f}%</b>\n"
        f"⏱ За {period} мин"
    )

    await app.bot.send_message(
        chat_id=cfg["chat_id"],
        text=msg,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )

# ================== MAIN ==================

app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("status", status_cmd))
app.add_handler(CallbackQueryHandler(button))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

# частота опроса Binance (как в pump)
app.job_queue.run_repeating(
    scanner,
    interval=60,   # 1 раз в минуту
    first=5,
)

print(">>> BINANCE OI SCREENER RUNNING <<<")
app.run_polling()
