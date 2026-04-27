"""Pyrogram-based audio sender for bypassing Telegram Bot API 50MB upload limit.

Uses MTProto protocol directly, supporting files up to 2GB.
Falls back gracefully if Pyrogram client is unavailable.
"""
import logging
import asyncio
import io
from pathlib import Path
from typing import Optional

from pyrogram import Client
from pyrogram.errors import FloodWait, RPCError

from config import API_ID, API_HASH, TELEGRAM_BOT_TOKEN

logger = logging.getLogger(__name__)

_pyro_client: Optional[Client] = None
_pyro_lock = asyncio.Lock()


async def get_pyrogram_client() -> Client:
    """Get or create a shared Pyrogram bot client."""
    global _pyro_client
    async with _pyro_lock:
        if _pyro_client is None:
            _pyro_client = Client(
                name="syncloud_bot",
                api_id=API_ID,
                api_hash=API_HASH,
                bot_token=TELEGRAM_BOT_TOKEN,
                workdir=str(Path(__file__).resolve().parent),
                no_updates=True,
            )
        if not _pyro_client.is_connected:
            await _pyro_client.start()
            logger.info("Pyrogram client started successfully.")
    return _pyro_client


async def stop_pyrogram_client():
    """Stop the Pyrogram client gracefully."""
    global _pyro_client
    async with _pyro_lock:
        if _pyro_client and _pyro_client.is_connected:
            await _pyro_client.stop()
            logger.info("Pyrogram client stopped.")
            _pyro_client = None


async def send_audio_pyrogram(
    chat_id: int,
    audio_path: str,
    filename: str,
    title: str,
    performer: str,
    thumbnail_data: Optional[io.BytesIO] = None,
    reply_to_message_id: Optional[int] = None,
    max_retries: int = 3,
) -> Optional[int]:
    """Send audio file via Pyrogram (MTProto), supporting up to 2GB.

    Returns the message_id of the sent message, or None on failure.
    """
    client = await get_pyrogram_client()

    thumb_path = None
    try:
        if thumbnail_data:
            thumb_path = str(Path(audio_path).parent / "thumb_pyro.jpg")
            thumbnail_data.seek(0)
            with open(thumb_path, "wb") as f:
                f.write(thumbnail_data.read())

        for attempt in range(1, max_retries + 1):
            try:
                msg = await client.send_audio(
                    chat_id=chat_id,
                    audio=audio_path,
                    file_name=filename,
                    title=title,
                    performer=performer,
                    thumb=thumb_path,
                    reply_to_message_id=reply_to_message_id,
                )
                logger.info(f"Pyrogram: аудио отправлено в чат {chat_id}, msg_id={msg.id}")
                return msg.id
            except FloodWait as e:
                logger.warning(
                    f"Pyrogram FloodWait: ждём {e.value}с (попытка {attempt}/{max_retries})"
                )
                await asyncio.sleep(e.value + 1)
                if attempt == max_retries:
                    logger.error(f"Pyrogram: превышено макс. попыток для чата {chat_id}")
                    raise
            except RPCError as e:
                logger.error(
                    f"Pyrogram RPC error (попытка {attempt}/{max_retries}): {e}"
                )
                if attempt == max_retries:
                    raise
                await asyncio.sleep(1 + attempt)
    finally:
        if thumb_path:
            try:
                Path(thumb_path).unlink(missing_ok=True)
            except OSError:
                pass

    return None

