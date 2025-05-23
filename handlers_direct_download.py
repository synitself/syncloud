# handlers_direct_download.py
import logging
import asyncio
import re
from pathlib import Path
import io
from typing import Optional, Tuple, Any, cast
import os
import unicodedata

from telegram import Update, Message
from telegram.ext import ContextTypes
import telegram.error

from mutagen.id3 import ID3, APIC, TIT2, TPE1, TALB
from mutagen.mp3 import MP3
from mutagen.easyid3 import EasyID3
from mutagen.mp4 import MP4, MP4Cover
from mutagen.flac import FLAC

from config import DOWNLOAD_FOLDER
from utils import sanitize_filename, create_progress_bar

logger = logging.getLogger(__name__)


async def modified_handle_soundcloud_link(
        url: str, user_id: int, chat_id: int, context: ContextTypes.DEFAULT_TYPE,
        sync_mode: bool = False,
        progress_message_to_update: Optional[Message] = None,
        current_track_num: int = 0,
        total_tracks_for_sync: int = 0,
        reply_to_message_id_for_final_audio: Optional[int] = None
) -> Tuple[bool, Optional[int]]:
    logger.info(f"Processing URL ({'sync' if sync_mode else 'direct'}): {url} for user {user_id}")

    local_progress_msg_for_direct_download: Optional[Message] = None
    if not sync_mode:
        local_progress_msg_for_direct_download = progress_message_to_update

    import hashlib
    url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
    request_temp_path = Path(DOWNLOAD_FOLDER) / str(user_id) / f"track_{url_hash}"
    request_temp_path.mkdir(parents=True, exist_ok=True)

    original_downloaded_file: Optional[Path] = None;
    mp3_final_file: Optional[Path] = None
    artwork_from_original_data: Optional[bytes] = None;
    artwork_from_original_mime: Optional[str] = None
    artwork_external_file_path: Optional[Path] = None;
    embedded_artwork_data_io: Optional[io.BytesIO] = None
    artwork_data_to_embed_final: Optional[bytes] = None;
    artwork_mime_type_final: Optional[str] = None
    sent_audio_message_id: Optional[int] = None

    try:
        async def update_progress(percent: int, stage_msg_local: str):
            nonlocal local_progress_msg_for_direct_download
            progress_text_to_show: str
            target_message_to_edit: Optional[Message] = None

            if sync_mode:
                target_message_to_edit = progress_message_to_update
                sync_stage_text = f"𝙏𝙍𝘼𝘾𝙆 {current_track_num}/{total_tracks_for_sync} ({percent}%): {stage_msg_local}"
                progress_text_to_show = create_progress_bar(percent, stage_message=sync_stage_text)
            else:
                target_message_to_edit = local_progress_msg_for_direct_download
                progress_text_to_show = create_progress_bar(percent, stage_message=stage_msg_local)

            if not target_message_to_edit and not sync_mode and reply_to_message_id_for_final_audio:
                local_progress_msg_for_direct_download = await context.bot.send_message(
                    chat_id, progress_text_to_show,
                    reply_to_message_id=reply_to_message_id_for_final_audio)
            elif target_message_to_edit:
                try:
                    await target_message_to_edit.edit_text(progress_text_to_show)
                except telegram.error.BadRequest as e_edit:
                    if "Message is not modified" not in str(e_edit).lower() and "message to edit not found" not in str(
                            e_edit).lower():
                        logger.warning(f"Progress edit failed (msg_id {target_message_to_edit.message_id}): {e_edit}")
                except Exception as e_unexp:
                    logger.error(
                        f"Unexpected error updating progress (msg_id {target_message_to_edit.message_id if target_message_to_edit else 'unknown'}): {e_unexp}")

        await update_progress(0, "𝙎𝙏𝘼𝙍𝙏𝙄𝙉𝙂...")
        await update_progress(5, "𝘿𝙊𝙒𝙉𝙇𝙊𝘼𝘿𝙄𝙉𝙂...")
        scdl_cmd = ["scdl", "-l", url, "-c", "--path", str(request_temp_path), "--overwrite", "--hide-progress"]
        process_scdl = await asyncio.create_subprocess_exec(*scdl_cmd, stdout=asyncio.subprocess.PIPE,
                                                            stderr=asyncio.subprocess.PIPE)
        scdl_stdout, scdl_stderr = await asyncio.wait_for(process_scdl.communicate(), timeout=300)
        if process_scdl.returncode != 0:
            err_msg = scdl_stderr.decode(errors='ignore').strip()
            raise RuntimeError(f"scdl failed: {err_msg.splitlines()[-1][:100] if err_msg else 'Неизв.'}")
        for item_name in os.listdir(request_temp_path):
            item_path = request_temp_path / item_name;
            ext_lower = item_path.suffix.lower()
            if item_path.is_file():
                if ext_lower in (".mp3", ".m4a", ".ogg", ".flac", ".wav"):
                    original_downloaded_file = item_path
                elif ext_lower in (".jpg", ".jpeg", ".png"):
                    artwork_external_file_path = item_path
        if not original_downloaded_file: raise FileNotFoundError("𝘼𝙐𝘿𝙄𝙊 𝙉𝙊𝙏 𝙁𝙊𝙐𝙉𝘿")
        await update_progress(35, "...")
        if original_downloaded_file:
            try:
                ext = original_downloaded_file.suffix.lower();
                audio_o: Any = None
                if ext == ".m4a":
                    audio_o = MP4(str(original_downloaded_file))
                elif ext == ".mp3":
                    audio_o = MP3(str(original_downloaded_file), ID3=ID3)
                elif ext == ".flac":
                    audio_o = FLAC(str(original_downloaded_file))
                if isinstance(audio_o, MP4) and 'covr' in audio_o and audio_o['covr'] and audio_o['covr'][0]:
                    artwork_from_original_data = bytes(audio_o['covr'][0]);
                    img_fmt = audio_o['covr'][0].imageformat
                    if img_fmt == MP4Cover.FORMAT_JPEG:
                        artwork_from_original_mime = 'image/jpeg'
                    elif img_fmt == MP4Cover.FORMAT_PNG:
                        artwork_from_original_mime = 'image/png'
                elif isinstance(audio_o, MP3) and audio_o.tags:
                    for k in list(audio_o.tags.keys()):
                        if k.startswith('APIC:'): artwork_from_original_data = audio_o.tags[
                            k].data;artwork_from_original_mime = audio_o.tags[k].mime;break
                elif isinstance(audio_o, FLAC) and audio_o.pictures:
                    artwork_from_original_data = audio_o.pictures[0].data;
                    artwork_from_original_mime = audio_o.pictures[0].mime
            except Exception as e:
                logger.warning(f"Art extr. fail {original_downloaded_file.name}: {e}")
        base_name_sanitized = sanitize_filename(original_downloaded_file.stem)
        is_conversion_needed = original_downloaded_file.suffix.lower() != ".mp3"
        if is_conversion_needed:
            await update_progress(40, "𝘾𝙊𝙉𝙑𝙀𝙍𝙏𝙄𝙉𝙂...")
            mp3_final_file = request_temp_path / f"{base_name_sanitized}.mp3"
            ffmpeg_cmd = ["ffmpeg", "-y", "-i", str(original_downloaded_file), "-vn", "-ar", "44100", "-ac", "2",
                          "-b:a", "192k", str(mp3_final_file)]
            proc_ff = await asyncio.create_subprocess_exec(*ffmpeg_cmd, stdout=asyncio.subprocess.PIPE,
                                                           stderr=asyncio.subprocess.PIPE)
            ff_out, ff_err = await asyncio.wait_for(proc_ff.communicate(), timeout=300)
            if proc_ff.returncode != 0: raise RuntimeError(f"ffmpeg fail: {ff_err.decode(errors='ignore')[:150]}")
        else:
            mp3_final_file = original_downloaded_file
        if not mp3_final_file or not mp3_final_file.exists(): raise FileNotFoundError("MP3 файл не найден")
        await update_progress(70, "𝙋𝙇𝙀𝘼𝙎𝙀 𝙒𝘼𝙄𝙏...")
        title_str, performer_str = "Unknown Title", "Unknown Artist"
        audio_id3 = MP3(str(mp3_final_file), ID3=ID3);
        audio_tags_easy = None
        if audio_id3.tags is None: audio_id3.add_tags()
        try:
            audio_tags_easy = EasyID3(str(mp3_final_file))
        except:
            logger.warning(f"EasyID3 fail {mp3_final_file.name}")
        match_fn = re.match(r"(.+?) - (.+)", original_downloaded_file.stem, re.IGNORECASE)
        if match_fn:
            fn_p, fn_t = match_fn.group(1).strip(), match_fn.group(2).strip()
            _at_obj = audio_id3.tags.get('TPE1');
            _at_text_list = _at_obj.text if _at_obj else [fn_p]
            performer_str = (audio_tags_easy.get('artist', [fn_p])[0] if audio_tags_easy else (
                _at_text_list[0] if _at_text_list else fn_p)) or fn_p
            _tt_obj = audio_id3.tags.get('TIT2');
            _tt_text_list = _tt_obj.text if _tt_obj else [fn_t]
            title_str = (audio_tags_easy.get('title', [fn_t])[0] if audio_tags_easy else (
                _tt_text_list[0] if _tt_text_list else fn_t)) or fn_t
        else:
            _ttbs_obj = audio_id3.tags.get('TIT2');
            _ttbs_text_list = _ttbs_obj.text if _ttbs_obj else [original_downloaded_file.stem]
            title_str = (audio_tags_easy.get('title', [original_downloaded_file.stem])[0] if audio_tags_easy else (
                _ttbs_text_list[
                    0] if _ttbs_text_list else original_downloaded_file.stem)) or original_downloaded_file.stem
            _atbs_obj = audio_id3.tags.get('TPE1');
            _atbs_text_list = _atbs_obj.text if _atbs_obj else ["Unknown Artist"]
            performer_str = (audio_tags_easy.get('artist', ["Unknown Artist"])[0] if audio_tags_easy else (
                _atbs_text_list[0] if _atbs_text_list else "Unknown Artist")) or "Unknown Artist"
        if audio_tags_easy:
            audio_tags_easy['artist'] = performer_str; audio_tags_easy['title'] = title_str; audio_tags_easy.save()
        else:
            audio_id3.tags.delall('TPE1'); audio_id3.tags.add(
                TPE1(encoding=3, text=performer_str)); audio_id3.tags.delall('TIT2'); audio_id3.tags.add(
                TIT2(encoding=3, text=title_str))
        if artwork_external_file_path and artwork_external_file_path.exists():
            with open(artwork_external_file_path, 'rb') as afp:
                artwork_data_to_embed_final = afp.read()
            artwork_mime_type_final = 'image/jpeg' if artwork_external_file_path.suffix.lower() in ['.jpg',
                                                                                                    '.jpeg'] else 'image/png'
        elif artwork_from_original_data:
            artwork_data_to_embed_final = artwork_from_original_data; artwork_mime_type_final = artwork_from_original_mime or 'image/jpeg'
        audio_id3.tags.delall('APIC')
        if artwork_data_to_embed_final and artwork_mime_type_final:
            try:
                audio_id3.tags.add(APIC(encoding=3, mime=artwork_mime_type_final, type=3, desc='Cover',
                                        data=artwork_data_to_embed_final))
            except Exception as e_apic_add:
                logger.error(f"Failed to add APIC tag: {e_apic_add}"); artwork_data_to_embed_final = None
        audio_id3.save()
        await update_progress(99, "𝙐𝙋𝙇𝙊𝘼𝘿𝙄𝙉𝙂...")
        if artwork_data_to_embed_final:
            embedded_artwork_data_io = io.BytesIO(artwork_data_to_embed_final)
        else:
            final_mp3_chk = MP3(str(mp3_final_file), ID3=ID3)
            if final_mp3_chk.tags:
                for k_apic in list(final_mp3_chk.tags.keys()):
                    if k_apic.startswith('APIC:'): embedded_artwork_data_io = io.BytesIO(
                        final_mp3_chk.tags[k_apic].data);break
        telegram_filename = sanitize_filename(f"{performer_str} - {title_str}.mp3")
        with open(mp3_final_file, "rb") as audio_f:
            if embedded_artwork_data_io: embedded_artwork_data_io.seek(0)
            sent_msg_obj = await context.bot.send_audio(chat_id=chat_id, audio=audio_f, filename=telegram_filename,
                                                        title=title_str, performer=performer_str,
                                                        thumbnail=embedded_artwork_data_io,
                                                        reply_to_message_id=reply_to_message_id_for_final_audio)
            sent_audio_message_id = sent_msg_obj.message_id if sent_msg_obj else None

        target_to_delete = progress_message_to_update if sync_mode else local_progress_msg_for_direct_download
        # ИЗМЕНЕНИЕ: Удаляем ТОЛЬКО если это НЕ режим синхронизации (т.е. это локальный прогресс-бар)
        # Общее сообщение синхронизации удалять здесь не нужно.
        if not sync_mode and local_progress_msg_for_direct_download:
            try:
                await local_progress_msg_for_direct_download.delete()
            except telegram.error.TelegramError as e_del_prog:
                logger.warning(f"Could not delete local progress msg: {e_del_prog}")
        return True, sent_audio_message_id

    except (RuntimeError, FileNotFoundError, asyncio.TimeoutError) as e_proc:
        err_name = original_downloaded_file.name if original_downloaded_file else url.split('/')[-1]
        error_text = f"🚫 Ошибка ({err_name[:20]}...): {str(e_proc)[:100]}"
        target_err_msg_obj = progress_message_to_update if sync_mode else local_progress_msg_for_direct_download
        # В режиме синхронизации отправляем отдельное сообщение об ошибке для трека, не редактируя общее
        if sync_mode:
            await context.bot.send_message(chat_id, error_text)
        elif target_err_msg_obj:
            await target_err_msg_obj.edit_text(error_text)
        elif reply_to_message_id_for_final_audio:
            await context.bot.send_message(chat_id, error_text, reply_to_message_id=reply_to_message_id_for_final_audio)
        else:
            await context.bot.send_message(chat_id, error_text)
        return False, None
    except telegram.error.TelegramError as e_tg:
        err_name = original_downloaded_file.name if original_downloaded_file else url.split('/')[-1]
        error_text = f"🚫 𝙏𝙀𝙇𝙀𝙂𝙍𝘼𝙈 𝙀𝙍𝙍𝙊𝙍 ({err_name[:20]}...): {e_tg.message[:100]}"
        target_err_msg_obj = progress_message_to_update if sync_mode else local_progress_msg_for_direct_download
        if sync_mode:
            await context.bot.send_message(chat_id, error_text)
        elif target_err_msg_obj:
            await target_err_msg_obj.edit_text(error_text)
        elif reply_to_message_id_for_final_audio:
            await context.bot.send_message(chat_id, error_text, reply_to_message_id=reply_to_message_id_for_final_audio)
        else:
            await context.bot.send_message(chat_id, error_text)
        return False, None
    except Exception as e_gen:
        logger.exception(f"General error in modified_handle_soundcloud_link for {url}, user {user_id}: {e_gen}")
        err_name = original_downloaded_file.name if original_downloaded_file else url.split('/')[-1]
        error_text = f"🚫 𝙐𝙉𝙀𝙓𝙋𝙀𝘾𝙏𝙀𝘿 𝙀𝙍𝙍𝙊𝙍 ({err_name[:20]}...)"
        target_err_msg_obj = progress_message_to_update if sync_mode else local_progress_msg_for_direct_download
        if sync_mode:
            await context.bot.send_message(chat_id, error_text)
        elif target_err_msg_obj:
            await target_err_msg_obj.edit_text(error_text)
        elif reply_to_message_id_for_final_audio:
            await context.bot.send_message(chat_id, error_text, reply_to_message_id=reply_to_message_id_for_final_audio)
        else:
            await context.bot.send_message(chat_id, error_text)
        return False, None
    finally:
        if embedded_artwork_data_io: embedded_artwork_data_io.close()
        if request_temp_path and request_temp_path.exists():
            try:
                for item in request_temp_path.iterdir():
                    if item.is_file(): os.remove(item)
                os.rmdir(request_temp_path)
            except OSError as e_clean:
                logger.error(f"Error cleaning temp track folder {request_temp_path}: {e_clean}")


async def handle_soundcloud_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from handlers_menu import AWAITING_TEXT_INPUT_KEY
    if not update.message or not update.message.text: return
    message_text = update.message.text
    if context.user_data.get(AWAITING_TEXT_INPUT_KEY, False): return

    soundcloud_url_match = re.search(r'(https?://soundcloud\.com/[^\s]+)', message_text)
    if not soundcloud_url_match:
        if not message_text.startswith('/'): pass
        return
    url = soundcloud_url_match.group(1)
    initial_progress_msg = await update.message.reply_text(create_progress_bar(0, stage_message="STARTING..."))
    await modified_handle_soundcloud_link(
        url=url, user_id=update.effective_user.id, chat_id=update.effective_chat.id, context=context,
        sync_mode=False,
        progress_message_to_update=initial_progress_msg,
        reply_to_message_id_for_final_audio=update.message.message_id if update.message else None)