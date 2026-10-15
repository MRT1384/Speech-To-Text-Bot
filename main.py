import os
import asyncio
import logging
import tempfile
import uuid

from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import speech_recognition as sr
from pydub import AudioSegment

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.request import HTTPXRequest


# =========================================================
# تنظیمات دسترسی و محدودیت‌ها
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")

# ۱. شناسه‌های کاربری (User ID) افراد VIP (بدون هیچ‌گونه محدودیت)
vip_env = os.getenv("VIP_USER_IDS")
VIP_USER_IDS = [int(uid.strip()) for uid in vip_env.split(",") if uid.strip()]

# ۲. شناسه‌های کاربری (User ID) افراد مجاز با محدودیت
limited_env = os.getenv("LIMITED_USER_IDS")
LIMITED_USER_IDS = [int(uid.strip()) for uid in limited_env.split(",") if uid.strip()]

# ۳. کلید کنترلی برای افراد ناشناس (خارج از لیست‌های بالا):
# True  -> افراد ناشناس هم می‌توانند با اعمال محدودیت از ربات استفاده کنند.
# False -> افراد ناشناس کاملاً مسدود هستند و فقط لیست‌های بالا اجازه دارند.
ALLOW_UNLISTED_USERS = os.getenv("ALLOW_UNLISTED_USERS").lower() in ("true", "1", "yes", "t")

MAX_Size_MB = int(os.getenv("MAX_SIZE_MB"))
MAX_DURATION_MiNUTE = int(os.getenv("MAX_DURATION_MINUTE"))

MAX_FILE_SIZE = MAX_Size_MB * 1024 * 1024  # MB (برای کاربران محدود)
MAX_DURATION_MS = MAX_DURATION_MiNUTE * 60 * 1000  # دقیقه (برای کاربران محدود)
CHUNK_LENGTH_MS = 60 * 1000       # یک دقیقه

REPORT_DELETE_SECONDS = int(os.getenv("REPORT_DELETE_SECONDS"))
MAX_CONCURRENT_TASKS = int(os.getenv("MAX_CONCURRENT_TASKS"))

BASE_DIR = Path(tempfile.gettempdir()) / "cvtt_bot"
INPUT_DIR = BASE_DIR / "input"
OUTPUT_DIR = BASE_DIR / "output"

INPUT_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# =========================================================
# لاگ
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("CVTTBot")


# =========================================================
# وضعیت پردازش‌ها
# =========================================================

executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_TASKS)
processing_semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)


# =========================================================
# ابزارهای عمومی
# =========================================================

async def delete_message_safely(message):
    """حذف پیام بدون ایجاد خطای اضافی."""
    if message is None:
        return
    try:
        await message.delete()
    except Exception as exc:
        logger.debug("حذف پیام انجام نشد: %s", exc)


async def delete_after_delay(message, delay=REPORT_DELETE_SECONDS):
    """حذف پیام پس از مدت مشخص."""
    if message is None:
        return
    await asyncio.sleep(delay)
    await delete_message_safely(message)


async def send_temp_message(message, text, delay=REPORT_DELETE_SECONDS):
    """ارسال پیام موقت."""
    try:
        sent = await message.reply_text(text)
        asyncio.create_task(delete_after_delay(sent, delay))
        return sent
    except Exception as exc:
        logger.warning("ارسال پیام موقت ناموفق بود: %s", exc)
        return None


async def update_status(status_message, text):
    """به‌روزرسانی پیام وضعیت."""
    if status_message is None:
        return
    try:
        await status_message.edit_text(text)
    except Exception as exc:
        logger.debug("به‌روزرسانی وضعیت انجام نشد: %s", exc)


# =========================================================
# تشخیص فایل صوتی
# =========================================================

def get_audio_attachment(message):
    """دریافت اطلاعات فایل صوتی از پیام تلگرام."""
    if message is None:
        return None

    if message.voice:
        return message.voice

    if message.audio:
        return message.audio

    document = message.document
    if document:
        mime_type = (document.mime_type or "").lower()
        if mime_type.startswith("audio/"):
            return document

    return None


def get_audio_suffix(attachment):
    """تعیین پسوند فایل ورودی."""
    file_name = getattr(attachment, "file_name", None)
    if file_name:
        suffix = Path(file_name).suffix.lower()
        if suffix:
            return suffix

    mime_type = (getattr(attachment, "mime_type", None) or "").lower()
    if "ogg" in mime_type or "opus" in mime_type:
        return ".ogg"
    if "mpeg" in mime_type or "mp3" in mime_type:
        return ".mp3"
    if "wav" in mime_type:
        return ".wav"
    if "mp4" in mime_type or "m4a" in mime_type:
        return ".m4a"
    if "flac" in mime_type:
        return ".flac"

    return ".audio"


# =========================================================
# تبدیل صوت به متن
# =========================================================

def recognize_chunk(chunk, chunk_path):
    """ذخیره یک قطعه به‌صورت WAV و تبدیل آن به متن فارسی."""
    recognizer = sr.Recognizer()
    chunk.export(chunk_path, format="wav")

    try:
        with sr.AudioFile(chunk_path) as source:
            audio_data = recognizer.record(source)

        text = recognizer.recognize_google(
            audio_data,
            language="fa-IR",
        )
        return text.strip()
    finally:
        try:
            if os.path.exists(chunk_path):
                os.remove(chunk_path)
        except OSError:
            pass


def format_time(milliseconds):
    """تبدیل میلی‌ثانیه به قالب دقیقه و ثانیه."""
    total_seconds = milliseconds // 1000
    minutes = total_seconds // 60
    seconds = total_seconds % 60
    return f"{minutes:02d}:{seconds:02d}"


# =========================================================
# دستورات راهنما و تبدیل در گروه
# =========================================================

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نمایش راهنمای استفاده از ربات."""
    message = update.effective_message
    if not message:
        return

    help_text = (
        "📖 **راهنمای استفاده از ربات تبدیل صوت به متن**\n\n"
        "🔹 **در گفت‌وگوی خصوصی (PV):**\n"
        "کافیست ویس یا فایل صوتی خود را بفرستید تا به‌صورت خودکار تبدیل شود.\n\n"
        "🔹 **در گروه‌ها:**\n"
        "روی ویس مورد نظر **ریپلای (Reply)** کرده و دستور `/convert` را بفرستید.\n\n"
        f"⚠️ حداکثر مدت: {MAX_DURATION_MiNUTE} دقیقه | حداکثر حجم: {MAX_Size_MB} مگابایت"
    )
    await message.reply_text(help_text, parse_mode="Markdown")


async def convert_group_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """دستور تبدیل ویس در گروه‌ها (با ریپلای)."""
    message = update.effective_message
    chat = update.effective_chat

    if not message or not chat:
        return

    if chat.type not in ("group", "supergroup"):
        return

    replied_message = message.reply_to_message
    if replied_message is None:
        await send_temp_message(
            message,
            "🎤 برای تبدیل ویس، ابتدا روی همان ویس ریپلای کن و سپس دستور /convert را بفرست."
        )
        return

    if get_audio_attachment(replied_message) is None:
        await send_temp_message(
            message,
            "⚠️ پیامی که به آن ریپلای کرده‌اید، ویس یا فایل صوتی نیست."
        )
        return

    await process_audio_message(replied_message, context, request_user=message)


async def process_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """هندلر پیام صوتی در خصوصی."""
    message = update.effective_message
    chat = update.effective_chat

    if not message or not chat:
        return

    if chat.type in ("group", "supergroup"):
        return

    if chat.type != "private":
        return

    await process_audio_message(message, context, request_user=message)


async def process_audio_message(message, context: ContextTypes.DEFAULT_TYPE, request_user=None):
    """پردازش اصلی پیام صوتی با بررسی سطوح دسترسی بر اساس کاربر درخواست‌دهنده."""
    if message is None or message.chat is None:
        return

    chat = message.chat
    
    # شناسایی دقیق کاربر درخواست‌دهنده (چه در گروه چه در پی‌وی)
    user_msg = request_user if request_user else message
    user = user_msg.from_user
    user_id = user.id if user else None

    # بررسی سطح دسترسی کاربر
    if user_id in VIP_USER_IDS:
        is_vip = True
        is_allowed = True
    elif user_id in LIMITED_USER_IDS:
        is_vip = False
        is_allowed = True
    else:
        is_vip = False
        is_allowed = ALLOW_UNLISTED_USERS

    # اگر کاربر اجازه استفاده نداشت، به پیام خودش ریپلای بدهیم
    if not is_allowed:
        await send_temp_message(
            user_msg,
            "⛔ شما اجازه استفاده از این ربات را ندارید."
        )
        return

    attachment = get_audio_attachment(message)
    if attachment is None:
        return

    # بررسی حجم فایل (فقط برای افراد غیر VIP)
    file_size = getattr(attachment, "file_size", None)
    if not is_vip and file_size is not None and file_size > MAX_FILE_SIZE:
        await send_temp_message(user_msg, f"⚠️ حجم فایل بیشتر از {MAX_Size_MB} مگابایت است.")
        return

    job_id = uuid.uuid4().hex
    suffix = get_audio_suffix(attachment)

    input_path = INPUT_DIR / f"{job_id}{suffix}"
    output_path = OUTPUT_DIR / f"{job_id}.txt"

    chunk_paths = []
    status_message = None

    async with processing_semaphore:
        try:
            status_message = await message.reply_text(
                "⏳ فایل صوتی دریافت شد.\nدر حال دانلود و بررسی فایل..."
            )

            telegram_file = await context.bot.get_file(attachment.file_id)
            await telegram_file.download_to_drive(custom_path=str(input_path))

            actual_size = input_path.stat().st_size
            if not is_vip and actual_size > MAX_FILE_SIZE:
                await update_status(status_message, f"⚠️ حجم فایل بیشتر از {MAX_Size_MB} مگابایت است.")
                asyncio.create_task(delete_after_delay(status_message))
                status_message = None
                return

            await update_status(
                status_message,
                "🔍 فایل دریافت شد.\nدر حال بررسی مدت‌زمان صوت..."
            )

            try:
                audio = await asyncio.to_thread(AudioSegment.from_file, str(input_path))
            except Exception:
                logger.exception("خطا در بازکردن فایل صوتی: %s", input_path)
                await update_status(
                    status_message,
                    "❌ فایل صوتی قابل خواندن نیست.\nفرمت فایل یا نصب FFmpeg را بررسی کن."
                )
                asyncio.create_task(delete_after_delay(status_message))
                status_message = None
                return

            duration_ms = len(audio)

            if duration_ms <= 0:
                await update_status(status_message, "⚠️ فایل صوتی خالی است.")
                asyncio.create_task(delete_after_delay(status_message))
                status_message = None
                return

            if not is_vip and duration_ms > MAX_DURATION_MS:
                await update_status(status_message, f"⚠️ مدت فایل بیشتر از {MAX_DURATION_MiNUTE} دقیقه است.")
                asyncio.create_task(delete_after_delay(status_message))
                status_message = None
                return

            total_chunks = (duration_ms + CHUNK_LENGTH_MS - 1) // CHUNK_LENGTH_MS

            logger.info(
                "شروع پردازش | chat_id=%s | message_id=%s | duration_ms=%s | chunks=%s",
                chat.id, message.message_id, duration_ms, total_chunks,
            )

            sections = []

            for index, start_ms in enumerate(
                range(0, duration_ms, CHUNK_LENGTH_MS),
                start=1,
            ):
                end_ms = min(start_ms + CHUNK_LENGTH_MS, duration_ms)
                chunk = audio[start_ms:end_ms]

                chunk_path = INPUT_DIR / f"{job_id}_chunk_{index}.wav"
                chunk_paths.append(chunk_path)

                start_time = format_time(start_ms)
                end_time = format_time(end_ms)

                await update_status(
                    status_message,
                    f"🎙️ در حال تبدیل صوت به متن...\n\n"
                    f"بخش: {index} از {total_chunks}\n"
                    f"زمان: {start_time} تا {end_time}\n"
                    f"پیشرفت: {index / total_chunks:.0%}"
                )

                try:
                    text = await asyncio.to_thread(recognize_chunk, chunk, str(chunk_path))
                    section_text = text if text else "[این بخش متن قابل تشخیصی برنگرداند.]"
                except sr.UnknownValueError:
                    section_text = "[صدای این بخش توسط سرویس تشخیص گفتار قابل تشخیص نبود.]"
                except sr.RequestError as exc:
                    logger.warning("خطای سرویس تشخیص گفتار در بخش %s: %s", index, exc)
                    section_text = "[خطا در ارتباط با سرویس تشخیص گفتار؛ این بخش تبدیل نشد.]"
                except Exception as exc:
                    logger.exception("خطا در پردازش بخش %s", index)
                    section_text = f"[خطای پردازش این بخش: {type(exc).__name__}]"

                sections.append(
                    f"دقیقه {index} ({start_time} تا {end_time})\n\n{section_text}"
                )

            separator = "\n\n" + ("-" * 35) + "\n\n"
            final_text = separator.join(sections)

            with open(output_path, "w", encoding="utf-8-sig") as file:
                file.write("گزارش تبدیل صوت به متن\n")
                file.write("=" * 35 + "\n\n")
                file.write(f"مدت صوت: {format_time(duration_ms)}\n")
                file.write(f"تعداد بخش‌ها: {total_chunks}\n\n")
                file.write(final_text)

            await update_status(
                status_message,
                "✅ تبدیل صوت تمام شد.\nدر حال ارسال فایل متنی...",
            )

            caption = (
                "✅ تبدیل صوت به متن انجام شد.\n"
                f"⏱ مدت صوت: {format_time(duration_ms)}\n"
                f"📝 تعداد بخش‌ها: {total_chunks}"
            )

            with open(output_path, "rb") as result_file:
                await message.reply_document(
                    document=result_file,
                    filename="transcription.txt",
                    caption=caption,
                    read_timeout=120,
                    write_timeout=120,
                    connect_timeout=30,
                    pool_timeout=30,
                )

            logger.info("پردازش موفق | chat_id=%s | message_id=%s", chat.id, message.message_id)

            await delete_message_safely(status_message)
            status_message = None

        except Exception:
            logger.exception("خطای کلی پردازش | chat_id=%s | message_id=%s", chat.id, message.message_id)

            if status_message is not None:
                await update_status(
                    status_message,
                    "❌ هنگام پردازش فایل خطایی رخ داد.\nگزارش خطا در ترمینال ثبت شده است."
                )
                asyncio.create_task(delete_after_delay(status_message))
                status_message = None
            else:
                await send_temp_message(user_msg, "❌ پردازش فایل با خطا مواجه شد.")

        finally:
            paths_to_remove = [input_path, output_path, *chunk_paths]
            for path in paths_to_remove:
                try:
                    if path.exists():
                        path.unlink()
                except OSError as exc:
                    logger.warning("حذف فایل موقت ناموفق بود: %s | %s", path, exc)


# =========================================================
# مدیریت خطاهای برنامه
# =========================================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("خطای مدیریت‌نشده در برنامه", exc_info=context.error)


# =========================================================
# اجرای ربات
# =========================================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError("متغیر محیطی BOT_TOKEN تنظیم نشده است.")

    request = HTTPXRequest(
        connection_pool_size=10,
        connect_timeout=30,
        read_timeout=120,
        write_timeout=120,
        pool_timeout=30,
    )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(request)
        .build()
    )

    application.add_handler(CommandHandler(["start", "help"], help_command))
    application.add_handler(CommandHandler("convert", convert_group_command))

    audio_filter = (
        filters.VOICE
        | filters.AUDIO
        | filters.Document.AUDIO
    )
    application.add_handler(MessageHandler(audio_filter, process_audio))

    application.add_error_handler(error_handler)

    logger.info("🤖 Bot is up and running...")
    application.run_polling(allowed_updates=["message"], drop_pending_updates=False)


if __name__ == "__main__":
    main()