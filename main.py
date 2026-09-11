import os
import re
import sqlite3
import asyncio
import logging
import tempfile
from pathlib import Path

import fitz
from aiohttp import web

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from telethon import TelegramClient
from telethon.tl.types import DocumentAttributeFilename


# =========================================================
# الإعدادات
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")

API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")

# معرف أو رابط قناتك
# مثال:
# CHANNEL = "@mychannel"
CHANNEL = os.getenv("CHANNEL")

DB_FILE = "books.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)


# =========================================================
# قاعدة البيانات
# =========================================================

def init_db():

    conn = sqlite3.connect(DB_FILE)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS pages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            book TEXT NOT NULL,
            page INTEGER NOT NULL,
            text TEXT NOT NULL,
            message_id INTEGER,
            channel_id TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS indexed_messages (
            message_id INTEGER PRIMARY KEY,
            book TEXT,
            indexed_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    conn.close()


# =========================================================
# تنظيف النص
# =========================================================

def clean_text(text):

    text = text.replace("\x00", " ")
    text = re.sub(r"\s+", " ", text)

    return text.strip()


def normalize(text):

    text = text.lower()

    replacements = {
        "أ": "ا",
        "إ": "ا",
        "آ": "ا",
        "ة": "ه",
        "ى": "ي",
        "ؤ": "و",
        "ئ": "ي",
    }

    for a, b in replacements.items():
        text = text.replace(a, b)

    text = re.sub(r"[^\w\s\u0600-\u06FF]", " ", text)
    text = re.sub(r"\s+", " ", text)

    return text.strip()


def words(text):

    return [
        x for x in normalize(text).split()
        if len(x) >= 3
    ]


# =========================================================
# فهرسة PDF
# =========================================================

def index_pdf(
    pdf_path,
    book_name,
    message_id=None,
    channel_id=None
):

    logger.info("Reading: %s", book_name)

    document = fitz.open(pdf_path)

    conn = sqlite3.connect(DB_FILE)

    # حذف النسخة القديمة من الكتاب
    conn.execute(
        "DELETE FROM pages WHERE book = ?",
        (book_name,)
    )

    total = 0

    for page_number, page in enumerate(
        document,
        start=1
    ):

        text = page.get_text("text")
        text = clean_text(text)

        if not text:
            continue

        conn.execute(
            """
            INSERT INTO pages
            (book, page, text, message_id, channel_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                book_name,
                page_number,
                text,
                message_id,
                str(channel_id)
                if channel_id else None
            )
        )

        total += 1

    conn.commit()
    conn.close()

    document.close()

    logger.info(
        "Indexed %s pages from %s",
        total,
        book_name
    )

    return total


# =========================================================
# البحث
# =========================================================

def search_books(query):

    conn = sqlite3.connect(DB_FILE)

    rows = conn.execute(
        """
        SELECT
            book,
            page,
            text,
            message_id,
            channel_id
        FROM pages
        """
    ).fetchall()

    conn.close()

    query_words = words(query)

    if not query_words:
        return []

    results = []

    for row in rows:

        book, page, text, message_id, channel_id = row

        normalized_text = normalize(text)

        score = 0

        for word in query_words:

            count = normalized_text.count(word)

            if count:
                score += 3
                score += min(count, 5)

        # تطابق العبارة كاملة
        if normalize(query) in normalized_text:
            score += 15

        if score:

            results.append({
                "book": book,
                "page": page,
                "text": text,
                "message_id": message_id,
                "channel_id": channel_id,
                "score": score,
            })

    results.sort(
        key=lambda x: x["score"],
        reverse=True
    )

    return results[:5]


# =========================================================
# اسم الكتاب
# =========================================================

def get_book_name(message):

    # Caption المنشور
    if message.message:

        caption = message.message.strip()

        if caption:
            return caption[:200]

    # اسم الملف
    if message.document:

        for attr in message.document.attributes:

            if isinstance(
                attr,
                DocumentAttributeFilename
            ):

                return Path(
                    attr.file_name
                ).stem

    return f"كتاب-{message.id}"


# =========================================================
# فحص هل PDF
# =========================================================

def is_pdf(message):

    if not message.document:
        return False

    mime = getattr(
        message.document,
        "mime_type",
        ""
    )

    if mime == "application/pdf":
        return True

    for attr in message.document.attributes:

        if isinstance(
            attr,
            DocumentAttributeFilename
        ):

            name = attr.file_name.lower()

            if name.endswith(".pdf"):
                return True

    return False


# =========================================================
# Telethon:
# قراءة الكتب القديمة من القناة
# =========================================================

async def index_old_books():

    if not API_ID or not API_HASH:

        logger.warning(
            "API_ID/API_HASH not available. "
            "Old books cannot be scanned yet."
        )

        return

    if not CHANNEL:

        logger.warning(
            "CHANNEL is not configured."
        )

        return

    api_id = int(API_ID)

    client = TelegramClient(
        "telegram_user",
        api_id,
        API_HASH
    )

    await client.start()

    logger.info(
        "Scanning old channel posts..."
    )

    entity = await client.get_entity(
        CHANNEL
    )

    count = 0

    async for message in client.iter_messages(
        entity,
        reverse=True
    ):

        if not is_pdf(message):
            continue

        book_name = get_book_name(message)

        logger.info(
            "Found old book: %s",
            book_name
        )

        temp_path = None

        try:

            with tempfile.NamedTemporaryFile(
                delete=False,
                suffix=".pdf"
            ) as temp:

                temp_path = temp.name

            await client.download_media(
                message,
                file=temp_path
            )

            pages = index_pdf(
                temp_path,
                book_name,
                message.id,
                entity.id
            )

            conn = sqlite3.connect(DB_FILE)

            conn.execute(
                """
                INSERT OR REPLACE INTO indexed_messages
                (message_id, book)
                VALUES (?, ?)
                """,
                (
                    message.id,
                    book_name
                )
            )

            conn.commit()
            conn.close()

            count += 1

            logger.info(
                "Indexed %s (%s pages)",
                book_name,
                pages
            )

        except Exception:

            logger.exception(
                "Failed to index %s",
                book_name
            )

        finally:

            if temp_path and os.path.exists(
                temp_path
            ):

                os.remove(temp_path)

    await client.disconnect()

    logger.info(
        "Finished old books scan. "
        "Books indexed: %s",
        count
    )


# =========================================================
# /start
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(
        "📚 مرحبًا بك في مرجع الكتب.\n\n"
        "أرسل سؤالك وسأبحث داخل الكتب "
        "المفهرسة وأذكر اسم الكتاب ورقم الصفحة."
    )


# =========================================================
# /books
# =========================================================

async def books(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    conn = sqlite3.connect(DB_FILE)

    rows = conn.execute(
        """
        SELECT book, COUNT(*)
        FROM pages
        GROUP BY book
        ORDER BY book
        """
    ).fetchall()

    conn.close()

    if not rows:

        await update.message.reply_text(
            "📚 لا توجد كتب مفهرسة حتى الآن."
        )

        return

    text = "📚 الكتب المفهرسة:\n\n"

    for book, pages in rows:

        text += (
            f"• {book}\n"
            f"  📄 {pages} صفحة\n\n"
        )

    await update.message.reply_text(
        text
    )


# =========================================================
# أسئلة المتابعين
# =========================================================

async def question(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.message.text.strip()

    if not query:
        return

    results = search_books(query)

    if not results:

        await update.message.reply_text(
            "🔎 لم أجد هذه المعلومة "
            "في الكتب المفهرسة."
        )

        return

    response = "📚 وجدت هذه النتائج:\n\n"

    for result in results:

        text = result["text"]

        response += (
            f"📖 الكتاب: {result['book']}\n"
            f"📄 الصفحة: {result['page']}\n\n"
            f"{text[:900]}\n\n"
            "━━━━━━━━━━━━\n\n"
        )

        if len(response) > 3500:
            break

    await update.message.reply_text(
        response[:4000]
    )


# =========================================================
# استقبال كتاب جديد في القناة
# =========================================================

async def new_channel_book(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    post = update.channel_post

    if not post:
        return

    document = post.document

    if not document:
        return

    file_name = (
        document.file_name or ""
    )

    mime = (
        document.mime_type or ""
    )

    if not (
        mime == "application/pdf"
        or file_name.lower().endswith(".pdf")
    ):

        return

    book_name = (
        post.caption.strip()
        if post.caption
        else Path(file_name).stem
    )

    await context.bot.send_message(
        chat_id=post.chat_id,
        text=(
            f"📚 تم العثور على كتاب جديد:\n"
            f"{book_name}\n\n"
            f"⏳ جارٍ قراءته..."
        )
    )

    temp_path = None

    try:

        telegram_file = await context.bot.get_file(
            document.file_id
        )

        with tempfile.NamedTemporaryFile(
            delete=False,
            suffix=".pdf"
        ) as temp:

            temp_path = temp.name

        await telegram_file.download_to_drive(
            temp_path
        )

        pages = index_pdf(
            temp_path,
            book_name,
            post.message_id,
            post.chat_id
        )

        await context.bot.send_message(
            chat_id=post.chat_id,
            text=(
                f"✅ تمت قراءة الكتاب وفهرسته.\n\n"
                f"📖 {book_name}\n"
                f"📄 الصفحات المقروءة: {pages}"
            )
        )

    except Exception:

        logger.exception(
            "Error reading new book"
        )

        await context.bot.send_message(
            chat_id=post.chat_id,
            text=(
                "❌ حدث خطأ أثناء قراءة الكتاب."
            )
        )

    finally:

        if temp_path and os.path.exists(
            temp_path
        ):

            os.remove(temp_path)


# =========================================================
# Health check لـ Railway
# =========================================================

async def health(request):

    return web.Response(
        text="Book Reference Bot is running."
    )


async def start_web_server():

    app = web.Application()

    app.router.add_get(
        "/",
        health
    )

    runner = web.AppRunner(app)

    await runner.setup()

    port = int(
        os.getenv("PORT", "8080")
    )

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        port
    )

    await site.start()

    logger.info(
        "Web server running on port %s",
        port
    )


# =========================================================
# التشغيل
# =========================================================

async def main():

    init_db()

    # بدء خادم Railway
    await start_web_server()

    # فهرسة الكتب القديمة
    # لن تعمل إلا بعد وضع API_ID/API_HASH
    try:

        await index_old_books()

    except Exception:

        logger.exception(
            "Old books indexing failed"
        )

    # تشغيل البوت
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    application.add_handler(
        CommandHandler(
            "books",
            books
        )
    )

    application.add_handler(
        MessageHandler(
            filters.UpdateType.CHANNEL_POST,
            new_channel_book
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            question
        )
    )

    await application.initialize()

    await application.start()

    await application.updater.start_polling(
        allowed_updates=Update.ALL_TYPES
    )

    logger.info(
        "BOOK REFERENCE BOT STARTED"
    )

    # إبقاء البرنامج يعمل
    while True:

        await asyncio.sleep(3600)


if __name__ == "__main__":

    asyncio.run(main())
