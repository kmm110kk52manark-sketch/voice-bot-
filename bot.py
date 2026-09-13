"""
ربات تلگرام: دریافت پیام صوتی -> تبدیل به متن و خلاصه‌سازی
با استفاده از Groq API (سهمیه رایگان، سبک، مناسب هاست رایگان مثل Railway)

نیازی به نصب ffmpeg یا مدل لوکال نیست.
"""

import asyncio
import logging
import os
import tempfile

from groq import Groq
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# تنظیمات (از متغیرهای محیطی خوانده می‌شود - در Railway تنظیم می‌کنید)
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]

WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "whisper-large-v3")
SUMMARY_MODEL = os.environ.get("SUMMARY_MODEL", "llama-3.1-8b-instant")
WHISPER_LANGUAGE = os.environ.get("WHISPER_LANGUAGE", "fa")
ENABLE_SUMMARY = os.environ.get("ENABLE_SUMMARY", "true").lower() == "true"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

groq_client = Groq(api_key=GROQ_API_KEY)

# ---------------------------------------------------------------------------
# تبدیل صدا به متن
# ---------------------------------------------------------------------------


def transcribe_audio(file_path: str) -> str:
    with open(file_path, "rb") as audio_file:
        result = groq_client.audio.transcriptions.create(
            file=audio_file,
            model=WHISPER_MODEL,
            language=WHISPER_LANGUAGE or None,
        )
    return result.text.strip()


# ---------------------------------------------------------------------------
# خلاصه/توضیح متن
# ---------------------------------------------------------------------------


def summarize_text(text: str):
    try:
        prompt = (
            "متن زیر رونوشت یک پیام صوتی است. آن را به فارسی، در ۲ تا ۴ جمله، "
            "خلاصه و روشن توضیح بده:\n\n" + text
        )
        response = groq_client.chat.completions.create(
            model=SUMMARY_MODEL,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.choices[0].message.content.strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("خلاصه‌سازی ناموفق بود: %s", exc)
        return None


# ---------------------------------------------------------------------------
# هندلرهای تلگرام
# ---------------------------------------------------------------------------


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "سلام! یک پیام صوتی (ویس) برام بفرست تا متنش کنم و خلاصه‌اش رو بهت بدم."
    )


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    voice = update.message.voice or update.message.audio
    if voice is None:
        return

    status_msg = await update.message.reply_text("در حال دریافت ویس... ⏳")

    with tempfile.TemporaryDirectory() as tmp_dir:
        ogg_path = os.path.join(tmp_dir, "voice.ogg")
        tg_file = await context.bot.get_file(voice.file_id)
        await tg_file.download_to_drive(ogg_path)

        await status_msg.edit_text("در حال تبدیل صدا به متن... 🎙️")

        loop = asyncio.get_running_loop()
        try:
            text = await loop.run_in_executor(None, transcribe_audio, ogg_path)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Transcription failed")
            await status_msg.edit_text(f"خطا در تبدیل صدا به متن: {exc}")
            return

        if not text:
            await status_msg.edit_text("متأسفانه چیزی از این ویس تشخیص داده نشد.")
            return

        reply_lines = [f"📝 متن ویس:\n{text}"]

        if ENABLE_SUMMARY:
            await status_msg.edit_text("در حال خلاصه‌سازی... ✍️")
            summary = await loop.run_in_executor(None, summarize_text, text)
            if summary:
                reply_lines.append(f"\n\n📌 خلاصه/توضیح:\n{summary}")

        await status_msg.edit_text("\n".join(reply_lines))


# ---------------------------------------------------------------------------
# اجرای ربات
# ---------------------------------------------------------------------------


def main() -> None:
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    logger.info("Bot is starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
