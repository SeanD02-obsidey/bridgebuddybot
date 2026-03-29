import asyncio
import logging
import os
import io
import base64
import time
from datetime import datetime, timezone
from dotenv import load_dotenv

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from telegram import Update
from telegram.ext import Application, filters, MessageHandler

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

load_dotenv()

# logging 
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("googleapiclient").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# environment and app-local variables
URL               = os.getenv("RENDER_EXTERNAL_URL")
PORT              = int(os.getenv("PORT", "8443"))
TG_API_TOKEN      = os.getenv("TG_API_TOKEN", "")
GS_SERVICE_JSON   = os.getenv("GS_SERVICE_JSON", "credentials.json")
GS_SPREADSHEET_ID = os.getenv("GS_SPREADSHEET_ID")

MAX_FILE_SIZE  = 50000          # 50 KB, corresponds to the character count ceiling in a single cell
FLUSH_INTERVAL = 30             # seconds between cache flushes
MAX_BACKOFF    = 64             # seconds, ceiling for exponential backoff

# check for presence of vital variables
for name, value in [
    ("RENDER_EXTERNAL_URL", URL),
    ("TG_API_TOKEN",        TG_API_TOKEN),
    ("GS_SERVICE_JSON",     GS_SERVICE_JSON),
    ("GS_SPREADSHEET_ID",   GS_SPREADSHEET_ID),
]:
    if not value:
        raise ValueError(f"Environment variable {name!r} is missing or empty.")

# Google Sheets client
# wrapped in try/except so a bad credentials file gives a clear error
try:
    _creds = service_account.Credentials.from_service_account_file(
        GS_SERVICE_JSON,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    _sheets = build("sheets", "v4", credentials=_creds, cache_discovery=False)
except Exception as exc:
    raise RuntimeError(f"Failed to initialise Google Sheets client: {exc}") from exc

# FIX: track confirmed sheet tabs so _ensure_sheet only hits the API once per tab
_known_tabs: set[str] = set()

# caches
# Message entry: { "ts": str, "user_id": int, "username": str, "text": str }
# File entry:    { "ts": str, "user_id": int, "username": str,
#                  "filename": str, "mime_type": str, "data": bytes }
_message_cache: list[dict] = []
_file_cache:    list[dict] = []
_cache_lock = asyncio.Lock()


# exponential backoff 
async def _with_backoff(coro_fn, *args, **kwargs):
    """
    Retry an async callable with exponential backoff + jitter.
    Retries on 429 / 5xx HttpErrors and generic transient exceptions.
    Raises after 7 failed attempts (~2 min cumulative wait).
    """
    attempt = 0
    while True:
        try:
            return await coro_fn(*args, **kwargs)
        except HttpError as exc:
            status = exc.resp.status
            if status in (429, 500, 502, 503, 504):
                wait = min(2 ** attempt + (time.monotonic() % 1), MAX_BACKOFF)
                logger.warning(
                    "Sheets API HTTP %s on attempt %d — retrying in %.1fs",
                    status, attempt, wait,
                )
                await asyncio.sleep(wait)
                attempt += 1
            else:
                raise  # non-retryable (e.g. 403 permission denied)
        except Exception as exc:
            if attempt >= 7:
                logger.error("Sheets call failed after %d attempts: %s", attempt, exc)
                raise
            wait = min(2 ** attempt + (time.monotonic() % 1), MAX_BACKOFF)
            logger.warning(
                "Transient error on attempt %d (%s) — retrying in %.1fs",
                attempt, exc, wait,
            )
            await asyncio.sleep(wait)
            attempt += 1


# sheet helpers
async def _ensure_sheet(name: str) -> None:
    """Create a worksheet tab if it does not already exist (cached after first check)."""
    if name in _known_tabs:
        return

    def _run():
        meta = _sheets.spreadsheets().get(spreadsheetId=GS_SPREADSHEET_ID).execute()
        existing = {s["properties"]["title"] for s in meta["sheets"]}
        if name not in existing:
            body = {"requests": [{"addSheet": {"properties": {"title": name}}}]}
            _sheets.spreadsheets().batchUpdate(
                spreadsheetId=GS_SPREADSHEET_ID, body=body
            ).execute()
            logger.info("Created sheet tab: %s", name)
        _known_tabs.add(name)

    await _with_backoff(asyncio.to_thread, _run)


async def _append_rows(sheet_name: str, rows: list[list]) -> None:
    """Append rows to a named sheet tab."""
    def _run():
        _sheets.spreadsheets().values().append(
            spreadsheetId=GS_SPREADSHEET_ID,
            range=f"{sheet_name}!A1",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": rows},
        ).execute()

    await _with_backoff(asyncio.to_thread, _run)


# flush logic
async def _flush_messages(batch: list[dict]) -> None:
    """Write a batch of text messages to the 'Messages' sheet."""
    await _ensure_sheet("Messages")
    rows = [
        [entry["ts"], str(entry["user_id"]), entry["username"], entry["text"]]
        for entry in batch
    ]
    await _append_rows("Messages", rows)
    logger.info("Flushed %d message(s) to Sheets.", len(rows))


async def _flush_files(batch: list[dict]) -> None:
    """
    Write a batch of file entries to the 'Files' sheet.
    Binary content is base64-encoded into column F so the sheet stays valid UTF-8.
    For production, consider uploading to GCS/Drive and storing only the URL.
    """
    await _ensure_sheet("Files")
    rows = [
        [
            entry["ts"],
            str(entry["user_id"]),
            entry["username"],
            entry["filename"],
            entry["mime_type"],
            base64.b64encode(entry["data"]).decode(),
        ]
        for entry in batch
    ]
    await _append_rows("Files", rows)
    logger.info("Flushed %d file(s) to Sheets.", len(rows))


async def flush_caches() -> None:
    """Drain both caches and write to Google Sheets. Returns entries to cache on failure."""
    async with _cache_lock:
        messages = _message_cache.copy()
        files    = _file_cache.copy()
        _message_cache.clear()
        _file_cache.clear()

    if not messages and not files:
        return

    try:
        if messages:
            await _flush_messages(messages)
        if files:
            await _flush_files(files)
    except Exception:
        async with _cache_lock:
            _message_cache[:0] = messages  # prepend to preserve order
            _file_cache[:0]    = files
        logger.error("Flush failed — entries returned to cache for next attempt.")

        if HttpError.status_code == 400:
            for file in files:
                if len(file["data"]) > 50000:
                    files.remove(file)
        raise



async def periodic_flush() -> None:
    """Background task: flush every FLUSH_INTERVAL seconds."""
    while True:
        await asyncio.sleep(FLUSH_INTERVAL)
        try:
            await flush_caches()
        except Exception as exc:
            logger.error("Periodic flush error (will retry next cycle): %s", exc)


# file download helper
async def _download_and_cache(
    file_id: str,
    filename: str,
    mime_type: str,
    file_size: int | None,
    user_id: int,
    username: str,
    bot,
) -> bool:
    """Download a Telegram file into memory and append it to the file cache."""
    if file_size and file_size > MAX_FILE_SIZE:
        return False

    tg_file = await bot.get_file(file_id)
    buf = io.BytesIO()
    await tg_file.download_to_memory(buf)
    data = buf.getvalue()

    if len(data) > MAX_FILE_SIZE:  # defensive re-check after download
        return False

    async with _cache_lock:
        _file_cache.append({
            "ts":        datetime.now(timezone.utc).isoformat(),
            "user_id":   user_id,
            "username":  username,
            "filename":  filename,
            "mime_type": mime_type,
            "data":      data,
        })
    logger.info("Cached file %s (%d bytes)", filename, len(data))
    return True


# telegram handlers
def _sender(msg) -> tuple[int, str]:
    if not msg.from_user:
        return (0, "unknown")
    return (msg.from_user.id, msg.from_user.username or msg.from_user.first_name)


async def reply(update: Update, _) -> None:
    if not update.message:
        return
    msg = update.message
    user_id, username = _sender(msg)

    async with _cache_lock:
        _message_cache.append({
            "ts":       datetime.now(timezone.utc).isoformat(),
            "user_id":  user_id,
            "username": username,
            "text":     msg.text or "",
        })

    await msg.reply_text(
        "Thank you for contacting us! Your message has been successfully recorded."
    )


async def handle_attachment(update: Update, _) -> None:
    if not update.message:
        return
    msg = update.message
    bot = msg.get_bot()
    user_id, username = _sender(msg)

    if msg.document:
        file_id   = msg.document.file_id
        filename  = msg.document.file_name or f"doc_{msg.document.file_id}"
        mime_type = msg.document.mime_type or "application/octet-stream"
        file_size = msg.document.file_size
    elif msg.photo:
        p         = msg.photo[-1]
        file_id   = p.file_id
        filename  = f"photo_{p.file_id}.jpg"
        mime_type = "image/jpeg"
        file_size = p.file_size
    elif msg.video:
        file_id   = msg.video.file_id
        filename  = msg.video.file_name or f"video_{msg.video.file_id}.mp4"
        mime_type = msg.video.mime_type or "video/mp4"
        file_size = msg.video.file_size
    elif msg.sticker:
        ext       = "tgs" if msg.sticker.is_animated else ("webm" if msg.sticker.is_video else "webp")
        file_id   = msg.sticker.file_id
        filename  = f"sticker_{msg.sticker.file_id}.{ext}"
        mime_type = "image/webp"
        file_size = msg.sticker.file_size
    else:
        await msg.reply_text("Unsupported attachment type.")
        return

    if file_size and file_size > MAX_FILE_SIZE:
        await msg.reply_text(
            f"⚠️ File is too large ({file_size / 1_048_576:.1f} MB). "
            "Please keep attachments under 10 MB."
        )
        return

    ok = await _download_and_cache(file_id, filename, mime_type, file_size, user_id, username, bot)
    if not ok:
        await msg.reply_text("⚠️ The file exceeds the 10 MB limit and was not recorded.")
        return

    await msg.reply_text(f"✅ Your attachment ({filename}) has been received and recorded.")


async def handle_audio(update: Update, _) -> None:
    if not update.message:
        return
    msg = update.message
    bot = msg.get_bot()
    user_id, username = _sender(msg)

    if msg.voice:
        file_id   = msg.voice.file_id
        filename  = f"voice_{msg.voice.file_id}.ogg"
        mime_type = msg.voice.mime_type or "audio/ogg"
        file_size = msg.voice.file_size
    elif msg.audio:
        file_id   = msg.audio.file_id
        filename  = msg.audio.file_name or f"audio_{msg.audio.file_id}.mp3"
        mime_type = msg.audio.mime_type or "audio/mpeg"
        file_size = msg.audio.file_size
    else:
        await msg.reply_text("Unsupported audio type.")
        return

    if file_size and file_size > MAX_FILE_SIZE:
        await msg.reply_text(
            f"⚠️ Audio is too large ({file_size / 1_048_576:.1f} MB). "
            "Please keep audio under 10 MB."
        )
        return

    ok = await _download_and_cache(file_id, filename, mime_type, file_size, user_id, username, bot)
    if not ok:
        await msg.reply_text("⚠️ The audio file exceeds the 10 MB limit and was not recorded.")
        return

    await msg.reply_text("✅ Your audio message has been received and recorded.")


# app setup
async def main() -> None:
    application = Application.builder().token(TG_API_TOKEN).updater(None).build()

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, reply))
    application.add_handler(MessageHandler(
        filters.Document.ALL | filters.PHOTO | filters.VIDEO | filters.Sticker.ALL,
        handle_attachment,
    ))
    application.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_audio))

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
            Route("/telegram",    telegram, methods=["POST"]),
            Route("/healthcheck", health,   methods=["GET"]),
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
        flush_task = asyncio.create_task(periodic_flush())
        try:
            await webserver.serve()
        finally:
            flush_task.cancel()
            await flush_caches()  # final drain on shutdown
            await application.stop()


if __name__ == "__main__":
    asyncio.run(main())
