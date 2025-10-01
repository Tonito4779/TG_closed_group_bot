import os
from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import Application, ChatJoinRequestHandler, ContextTypes

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "change_me_secret")  # довільний рядок

app = FastAPI()
tg_app: Application | None = None

# --- обробник події join request ---
async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    req = update.chat_join_request
    user = req.from_user
    # Логи в консоль (для MVP):
    print(f"[JOIN_REQUEST] chat={req.chat.id} user_id={user.id} @{user.username} {user.full_name} "
          f"invite_name={getattr(req.invite_link, 'name', None)}")

    # Авто-апрув для тесту:
    await req.approve()

    # (не обов'язково) написати юзеру:
    try:
        await context.bot.send_message(
            chat_id=req.user_chat_id,
            text="Вітаю! Запит на приєднання схвалено ✅"
        )
    except Exception as e:
        print(f"[WARN] Не вдалося написати користувачу: {e}")

# --- FastAPI lifecycle ---
@app.on_event("startup")
async def startup():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN env var is required")
    global tg_app
    tg_app = Application.builder().token(BOT_TOKEN).build()
    tg_app.add_handler(ChatJoinRequestHandler(on_join_request))
    await tg_app.initialize()
    await tg_app.start()
    print("[OK] Telegram application started")

@app.on_event("shutdown")
async def shutdown():
    if tg_app:
        await tg_app.stop()
        await tg_app.shutdown()
        print("[OK] Telegram application stopped")

@app.get("/health")
async def health():
    return {"ok": True}

# --- webhook endpoint ---
@app.post(f"/webhook/{{secret}}")
async def telegram_webhook(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
        return {"ok": False, "error": "bad secret"}
    data = await request.json()
    update = Update.de_json(data, tg_app.bot)  # type: ignore
    await tg_app.process_update(update)        # type: ignore
    return {"ok": True}
