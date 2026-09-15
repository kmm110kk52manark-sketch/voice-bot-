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
import db as subscriptions
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

# آیدی عددی تلگرام مالک ربات (همیشه دسترسی کامل دارد، حتی بدون اشتراک).
# آیدی عددی خودتان را می‌توانید با فرستادن پیام به رباتی مثل @userinfobot بگیرید.
ADMIN_TELEGRAM_ID = int(os.environ.get("ADMIN_TELEGRAM_ID", "0"))

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


_SEP_TEXT = "@@TEXT@@"
_SEP_POINTS = "@@POINTS@@"


def generate_summary_direct(raw_text: str) -> str:
    """خلاصه‌سازی مستقیم از کل متن خام (فقط به‌عنوان راه برگشت اضطراری)."""
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


def synthesize_summary(combined_points: str) -> str:
    """از روی نکات کلیدی جمع‌آوری‌شده از همه‌ی تکه‌های متن، خلاصه نهایی می‌سازد.

    چون نکات از تک‌تک تکه‌های متن (نه کل متن یک‌جا) استخراج شده‌اند، احتمال
    نادیده گرفتن مطلبی از وسط متن‌های طولانی خیلی کمتر می‌شود.
    """
    prompt = (
        "در ادامه فهرستی از نکات کلیدی است که جداگانه از بخش‌های مختلف یک "
        "فایل صوتی/ویدیویی فارسی استخراج شده‌اند. بر اساس همه‌ی این نکات "
        "(هیچ‌کدام را حذف نکن، حتی نکات میانی)، یک خلاصه حرفه‌ای و منسجم بنویس.\n"
        f"{_HTML_FORMAT_RULES}\n"
        "خروجی را دقیقاً با این نشانه شروع کن (بدون هیچ متن دیگری قبلش):\n\n"
        f"{_SEP_SUMMARY}\n\n"
        f"نکات کلیدی همه بخش‌ها:\n{combined_points}"
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


def format_chunk(chunk: str) -> dict:
    """یک تکه از متن خام را هم فرمت‌بندی می‌کند (اصلاح/پاراگراف/پررنگ)، هم
    نکات کلیدی‌اش را استخراج می‌کند (برای استفاده در خلاصه نهایی)."""
    prompt = (
        "متن زیر بخشی از رونوشت خام یک تشخیص گفتار (speech-to-text) فارسی است "
        "که ممکن است غلط‌های تشخیصی، بی‌نقطه‌گذاری بودن، یا کلمات نامفهوم داشته باشد.\n\n"
        "دقیقاً دو بخش زیر را بنویس، هرکدام را با نشانه‌ی مشخص‌شده شروع کن "
        "(این نشانه‌ها را دقیقاً همین‌طور تایپ کن، بدون توضیح اضافه):\n\n"
        f"{_SEP_TEXT}\n"
        "این متن را ویرایش کن: غلط‌های واضح گفتار را بر اساس بافت جمله تصحیح کن، "
        "نقطه‌گذاری مناسب اضافه کن؛ محتوا و لحن اصلی را عوض نکن. برای خوانایی بهتر:\n"
        "- هر جا موضوع عوض می‌شود یک خط خالی بگذار تا پاراگراف جدید شروع شود "
        "(پاراگراف‌ها را کوتاه، حدود ۲ تا ۴ جمله، نگه دار)\n"
        "- فقط مهم‌ترین عبارات کلیدی (اسم افراد، اعداد و ارقام مهم، "
        "نتیجه‌گیری‌های اصلی) را داخل <b>...</b> پررنگ کن؛ در هر پاراگراف حداکثر "
        "یک یا دو عبارت پررنگ کافی است. به‌جز تگ <b>، از هیچ تگ HTML یا "
        "Markdown دیگری (مثل ** یا #) استفاده نکن\n\n"
        f"{_SEP_POINTS}\n"
        "فهرست ۲ تا ۵ نکته یا واقعیت کلیدی همین بخش را بنویس (چیزهایی که اگر "
        "در خلاصه نهایی نیایند، مطلب مهمی جا می‌افتد). هر نکته یک خط جدا و با "
        "«- » شروع شود. فقط متن ساده، بدون تگ HTML.\n\n"
        f"متن:\n{chunk}"
    )
    response = groq_client.chat.completions.create(
        model=SUMMARY_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=2500,
    )
    content = response.choices[0].message.content.strip()

    text_part, points_part = chunk, ""
    if _SEP_TEXT in content and _SEP_POINTS in content:
        after_text = content.split(_SEP_TEXT, 1)[1]
        text_part, points_part = after_text.split(_SEP_POINTS, 1)
        text_part, points_part = text_part.strip(), points_part.strip()
    elif _SEP_TEXT in content:
        text_part = content.split(_SEP_TEXT, 1)[1].strip()

    return {"text": text_part, "points": points_part}


def extract_points_only(chunk: str) -> str:
    """فقط نکات کلیدی یک تکه را استخراج می‌کند (ارزان‌تر از format_chunk،
    برای تکه‌های اضافی که فرمت‌بندی کامل نمی‌شوند)."""
    prompt = (
        "متن زیر بخشی از رونوشت خام یک تشخیص گفتار فارسی است. فهرست ۲ تا ۵ "
        "نکته یا واقعیت کلیدی این بخش را بنویس (چیزهایی که اگر در خلاصه نهایی "
        "نیایند، مطلب مهمی جا می‌افتد). هر نکته یک خط جدا و با «- » شروع شود. "
        "فقط همین فهرست را برگردان، بدون توضیح اضافه.\n\n"
        f"متن:\n{chunk}"
    )
    response = groq_client.chat.completions.create(
        model=SUMMARY_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=400,
    )
    return response.choices[0].message.content.strip()


def format_full_transcript(raw_text: str):
    """کل متن خام را فرمت‌بندی می‌کند و هم‌زمان نکات کلیدی همه‌ی تکه‌ها را
    جمع‌آوری می‌کند تا خلاصه نهایی از روی آن‌ها ساخته شود.

    خروجی: (متن_فرمت‌بندی‌شده, نکات_کلیدی_ترکیبی)
    """
    chunks = split_into_sentence_chunks(raw_text)
    to_format, remainder = chunks[:MAX_FORMAT_CHUNKS], chunks[MAX_FORMAT_CHUNKS:]

    formatted_parts = []
    all_points = []

    for chunk in to_format:
        try:
            result = format_chunk(chunk)
            formatted_parts.append(result["text"])
            if result["points"]:
                all_points.append(result["points"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("Chunk formatting failed: %s", exc)
            formatted_parts.append(chunk)  # حداقل خود تکه خام را نگه دار

    for chunk in remainder:
        # برای کنترل هزینه/زمان، فقط پاراگراف‌بندی ساده (بدون فرمت هوش مصنوعی)
        # ولی نکات کلیدی‌اش را همچنان استخراج می‌کنیم تا در خلاصه گم نشود
        formatted_parts.append(auto_paragraph(chunk))
        try:
            points = extract_points_only(chunk)
            if points:
                all_points.append(points)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Point extraction failed for remainder chunk: %s", exc)

    corrected = "\n\n".join(formatted_parts)
    combined_points = "\n".join(all_points)
    return corrected, combined_points


def polish_text(raw_text: str):
    """متن خام را فرمت‌بندی و خلاصه حرفه‌ای می‌کند.

    خروجی: دیکشنری با دو کلید corrected و summary (هرکدام ممکن است None باشد
    اگر آن مرحله خطا داد)، یا None اگر هر دو مرحله شکست خوردند.
    """
    corrected = None
    combined_points = None
    summary = None

    try:
        corrected, combined_points = format_full_transcript(raw_text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("فرمت‌بندی متن اصلی ناموفق بود: %s", exc)

    try:
        if combined_points:
            summary = synthesize_summary(combined_points)
        else:
            # اگر جمع‌آوری نکات کلیدی کلاً شکست خورد، مستقیم از کل متن خلاصه بگیر
            summary = generate_summary_direct(raw_text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("خلاصه‌سازی ناموفق بود: %s", exc)

    if summary is None and corrected is None:
        return None
    return {"corrected": corrected, "summary": summary}


# ---------------------------------------------------------------------------
# هندلرهای تلگرام
# ---------------------------------------------------------------------------


def _is_authorized(telegram_id: int) -> bool:
    if telegram_id == ADMIN_TELEGRAM_ID:
        return True
    try:
        return subscriptions.is_subscribed(telegram_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Subscription check failed: %s", exc)
        # اگر دیتابیس در دسترس نبود، برای احتیاط دسترسی را رد می‌کنیم
        return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if _is_authorized(user_id):
        await update.message.reply_text(
            "سلام! یک پیام صوتی یا ویدیو برام بفرست تا متنش کنم و خلاصه‌اش رو بهت بدم."
        )
    else:
        await update.message.reply_text(
            "سلام! برای استفاده از این ربات نیاز به اشتراک فعال داری. "
            "برای خرید یا تمدید اشتراک با ادمین در ارتباط باش."
        )


async def adduser_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """فقط ادمین: /adduser <آیدی_عددی_تلگرام> <تعداد_روز> [نام دلخواه]"""
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "استفاده: /adduser <آیدی_تلگرام> <تعداد_روز> [نام]"
        )
        return
    try:
        telegram_id = int(args[0])
        days = int(args[1])
    except ValueError:
        await update.message.reply_text("آیدی و تعداد روز باید عدد باشند.")
        return
    display_name = " ".join(args[2:])
    try:
        new_expiry = subscriptions.add_or_renew_subscriber(
            telegram_id, days, display_name
        )
        await update.message.reply_text(
            f"✅ اشتراک {telegram_id} تا تاریخ {new_expiry} فعال شد."
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("adduser failed")
        await update.message.reply_text(f"خطا: {exc}")


async def removeuser_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """فقط ادمین: /removeuser <آیدی_عددی_تلگرام>"""
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        return
    if not context.args:
        await update.message.reply_text("استفاده: /removeuser <آیدی_تلگرام>")
        return
    try:
        telegram_id = int(context.args[0])
        subscriptions.remove_subscriber(telegram_id)
        await update.message.reply_text(f"❌ اشتراک {telegram_id} حذف شد.")
    except Exception as exc:  # noqa: BLE001
        logger.exception("removeuser failed")
        await update.message.reply_text(f"خطا: {exc}")


async def listusers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """فقط ادمین: /listusers"""
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        return
    try:
        rows = subscriptions.list_subscribers()
    except Exception as exc:  # noqa: BLE001
        logger.exception("listusers failed")
        await update.message.reply_text(f"خطا: {exc}")
        return

    if not rows:
        await update.message.reply_text("هنوز مشترکی ثبت نشده.")
        return

    lines = []
    for row in rows:
        status = "✅ فعال" if row["active"] else "⛔ منقضی"
        name = row["display_name"] or "-"
        lines.append(f"{row['telegram_id']} | {name} | تا {row['expires_at']} | {status}")
    await send_long_reply_plain(update.message, "\n".join(lines))


TELEGRAM_MAX_LEN = 4000  # کمی کمتر از سقف واقعی تلگرام (۴۰۹۶) برای احتیاط
_TAG_STRIP_RE = re.compile(r"<[^>]+>")


async def send_long_reply_plain(message, text: str) -> None:
    """برای پیام‌های متن ساده (بدون HTML) مثل خروجی /listusers."""
    for i in range(0, len(text), TELEGRAM_MAX_LEN):
        await message.reply_text(text[i : i + TELEGRAM_MAX_LEN])



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


_MARKDOWN_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


def _sanitize_model_html(text: str) -> str:
    """تگ‌های <b> را نگه می‌دارد، ستاره‌های مارک‌داون (**...**) را هم به پررنگ HTML
    تبدیل می‌کند (چون بعضی وقت‌ها مدل به‌جای <b> از ** استفاده می‌کند)، و بقیه‌ی
    متن را برای HTML امن می‌کند."""
    text = _MARKDOWN_BOLD_RE.sub(r"<b>\1</b>", text)
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

    if not _is_authorized(update.effective_user.id):
        await update.message.reply_text(
            "برای استفاده از این ربات نیاز به اشتراک فعال داری. "
            "برای خرید یا تمدید اشتراک با ادمین در ارتباط باش."
        )
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

    if not _is_authorized(update.effective_user.id):
        await msg.reply_text(
            "برای استفاده از این ربات نیاز به اشتراک فعال داری. "
            "برای خرید یا تمدید اشتراک با ادمین در ارتباط باش."
        )
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
    try:
        subscriptions.init_db()
    except Exception as exc:  # noqa: BLE001
        logger.exception("DB init failed (subscriptions will fail closed): %s", exc)

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("adduser", adduser_cmd))
    app.add_handler(CommandHandler("removeuser", removeuser_cmd))
    app.add_handler(CommandHandler("listusers", listusers_cmd))
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
