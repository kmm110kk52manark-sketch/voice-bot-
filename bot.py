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
SUMMARY_MODEL = os.environ.get("SUMMARY_MODEL", "openai/gpt-oss-120b")
WHISPER_LANGUAGE = os.environ.get("WHISPER_LANGUAGE", "fa")
ENABLE_SUMMARY = os.environ.get("ENABLE_SUMMARY", "true").lower() == "true"

# متن راهنما به مدل برای بهتر تشخیص دادن اسامی خاص و اصطلاحات رایج.
# می‌توانید در Railway، متغیر WHISPER_PROMPT را با اسامی/اصطلاحات پرتکرار
# محتوای خودتان (اسم افراد، برندها، واژه‌های تخصصی) جایگزین یا تکمیل کنید.
WHISPER_PROMPT = os.environ.get(
    "WHISPER_PROMPT",
    "این یک فایل صوتی گفتاری، محاوره‌ای و روان به زبان فارسی است. "
    "لطفاً علائم نگارشی مناسب (نقطه، ویرگول) را رعایت کن. "
    "نمونه اسامی رایج: ایلان ماسک، پیکاسو، اینستاگرام، بیت‌کوین.",
)

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
            prompt=WHISPER_PROMPT or None,
            temperature=0,
        )
    return result.text.strip()


# ---------------------------------------------------------------------------
# استخراج و نرمال‌سازی صدا (از ویس یا ویدیو)
# ---------------------------------------------------------------------------

# فیلتر نرمال‌سازی بلندی صدا + کاهش نویز فرکانس پایین (مثل هوم/نویز پس‌زمینه)
_AUDIO_FILTER = "highpass=f=80,loudnorm=I=-16:TP=-1.5:LRA=11"


def extract_audio_from_video(video_path: str, audio_path: str) -> None:
    """صدا را از فایل ویدیویی جدا و نرمال‌سازی می‌کند."""
    ffmpeg_bin = imageio_ffmpeg.get_ffmpeg_exe()
    subprocess.run(
        [
            ffmpeg_bin,
            "-y",
            "-i",
            video_path,
            "-vn",
            "-af",
            _AUDIO_FILTER,
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


def normalize_audio(input_path: str, output_path: str) -> None:
    """فایل صوتی (مثل ویس تلگرام) را نرمال‌سازی می‌کند تا دقت تشخیص گفتار بهتر شود."""
    ffmpeg_bin = imageio_ffmpeg.get_ffmpeg_exe()
    subprocess.run(
        [
            ffmpeg_bin,
            "-y",
            "-i",
            input_path,
            "-af",
            _AUDIO_FILTER,
            "-ar",
            "16000",
            "-ac",
            "1",
            output_path,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


# ---------------------------------------------------------------------------
# اصلاح متن + خلاصه/توضیح حرفه‌ای
# ---------------------------------------------------------------------------

_SEPARATOR = "---خلاصه---"


def polish_text(raw_text: str):
    """متن خام رونوشت را اصلاح (غلط‌گیری/نقطه‌گذاری) و خلاصه حرفه‌ای می‌کند.

    خروجی: دیکشنری با دو کلید corrected و summary، یا None در صورت خطا.
    """
    try:
        prompt = (
            "متن زیر رونوشت خام یک تشخیص گفتار (speech-to-text) فارسی است که "
            "ممکن است غلط‌های تشخیصی، بی‌نقطه‌گذاری بودن، یا کلمات نامفهوم داشته باشد.\n\n"
            "۱. ابتدا این متن را ویرایش کن: غلط‌های واضح تشخیص گفتار را بر اساس "
            "بافت جمله تصحیح کن، نقطه‌گذاری مناسب (نقطه، ویرگول) اضافه کن، ولی "
            "محتوا و لحن اصلی گوینده را عوض نکن و چیزی از خودت اضافه نکن.\n"
            "۲. سپس یک خلاصه حرفه‌ای و روشن در ۲ تا ۴ جمله از متن بنویس.\n\n"
            f"خروجی را دقیقاً با همین قالب بده (بدون توضیح اضافه):\n"
            f"متن اصلاح‌شده اینجا\n{_SEPARATOR}\nخلاصه اینجا\n\n"
            f"متن خام:\n{raw_text}"
        )
        response = groq_client.chat.completions.create(
            model=SUMMARY_MODEL,
            messages=[{"role": "user", "content": prompt}],
        )
        content = response.choices[0].message.content.strip()

        if _SEPARATOR in content:
            corrected, summary = content.split(_SEPARATOR, 1)
            return {"corrected": corrected.strip(), "summary": summary.strip()}
        # اگر مدل قالب را رعایت نکرد، کل خروجی را به‌عنوان خلاصه در نظر می‌گیریم
        return {"corrected": None, "summary": content}
    except Exception as exc:  # noqa: BLE001
        logger.warning("اصلاح/خلاصه‌سازی ناموفق بود: %s", exc)
        return None


# ---------------------------------------------------------------------------
# هندلرهای تلگرام
# ---------------------------------------------------------------------------


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "سلام! یک پیام صوتی (ویس) برام بفرست تا متنش کنم و خلاصه‌اش رو بهت بدم."
    )


async def build_final_reply(raw_text: str, loop, label: str) -> str:
    """از متن خام، پاسخ نهایی (متن اصلاح‌شده + خلاصه یا فقط متن خام) را می‌سازد."""
    if not ENABLE_SUMMARY:
        return f"📝 متن {label}:\n{raw_text}"

    result = await loop.run_in_executor(None, polish_text, raw_text)
    if not result:
        # اصلاح/خلاصه‌سازی شکست خورد؛ حداقل متن خام را بفرست
        return f"📝 متن {label}:\n{raw_text}"

    lines = []
    if result.get("corrected"):
        lines.append(f"📝 متن {label} (اصلاح‌شده):\n{result['corrected']}")
    else:
        lines.append(f"📝 متن {label}:\n{raw_text}")

    if result.get("summary"):
        lines.append(f"\n\n📌 خلاصه حرفه‌ای:\n{result['summary']}")

    return "\n".join(lines)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    voice = update.message.voice or update.message.audio
    if voice is None:
        return

    status_msg = await update.message.reply_text("در حال دریافت ویس... ⏳")

    with tempfile.TemporaryDirectory() as tmp_dir:
        ogg_path = os.path.join(tmp_dir, "voice.ogg")
        wav_path = os.path.join(tmp_dir, "voice_normalized.wav")
        tg_file = await context.bot.get_file(voice.file_id)
        await tg_file.download_to_drive(ogg_path)

        loop = asyncio.get_running_loop()

        await status_msg.edit_text("در حال بهبود کیفیت صدا... 🔊")
        try:
            await loop.run_in_executor(None, normalize_audio, ogg_path, wav_path)
            audio_for_transcription = wav_path
        except subprocess.CalledProcessError:
            # اگر نرمال‌سازی به هر دلیلی شکست خورد، از فایل اصلی استفاده می‌کنیم
            logger.warning("Audio normalization failed; falling back to raw file")
            audio_for_transcription = ogg_path

        await status_msg.edit_text("در حال تبدیل صدا به متن... 🎙️")

        try:
            text = await loop.run_in_executor(
                None, transcribe_audio, audio_for_transcription
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Transcription failed")
            await status_msg.edit_text(f"خطا در تبدیل صدا به متن: {exc}")
            return

        if not text:
            await status_msg.edit_text("متأسفانه چیزی از این ویس تشخیص داده نشد.")
            return

        await status_msg.edit_text("در حال اصلاح و خلاصه‌سازی... ✍️")
        final_reply = await build_final_reply(text, loop, "ویس")
        await status_msg.edit_text(final_reply)


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

        await status_msg.edit_text("در حال اصلاح و خلاصه‌سازی... ✍️")
        final_reply = await build_final_reply(text, loop, "ویدیو")
        await status_msg.edit_text(final_reply)


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
