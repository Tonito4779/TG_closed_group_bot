# app.py — FastAPI + python-telegram-bot v21.x + Google Ads Offline Click Conversions
# Працює на безкоштовному Render без БД.
# Збереження прив'язок:
#   1) In-memory (дефолт, злітає після рестарту)
#   2) (Опціонально) Google Sheets, якщо задані GSA_CREDENTIALS + SHEET_ID
#
# ENV (Render → Settings → Environment):
#   BOT_TOKEN               = <токен бота>
#   WEBHOOK_SECRET          = <секрет у URL вебхука>
#   GOOGLE_ADS_YAML         = <повний google-ads.yaml (developer_token, client_id, client_secret, refresh_token, ...)>
#   GA_CUSTOMER_ID          = <ID Google Ads БЕЗ дефісів, напр. 1234567890>
#   GA_CONVERSION_ACTION_ID = <ID дії конверсії>
#   # (опційно для Sheets)
#   GSA_CREDENTIALS         = <JSON Service Account, весь файл як текст>
#   SHEET_ID                = <ID Google Sheets (з URL)>
#   LOG_LEVEL               = INFO | DEBUG (опційно)
#
# Start Command (Render):
#   uvicorn app:app --host 0.0.0.0 --port $PORT

import os
import re
import json
import uuid
import logging
from datetime import datetime, timezone
from typing import Optional, Tuple, Dict, Any

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, JSONResponse
from urllib.parse import quote

from telegram import Update
from telegram.ext import (
    Application, ChatJoinRequestHandler, CommandHandler,
    MessageHandler, ContextTypes, filters
)

# ===== ENV =====
BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]

GA_YAML = os.getenv("GOOGLE_ADS_YAML")
GA_CUSTOMER_ID = os.getenv("GA_CUSTOMER_ID")
GA_CONVERSION_ACTION_ID = os.getenv("GA_CONVERSION_ACTION_ID")

GSA_CREDENTIALS = os.getenv("GSA_CREDENTIALS")  # JSON text
SHEET_ID = os.getenv("SHEET_ID")                # Google Sheets ID

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# ===== Logging =====
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO))
logger = logging.getLogger("uvicorn.error")
logger.info("[BOOT] WEBHOOK_SECRET prefix=%s len=%s", WEBHOOK_SECRET[:5], len(WEBHOOK_SECRET))

# ===== FastAPI / Telegram =====
app = FastAPI()
tg_app: Application | None = None

# ===== Storage layer =====
class Storage:
    """Абстракція сховища: in-memory або Google Sheets."""
    def __init__(self):
        self.backend = "memory"
        self._mem: Dict[int, Dict[str, str]] = {}
        self._sheet = None

        if GSA_CREDENTIALS and SHEET_ID:
            try:
                import gspread
                from google.oauth2.service_account import Credentials

                scopes = [
                    "https://www.googleapis.com/auth/spreadsheets",
                    "https://www.googleapis.com/auth/drive",
                ]
                info = json.loads(GSA_CREDENTIALS)
                creds = Credentials.from_service_account_info(info, scopes=scopes)
                gc = gspread.authorize(creds)
                sh = gc.open_by_key(SHEET_ID)
                try:
                    ws = sh.worksheet("click_map")
                except gspread.exceptions.WorksheetNotFound:
                    ws = sh.add_worksheet(title="click_map", rows=1000, cols=3)
                    ws.update("A1:C1", [["user_id", "key", "value"]])
                self._sheet = ws
                self.backend = "sheets"
                logger.info("[STORAGE] Using Google Sheets backend (worksheet 'click_map').")
            except Exception as e:
                logger.error("[STORAGE] Sheets init failed, fallback to memory: %s", e)
                self.backend = "memory"

    def set_click(self, user_id: int, key: str, value: str) -> None:
        if self.backend == "memory":
            self._mem[user_id] = {key: value}
            return
        # Sheets
        try:
            # Пошук user_id; якщо нема — append
            records = self._sheet.get_all_records()  # type: ignore
            row_index = None
            for idx, rec in enumerate(records, start=2):
                if str(rec.get("user_id")) == str(user_id):
                    row_index = idx
                    break
            if row_index:
                self._sheet.update(f"A{row_index}:C{row_index}", [[user_id, key, value]])  # type: ignore
            else:
                self._sheet.append_row([user_id, key, value])  # type: ignore
        except Exception as e:
            logger.error("[STORAGE] set_click sheets failed: %s; fallback memory", e)
            self._mem[user_id] = {key: value}
            self.backend = "memory"

    def get_click(self, user_id: int) -> Optional[Tuple[str, str]]:
        if self.backend == "memory":
            entry = self._mem.get(user_id)
            if not entry:
                return None
            k = next(iter(entry.keys()))
            return (k, entry[k])
        # Sheets
        try:
            records = self._sheet.get_all_records()  # type: ignore
            for rec in records:
                if str(rec.get("user_id")) == str(user_id):
                    return (rec.get("key"), rec.get("value"))
            return None
        except Exception as e:
            logger.error("[STORAGE] get_click sheets failed: %s; fallback memory", e)
            entry = self._mem.get(user_id)
            if not entry:
                return None
            k = next(iter(entry.keys()))
            return (k, entry[k])

    def remove_click(self, user_id: int) -> bool:
        if self.backend == "memory":
            return self._mem.pop(user_id, None) is not None
        # Sheets
        try:
            # Проста реалізація: перечитати і переписати (ок для малих обсягів)
            records = self._sheet.get_all_records()  # type: ignore
            new_rows = [["user_id", "key", "value"]]
            removed = False
            for rec in records:
                if str(rec.get("user_id")) == str(user_id):
                    removed = True
                    continue
                new_rows.append([rec.get("user_id"), rec.get("key"), rec.get("value")])
            self._sheet.clear()  # type: ignore
            self._sheet.update("A1", new_rows)  # type: ignore
            return removed
        except Exception as e:
            logger.error("[STORAGE] remove_click sheets failed: %s", e)
            return False

ST = Storage()

# ===== Google Ads client =====
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

# ===== Helpers =====
def iso_for_google_ads(dt: datetime) -> str:
    """Google Ads очікує формат 'YYYY-MM-DD HH:MM:SS+/-HH:MM'."""
    s = dt.strftime("%Y-%m-%d %H:%M:%S%z")  # ...+0000
    return s[:-2] + ":" + s[-2:]            # ...+00:00

def classify_click_id(value: str) -> Tuple[str, str]:
    """
    Визначає тип ідентифікатора та повертає (key, clean_value),
    key ∈ {gclid, gbraid, wbraid}
    """
    v = value.strip()
    low = v.lower()

    if "wbraid=" in low or low.startswith("wbraid."):
        return "wbraid", re.sub(r"^.*wbraid=([^&\s]+).*$", r"\1", v)
    if "gbraid=" in low or low.startswith("gbraid."):
        return "gbraid", re.sub(r"^.*gbraid=([^&\s]+).*$", r"\1", v)
    if "gclid=" in low or low.startswith("c."):  # gclid часто Cj0..., може прийти як "gclid=XXX"
        return "gclid", re.sub(r"^.*gclid=([^&\s]+).*$", r"\1", v)

    # якщо просто значення — спробуємо евристику
    if re.match(r"^[A-Za-z0-9._-]{10,}$", v):
        return "gclid", v

    return "gclid", v  # за замовчуванням

def upload_click_conversion(
    *,
    gclid: Optional[str]=None,
    gbraid: Optional[str]=None,
    wbraid: Optional[str]=None,
    conversion_action_id: str,
    conversion_datetime_iso: str,
    value: float=0.0,
    currency: str="UAH",
    order_id: Optional[str]=None
) -> Any:
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

    if order_id:
        conv.order_id = order_id  # дедуп

    req = google_ads_client.get_type("UploadClickConversionsRequest")
    req.customer_id = str(GA_CUSTOMER_ID)
    req.conversions.append(conv)
    req.partial_failure = True

    resp = conv_service.upload_click_conversions(request=req)

    pf = getattr(resp, "partial_failure_error", None)
    if pf and getattr(pf, "code", 0) != 0:
        logger.error("[GADS] partial_failure: %s", pf)
    else:
        logger.info("[GADS] upload OK: %s", resp)

    return resp

# ===== Handlers =====
async def on_start_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else None
    if update.message:
        has_ga = bool(google_ads_client)
        await update.message.replyText(
            "Бот живий. Вебхук працює ✅\n"
            f"Google Ads клієнт: {'OK' if has_ga else '— (env неповні)'}\n"
            f"Сховище: {ST.backend}\n\n"
            "Команди:\n"
            "/bind_click <gclid|gbraid|wbraid або URL з ними>\n"
            "/convert_test — ручний аплоуд конверсії\n"
            "/whoami — показати прив’язаний click id\n"
            "/clear_bind — видалити прив’язку"
        )

async def on_any_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        keys = list(update.to_dict().keys())
    except Exception:
        keys = []
    logger.info("[MSG] any update keys=%s", keys)

async def on_bind_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("Використай: /bind_click <gclid|gbraid|wbraid або URL з ними>")
        return

    raw = args[0].strip()
    key, clean = classify_click_id(raw)
    uid = update.effective_user.id

    ST.set_click(uid, key, clean)
    show = clean[:16] + "..." if len(clean) > 16 else clean
    logger.info("[BIND] user_id=%s -> %s=%s (backend=%s)", uid, key, show, ST.backend)
    await update.message.reply_text(f"Ок, зв’язав {key} для user_id={uid}")

async def on_convert_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    if not google_ads_client:
        await update.message.reply_text("Google Ads клієнт не ініціалізовано (перевір GOOGLE_ADS_YAML / GA_* env).")
        return

    uid = update.effective_user.id
    pair = ST.get_click(uid)
    if not pair:
        await update.message.reply_text("Спочатку задай click id: /bind_click <gclid|gbraid|wbraid>")
        return

    key, val = pair
    try:
        when = iso_for_google_ads(datetime.now(timezone.utc))
        dedup_id = f"tg-{uid}-{int(datetime.now().timestamp())}-{uuid.uuid4().hex[:8]}"
        kwargs = {"conversion_action_id": str(GA_CONVERSION_ACTION_ID),
                  "conversion_datetime_iso": when,
                  "value": 0.0, "currency": "UAH",
                  "order_id": dedup_id}
        if key == "gclid":
            kwargs["gclid"] = val
        elif key == "gbraid":
            kwargs["gbraid"] = val
        else:
            kwargs["wbraid"] = val

        resp = upload_click_conversion(**kwargs)
        await update.message.reply_text("✅ Відправив офлайн-конверсію в Google Ads. Перевір у дії конверсії.")
    except Exception as e:
        logger.exception("[GADS] manual upload failed")
        await update.message.reply_text(f"❌ Помилка аплоуду: {e}")

async def on_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    uid = update.effective_user.id
    pair = ST.get_click(uid)
    if pair:
        key, val = pair
        show = val[:32] + "..." if len(val) > 32 else val
        await update.message.reply_text(f"Прив’язано: {key} = {show}\nСховище: {ST.backend}")
    else:
        await update.message.reply_text("Прив’язок не знайдено.")

async def on_clear_bind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    uid = update.effective_user.id
    ok = ST.remove_click(uid)
    await update.message.reply_text("Готово." if ok else "Нічого не видалено.")

async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        pair = ST.get_click(user.id)
        if google_ads_client and pair:
            key, val = pair
            when = iso_for_google_ads(datetime.now(timezone.utc))
            dedup_id = f"join-{user.id}-{int(datetime.now().timestamp())}-{uuid.uuid4().hex[:8]}"

            kwargs = {"conversion_action_id": str(GA_CONVERSION_ACTION_ID),
                      "conversion_datetime_iso": when,
                      "value": 0.0, "currency": "UAH",
                      "order_id": dedup_id}
            if key == "gclid":
                kwargs["gclid"] = val
            elif key == "gbraid":
                kwargs["gbraid"] = val
            else:
                kwargs["wbraid"] = val

            resp = upload_click_conversion(**kwargs)
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
    tg_app.add_handler(CommandHandler("whoami", on_whoami))
    tg_app.add_handler(CommandHandler("clear_bind", on_clear_bind))
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
    return {"ok": True, "storage": ST.backend, "ads_ready": bool(google_ads_client)}

@app.post("/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
        logger.warning("Bad secret on webhook call: got=%s", secret)
        return JSONResponse({"ok": False, "error": "bad secret"}, status_code=403)

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

# Зручна установка вебхука
@app.get("/setup-webhook", response_class=PlainTextResponse)
async def setup_webhook(request: Request):
    base = str(request.url).split("/setup-webhook")[0]
    url = f"{base}/webhook/{quote(WEBHOOK_SECRET)}"
    try:
        await tg_app.bot.set_webhook(url)  # type: ignore
        return f"Webhook set to: {url}"
    except Exception as e:
        logger.error("set_webhook failed: %s", e)
        return f"Error: {e}"
