import os
import re
import json
import logging
import tempfile
import asyncio
from contextlib import asynccontextmanager
from typing import Dict, Optional

from fastapi import FastAPI, Request, Response
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes
)
import aiohttp

# ---------- تنظیمات ----------
TOKEN = os.environ.get("8665630689:AAHLoaGSjfEl3ParM4pOXJnBy0LMSYAZxkM")
if not TOKEN:
    raise ValueError("BOT_TOKEN environment variable not set")

# API‌های اینستاگرام
API_INFO = "https://norax.s2026h.space/api/info.py"
API_DOWNLOAD = "https://norax.s2026h.space/api/app.py"  # در صورت نیاز

WEBHOOK_PATH = "/webhook"
KV_NAMESPACE = os.environ.get("KV_NAMESPACE", "BOT_STATE")  # Namespace KV در Cloudflare

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------- دکمه‌های اصلی (ثابت) ----------
def get_main_keyboard():
    keyboard = [
        [InlineKeyboardButton("👤 ورود به اکانت سازنده", url="https://t.me/ciah_am")],
        [InlineKeyboardButton("🤖 ورود به سایت امیر جی‌پی‌تی", url="https://amirgpt.ir")],
    ]
    return InlineKeyboardMarkup(keyboard)

# ---------- ابزارهای کمکی ----------
def extract_instagram_url(text: str) -> Optional[str]:
    pattern = r"(https?://(?:www\.)?(?:instagram\.com|instagr\.am)/(?:p|reel|tv)/[A-Za-z0-9_-]+)"
    match = re.search(pattern, text)
    return match.group(1) if match else None

async def get_kv_value(key: str) -> Optional[str]:
    """خواندن از Cloudflare KV (از env.kv)"""
    # در Pages Functions، KV از طریق env در دسترس است
    # ما از یک دیکشنری سراسری برای شبیه‌سازی (در صورتی که KV در دسترس نباشد) استفاده می‌کنیم
    # اما در محیط واقعی باید از env.KV_NAMESPACE استفاده کنید.
    try:
        # اینجا باید به KV دسترسی داشته باشید، اما برای تست از یک dict استفاده می‌کنیم
        # در Cloudflare Pages، kv را از طریق env دریافت می‌کنید.
        kv = getattr(env, KV_NAMESPACE, None)
        if kv:
            return await kv.get(key)
        return None
    except:
        return None

async def set_kv_value(key: str, value: str, ttl: int = 300):
    """ذخیره در KV با انقضای ۵ دقیقه"""
    try:
        kv = getattr(env, KV_NAMESPACE, None)
        if kv:
            await kv.put(key, value, expiration_ttl=ttl)
    except:
        pass

# ---------- دریافت اطلاعات از API ----------
async def fetch_media_info(session: aiohttp.ClientSession, url: str) -> Optional[Dict]:
    try:
        params = {"url": url}
        async with session.get(API_INFO, params=params, timeout=30) as resp:
            if resp.status != 200:
                logger.error(f"Info API error: {resp.status}")
                return None
            data = await resp.json()
            if data.get("status") == "ok":
                return data
            logger.error(f"Info API response: {data}")
            return None
    except Exception as e:
        logger.error(f"fetch_media_info error: {e}")
        return None

async def download_file(session: aiohttp.ClientSession, url: str) -> Optional[str]:
    try:
        async with session.get(url, timeout=90) as resp:
            resp.raise_for_status()
            fd, temp_path = tempfile.mkstemp(suffix=".mp4")
            os.close(fd)
            with open(temp_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(8192):
                    f.write(chunk)
            return temp_path
    except Exception as e:
        logger.error(f"download_file error: {e}")
        return None

# ---------- هندلرهای ربات ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎥 به **ربات دانلودر امیر** خوش آمدید!\n\n"
        "من می‌توانم ویدئوهای اینستاگرام را با کیفیت دلخواه شما دانلود کنم.\n"
        "🔹 کافیه لینک پست، رییل یا استوری رو برام بفرستی.\n"
        "🔹 بعد کیفیت مورد نظرت رو انتخاب کن.\n\n"
        "👤 سازنده: @ciah_am\n"
        "🤖 سایت امیر جی‌پی‌تی: https://amirgpt.ir",
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    url = extract_instagram_url(text)
    if not url:
        await update.message.reply_text(
            "❌ لینک معتبر اینستاگرام پیدا نشد. لطفاً یک لینک از پست، رییل یا استوری ارسال کنید.",
            reply_markup=get_main_keyboard()
        )
        return

    msg = await update.message.reply_text("⏳ در حال دریافت اطلاعات ویدئو از سرور...")

    async with aiohttp.ClientSession() as session:
        info = await fetch_media_info(session, url)

    if not info:
        await msg.edit_text("❌ خطا در دریافت اطلاعات. لطفاً دوباره تلاش کنید یا لینک دیگری بفرستید.")
        return

    medias = info.get("medias", [])
    if not medias:
        await msg.edit_text("❌ هیچ کیفیتی برای دانلود یافت نشد.")
        return

    # ذخیره‌ی اطلاعات کیفیت‌ها در KV با کلید unique (مثلاً chat_id + message_id)
    # از chat_id و message_id به عنوان کلید استفاده می‌کنیم
    chat_id = str(update.effective_chat.id)
    message_id = str(update.message.message_id)
    key = f"{chat_id}_{message_id}"
    # ساخت دیکشنری از کیفیت‌ها با لینک
    quality_map = {media.get("quality"): media.get("url") for media in medias if media.get("url")}
    await set_kv_value(key, json.dumps(quality_map), ttl=300)  # ۵ دقیقه انقضا

    # ساخت دکمه‌های کیفیت
    keyboard = []
    for quality in quality_map.keys():
        keyboard.append([InlineKeyboardButton(f"📥 کیفیت {quality}", callback_data=f"q_{key}_{quality}")])
    keyboard.append([InlineKeyboardButton("🔙 بازگشت", callback_data="back")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    thumbnail = info.get("thumbnail")
    caption = f"🎬 {info.get('title', 'ویدئو')}\n\nکیفیت مورد نظر را انتخاب کنید:"
    if thumbnail:
        try:
            await update.message.reply_photo(
                photo=thumbnail,
                caption=caption,
                reply_markup=reply_markup
            )
            await msg.delete()
            return
        except Exception as e:
            logger.error(f"Thumbnail send error: {e}")

    await msg.edit_text(caption, reply_markup=reply_markup)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "back":
        await query.message.delete()
        return

    # داده‌ها به صورت q_{key}_{quality}
    parts = data.split("_", 2)
    if len(parts) != 3:
        await query.edit_message_text("❌ خطا در پردازش درخواست.")
        return
    _, key, quality = parts

    # دریافت اطلاعات از KV
    kv_data = await get_kv_value(key)
    if not kv_data:
        await query.edit_message_text(
            "⏳ زمان ذخیره‌سازی اطلاعات منقضی شده است. لطفاً دوباره لینک را ارسال کنید."
        )
        return
    try:
        quality_map = json.loads(kv_data)
    except:
        await query.edit_message_text("❌ خطا در بازیابی اطلاعات.")
        return

    download_url = quality_map.get(quality)
    if not download_url:
        await query.edit_message_text("❌ لینک دانلود برای این کیفیت یافت نشد.")
        return

    await query.edit_message_text(f"⏳ در حال دانلود ویدئو با کیفیت {quality}...")

    async with aiohttp.ClientSession() as session:
        file_path = await download_file(session, download_url)

    if not file_path:
        await query.edit_message_text(
            "❌ خطا در دانلود ویدئو. ممکن است لینک نامعتبر باشد یا سرور پاسخ ندهد."
        )
        return

    # ارسال ویدئو
    try:
        with open(file_path, "rb") as f:
            await context.bot.send_video(
                chat_id=update.effective_chat.id,
                video=f,
                caption=f"✅ دانلود با کیفیت {quality} انجام شد.\n\n"
                        "🎉 زود باش برو از **امیر** (@Ciah_am) تشکر کن! 🙏"
            )
        await query.delete_message()
    except Exception as e:
        logger.error(f"send_video error: {e}")
        # اگر ویدئو بزرگ‌تر از ۵۰ مگابایت بود، به عنوان سند ارسال می‌کنیم
        try:
            with open(file_path, "rb") as f:
                await context.bot.send_document(
                    chat_id=update.effective_chat.id,
                    document=f,
                    caption=f"✅ دانلود با کیفیت {quality} (ارسال به عنوان فایل)\n\n"
                            "🎉 زود باش برو از **امیر** (@Ciah_am) تشکر کن! 🙏"
                )
            await query.delete_message()
        except Exception as e2:
            logger.error(f"send_document error: {e2}")
            await query.edit_message_text(
                f"❌ خطا در ارسال ویدئو. اما می‌توانید از لینک مستقیم دانلود کنید:\n{download_url}\n\n"
                "🎉 زود باش برو از **امیر** (@Ciah_am) تشکر کن! 🙏"
            )
    finally:
        try:
            os.remove(file_path)
        except:
            pass

# ---------- ساخت اپلیکیشن ربات ----------
application = Application.builder().token(TOKEN).build()
application.add_handler(CommandHandler("start", start))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
application.add_handler(CallbackQueryHandler(button_callback))

# ---------- FastAPI (برای Cloudflare Pages Functions) ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # تنظیم وب‌هوک هنگام راه‌اندازی
    webhook_url = f"https://{os.environ.get('PROJECT_DOMAIN', 'your-project.pages.dev')}{WEBHOOK_PATH}"
    await application.bot.set_webhook(url=webhook_url, drop_pending_updates=True)
    logger.info(f"Webhook set to {webhook_url}")
    yield
    # در صورت خاموشی (اختیاری)
    await application.bot.delete_webhook()

app = FastAPI(lifespan=lifespan)

# این متغیر برای دسترسی به KV در handlerها استفاده می‌شود (در محیط Pages)
# ما از یک global استفاده می‌کنیم که در طول درخواست مقداردهی می‌شود.
# اما بهتر است از context استفاده کنیم.
# برای سادگی، یک کلاس Context با env می‌سازیم.
class EnvContext:
    def __init__(self, env):
        self.env = env

# این تابع برای دسترسی به env درون requestها استفاده می‌شود
# اما ما در handlerها مستقیماً از env استفاده نمی‌کنیم، بلکه از توابع کمکی که از env استفاده می‌کنند.
# برای اینکه توابع کمکی به env دسترسی داشته باشند، یک متغیر سراسری تعریف می‌کنیم.
env_global = None

async def get_kv_value(key: str) -> Optional[str]:
    if env_global and hasattr(env_global, 'KV_NAMESPACE'):
        kv = getattr(env_global, os.environ.get("KV_NAMESPACE", "BOT_STATE"), None)
        if kv:
            return await kv.get(key)
    return None

async def set_kv_value(key: str, value: str, ttl: int = 300):
    if env_global and hasattr(env_global, 'KV_NAMESPACE'):
        kv = getattr(env_global, os.environ.get("KV_NAMESPACE", "BOT_STATE"), None)
        if kv:
            await kv.put(key, value, expiration_ttl=ttl)

# بازنویسی توابع KV با استفاده از env_global
# (در کد بالا توابع قبلی را با این نسخه جایگزین کنید)

# ---------- Webhook endpoint ----------
@app.post(WEBHOOK_PATH)
async def webhook(request: Request):
    global env_global
    # دریافت env از request (در Pages Functions از طریق request.state یا bindings)
    # ما از request.scope برای دریافت env استفاده می‌کنیم
    env_global = request.scope.get("env")  # در Pages Functions، env در scope قرار دارد
    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return Response(status_code=200)

@app.get("/")
async def root():
    return {"status": "ok", "message": "ربات دانلودر امیر فعال است"}

# ---------- اجرای محلی (برای تست) ----------
if __name__ == "__main__":
    import uvicorn
    # برای تست محلی، KV را شبیه‌سازی می‌کنیم
    # می‌توانید از یک دیکشنری استفاده کنید
    uvicorn.run(app, host="0.0.0.0", port=8080)
