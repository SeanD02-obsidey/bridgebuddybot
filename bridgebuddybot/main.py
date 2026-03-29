import asyncio
import logging
import os
import io
from datetime import datetime, timezone
from dotenv import load_dotenv

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from telegram import Update
from telegram.ext import (
    Application,
    filters,
    MessageHandler,
)

load_dotenv # read variables from .env and set them in os.environ

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Define configuration constants
URL = os.getenv("RENDER_EXTERNAL_URL") # web address that the bot is reachable on
PORT = 8443 # Telegram Bot API supports ports 443, 80, 88, 8443
TG_API_TOKEN = os.getenv("TG_API_TOKEN", "") # telegram token
GS_API_TOKEN = os.getenv("GS_API_TOKEN", "") # Google Sheets token
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB


# Check presence of application critical variables;
# Raise exception if variable content is empty
critical_variables = [URL, TG_API_TOKEN, GS_API_TOKEN]
for variable in critical_variables:
    if variable is None:
        raise ValueError(f"{variable} environment variable is not set properly: check env?")


# file cache
# Each entry: { "filename": str, "mime_type": str, "data": bytes, "ts": str }
_file_cache: list[dict] = []


async def _download_and_cache(
    file_id: str,
    filename: str,
    mime_type: str,
    file_size: int | None,
    bot,
) -> bool:
    """
    Download a Telegram file into memory and append it to _file_cache.
    Returns False (and notifies nothing) if the file exceeds MAX_FILE_SIZE.
    """
    # caller sends the "too large" reply
    if file_size and file_size > MAX_FILE_SIZE:
        return False 

    tg_file = await bot.get_file(file_id)

    buf = io.BytesIO()
    await tg_file.download_to_memory(buf)
    data = buf.getvalue()

    # double-check after download
    if len(data) > MAX_FILE_SIZE: 
        return False

    _file_cache.append({
        "filename":  filename,
        "mime_type": mime_type,
        "data":      data,
        "ts":        datetime.now(timezone.utc)
    })
    logger.info("Cached %s (%d bytes)", filename, len(data))
    return True

async def reply(update: Update, _) -> None:
    """Handle plain text messages."""
    if not update.message:
        return
    text = update.message.text or ""
    logger.info("Text message: %s", text)
    # TODO: push `text` to Google Sheets via your API
    await update.message.reply_text(
        "Thank you for contacting us! Your message has been successfully recorded."
    )


async def handle_attachment(update: Update, _) -> None:
    """Handle documents, photos, video, stickers, etc."""
    if not update.message:
        return

    msg = update.message
    bot = msg.get_bot()

    # ── resolve the attachment ─────────────────────────────────────────────────
    if msg.document:
        file_id   = msg.document.file_id
        filename  = msg.document.file_name or f"document_{file_id}"
        mime_type = msg.document.mime_type or "application/octet-stream"
        file_size = msg.document.file_size

    elif msg.photo:
        # Telegram sends multiple sizes; take the largest
        photo     = msg.photo[-1]
        file_id   = photo.file_id
        filename  = f"photo_{file_id}.jpg"
        mime_type = "image/jpeg"
        file_size = photo.file_size

    elif msg.video:
        file_id   = msg.video.file_id
        filename  = msg.video.file_name or f"video_{file_id}.mp4"
        mime_type = msg.video.mime_type or "video/mp4"
        file_size = msg.video.file_size

    else:
        await msg.reply_text("Unsupported attachment type.")
        return

    if file_size and file_size > MAX_FILE_SIZE:
        await msg.reply_text(
            f"⚠️ File is too large ({file_size / 1_048_576:.1f} MB). "
            "Please keep attachments under 10 MB."
        )
        return

    ok = await _download_and_cache(file_id, filename, mime_type, file_size, bot)
    if not ok:
        await msg.reply_text(
            "⚠️ The file exceeds the 10 MB limit and was not recorded."
        )
        return

    # TODO: flush _file_cache to Google Sheets via your API
    await msg.reply_text(
        f"✅ Your attachment ({filename}) has been received and recorded."
    )


async def handle_audio(update: Update, _) -> None:
    """Handle voice messages and audio files."""
    if not update.message:
        return

    msg = update.message
    bot = msg.get_bot()

    if msg.voice:
        file_id   = msg.voice.file_id
        filename  = f"voice_{file_id}.ogg"
        mime_type = msg.voice.mime_type or "audio/ogg"
        file_size = msg.voice.file_size

    elif msg.audio:
        file_id   = msg.audio.file_id
        filename  = (msg.audio.file_name or f"audio_{file_id}.mp3")
        mime_type = msg.audio.mime_type or "audio/mpeg"
        file_size = msg.audio.file_size

    else:
        await msg.reply_text("Unsupported audio type.")
        return

    if file_size and file_size > MAX_FILE_SIZE:
        await msg.reply_text(
            f"⚠️ Audio is too large ({file_size / 1_048_576:.1f} MB). "
            "Please keep audio messages under 10 MB."
        )
        return

    ok = await _download_and_cache(file_id, filename, mime_type, file_size, bot)
    if not ok:
        await msg.reply_text(
            "⚠️ The audio file exceeds the 10 MB limit and was not recorded."
        )
        return

    # TODO: flush _file_cache to Google Sheets via your API
    await msg.reply_text(
        "✅ Your audio message has been received and recorded."
    )

async def main() -> None:
    application = (
        Application.builder().token(TG_API_TOKEN).updater(None).build()
    )

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, reply))
    application.add_handler(MessageHandler(
        filters.Document.ALL | filters.PHOTO | filters.VIDEO | filters.Sticker.ALL,
        handle_attachment,
    ))
    application.add_handler(MessageHandler(
        filters.VOICE | filters.AUDIO,
        handle_audio,
    ))

    await application.bot.set_webhook(
        url=f"{URL}/telegram", allowed_updates=Update.ALL_TYPES
    )

    async def telegram(request: Request) -> Response:
        await application.update_queue.put(
            Update.de_json(data=await request.json(), bot=application.bot)
        )
        return Response()

    async def health(_: Request) -> PlainTextResponse:
        return PlainTextResponse(content="The bot is still running fine :)")

    starlette_app = Starlette(
        routes=[
            Route("/telegram", telegram, methods=["POST"]),
            Route("/healthcheck", health, methods=["GET"]),
        ]
    )

    webserver = uvicorn.Server(
        config=uvicorn.Config(
            app=starlette_app,
            port=PORT,
            use_colors=False,
            host="0.0.0.0",
        )
    )

    async with application:
        await application.start()
        await webserver.serve()
        await application.stop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
