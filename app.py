# app.py — перевірений варіант з /health, /, webhook і хендлерами
import os, json, logging
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import (
    Application, ChatJoinRequestHandler, CommandHandler,
    MessageHandler, ContextTypes, filters
)

# ==== ОБОВ'ЯЗКОВІ ENV (без дефолтів) ====
BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]

# ==== LOGGING ====
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("uvicorn.error")
logger.info("[BOOT] WEBHOOK_SECRET prefix=%s len=%s", WEBHOOK_SECRET[:5], len(WEBHOOK_SECRET))

# ==== FASTAPI ====
app = FastAPI()
tg_app: Application | None = None

# ==== HANDLERS ====
async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    req = update.chat_join_request
    user = req.from_user
    logger.info(
        "[JOIN_REQUEST] chat_id=%s user_id=%s username=@%s name=%s invite_name=%s",
        req.chat.id, user.id, user.username, user.full_name,
        getattr(req.invite_link, "name", None),
    )
    try:
        await req.approve()
        logger.info("[JOIN_REQUEST] approved user_id=%s", user.id)
    except Exception as e:
        logger.error("Approve failed for user_id=%s: %s", user.id, e)

async def on_start_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else None
    logger.info("[MSG] /start from user_id=%s", uid)
    if update.message:
        await update.message.reply_text("Бот живий. Вебхук працює ✅")

async def on_any_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        keys = list(update.to_dict().keys())
    except Exception:
        keys = []
    logger.info("[MSG] any message: keys=%s", keys)

# ==== LIFECYCLE ====
@app.on_event("startup")
async def startup():
    global tg_app
    tg_app = Application.builder().token(BOT_TOKEN).build()
    tg_app.add_handler(ChatJoinRequestHandler(on_join_request))
    tg_app.add_handler(CommandHandler("start", on_start_msg))
    tg_app.add_handler(MessageHandler(filters.ALL, on_any_msg))
    await tg_app.initialize()
    await tg_app.start()
    logger.info("[OK] Telegram application started")

@app.on_event("shutdown")
async def shutdown():
    if tg_app:
        await tg_app.stop()
        await tg_app.shutdown()
        logger.info("[OK] Telegram application stopped")

# ==== ROUTES ====
@app.get("/")
async def root():
    return {"ok": True, "service": "tg-closed-group-bot", "webhook": "/webhook/{secret}", "health": "/health"}

@app.get("/health")
async def health():
    return {"ok": True}

@app.post("/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
        logger.warning("Bad secret on webhook call: got=%s", secret)
        return {"ok": False, "error": "bad secret"}
    raw = await request.body()
    logger.info("[WEBHOOK] received %d bytes", len(raw))
    payload = None
    try:
        payload = json.loads(raw.decode("utf-8"))
        logger.info("[WEBHOOK] update keys: %s", list(payload.keys()))
    except Exception as e:
        logger.warning("Failed to parse webhook JSON: %s", e)
    try:
        update = Update.de_json(payload or {}, tg_app.bot)  # type: ignore
        await tg_app.process_update(update)                 # type: ignore
    except Exception as e:
        logger.error("Failed to process update: %s", e)
    return {"ok": True}
