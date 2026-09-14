"""
ربات تلگرام: دریافت پیام صوتی -> تبدیل به متن و خلاصه‌سازی
با استفاده از Groq API (سهمیه رایگان، سبک، مناسب هاست رایگان مثل Railway)

نیازی به نصب ffmpeg یا مدل لوکال نیست.
"""

import asyncio
import html
import logging
import os
import re
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

groq_client = Groq(api_key=GROQ_API_KEY, timeout=90.0)

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

_SEP_SUMMARY = "@@SUMMARY@@"

_HTML_FORMAT_RULES = (
    "خروجی خلاصه را طوری بنویس که خوانا و دسته‌بندی‌شده باشد، دقیقاً با این قوانین "
    "قالب‌بندی HTML سادهٔ تلگرام (فقط از تگ <b>...</b> برای تیترها استفاده کن، "
    "هیچ تگ دیگری مثل <ul> یا <h1> یا Markdown با ستاره به کار نبر):\n"
    "- هر تیتر بخش را داخل <b>...</b> بگذار و یک ایموجی مرتبط قبلش بیاور\n"
    "- زیر هر تیتر، نکات را در خط‌های جدا و هرکدام با «• » شروع کن\n"
    "- ساختار پیشنهادی: <b>🎯 موضوع اصلی</b> (یک خط)، <b>🔑 نکات کلیدی</b> "
    "(چند بولت)، <b>📌 جمع‌بندی</b> (یک یا دو خط)\n"
)

# متن اصلی طولانی به تکه‌هایی با این حداکثر طول تقسیم و هر تکه جدا فرمت‌بندی می‌شود
CHUNK_CHAR_SIZE = 1800
# حداکثر تعداد تکه‌هایی که با هوش مصنوعی فرمت‌بندی می‌شوند (برای کنترل هزینه/زمان)
MAX_FORMAT_CHUNKS = 6


def split_into_sentence_chunks(text: str, max_chars: int = CHUNK_CHAR_SIZE):
    """متن را در مرز جمله‌ها به تکه‌هایی با حداکثر طول مشخص تقسیم می‌کند."""
    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
    chunks, current = [], ""
    for sentence in sentences:
        candidate = f"{current} {sentence}".strip() if current else sentence
        if len(candidate) > max_chars and current:
            chunks.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks or [text]


def generate_summary(raw_text: str) -> str:
    """یک خلاصه حرفه‌ای و ساختاریافته (با تیتر و بولت) از کل متن می‌سازد."""
    prompt = (
        "متن زیر رونوشت خام یک تشخیص گفتار (speech-to-text) فارسی است که ممکن "
        "است غلط‌های تشخیصی داشته باشد. کل متن را نادیده بگیر و فقط یک خلاصه "
        "حرفه‌ای و بدون غلط از محتوای اصلی آن بنویس.\n"
        f"{_HTML_FORMAT_RULES}\n"
        "خروجی را دقیقاً با این نشانه شروع کن (بدون هیچ متن دیگری قبلش):\n\n"
        f"{_SEP_SUMMARY}\n\n"
        f"متن خام:\n{raw_text}"
    )
    response = groq_client.chat.completions.create(
        model=SUMMARY_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1500,
    )
    content = response.choices[0].message.content.strip()
    if _SEP_SUMMARY in content:
        return content.split(_SEP_SUMMARY, 1)[1].strip()
    return content


def format_chunk(chunk: str) -> str:
    """یک تکه از متن خام را اصلاح، پاراگراف‌بندی و کلیدواژه‌هایش را پررنگ می‌کند."""
    prompt = (
        "متن زیر بخشی از رونوشت خام یک تشخیص گفتار (speech-to-text) فارسی است "
        "که ممکن است غلط‌های تشخیصی، بی‌نقطه‌گذاری بودن، یا کلمات نامفهوم داشته باشد.\n"
        "این متن را ویرایش کن: غلط‌های واضح گفتار را بر اساس بافت جمله تصحیح کن، "
        "نقطه‌گذاری مناسب اضافه کن؛ محتوا و لحن اصلی را عوض نکن. برای خوانایی بهتر:\n"
        "- هر جا موضوع عوض می‌شود یک خط خالی بگذار تا پاراگراف جدید شروع شود "
        "(پاراگراف‌ها را کوتاه، حدود ۲ تا ۴ جمله، نگه دار)\n"
        "- فقط مهم‌ترین عبارات کلیدی (اسم افراد، اعداد و ارقام مهم، "
        "نتیجه‌گیری‌های اصلی) را داخل <b>...</b> پررنگ کن؛ در هر پاراگراف حداکثر "
        "یک یا دو عبارت پررنگ کافی است\n"
        "- به‌جز تگ <b>، از هیچ تگ HTML یا Markdown دیگری استفاده نکن\n"
        "- فقط خودِ متن ویرایش‌شده را برگردان، بدون هیچ توضیح یا مقدمه‌ی اضافه\n\n"
        f"متن:\n{chunk}"
    )
    response = groq_client.chat.completions.create(
        model=SUMMARY_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=2000,
    )
    return response.choices[0].message.content.strip()


def format_full_transcript(raw_text: str) -> str:
    """کل متن خام را (با تکه‌تکه کردن در صورت بلند بودن) فرمت‌بندی می‌کند."""
    chunks = split_into_sentence_chunks(raw_text)
    to_format, remainder = chunks[:MAX_FORMAT_CHUNKS], chunks[MAX_FORMAT_CHUNKS:]

    formatted_parts = []
    for chunk in to_format:
        try:
            formatted_parts.append(format_chunk(chunk))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Chunk formatting failed: %s", exc)
            formatted_parts.append(chunk)  # حداقل خود تکه خام را نگه دار

    if remainder:
        # برای کنترل هزینه/زمان، باقیمانده‌ی خیلی طولانی فقط پاراگراف‌بندی ساده می‌شود
        formatted_parts.append(auto_paragraph(" ".join(remainder)))

    return "\n\n".join(formatted_parts)


def polish_text(raw_text: str):
    """متن خام را فرمت‌بندی و خلاصه حرفه‌ای می‌کند.

    خروجی: دیکشنری با دو کلید corrected و summary (هرکدام ممکن است None باشد
    اگر آن مرحله خطا داد)، یا None اگر هر دو مرحله شکست خوردند.
    """
    summary = None
    corrected = None

    try:
        summary = generate_summary(raw_text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("خلاصه‌سازی ناموفق بود: %s", exc)

    try:
        corrected = format_full_transcript(raw_text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("فرمت‌بندی متن اصلی ناموفق بود: %s", exc)

    if summary is None and corrected is None:
        return None
    return {"corrected": corrected, "summary": summary}


# ---------------------------------------------------------------------------
# هندلرهای تلگرام
# ---------------------------------------------------------------------------


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "سلام! یک پیام صوتی (ویس) برام بفرست تا متنش کنم و خلاصه‌اش رو بهت بدم."
    )


TELEGRAM_MAX_LEN = 4000  # کمی کمتر از سقف واقعی تلگرام (۴۰۹۶) برای احتیاط
_TAG_STRIP_RE = re.compile(r"<[^>]+>")


async def _safe_edit(status_msg, text: str) -> None:
    """پیام را با فرمت HTML ویرایش می‌کند؛ اگر تلگرام فرمت را رد کرد، متن ساده می‌فرستد."""
    try:
        await status_msg.edit_text(text, parse_mode="HTML")
    except Exception:  # noqa: BLE001
        logger.warning("HTML parse failed, falling back to plain text")
        await status_msg.edit_text(_TAG_STRIP_RE.sub("", text))


async def _safe_send(chat, text: str) -> None:
    try:
        await chat.send_message(text, parse_mode="HTML")
    except Exception:  # noqa: BLE001
        logger.warning("HTML parse failed, falling back to plain text")
        await chat.send_message(_TAG_STRIP_RE.sub("", text))


async def send_long_reply(status_msg, chat, text: str) -> None:
    """پاسخ را در صورت طولانی بودن به چند پیام تقسیم می‌کند (سقف تلگرام ۴۰۹۶ کاراکتر)."""
    if len(text) <= TELEGRAM_MAX_LEN:
        await _safe_edit(status_msg, text)
        return

    chunks = [
        text[i : i + TELEGRAM_MAX_LEN] for i in range(0, len(text), TELEGRAM_MAX_LEN)
    ]
    await _safe_edit(status_msg, chunks[0])
    for chunk in chunks[1:]:
        await _safe_send(chat, chunk)


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!؟])\s+")


def auto_paragraph(text: str, sentences_per_paragraph: int = 3) -> str:
    """متن خام را (بدون تماس اضافه با مدل) به پاراگراف‌های کوتاه‌تر تقسیم می‌کند
    تا خواندنش راحت‌تر شود."""
    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
    if len(sentences) <= sentences_per_paragraph:
        return text

    paragraphs = [
        " ".join(sentences[i : i + sentences_per_paragraph])
        for i in range(0, len(sentences), sentences_per_paragraph)
    ]
    return "\n\n".join(paragraphs)


def _sanitize_model_html(text: str) -> str:
    """فقط تگ‌های <b> و </b> را نگه می‌دارد و بقیه‌ی متن را برای HTML امن می‌کند
    (جلوگیری از خطای تلگرام به‌خاطر تگ یا کاراکتر غیرمنتظره از خروجی مدل)."""
    text = text.replace("<b>", "\x00B\x00").replace("</b>", "\x00/B\x00")
    text = html.escape(text, quote=False)
    text = text.replace("\x00B\x00", "<b>").replace("\x00/B\x00", "</b>")
    return text


async def build_final_reply(raw_text: str, loop, label: str) -> str:
    """از متن خام، پاسخ نهایی HTML (متن اصلاح‌شده/خام + خلاصه ساختاریافته) را می‌سازد."""
    if not ENABLE_SUMMARY:
        return f"<b>📝 متن {label}:</b>\n{html.escape(auto_paragraph(raw_text))}"

    result = await loop.run_in_executor(None, polish_text, raw_text)
    if not result:
        # اصلاح/خلاصه‌سازی شکست خورد؛ حداقل متن خام را بفرست
        return f"<b>📝 متن {label}:</b>\n{html.escape(auto_paragraph(raw_text))}"

    lines = []
    if result.get("corrected"):
        lines.append(
            f"<b>📝 متن {label} (اصلاح‌شده):</b>\n{_sanitize_model_html(result['corrected'])}"
        )
    else:
        lines.append(f"<b>📝 متن {label}:</b>\n{html.escape(auto_paragraph(raw_text))}")

    if result.get("summary"):
        lines.append(f"\n\n{_sanitize_model_html(result['summary'])}")

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
        try:
            final_reply = await build_final_reply(text, loop, "ویس")
            await send_long_reply(status_msg, update.message.chat, final_reply)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to build/send final reply")
            await status_msg.edit_text(
                f"📝 متن ویس:\n{text}\n\n(خطا در مرحله خلاصه‌سازی: {exc})"
            )


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
        try:
            final_reply = await build_final_reply(text, loop, "ویدیو")
            await send_long_reply(status_msg, msg.chat, final_reply)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to build/send final reply")
            await status_msg.edit_text(
                f"📝 متن ویدیو:\n{text}\n\n(خطا در مرحله خلاصه‌سازی: {exc})"
            )


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
