# app.py
# FastAPI + python-telegram-bot (v21.x)
# Вебхук-бот, який ловить chat_join_request і логує все у Render → Logs

import os
import json
import logging
from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import (
    Application,
    ChatJoinRequestHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ========= ENV =========
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "change_me_secret")  # має збігатися з тим, що ти ставиш у setWebhook

# ========= LOGGING =========
# Логи видно у Render → Logs
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("uvicorn.error")

# ========= FASTAPI APP =========
app = FastAPI()
tg_app: Application | None = None  # PTB Application (глобально, щоб обробляти апдейти у вебхуці)

# ========= HANDLERS =========
async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробляє запити на приєднання до групи (Request to Join)"""
    req = update.chat_join_request
    user = req.from_user

    logger.info(
        "[JOIN_REQUEST] chat_id=%s user_id=%s username=@%s name=%s invite_name=%s",
        req.chat.id,
        user.id,
        user.username,
        user.full_name,
        getattr(req.invite_link, "name", None),
    )

    # MVP: автоматично схвалюємо заявку
    try:
        await req.approve()
        logger.info("[JOIN_REQUEST] approved user_id=%s", user.id)
    except Exception as e:
        logger.error("Approve failed for user_id=%s: %s", user.id, e)

    # (опціонально) надіслати користувачу повідомлення
    try:
        await context.bot.send_message(
            chat_id=req.user_chat_id,
            text="Вітаю! Запит на приєднання схвалено ✅",
        )
    except Exception as e:
        # не критично — юзер міг не відкрити діалог з ботом
        logger.warning("Cannot DM user_id=%s after approve: %s", user.id, e)


async def on_start_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Простий тест-хендлер: у приватному чаті з ботом на /start повертає відповідь"""
    uid = update.effective_user.id if update.effective_user else None
    logger.info("[MSG] /start from user_id=%s", uid)
    await update.message.reply_text("Бот живий. Вебхук працює ✅")


# ========= LIFECYCLE =========
@app.on_event("startup")
async def startup():
    """Стартує PTB Application усередині FastAPI-процесу"""
    if not BOT_TOKEN:
        # Якщо не задано BOT_TOKEN у Render → Settings → Environment — падатиме тут
        raise RuntimeError("BOT_TOKEN env var is required")

    global tg_app
    tg_app = Application.builder().token(BOT_TOKEN).build()

    # Реєструємо хендлери
    tg_app.add_handler(ChatJoinRequestHandler(on_join_request))
    tg_app.add_handler(MessageHandler(filters.Regex(r"^/start$"), on_start_msg))

    # Запускаємо PTB усередині цього процесу
    await tg_app.initialize()
    await tg_app.start()
    logger.info("[OK] Telegram application started")


@app.on_event("shutdown")
async def shutdown():
    """Коректно гасимо PTB Application при зупинці FastAPI"""
    if tg_app:
        await tg_app.stop()
        await tg_app.shutdown()
        logger.info("[OK] Telegram application stopped")


# ========= ROUTES =========
@app.get("/")
async def root():
    # Щоб не було 404 на корені
    return {"ok": True, "service": "tg-closed-group-bot", "webhook": "/webhook/{secret}", "health": "/health"}


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request):
    """Єдиний вебхук-ендпоінт, куди Telegram шле апдейти (ми в setWebhook ставимо цей URL)."""
    if secret != WEBHOOK_SECRET:
        logger.warning("Bad secret on webhook call: got=%s", secret)
        return {"ok": False, "error": "bad secret"}

    raw = await request.body()
    logger.info("[WEBHOOK] received %d bytes", len(raw))

    try:
        payload = json.loads(raw.decode("utf-8"))
        # Підсвітимо ключі апдейту в логах (для діагностики)
        logger.info("[WEBHOOK] update keys: %s", list(payload.keys()))
    except Exception as e:
        logger.warning("Failed to parse webhook JSON: %s", e)
        payload = None

    try:
        update = Update.de_json(payload or {}, tg_app.bot)  # type: ignore
        await tg_app.process_update(update)                 # type: ignore
    except Exception as e:
        logger.error("Failed to process update: %s", e)

    return {"ok": True}
