"""
ربات تلگرام: دریافت پیام صوتی -> تبدیل به متن و خلاصه‌سازی
با استفاده از Groq API (سهمیه رایگان، سبک، مناسب هاست رایگان مثل Railway)

نیازی به نصب ffmpeg یا مدل لوکال نیست.
"""

import asyncio
import logging
import os
import subprocess
import tempfile

import imageio_ffmpeg
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
# استخراج صدا از ویدیو
# ---------------------------------------------------------------------------


def extract_audio_from_video(video_path: str, audio_path: str) -> None:
    ffmpeg_bin = imageio_ffmpeg.get_ffmpeg_exe()
    subprocess.run(
        [
            ffmpeg_bin,
            "-y",
            "-i",
            video_path,
            "-vn",
            "-acodec",
            "libmp3lame",
            "-ar",
            "16000",
            "-ac",
            "1",
            audio_path,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


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


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    media = msg.video or msg.video_note or msg.document
    if media is None:
        return

    status_msg = await msg.reply_text("در حال دریافت ویدیو... ⏳")

    with tempfile.TemporaryDirectory() as tmp_dir:
        video_path = os.path.join(tmp_dir, "video.mp4")
        audio_path = os.path.join(tmp_dir, "audio.mp3")

        tg_file = await context.bot.get_file(media.file_id)
        await tg_file.download_to_drive(video_path)

        await status_msg.edit_text("در حال استخراج صدا از ویدیو... 🎬")

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None, extract_audio_from_video, video_path, audio_path
            )
        except subprocess.CalledProcessError:
            await status_msg.edit_text(
                "نتونستم صدا رو از این ویدیو جدا کنم. مطمئن شو فایل واقعاً ویدیوئه."
            )
            return

        await status_msg.edit_text("در حال تبدیل صدا به متن... 🎙️")

        try:
            text = await loop.run_in_executor(None, transcribe_audio, audio_path)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Transcription failed")
            await status_msg.edit_text(f"خطا در تبدیل صدا به متن: {exc}")
            return

        if not text:
            await status_msg.edit_text("متأسفانه چیزی از صدای این ویدیو تشخیص داده نشد.")
            return

        reply_lines = [f"📝 متن ویدیو:\n{text}"]

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
    app.add_handler(
        MessageHandler(
            filters.VIDEO | filters.VIDEO_NOTE | filters.Document.VIDEO,
            handle_video,
        )
    )
    logger.info("Bot is starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
