import asyncio
import json
import requests
import os
import time
from datetime import date
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ================== НАСТРОЙКИ ==================

TOKEN = os.getenv("BOT_TOKEN")
ALLOWED_USERS = {1128293345}  # твой Telegram ID

BINANCE = "https://fapi.binance.com"
CONFIG_FILE = "scanner_config.json"

cfg = {
    "period_min": 10,          # период в минутах
    "oi_percent": 5,           # % роста OI
    "oi_usd": 100000,          # рост OI в $
    "max_signals_per_day": 5,
    "enabled": False,
    "chat_id": None
}

oi_snapshot = {}
price_snapshot = {}
signals_today = {}

scanner_running = False

# ===== кеш символов + обновление раз в сутки =====
SYMBOLS_CACHE = []
LAST_SYMBOLS_UPDATE = 0
SYMBOLS_UPDATE_INTERVAL = 24 * 60 * 60  # 24 часа

# ================== UTILS ==================

def allowed(update: Update) -> bool:
    return update.effective_user.id in ALLOWED_USERS

def save_cfg():
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

def get_symbols():
    try:
        r = requests.get(f"{BINANCE}/fapi/v1/exchangeInfo", timeout=10).json()

        if "symbols" not in r:
            print("[WARN] exchangeInfo error:", r)
            return []

        return [
            s["symbol"] for s in r["symbols"]
            if s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING"
        ]

    except Exception as e:
        print("[ERROR] get_symbols:", e)
        return []

def refresh_symbols_if_needed(force=False):
    global SYMBOLS_CACHE, LAST_SYMBOLS_UPDATE

    now = time.time()

    if not force and (now - LAST_SYMBOLS_UPDATE < SYMBOLS_UPDATE_INTERVAL):
        return

    print("[DEBUG] trying to refresh symbols list")

    symbols = get_symbols()

    if symbols:
        SYMBOLS_CACHE = symbols
        LAST_SYMBOLS_UPDATE = now
        print(f"[DEBUG] symbols refreshed: {len(SYMBOLS_CACHE)}")
    else:
        print("[WARN] symbols refresh failed, keeping old list")

def get_oi(symbol):
    r = requests.get(
        f"{BINANCE}/fapi/v1/openInterest",
        params={"symbol": symbol},
        timeout=5
    ).json()
    return float(r["openInterest"])

def get_price(symbol):
    r = requests.get(
        f"{BINANCE}/fapi/v1/ticker/price",
        params={"symbol": symbol},
        timeout=5
    ).json()
    return float(r["price"])

# ================== COMMANDS ==================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return

    cfg["chat_id"] = update.effective_chat.id
    save_cfg()

    await update.message.reply_text(
        "🤖 Binance OI Scanner (PRIVATE)\n\n"
        "Команды:\n"
        "/on — включить сканер\n"
        "/off — выключить\n"
        "/setoipercent X\n"
        "/setoivolume USD\n"
        "/status"
    )

async def on(update: Update, context):
    if not allowed(update):
        return
    cfg["enabled"] = True
    save_cfg()
    await update.message.reply_text("✅ Сканер включен")

async def off(update: Update, context):
    if not allowed(update):
        return
    cfg["enabled"] = False
    save_cfg()
    await update.message.reply_text("⛔ Сканер выключен")

async def setoipercent(update: Update, context):
    if not allowed(update):
        return
    cfg["oi_percent"] = float(context.args[0])
    save_cfg()
    await update.message.reply_text(f"📈 Порог OI: {cfg['oi_percent']}%")

async def setoivolume(update: Update, context):
    if not allowed(update):
        return
    cfg["oi_usd"] = float(context.args[0])
    save_cfg()
    await update.message.reply_text(f"💰 Мин. рост OI: {cfg['oi_usd']:,.0f} $")

async def status(update: Update, context):
    if not allowed(update):
        return
    await update.message.reply_text(
        f"📊 Статус\n"
        f"Включен: {cfg['enabled']}\n"
        f"Период: {cfg['period_min']} мин\n"
        f"OI %: {cfg['oi_percent']}\n"
        f"OI $: {cfg['oi_usd']:,.0f}"
    )

# ================== SCANNER JOB ==================

async def scanner_job(context: ContextTypes.DEFAULT_TYPE):
    global scanner_running

    if scanner_running:
        return

    scanner_running = True
    app = context.application

    try:
        # обновление списка символов (раз в сутки)
        refresh_symbols_if_needed()

        # ---------- ПЕРВЫЙ ЗАПУСК ----------
        if not oi_snapshot:
            if not SYMBOLS_CACHE:
                print("[WARN] symbols cache empty, retry later")
                return

            for s in SYMBOLS_CACHE:
                try:
                    oi_snapshot[s] = get_oi(s)
                    price_snapshot[s] = get_price(s)
                    await asyncio.sleep(0.1)
                except:
                    pass

            print("[DEBUG] initial snapshot done")
            return
        print("[DEBUG] scanner cycle started")
        if not cfg["enabled"] or not cfg["chat_id"]:
            return

        today = str(date.today())

        for s in list(oi_snapshot.keys()):
            try:
                oi_now = get_oi(s)
                price_now = get_price(s)

                oi_prev = oi_snapshot[s]
                price_prev = price_snapshot[s]

                oi_delta = oi_now - oi_prev
                oi_pct = (oi_delta / oi_prev) * 100
                price_pct = (price_now - price_prev) / price_prev * 100

                circle = "🟢" if price_pct >= 0 else "🔴"

                key = (s, today)
                cnt = signals_today.get(key, 0)

                if (
                    oi_pct >= cfg["oi_percent"]
                    and oi_delta * price_now >= cfg["oi_usd"]
                    and cnt < cfg["max_signals_per_day"]
                ):
                    signals_today[key] = cnt + 1

                    coin_link = f"https://www.coinglass.com/tv/Binance_{s}"

                    message = (
                        f"{circle} <b>Binance — {cfg['period_min']}m</b>\n"
                        f"📊 <b><a href='{coin_link}'>{s}</a></b>\n"
                        f"📈 <b>OI вырос:</b> {oi_pct:.2f}% "
                        f"({oi_delta * price_now:,.0f} $)\n"
                        f"💰 <b>Изменение цены:</b> {price_pct:.2f}%\n"
                        f"🔔 <b>Сигнал за сутки:</b> {signals_today[key]}"
                    )

                    await app.bot.send_message(
                        chat_id=cfg["chat_id"],
                        text=message,
                        parse_mode="HTML",
                        disable_web_page_preview=True
                    )

                oi_snapshot[s] = oi_now
                price_snapshot[s] = price_now
                await asyncio.sleep(0.02)

            except Exception as e:
                print("[ERROR]", s, e)

    finally:
        scanner_running = False

# ================== LIFECYCLE ==================

async def post_init(app):
    app.job_queue.run_repeating(
        scanner_job,
        interval=cfg["period_min"] * 60,
        first=1
    )

def main():
    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("on", on))
    app.add_handler(CommandHandler("off", off))
    app.add_handler(CommandHandler("setoipercent", setoipercent))
    app.add_handler(CommandHandler("setoivolume", setoivolume))
    app.add_handler(CommandHandler("status", status))

    print(">>> БОТ ЗАПУЩЕН И РАБОТАЕТ <<<")
    app.run_polling()

if __name__ == "__main__":
    main()




