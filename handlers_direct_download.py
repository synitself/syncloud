import logging
import asyncio
import re
from pathlib import Path
import io
from typing import Optional, Tuple, Any, cast
import os
from datetime import datetime, timezone
from PIL import Image
import httpx

from telegram import Update, Message
from telegram.ext import ContextTypes
import telegram.error

from mutagen.id3 import ID3, APIC, TIT2, TPE1
from mutagen.mp3 import MP3
from mutagen.easyid3 import EasyID3
from mutagen.mp4 import MP4, MP4Cover
from mutagen.flac import FLAC

from config import DOWNLOAD_FOLDER
from utils import sanitize_filename, create_progress_bar
import db
import ui_texts

logger = logging.getLogger(__name__)

MAX_TELEGRAM_API_RETRIES = 3
TELEGRAM_API_RETRY_BUFFER = 0.8
THUMBNAIL_MAX_SIZE = 320
THUMBNAIL_MAX_BYTES = 200 * 1024


def prepare_thumbnail_for_telegram(artwork_data: bytes) -> Optional[io.BytesIO]:
    """Resize and convert artwork to JPEG ≤320x320, ≤200KB for Telegram thumbnail."""
    try:
        img = Image.open(io.BytesIO(artwork_data))
        img = img.convert('RGB')
        img.thumbnail((THUMBNAIL_MAX_SIZE, THUMBNAIL_MAX_SIZE), Image.LANCZOS)

        quality = 90
        while quality >= 20:
            buf = io.BytesIO()
            img.save(buf, format='JPEG', quality=quality)
            if buf.tell() <= THUMBNAIL_MAX_BYTES:
                buf.seek(0)
                logger.info(f"Thumbnail подготовлен: {img.size[0]}x{img.size[1]}, {buf.getbuffer().nbytes} bytes, quality={quality}")
                return buf
            quality -= 10

        buf.seek(0)
        return buf
    except Exception as e_thumb:
        logger.warning(f"Не удалось подготовить thumbnail: {e_thumb}")
        return None


def fetch_artwork_from_soundcloud(url: str, save_path: Path) -> Optional[Path]:
    """Fetch track artwork from SoundCloud og:image meta tag.
    
    scdl 3.0.2 has a bug: it downloads the thumbnail jpg but deletes it
    after failing to embed it into m4a files. We fetch it ourselves.
    """
    try:
        import re as _re
        resp = httpx.get(url, follow_redirects=True, timeout=15,
                         headers={'User-Agent': 'Mozilla/5.0'})
        match = _re.search(r'<meta property="og:image" content="([^"]+)"', resp.text)
        if not match:
            logger.warning(f"og:image не найден на странице {url}")
            return None
        img_url = match.group(1)
        img_url = _re.sub(r'-t\d+x\d+\.', '-t500x500.', img_url)
        img_resp = httpx.get(img_url, timeout=15)
        if img_resp.status_code == 200 and len(img_resp.content) > 100:
            artwork_file = save_path / "cover.jpg"
            artwork_file.write_bytes(img_resp.content)
            logger.info(f"Обложка скачана с SoundCloud: {len(img_resp.content)} bytes -> {artwork_file}")
            return artwork_file
        else:
            logger.warning(f"Не удалось скачать обложку: HTTP {img_resp.status_code}, {len(img_resp.content)} bytes")
            return None
    except Exception as e:
        logger.warning(f"Ошибка при скачивании обложки с SoundCloud: {e}")
        return None


async def modified_handle_soundcloud_link(
        url: str, user_id: int, chat_id: int, context: ContextTypes.DEFAULT_TYPE,
        status_message_id_to_edit: Optional[int] = None,
        text_prefix_for_status: str = "",
        reply_to_message_id_for_final_audio: Optional[int] = None
) -> Tuple[bool, Optional[int]]:
    is_sync_mode = bool(text_prefix_for_status)
    logger.info(f"Processing URL ({'sync_mode' if is_sync_mode else 'direct_download'}): {url} for user {user_id}")

    import hashlib
    url_hash = hashlib.md5(url.encode()).hexdigest()[:8]

    timestamp_str = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')
    request_temp_path_base = Path(DOWNLOAD_FOLDER) / str(user_id)
    request_temp_path = request_temp_path_base / f"dl_{url_hash}_{timestamp_str}"

    try:
        request_temp_path.mkdir(parents=True, exist_ok=True)
    except Exception as e_mkdir:
        logger.error(f"Не удалось создать временную папку {request_temp_path}: {e_mkdir}")
        db.log_user_error(user_id, f"Ошибка создания временной папки: {e_mkdir}", context_info=url)
        return False, None

    original_downloaded_file: Optional[Path] = None
    mp3_final_file: Optional[Path] = None
    artwork_from_original_data: Optional[bytes] = None
    artwork_from_original_mime: Optional[str] = None
    artwork_external_file_path: Optional[Path] = None
    embedded_artwork_data_io: Optional[io.BytesIO] = None
    artwork_data_to_embed_final: Optional[bytes] = None
    artwork_mime_type_final: Optional[str] = None
    sent_audio_message_id: Optional[int] = None
    error_occurred_for_logging = False
    error_reason_for_db = "Unknown error"

    try:
        async def update_progress_display(percent: int, stage_msg_local_key: str):
            stage_msg_local = getattr(ui_texts, stage_msg_local_key, stage_msg_local_key)
            progress_bar_and_percent = create_progress_bar(percent)
            current_track_progress_line = f"{progress_bar_and_percent} {stage_msg_local.strip()}"

            full_message_text: str
            target_message_id_for_edit: Optional[int] = None

            if is_sync_mode:
                if status_message_id_to_edit:
                    full_message_text = f"{current_track_progress_line}\n{text_prefix_for_status}"
                    target_message_id_for_edit = status_message_id_to_edit
                else:
                    logger.warning(
                        f"Sync mode: status_message_id_to_edit не передан для user {user_id}, url {url}. Прогресс не будет обновлен.")
                    return
            else:
                full_message_text = current_track_progress_line
                target_message_id_for_edit = status_message_id_to_edit

            if target_message_id_for_edit:
                for attempt in range(1, MAX_TELEGRAM_API_RETRIES + 1):
                    try:
                        await context.bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=target_message_id_for_edit,
                            text=full_message_text
                        )
                        break
                    except telegram.error.RetryAfter as e_retry_progress:
                        wait_time = e_retry_progress.retry_after + TELEGRAM_API_RETRY_BUFFER
                        logger.warning(
                            f"Flood control in update_progress_display for {url}. Retrying in {wait_time:.2f}s (attempt {attempt}/{MAX_TELEGRAM_API_RETRIES}).")
                        await asyncio.sleep(wait_time)
                        if attempt == MAX_TELEGRAM_API_RETRIES:
                            logger.error(
                                f"Max retries for update_progress_display for {url}. Giving up on this edit attempt.")
                    except telegram.error.BadRequest as e_edit:
                        if "Message is not modified" not in str(e_edit).lower() and \
                                "message to edit not found" not in str(e_edit).lower():
                            logger.warning(
                                f"Ошибка редактирования сообщения (progress) {target_message_id_for_edit} (chat {chat_id}): {e_edit}")
                        break
                    except Exception as e_unexp:
                        logger.error(
                            f"Неожиданная ошибка при обновлении сообщения (progress) {target_message_id_for_edit} (chat {chat_id}): {e_unexp}")
                        break

        await update_progress_display(0, "TRACK_STAGE_STARTING")

        artwork_external_file_path = fetch_artwork_from_soundcloud(url, request_temp_path)

        await update_progress_display(5, "TRACK_STAGE_DOWNLOADING")
        scdl_cmd = ["scdl", "-l", url, "-c", "--path", str(request_temp_path), "--overwrite", "--hide-progress"]
        process_scdl = await asyncio.create_subprocess_exec(*scdl_cmd, stdout=asyncio.subprocess.PIPE,
                                                            stderr=asyncio.subprocess.PIPE)
        scdl_stdout, scdl_stderr = await asyncio.wait_for(process_scdl.communicate(), timeout=300)

        if process_scdl.returncode != 0:
            err_msg_scdl = scdl_stderr.decode(errors='ignore').strip()
            error_reason_for_db = f"scdl: {err_msg_scdl.splitlines()[-1][:100] if err_msg_scdl else 'unknown'}"
            full_error_message = f"scdl failed: {err_msg_scdl.splitlines()[-1][:250] if err_msg_scdl else 'Неизвестная ошибка scdl'}"
            raise RuntimeError(full_error_message)

        for item_name in os.listdir(request_temp_path):
            item_path = request_temp_path / item_name
            ext_lower = item_path.suffix.lower()
            if item_path.is_file():
                if ext_lower in (".mp3", ".m4a", ".ogg", ".flac", ".wav"):
                    original_downloaded_file = item_path
                elif ext_lower in (".jpg", ".jpeg", ".png"):
                    artwork_external_file_path = item_path
        if not original_downloaded_file:
            error_reason_for_db = "Audio file not found post-scdl"
            raise FileNotFoundError("Файл аудио не найден после скачивания scdl.")

        await update_progress_display(35, "TRACK_STAGE_INTERMEDIATE")
        if original_downloaded_file:
            try:
                audio_ext = original_downloaded_file.suffix.lower()
                audio_obj_for_art: Any = None
                if audio_ext == ".m4a":
                    audio_obj_for_art = MP4(str(original_downloaded_file))
                elif audio_ext == ".mp3":
                    audio_obj_for_art = MP3(str(original_downloaded_file), ID3=ID3)
                elif audio_ext == ".flac":
                    audio_obj_for_art = FLAC(str(original_downloaded_file))

                if isinstance(audio_obj_for_art, MP4) and 'covr' in audio_obj_for_art and audio_obj_for_art['covr'] and \
                        audio_obj_for_art['covr'][0]:
                    artwork_from_original_data = bytes(audio_obj_for_art['covr'][0])
                    img_fmt = audio_obj_for_art['covr'][0].imageformat
                    if img_fmt == MP4Cover.FORMAT_JPEG:
                        artwork_from_original_mime = 'image/jpeg'
                    elif img_fmt == MP4Cover.FORMAT_PNG:
                        artwork_from_original_mime = 'image/png'
                elif isinstance(audio_obj_for_art, MP3) and audio_obj_for_art.tags:
                    for tag_key in list(audio_obj_for_art.tags.keys()):
                        if tag_key.startswith('APIC:'):
                            artwork_from_original_data = audio_obj_for_art.tags[tag_key].data
                            artwork_from_original_mime = audio_obj_for_art.tags[tag_key].mime;
                            break
                elif isinstance(audio_obj_for_art, FLAC) and audio_obj_for_art.pictures:
                    artwork_from_original_data = audio_obj_for_art.pictures[0].data
                    artwork_from_original_mime = audio_obj_for_art.pictures[0].mime
            except Exception as e_art:
                logger.warning(f"Не удалось извлечь обложку из {original_downloaded_file.name}: {e_art}")

        base_name_sanitized = sanitize_filename(original_downloaded_file.stem)
        is_conversion_needed = original_downloaded_file.suffix.lower() != ".mp3"

        if is_conversion_needed:
            await update_progress_display(40, "TRACK_STAGE_CONVERTING")
            mp3_final_file = request_temp_path / f"{base_name_sanitized}.mp3"
            ffmpeg_cmd = ["ffmpeg", "-y", "-i", str(original_downloaded_file), "-vn", "-ar", "44100", "-ac", "2",
                          "-b:a", "192k", str(mp3_final_file)]
            process_ffmpeg = await asyncio.create_subprocess_exec(*ffmpeg_cmd, stdout=asyncio.subprocess.PIPE,
                                                                  stderr=asyncio.subprocess.PIPE)
            ffmpeg_stdout, ffmpeg_stderr = await asyncio.wait_for(process_ffmpeg.communicate(), timeout=300)
            if process_ffmpeg.returncode != 0:
                err_msg_ffmpeg = ffmpeg_stderr.decode(errors='ignore').strip()
                error_reason_for_db = f"ffmpeg: {err_msg_ffmpeg[:100]}"
                raise RuntimeError(f"ffmpeg fail: {err_msg_ffmpeg[:250]}")
        else:
            mp3_final_file = original_downloaded_file

        if not mp3_final_file or not mp3_final_file.exists():
            error_reason_for_db = "MP3 file not found post-conversion/check"
            raise FileNotFoundError("MP3 файл не найден после обработки.")

        await update_progress_display(70, "TRACK_STAGE_PROCESSING_METADATA")
        title_str, performer_str = "Unknown Title", "Unknown Artist"
        audio_id3 = MP3(str(mp3_final_file), ID3=ID3)
        if audio_id3.tags is None: audio_id3.add_tags()
        try:
            audio_tags_easy = EasyID3(str(mp3_final_file))
            if 'title' in audio_tags_easy and audio_tags_easy['title']: title_str = audio_tags_easy['title'][0]
            if 'artist' in audio_tags_easy and audio_tags_easy['artist']: performer_str = audio_tags_easy['artist'][0]
        except Exception:
            logger.warning(f"EasyID3 не смог прочитать теги для {mp3_final_file.name}, пробуем из имени файла.")

        if title_str == "Unknown Title" or performer_str == "Unknown Artist":
            match_filename = re.match(r"(.+?) - (.+)", original_downloaded_file.stem, re.IGNORECASE)
            if match_filename:
                fn_performer, fn_title = match_filename.group(1).strip(), match_filename.group(2).strip()
                if performer_str == "Unknown Artist" and fn_performer: performer_str = fn_performer
                if title_str == "Unknown Title" and fn_title: title_str = fn_title
            elif title_str == "Unknown Title":
                title_str = sanitize_filename(original_downloaded_file.stem)

        audio_id3.tags.delall('TPE1');
        audio_id3.tags.add(TPE1(encoding=3, text=performer_str))
        audio_id3.tags.delall('TIT2');
        audio_id3.tags.add(TIT2(encoding=3, text=title_str))

        if artwork_external_file_path and artwork_external_file_path.exists():
            with open(artwork_external_file_path, 'rb') as afp:
                artwork_data_to_embed_final = afp.read()
            artwork_mime_type_final = 'image/jpeg' if artwork_external_file_path.suffix.lower() in ['.jpg',
                                                                                                    '.jpeg'] else 'image/png'
            logger.info(f"Обложка из внешнего файла: {len(artwork_data_to_embed_final)} bytes, mime={artwork_mime_type_final}")
        elif artwork_from_original_data:
            artwork_data_to_embed_final = artwork_from_original_data
            artwork_mime_type_final = artwork_from_original_mime or 'image/jpeg'
            logger.info(f"Обложка из оригинального аудио: {len(artwork_data_to_embed_final)} bytes, mime={artwork_mime_type_final}")
        else:
            logger.warning(f"Обложка не найдена ни из файла, ни из оригинального аудио для {url}")

        audio_id3.tags.delall('APIC')
        if artwork_data_to_embed_final and artwork_mime_type_final:
            try:
                audio_id3.tags.add(APIC(encoding=3, mime=artwork_mime_type_final, type=3, desc='Cover',
                                        data=artwork_data_to_embed_final))
                logger.info(f"APIC тег добавлен: {len(artwork_data_to_embed_final)} bytes")
            except Exception as e_apic_add:
                logger.error(f"Не удалось добавить APIC тег: {e_apic_add}");
                artwork_data_to_embed_final = None
        audio_id3.save()

        await update_progress_display(99, "TRACK_STAGE_UPLOADING")
        raw_artwork_for_thumb: Optional[bytes] = None
        if artwork_data_to_embed_final:
            raw_artwork_for_thumb = artwork_data_to_embed_final
        else:  # Check if artwork was already in the mp3 and survived conversion
            final_mp3_check = MP3(str(mp3_final_file), ID3=ID3)
            if final_mp3_check.tags:
                for k_apic_check in list(final_mp3_check.tags.keys()):
                    if k_apic_check.startswith('APIC:'):
                        raw_artwork_for_thumb = final_mp3_check.tags[k_apic_check].data
                        break

        if raw_artwork_for_thumb:
            embedded_artwork_data_io = prepare_thumbnail_for_telegram(raw_artwork_for_thumb)

        telegram_filename = sanitize_filename(f"{performer_str} - {title_str}.mp3")
        from pyrogram_sender import send_audio_pyrogram
        sent_audio_message_id = await send_audio_pyrogram(
            chat_id=chat_id,
            audio_path=str(mp3_final_file),
            filename=telegram_filename,
            title=title_str,
            performer=performer_str,
            thumbnail_data=embedded_artwork_data_io,
            reply_to_message_id=reply_to_message_id_for_final_audio if not is_sync_mode else None,
        )
        return True, sent_audio_message_id

    except (RuntimeError, FileNotFoundError, asyncio.TimeoutError) as e_proc:
        error_occurred_for_logging = True
        if error_reason_for_db == "Unknown error": error_reason_for_db = f"Processing: {str(e_proc)[:100]}"
        err_name_short = original_downloaded_file.name if original_downloaded_file else url.split('/')[-1]
        error_text_for_log = ui_texts.LOG_ERR_PROCESSING_FORMAT.format(filename_short=err_name_short[:30],
                                                                       error_details=str(e_proc)[:150])
        db.log_user_error(user_id, error_text_for_log, context_info=url)
        if not is_sync_mode and status_message_id_to_edit:
            user_facing_error = ui_texts.USER_ERR_PROCESSING_DIRECT_FORMAT.format(filename_short=err_name_short[:30],
                                                                                  error_details=str(e_proc)[:150])
            try:
                await context.bot.edit_message_text(chat_id=chat_id, message_id=status_message_id_to_edit,
                                                    text=user_facing_error)
            except telegram.error.TelegramError:
                pass
        return False, None
    except telegram.error.RetryAfter as e_tg_retry_main:  # Should be caught by inner loops, but as a safeguard
        error_occurred_for_logging = True
        error_reason_for_db = f"TelegramAPI-FloodCtrl: {e_tg_retry_main.message[:100]}"
        err_name_short = original_downloaded_file.name if original_downloaded_file else url.split('/')[-1]
        error_text_for_log = ui_texts.LOG_ERR_TELEGRAM_FORMAT.format(filename_short=err_name_short[:20],
                                                                     error_details=f"Flood control (max retries {MAX_TELEGRAM_API_RETRIES}). {e_tg_retry_main.message[:130]}")
        db.log_user_error(user_id, error_text_for_log, context_info=url)
        if not is_sync_mode and status_message_id_to_edit:
            user_facing_error_text = ui_texts.USER_ERR_TELEGRAM_DIRECT_FORMAT.format(filename_short=err_name_short[:20],
                                                                                     error_details=f"Слишком много запросов к Telegram (ошибка после {MAX_TELEGRAM_API_RETRIES} попыток). Попробуйте позже.")
            for attempt_edit_err in range(1, MAX_TELEGRAM_API_RETRIES + 1):  # Retry for error message edit too
                try:
                    await context.bot.edit_message_text(chat_id=chat_id, message_id=status_message_id_to_edit,
                                                        text=user_facing_error_text); break
                except telegram.error.RetryAfter as e_retry_edit_err:
                    await asyncio.sleep(e_retry_edit_err.retry_after + TELEGRAM_API_RETRY_BUFFER)
                except telegram.error.TelegramError:
                    break
        return False, None
    except telegram.error.TelegramError as e_tg:
        error_occurred_for_logging = True
        error_reason_for_db = f"TelegramAPI: {e_tg.message[:100]}"
        err_name_short = original_downloaded_file.name if original_downloaded_file else url.split('/')[-1]
        error_text_for_log = ui_texts.LOG_ERR_TELEGRAM_FORMAT.format(filename_short=err_name_short[:20],
                                                                     error_details=e_tg.message[:150])
        db.log_user_error(user_id, error_text_for_log, context_info=url)
        if not is_sync_mode and status_message_id_to_edit:
            user_facing_error = ui_texts.USER_ERR_TELEGRAM_DIRECT_FORMAT.format(filename_short=err_name_short[:20],
                                                                                error_details=e_tg.message[:150])
            try:
                await context.bot.edit_message_text(chat_id=chat_id, message_id=status_message_id_to_edit,
                                                    text=user_facing_error)
            except telegram.error.TelegramError:
                pass
        return False, None
    except Exception as e_gen:
        error_occurred_for_logging = True
        error_reason_for_db = f"General: {str(e_gen)[:100]}"
        logger.exception(f"Общая ошибка в modified_handle_soundcloud_link для {url}, user {user_id}: {e_gen}")
        err_name_short = original_downloaded_file.name if original_downloaded_file else url.split('/')[-1]
        error_text_for_log = ui_texts.LOG_ERR_UNEXPECTED_FORMAT.format(filename_short=err_name_short[:20])
        db.log_user_error(user_id, error_text_for_log, context_info=url)
        if not is_sync_mode and status_message_id_to_edit:
            user_facing_error = ui_texts.USER_ERR_UNEXPECTED_DIRECT_FORMAT.format(filename_short=err_name_short[:20])
            try:
                await context.bot.edit_message_text(chat_id=chat_id, message_id=status_message_id_to_edit,
                                                    text=user_facing_error)
            except telegram.error.TelegramError:
                pass
        return False, None
    finally:
        if error_occurred_for_logging:
            db.add_failed_track(user_id, url, reason=error_reason_for_db)
        if embedded_artwork_data_io: embedded_artwork_data_io.close()
        if request_temp_path and request_temp_path.exists():
            try:
                for item in request_temp_path.iterdir():
                    if item.is_file(): os.remove(item)
                os.rmdir(request_temp_path)
            except OSError as e_clean:
                logger.error(f"Ошибка очистки временной папки {request_temp_path}: {e_clean}")


async def handle_soundcloud_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from handlers_menu import AWAITING_TEXT_INPUT_KEY, update_user_status_message
    if not update.message or not update.message.text: return
    message_text = update.message.text
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if context.user_data.get(AWAITING_TEXT_INPUT_KEY, False):
        logger.debug("Получено сообщение, но ожидается ввод для меню, игнорируем как ссылку.")
        return

    soundcloud_url_match = re.search(r'(https?://soundcloud\.com/[^\s]+)', message_text)
    if not soundcloud_url_match:
        if not message_text.startswith('/'):  # Only inform if it's not a command
            pass  # await update.message.reply_text(ui_texts.NOT_A_SOUNDCLOUD_LINK_OR_COMMAND)
        return
    url = soundcloud_url_match.group(1)

    temp_direct_dl_progress_msg: Optional[Message] = None
    temp_direct_dl_progress_msg_id: Optional[int] = None
    initial_text_for_direct_dl = create_progress_bar(0) + f" {ui_texts.DIRECT_DL_PREPARING}"

    for attempt in range(1, MAX_TELEGRAM_API_RETRIES + 1):
        try:
            temp_direct_dl_progress_msg = await update.message.reply_text(initial_text_for_direct_dl)
            if temp_direct_dl_progress_msg: temp_direct_dl_progress_msg_id = temp_direct_dl_progress_msg.message_id
            break
        except telegram.error.RetryAfter as e_retry_initial:
            wait_time = e_retry_initial.retry_after + TELEGRAM_API_RETRY_BUFFER
            logger.warning(
                f"Flood control sending initial direct DL progress. Retrying in {wait_time:.2f}s (attempt {attempt}/{MAX_TELEGRAM_API_RETRIES}).")
            await asyncio.sleep(wait_time)
            if attempt == MAX_TELEGRAM_API_RETRIES:
                logger.error(
                    f"{ui_texts.DIRECT_DL_ERROR_SENDING_INITIAL_PROGRESS_FORMAT.format(error_details=e_retry_initial)} (max retries)")
                db.log_user_error(user_id, ui_texts.DIRECT_DL_ERROR_START_PROCESSING_FORMAT.format(
                    error_details=e_retry_initial), url)
                return
        except telegram.error.TelegramError as e_initial:
            logger.error(ui_texts.DIRECT_DL_ERROR_SENDING_INITIAL_PROGRESS_FORMAT.format(error_details=e_initial))
            db.log_user_error(user_id, ui_texts.DIRECT_DL_ERROR_START_PROCESSING_FORMAT.format(error_details=e_initial),
                              url)
            return

    if not temp_direct_dl_progress_msg_id:
        logger.error(f"Failed to send initial progress message for {url} after all retries or other critical error.")
        return

    success, _ = await modified_handle_soundcloud_link(
        url=url, user_id=user_id, chat_id=chat_id, context=context,
        status_message_id_to_edit=temp_direct_dl_progress_msg_id,
        text_prefix_for_status="",
        reply_to_message_id_for_final_audio=update.message.message_id if update.message else None
    )

    if temp_direct_dl_progress_msg_id:
        if success:  # Only delete progress message on success, otherwise it shows the error
            for attempt_del in range(1, MAX_TELEGRAM_API_RETRIES + 1):
                try:
                    await context.bot.delete_message(chat_id=chat_id, message_id=temp_direct_dl_progress_msg_id)
                    break
                except telegram.error.RetryAfter as e_retry_delete:
                    wait_time = e_retry_delete.retry_after + TELEGRAM_API_RETRY_BUFFER
                    logger.warning(
                        f"Flood control deleting temp direct DL progress msg. Retrying in {wait_time:.2f}s (attempt {attempt_del}/{MAX_TELEGRAM_API_RETRIES}).")
                    await asyncio.sleep(wait_time)
                except telegram.error.TelegramError:
                    logger.warning(
                        f"Failed to delete temp progress message {temp_direct_dl_progress_msg_id} (non-retryable or max retries).")
                    break

    await update_user_status_message(user_id, chat_id, context.bot_data, context.bot)