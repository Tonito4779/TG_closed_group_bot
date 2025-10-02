# app.py — FastAPI + python-telegram-bot v21.x + Google Ads Offline Click Conversions
# ENV (Render → Settings → Environment):
#   BOT_TOKEN               = <токен бота>
#   WEBHOOK_SECRET          = <секрет у URL вебхука>
#   GOOGLE_ADS_YAML         = <повний текст google-ads.yaml (developer_token, client_id, client_secret, refresh_token, ...)>
#   GA_CUSTOMER_ID          = <ID Google Ads БЕЗ дефісів, напр. 1234567890>
#   GA_CONVERSION_ACTION_ID = <ID дії конверсії (ціле або рядок)>
#
# Start Command (Render):
#   uvicorn app:app --host 0.0.0.0 --port $PORT
#
# requirements.txt:
#   python-telegram-bot==21.10
#   fastapi==0.115.0
#   uvicorn==0.30.6
#   google-ads==24.1.0

import os
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import (
    Application, ChatJoinRequestHandler, CommandHandler,
    MessageHandler, ContextTypes, filters
)

# ===== Required ENV =====
BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]

GA_YAML = os.getenv("GOOGLE_ADS_YAML")                  # повний yaml як текст
GA_CUSTOMER_ID = os.getenv("GA_CUSTOMER_ID")            # "1234567890"
GA_CONVERSION_ACTION_ID = os.getenv("GA_CONVERSION_ACTION_ID")  # id дії

# ===== Logging =====
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("uvicorn.error")
logger.info("[BOOT] WEBHOOK_SECRET prefix=%s len=%s", WEBHOOK_SECRET[:5], len(WEBHOOK_SECRET))

# ===== FastAPI / Telegram =====
app = FastAPI()
tg_app: Application | None = None

# ===== Google Ads client init =====
google_ads_client = None
if GA_YAML and GA_CUSTOMER_ID and GA_CONVERSION_ACTION_ID:
    try:
        import pathlib
        from google.ads.googleads.client import GoogleAdsClient

        cfg_path = pathlib.Path("/var/tmp/google-ads.yaml")
        cfg_path.write_text(GA_YAML, encoding="utf-8")
        os.environ["GOOGLE_ADS_CONFIGURATION_FILE_PATH"] = str(cfg_path)

        google_ads_client = GoogleAdsClient.load_from_storage(str(cfg_path))
        logger.info("[GADS] client init OK (customer_id=%s, action_id=%s)",
                    GA_CUSTOMER_ID, GA_CONVERSION_ACTION_ID)
    except Exception as e:
        logger.error("[GADS] init failed: %s", e)
        google_ads_client = None
else:
    logger.info("[GADS] env incomplete -> disabled (no uploads)")

def iso_for_google_ads(dt: datetime) -> str:
    """Google Ads очікує формат 'YYYY-MM-DD HH:MM:SS+/-HH:MM'."""
    s = dt.strftime("%Y-%m-%d %H:%M:%S%z")  # ...+0000
    return s[:-2] + ":" + s[-2:]            # ...+00:00

def upload_click_conversion(
    *, gclid: Optional[str]=None, gbraid: Optional[str]=None, wbraid: Optional[str]=None,
    conversion_action_id: str, conversion_datetime_iso: str, value: float=0.0, currency: str="UAH"
):
    if not google_ads_client:
        raise RuntimeError("Google Ads client not initialized")

    conv_service = google_ads_client.get_service("ConversionUploadService")
    conv = google_ads_client.get_type("ClickConversion")

    # один із ідентифікаторів кліку ОБОВ’ЯЗКОВО
    if gclid:
        conv.gclid = gclid
    if gbraid:
        conv.gbraid = gbraid
    if wbraid:
        conv.wbraid = wbraid

    casvc = google_ads_client.get_service("ConversionActionService")
    conv.conversion_action = casvc.conversion_action_path(str(GA_CUSTOMER_ID), str(conversion_action_id))
    conv.conversion_date_time = conversion_datetime_iso
    conv.currency_code = currency
    conv.conversion_value = value

    req = google_ads_client.get_type("UploadClickConversionsRequest")
    req.customer_id = str(GA_CUSTOMER_ID)
    req.conversions.append(conv)
    req.partial_failure = True

    resp = conv_service.upload_click_conversions(request=req)
    return resp

# ===== In-memory mapping для демо (заміниш на БД/Sheets) =====
# {telegram_user_id: {"gclid": "...", "gbraid": "...", "wbraid": "..."}}
click_map: dict[int, dict[str, str]] = {}

# ===== Handlers =====
async def on_start_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else None
    logger.info("[MSG] /start from user_id=%s", uid)
    if update.message:
        has_ga = bool(google_ads_client)
        await update.message.reply_text(
            "Бот живий. Вебхук працює ✅\n"
            f"Google Ads клієнт: {'OK' if has_ga else '— (env неповні)'}\n\n"
            "Команди:\n"
            "/bind_click <gclid|gbraid|wbraid>\n"
            "/convert_test  — ручний аплоуд конверсії (за збереженим click id)"
        )

async def on_any_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        keys = list(update.to_dict().keys())
    except Exception:
        keys = []
    logger.info("[MSG] any message: keys=%s", keys)

async def on_bind_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Прив’язка click id до Telegram user_id: /bind_click <gclid|gbraid|wbraid>"""
    if not update.message:
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("Використай: /bind_click <gclid|gbraid|wbraid>")
        return

    value = args[0].strip()
    uid = update.effective_user.id

    # простенька евристика визначення типу
    key = None
    low = value.lower()
    if low.startswith("cj"):
        key = "gclid"
    if "wbraid" in low:
        key = "wbraid"
    if "gbraid" in low:
        key = "gbraid"
    if key is None:
        key = "gclid"

    click_map[uid] = {key: value}
    show = value[:16] + "..." if len(value) > 16 else value
    logger.info("[BIND] user_id=%s -> %s=%s", uid, key, show)
    await update.message.reply_text(f"Ок, зв’язав {key} для user_id={uid}")

async def on_convert_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ручний аплоуд конверсії для поточного користувача за збереженим click id."""
    if not update.message:
        return
    if not google_ads_client:
        await update.message.reply_text("Google Ads клієнт не ініціалізовано (перевір GOOGLE_ADS_YAML / GA_* env).")
        return

    uid = update.effective_user.id
    entry = click_map.get(uid)
    if not entry:
        await update.message.reply_text("Спочатку задай click id: /bind_click <gclid|gbraid|wbraid>")
        return

    try:
        when = iso_for_google_ads(datetime.now(timezone.utc))
        resp = upload_click_conversion(
            gclid=entry.get("gclid"),
            gbraid=entry.get("gbraid"),
            wbraid=entry.get("wbraid"),
            conversion_action_id=str(GA_CONVERSION_ACTION_ID),
            conversion_datetime_iso=when,
            value=0.0, currency="UAH"
        )
        logger.info("[GADS] manual upload resp: %s", resp)
        await update.message.reply_text("✅ Відправив офлайн-конверсію в Google Ads (перевір дію в акаунті).")
    except Exception as e:
        logger.error("[GADS] manual upload failed: %s", e)
        await update.message.reply_text(f"❌ Помилка аплоуду: {e}")

async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Авто-апрув + аплоуд конверсії, якщо є зв’язаний click id для цього user_id."""
    req = update.chat_join_request
    user = req.from_user
    logger.info(
        "[JOIN_REQUEST] chat_id=%s user_id=%s username=@%s name=%s invite_name=%s",
        req.chat.id, user.id, user.username, user.full_name, getattr(req.invite_link, "name", None)
    )

    # 1) Auto-approve заявки
    try:
        await req.approve()
        logger.info("[JOIN_REQUEST] approved user_id=%s", user.id)
    except Exception as e:
        logger.error("Approve failed for user_id=%s: %s", user.id, e)

    # 2) Upload конверсії, якщо є меппінг і клієнт готовий
    try:
        entry = click_map.get(user.id)
        if google_ads_client and entry:
            when = iso_for_google_ads(datetime.now(timezone.utc))
            resp = upload_click_conversion(
                gclid=entry.get("gclid"),
                gbraid=entry.get("gbraid"),
                wbraid=entry.get("wbraid"),
                conversion_action_id=str(GA_CONVERSION_ACTION_ID),
                conversion_datetime_iso=when,
                value=0.0, currency="UAH"
            )
            logger.info("[GADS] join upload resp: %s", resp)
        else:
            logger.info("[GADS] skip upload (no mapping or client not ready) user_id=%s", user.id)
    except Exception as e:
        logger.error("[GADS] join upload failed: %s", e)

# ===== Lifecycle =====
@app.on_event("startup")
async def startup():
    global tg_app
    tg_app = Application.builder().token(BOT_TOKEN).build()
    tg_app.add_handler(ChatJoinRequestHandler(on_join_request))
    tg_app.add_handler(CommandHandler("start", on_start_msg))
    tg_app.add_handler(CommandHandler("bind_click", on_bind_click))
    tg_app.add_handler(CommandHandler("convert_test", on_convert_test))
    tg_app.add_handler(MessageHandler(filters.ALL, on_any_msg))  # діагностика
    await tg_app.initialize()
    await tg_app.start()
    logger.info("[OK] Telegram application started")

@app.on_event("shutdown")
async def shutdown():
    if tg_app:
        await tg_app.stop()
        await tg_app.shutdown()
        logger.info("[OK] Telegram application stopped")

# ===== Routes =====
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
