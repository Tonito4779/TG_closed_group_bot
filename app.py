# app.py — FastAPI + python-telegram-bot v21.x
# Функції:
# - /health, / для перевірки
# - вебхук /webhook/{secret} з перевіркою секрету
# - /start через CommandHandler + catch-all логер
# - chat_join_request: лог + auto-approve + (опційно) запис у Google Sheets
# - /logme: ручний запис профілю користувача у Google Sheets (для тесту інтеграції)
#
# ENV (Render → Settings → Environment):
#   BOT_TOKEN          = <токен твого бота>
#   WEBHOOK_SECRET     = <твій_секрет_вебхука>
#   # опційно для Sheets:
#   GSA_CREDENTIALS    = <повний JSON сервіс-акаунта>
#   SHEET_ID           = <ID таблиці між /d/ і /edit>
#   SHEET_NAME         = Sheet1 (або твоя назва)
#
# requirements.txt:
#   python-telegram-bot==21.10
#   fastapi==0.115.0
#   uvicorn==0.30.6
#   gspread==6.1.4
#   google-auth==2.34.0

import os
import json
import logging
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import (
    Application,
    ChatJoinRequestHandler,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# --------- ОБОВ'ЯЗКОВІ ENV (без дефолтів) ---------
BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]

# --------- LOGGING ---------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("uvicorn.error")
logger.info("[BOOT] WEBHOOK_SECRET prefix=%s len=%s", WEBHOOK_SECRET[:5], len(WEBHOOK_SECRET))

# --------- FastAPI ---------
app = FastAPI()
tg_app: Application | None = None

# --------- Google Sheets (опційно) ---------
gspread_enabled = False
ws = None  # gspread.Worksheet | None

def _init_gsheets():
    """Ініціалізація клієнта Google Sheets, якщо задані ENV."""
    global gspread_enabled, ws
    try:
        gsa_json = os.environ.get("GSA_CREDENTIALS")
        sheet_id = os.environ.get("SHEET_ID")
        sheet_name = os.environ.get("SHEET_NAME", "Sheet1")

        if not gsa_json or not sheet_id:
            logger.info("[SHEETS] env not set -> disabled")
            gspread_enabled = False
            ws = None
            return None

        import gspread
        from google.oauth2.service_account import Credentials

        creds_info = json.loads(gsa_json)
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        gcreds = Credentials.from_service_account_info(creds_info, scopes=scopes)
        client = gspread.authorize(gcreds)

        sh = client.open_by_key(sheet_id)
        try:
            _ws = sh.worksheet(sheet_name)
        except gspread.WorksheetNotFound:
            _ws = sh.add_worksheet(title=sheet_name, rows=1000, cols=20)

        # заголовок, якщо порожньо
        if len(_ws.get_all_values()) == 0:
            _ws.append_row([
                "ts_utc", "chat_id", "user_id", "username",
                "first_name", "last_name", "lang", "bio",
                "invite_name"
            ])

        gspread_enabled = True
        ws = _ws
        logger.info("[SHEETS] ready: id=%s sheet=%s", sheet_id, sheet_name)
        return ws
    except Exception as e:
        logger.warning("[SHEETS] init failed: %s", e)
        gspread_enabled = False
        ws = None
        return None

# --------- HANDLERS ---------
async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    req = update.chat_join_request
    user = req.from_user
    logger.info(
        "[JOIN_REQUEST] chat_id=%s user_id=%s username=@%s name=%s invite_name=%s",
        req.chat.id, user.id, user.username, user.full_name,
        getattr(req.invite_link, "name", None),
    )

    # запис у Google Sheets (якщо увімкнено)
    if gspread_enabled and ws:
        try:
            ws.append_row([
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                str(req.chat.id),
                str(user.id),
                user.username or "",
                user.first_name or "",
                user.last_name or "",
                getattr(user, "language_code", "") or "",
                (req.bio or ""),
                getattr(req.invite_link, "name", "") or ""
            ])
            logger.info("[SHEETS] row appended for user_id=%s", user.id)
        except Exception as e:
            logger.error("[SHEETS] append failed: %s", e)

    # auto-approve
    try:
        await req.approve()
        logger.info("[JOIN_REQUEST] approved user_id=%s", user.id)
    except Exception as e:
        logger.error("Approve failed for user_id=%s: %s", user.id, e)

    # DM після approve НЕ шлемо (щоб не ловити 400, якщо юзер не стартував бота)

async def on_start_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else None
    logger.info("[MSG] /start from user_id=%s", uid)
    if update.message:
        await update.message.reply_text("Бот живий. Вебхук працює ✅")

async def on_logme(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ручний запис профілю користувача у Google Sheets — для тесту інтеграції."""
    if not update.message:
        return
    if not (gspread_enabled and ws):
        await update.message.reply_text("Sheets поки не налаштовані (нема GSA_CREDENTIALS/SHEET_ID).")
        return

    u = update.effective_user
    try:
        ws.append_row([
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "",                        # chat_id (не з join'у)
            str(u.id),
            u.username or "",
            u.first_name or "",
            u.last_name or "",
            getattr(u, "language_code", "") or "",
            "",                        # bio
            "manual:/logme"            # позначка джерела
        ])
        logger.info("[SHEETS] row appended via /logme for user_id=%s", u.id)
        await update.message.reply_text("✅ Додав твій запис у Google Sheets.")
    except Exception as e:
        logger.error("[SHEETS] /logme append failed: %s", e)
        await update.message.reply_text(f"❌ Не зміг записати у Sheets: {e}")

async def on_any_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        keys = list(update.to_dict().keys())
    except Exception:
        keys = []
    logger.info("[MSG] any message: keys=%s", keys)

# --------- LIFECYCLE ---------
@app.on_event("startup")
async def startup():
    global tg_app
    # Telegram app
    tg_app = Application.builder().token(BOT_TOKEN).build()
    tg_app.add_handler(ChatJoinRequestHandler(on_join_request))
    tg_app.add_handler(CommandHandler("start", on_start_msg))      # /start
    tg_app.add_handler(CommandHandler("logme", on_logme))          # /logme → запис у Sheets
    tg_app.add_handler(MessageHandler(filters.ALL, on_any_msg))    # діагностика
    await tg_app.initialize()
    await tg_app.start()
    logger.info("[OK] Telegram application started")

    # Google Sheets
    _init_gsheets()

@app.on_event("shutdown")
async def shutdown():
    if tg_app:
        await tg_app.stop()
        await tg_app.shutdown()
        logger.info("[OK] Telegram application stopped")

# --------- ROUTES ---------
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
