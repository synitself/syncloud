# bot.py
import logging
import os
import asyncio
import re
from pathlib import Path
import unicodedata
import io
import math
from datetime import datetime
from typing import Optional  # Добавим для Optional[Message]

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery, Message  # Добавили Message
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    CallbackQueryHandler, ContextTypes, ConversationHandler, Defaults
    # Defaults может быть убран, если не используется для других настроек
)
# Убираем import HTTPXRequest и httpx, так как используем прямые методы ApplicationBuilder
import telegram.error

# Импорты mutagen
from mutagen.id3 import ID3, APIC, TIT2, TPE1, TALB
from mutagen.mp3 import MP3
from mutagen.easyid3 import EasyID3
from mutagen.mp4 import MP4, MP4Cover
from mutagen.flac import FLAC

try:
    from config import TELEGRAM_BOT_TOKEN, DOWNLOAD_FOLDER
except ImportError:
    print("Критическая ошибка: Файл config.py не найден или не содержит TELEGRAM_BOT_TOKEN и DOWNLOAD_FOLDER.")
    print("Пожалуйста, создайте config.py с необходимыми данными.")
    exit(1)

import db

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx используется под капотом PTB
logging.getLogger("telegram.ext").setLevel(logging.INFO)
logger = logging.getLogger(__name__)

Path(DOWNLOAD_FOLDER).mkdir(parents=True, exist_ok=True)

(MAIN_MENU, SETTINGS_MENU,
 AWAIT_SC_USERNAME, AWAIT_SYNC_PERIOD, INFO_MENU) = map(str, range(5))


def sanitize_filename(filename: str) -> str:
    filename = str(filename)
    filename = filename.replace("/", "-").replace("\\", "-")
    try:
        filename = unicodedata.normalize('NFKD', filename).encode('ascii', 'ignore').decode('ascii')
    except TypeError:
        pass
    filename = re.sub(r'[^\w\s\.\-_()]', '', filename).strip()
    filename = re.sub(r'\s+', ' ', filename)
    if not filename: filename = "downloaded_track"
    return filename


def create_progress_bar(percentage: int, length: int = 10, stage_message: str = "") -> str:
    if not (0 <= percentage <= 100):
        percentage = max(0, min(100, percentage))
    filled_length = int(length * percentage // 100)
    bar = '█' * filled_length + '░' * (length - filled_length)
    return f"[{bar}] {int(percentage)}% {stage_message}"


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    logger.info(f"User {user_id} used /start command.")
    if not db.get_user_settings(user_id):
        db.update_user_settings(user_id, is_new_user_setup=True)

    await update.message.reply_text(
        "Привет! Я бот для скачивания музыки с SoundCloud.\n"
        "🎵 Просто отправь мне ссылку на трек.\n"
        "⚙️ Используй /menu для настройки синхронизации лайков."
    )


async def modified_handle_soundcloud_link(
        url: str,
        user_id: int,
        chat_id: int,
        context: ContextTypes.DEFAULT_TYPE,
        sync_mode: bool = False,
        original_sync_message_obj: Optional[Message] = None,  # Используем Optional[Message]
        current_track_num: int = 0,
        total_tracks_for_sync: int = 0,
        reply_to_this_message_id: int | None = None
) -> tuple[bool, int | None]:
    logger.info(f"Processing URL ({'sync mode' if sync_mode else 'direct'}): {url} for user {user_id}")

    progress_msg_obj_local: Optional[Message] = None  # Используем Optional[Message]
    import hashlib
    url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
    request_temp_path = Path(DOWNLOAD_FOLDER) / str(user_id) / f"track_{url_hash}"
    request_temp_path.mkdir(parents=True, exist_ok=True)

    original_downloaded_file: Path | None = None;
    mp3_final_file: Path | None = None
    artwork_from_original_data: bytes | None = None;
    artwork_from_original_mime: str | None = None
    artwork_external_file_path: Path | None = None;
    embedded_artwork_data_io: io.BytesIO | None = None
    artwork_data_to_embed_final: bytes | None = None;
    artwork_mime_type_final: str | None = None
    sent_message_id_for_track: int | None = None

    try:
        async def update_local_progress(percent, stage_msg_local):
            progress_text_to_show = create_progress_bar(percent, stage_message=stage_msg_local)
            nonlocal progress_msg_obj_local
            if sync_mode and original_sync_message_obj:
                sync_stage_text = f"Трек {current_track_num}/{total_tracks_for_sync}: {stage_msg_local}"
                try:
                    await original_sync_message_obj.edit_text(
                        create_progress_bar(percent, stage_message=sync_stage_text))
                except telegram.error.BadRequest as e_edit:
                    if "Message is not modified" not in str(e_edit): logger.warning(
                        f"Sync progress edit failed: {e_edit}")
            elif not sync_mode:
                if progress_msg_obj_local:
                    try:
                        await progress_msg_obj_local.edit_text(progress_text_to_show)
                    except telegram.error.BadRequest as e_edit:
                        if "Message is not modified" not in str(e_edit): logger.warning(
                            f"Local progress edit failed: {e_edit}")
                elif reply_to_this_message_id:
                    progress_msg_obj_local = await context.bot.send_message(chat_id, progress_text_to_show,
                                                                            reply_to_message_id=reply_to_this_message_id)

        if not sync_mode and reply_to_this_message_id:
            await update_local_progress(0, "Получение ссылки...")
        elif sync_mode and original_sync_message_obj:
            await update_local_progress(0, "Начало...")

        await update_local_progress(5, "Скачивание...")
        scdl_cmd = ["scdl", "-l", url, "-c", "--path", str(request_temp_path), "--overwrite"]
        process_scdl = await asyncio.create_subprocess_exec(*scdl_cmd, stdout=asyncio.subprocess.PIPE,
                                                            stderr=asyncio.subprocess.PIPE)
        scdl_stdout, scdl_stderr = await asyncio.wait_for(process_scdl.communicate(), timeout=300)

        if process_scdl.returncode != 0:
            err_msg = scdl_stderr.decode(errors='ignore').strip()
            raise ChildProcessError(
                f"scdl failed ({url.split('/')[-1][:20]}...): {err_msg.splitlines()[-1][:100] if err_msg else 'Неизв.'}")

        for item_name in os.listdir(request_temp_path):
            item_path = request_temp_path / item_name;
            ext_lower = item_path.suffix.lower()
            if item_path.is_file():
                if ext_lower in (".mp3", ".m4a", ".ogg", ".flac", ".wav"):
                    original_downloaded_file = item_path
                elif ext_lower in (".jpg", ".jpeg", ".png"):
                    artwork_external_file_path = item_path
        if not original_downloaded_file: raise FileNotFoundError("Аудиофайл не найден после scdl.")

        await update_local_progress(35, "Извлечение обложки...")
        if original_downloaded_file:
            try:
                ext = original_downloaded_file.suffix.lower()
                if ext == ".m4a":
                    audio_o = MP4(str(original_downloaded_file))
                    if 'covr' in audio_o and audio_o['covr'] and audio_o['covr'][0]:
                        artwork_from_original_data = bytes(audio_o['covr'][0])
                        if audio_o['covr'][0].imageformat == MP4Cover.FORMAT_JPEG:
                            artwork_from_original_mime = 'image/jpeg'
                        elif audio_o['covr'][0].imageformat == MP4Cover.FORMAT_PNG:
                            artwork_from_original_mime = 'image/png'
                elif ext == ".mp3":
                    audio_o = MP3(str(original_downloaded_file), ID3=ID3)
                    if audio_o.tags:
                        for k in list(audio_o.tags.keys()):
                            if k.startswith('APIC:'): artwork_from_original_data = audio_o.tags[
                                k].data; artwork_from_original_mime = audio_o.tags[k].mime; break
            except Exception as e:
                logger.warning(f"Art extr. fail {original_downloaded_file.name}: {e}")

        base_name_sanitized = sanitize_filename(original_downloaded_file.stem)
        is_conversion_needed = original_downloaded_file.suffix.lower() != ".mp3"
        if is_conversion_needed:
            await update_local_progress(40, "Конвертация...")
            mp3_final_file = request_temp_path / f"{base_name_sanitized}.mp3"
            ffmpeg_cmd = ["ffmpeg", "-y", "-i", str(original_downloaded_file), "-vn", "-ar", "44100", "-ac", "2",
                          "-b:a", "192k", str(mp3_final_file)]
            proc_ff = await asyncio.create_subprocess_exec(*ffmpeg_cmd, stdout=asyncio.subprocess.PIPE,
                                                           stderr=asyncio.subprocess.PIPE)
            ff_out, ff_err = await asyncio.wait_for(proc_ff.communicate(), timeout=300)
            if proc_ff.returncode != 0: raise ChildProcessError(f"ffmpeg fail: {ff_err.decode(errors='ignore')[:200]}")
        else:
            mp3_final_file = original_downloaded_file
        if not mp3_final_file or not mp3_final_file.exists(): raise FileNotFoundError("MP3 файл не найден.")

        await update_local_progress(70, "Обработка тегов...")
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
            _at = audio_id3.tags.get('TPE1', TPE1(text=[fn_p]));
            performer_str = (audio_tags_easy.get('artist', [fn_p])[0] if audio_tags_easy else (
                _at.text[0] if _at and _at.text else fn_p)) or fn_p
            _tt = audio_id3.tags.get('TIT2', TIT2(text=[fn_t]));
            title_str = (audio_tags_easy.get('title', [fn_t])[0] if audio_tags_easy else (
                _tt.text[0] if _tt and _tt.text else fn_t)) or fn_t
        else:
            _ttbs = audio_id3.tags.get('TIT2', TIT2(text=[original_downloaded_file.stem]));
            title_str = (audio_tags_easy.get('title', [original_downloaded_file.stem])[0] if audio_tags_easy else (
                _ttbs.text[
                    0] if _ttbs and _ttbs.text else original_downloaded_file.stem)) or original_downloaded_file.stem
            _atbs = audio_id3.tags.get('TPE1', TPE1(text=["Unknown Artist"]));
            performer_str = (audio_tags_easy.get('artist', ["Unknown Artist"])[0] if audio_tags_easy else (
                _atbs.text[0] if _atbs and _atbs.text else "Unknown Artist")) or "Unknown Artist"

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
            artwork_data_to_embed_final = artwork_from_original_data;
            artwork_mime_type_final = artwork_from_original_mime or 'image/jpeg'

        audio_id3.tags.delall('APIC')
        if artwork_data_to_embed_final and artwork_mime_type_final:
            audio_id3.tags.add(
                APIC(encoding=3, mime=artwork_mime_type_final, type=3, desc='Cover', data=artwork_data_to_embed_final))
        audio_id3.save()

        await update_local_progress(99, "Загрузка в Telegram...")
        if artwork_data_to_embed_final:
            embedded_artwork_data_io = io.BytesIO(artwork_data_to_embed_final)
        else:
            final_mp3_check = MP3(str(mp3_final_file), ID3=ID3)
            if final_mp3_check.tags:
                for k_apic in list(final_mp3_check.tags.keys()):
                    if k_apic.startswith('APIC:'): embedded_artwork_data_io = io.BytesIO(
                        final_mp3_check.tags[k_apic].data); break

        telegram_filename = sanitize_filename(f"{performer_str} - {title_str}.mp3")
        with open(mp3_final_file, "rb") as audio_f:
            if embedded_artwork_data_io: embedded_artwork_data_io.seek(0)
            sent_msg_obj = await context.bot.send_audio(chat_id=chat_id, audio=audio_f, filename=telegram_filename,
                                                        title=title_str, performer=performer_str,
                                                        thumbnail=embedded_artwork_data_io,
                                                        reply_to_message_id=reply_to_this_message_id)
            sent_message_id_for_track = sent_msg_obj.message_id if sent_msg_obj else None

        if not sync_mode and progress_msg_obj_local: await progress_msg_obj_local.delete()
        return True, sent_message_id_for_track

    except (ChildProcessError, FileNotFoundError, asyncio.TimeoutError) as e_proc:
        logger.error(f"Process/File error for {url} (user {user_id}): {e_proc}")
        error_text = f"🚫 Ошибка: {str(e_proc)[:150]}"
        if sync_mode and original_sync_message_obj:
            await context.bot.send_message(chat_id, error_text)
        elif not sync_mode and progress_msg_obj_local:
            await progress_msg_obj_local.edit_text(error_text)
        elif not sync_mode and reply_to_this_message_id:
            await context.bot.send_message(chat_id, error_text, reply_to_message_id=reply_to_this_message_id)
        else:
            await context.bot.send_message(chat_id, error_text)
        return False, None
    except telegram.error.TelegramError as e_tg:
        logger.error(f"Telegram API error for {url} (user {user_id}): {e_tg}")
        error_text = f"🚫 Ошибка Telegram: {e_tg.message[:150]}"
        if sync_mode and original_sync_message_obj:
            await context.bot.send_message(chat_id, error_text)
        elif not sync_mode and progress_msg_obj_local:
            await progress_msg_obj_local.edit_text(error_text)
        elif not sync_mode and reply_to_this_message_id:
            await context.bot.send_message(chat_id, error_text, reply_to_message_id=reply_to_this_message_id)
        else:
            await context.bot.send_message(chat_id, error_text)
        return False, None
    except Exception as e_gen:
        logger.exception(f"General error in modified_handle_soundcloud_link for {url}, user {user_id}: {e_gen}")
        error_text = f"🚫 Непредвиденная ошибка при обработке трека."
        if sync_mode and original_sync_message_obj:
            await context.bot.send_message(chat_id, error_text)
        elif not sync_mode and progress_msg_obj_local:
            await progress_msg_obj_local.edit_text(error_text)
        elif not sync_mode and reply_to_this_message_id:
            await context.bot.send_message(chat_id, error_text, reply_to_message_id=reply_to_this_message_id)
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
    if not update.message or not update.message.text: return
    message_text = update.message.text
    soundcloud_url_match = re.search(r'(https?://soundcloud\.com/[^\s]+)', message_text)

    active_conv_state = context.user_data.get(ConversationHandler.STATE)
    if active_conv_state in [AWAIT_SC_USERNAME, AWAIT_SYNC_PERIOD]: return

    if not soundcloud_url_match:
        if not message_text.startswith('/'): pass
        return

    url = soundcloud_url_match.group(1)
    await modified_handle_soundcloud_link(
        url=url, user_id=update.effective_user.id, chat_id=update.effective_chat.id,
        context=context, sync_mode=False, reply_to_this_message_id=update.message.message_id)


async def sync_user_likes_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id;
    chat_id = update.effective_chat.id
    settings = db.get_user_settings(user_id)
    if not settings or not settings.get('sync_enabled') or not settings.get('soundcloud_username'):
        await update.message.reply_text("Синхронизация не настроена или выключена. /menu")
        return

    sc_username = settings['soundcloud_username']
    logger.info(f"Starting likes sync for user {user_id} (SC: {sc_username})")
    sync_overall_message = await update.message.reply_text(f"⏳ Поиск лайков для '{sc_username}'...")

    soundcloud_likes_url = f"https://soundcloud.com/{sc_username}/likes"
    ytdlp_cmd = ["yt-dlp", "--flat-playlist", "--print", "%(url)s", soundcloud_likes_url]
    logger.info(f"Running yt-dlp to get like URLs: {' '.join(ytdlp_cmd)}")

    track_urls_from_likes = []
    try:
        process_ytdlp = await asyncio.create_subprocess_exec(*ytdlp_cmd, stdout=asyncio.subprocess.PIPE,
                                                             stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(process_ytdlp.communicate(), timeout=300)
        if process_ytdlp.returncode == 0:
            urls_output = stdout.decode(errors='ignore').strip()
            if urls_output: track_urls_from_likes = [url.strip() for url in urls_output.splitlines() if
                                                     url.strip().startswith("https://soundcloud.com/")]
        else:
            raise ChildProcessError(f"yt-dlp failed: {stderr.decode(errors='ignore')[:200]}")
    except (asyncio.TimeoutError, ChildProcessError) as e_ytdlp:
        logger.error(f"Error getting likes for {sc_username}: {e_ytdlp}")
        await sync_overall_message.edit_text(f"🚫 Ошибка получения списка лайков: {str(e_ytdlp)[:150]}")
        return

    if not track_urls_from_likes:
        await sync_overall_message.edit_text(f"✅ Лайки для '{sc_username}' не найдены или список пуст.")
        db.update_user_settings(user_id, last_sync_timestamp=datetime.now());
        return

    # track_urls_from_likes.reverse() # Для обработки старых сначала

    total_liked_tracks = len(track_urls_from_likes)
    new_tracks_to_download_urls = [url for url in track_urls_from_likes if not db.is_track_downloaded(user_id, url)]

    if not new_tracks_to_download_urls:
        await sync_overall_message.edit_text(f"✅ Все {total_liked_tracks} лайков уже синхронизированы.")
        db.update_user_settings(user_id, last_sync_timestamp=datetime.now());
        return

        # --- ИЗМЕНЕНИЕ ДЛЯ ПОРЯДКА: СНАЧАЛА СТАРЫЕ ---
        # Разворачиваем список новых треков, чтобы обрабатывать сначала те,
        # которые были в конце исходного списка от yt-dlp (предположительно, более старые лайки)
    new_tracks_to_download_urls.reverse()
    logger.info(f"Reversed the order of new tracks. Will process older likes first.")
    # ----------------------------------------------

    total_new_to_process = len(new_tracks_to_download_urls)
    await sync_overall_message.edit_text(
        f"Найдено {total_new_to_process} новых треков из {total_liked_tracks} лайков. Начинаю обработку (старые сначала)...")

    sent_count = 0;
    error_count = 0;
    delay_between_sends = 3

    for i, track_url_to_process in enumerate(new_tracks_to_download_urls):
        success, sent_msg_id = await modified_handle_soundcloud_link(
            url=track_url_to_process, user_id=user_id, chat_id=chat_id, context=context,
            sync_mode=True, original_sync_message_obj=sync_overall_message,
            current_track_num=i + 1, total_tracks_for_sync=total_new_to_process)

        if success and sent_msg_id:
            db.add_downloaded_track(user_id, track_url_to_process, sent_msg_id); sent_count += 1
        elif not success:
            error_count += 1

        if i < total_new_to_process - 1: await asyncio.sleep(delay_between_sends)

    final_summary_message = (f"🏁 Синхронизация для '{sc_username}' завершена.\n"
                             f"Всего лайков: {total_liked_tracks}\nНовых для обработки: {total_new_to_process}\n"
                             f"✅ Отправлено: {sent_count}\n❌ Ошибок: {error_count}")
    await sync_overall_message.edit_text(final_summary_message)
    db.update_user_settings(user_id, last_sync_timestamp=datetime.now())


# --- Функции для ConversationHandler меню ---
async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    user_id = update.effective_user.id
    logger.info(f"User {user_id} вызвал /menu")
    if not db.get_user_settings(user_id): db.update_user_settings(user_id, is_new_user_setup=True)
    keyboard = [[InlineKeyboardButton("⚙️ Настройки синхронизации", callback_data="settings_menu_nav")],
                [InlineKeyboardButton("ℹ️ Информация о боте", callback_data="info_bot_nav")],
                [InlineKeyboardButton("❌ Закрыть меню", callback_data="close_menu_nav")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    if update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text("Главное меню:", reply_markup=reply_markup)
            if update.callback_query.message: context.user_data[
                'last_menu_message_id'] = update.callback_query.message.message_id
        except telegram.error.BadRequest as e:
            if "Message is not modified" not in str(e): logger.error(f"Err editing main menu: {e}")
    else:
        msg = await update.message.reply_text("Главное меню:", reply_markup=reply_markup)
        context.user_data['last_menu_message_id'] = msg.message_id
    return MAIN_MENU


async def main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    query = update.callback_query;
    await query.answer();
    user_id = query.from_user.id;
    choice = query.data
    if query.message: context.user_data['last_menu_message_id'] = query.message.message_id
    if choice == "settings_menu_nav":
        return await display_settings_menu(update, context, query)
    elif choice == "info_bot_nav":
        info_text = ("Я бот для скачивания музыки с SoundCloud...\n"
                     "Версия: 0.3.6")
        keyboard = [[InlineKeyboardButton("🔙 Назад в меню", callback_data="back_to_main_menu_nav")]]
        await query.edit_message_text(info_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return INFO_MENU
    elif choice == "close_menu_nav":
        await query.edit_message_text("Меню закрыто.");
        context.user_data.pop('last_menu_message_id', None)
        return ConversationHandler.END
    return MAIN_MENU


async def info_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    query = update.callback_query;
    await query.answer()
    if query.data == "back_to_main_menu_nav": return await menu_command(update, context)
    return INFO_MENU


async def display_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                query: Optional[CallbackQuery] = None) -> str:
    effective_user_id = update.effective_user.id if update.effective_user else (query.from_user.id if query else None)
    if not effective_user_id: return ConversationHandler.END
    settings = db.get_user_settings(effective_user_id)
    if not settings:
        db.update_user_settings(effective_user_id, is_new_user_setup=True);
        settings = db.get_user_settings(effective_user_id)
        if not settings:
            error_message_text = "Ошибка получения настроек. Попробуйте /menu позже."
            target_msg_id = context.user_data.get('last_menu_message_id')
            if query and query.message:
                await query.edit_message_text(error_message_text)
            elif update.message and target_msg_id:
                await context.bot.edit_message_text(error_message_text, chat_id=update.effective_chat.id,
                                                    message_id=target_msg_id)
            elif update.message:
                await update.message.reply_text(error_message_text)
            return ConversationHandler.END
    sc_username_display = settings.get('soundcloud_username') or "Не указан"
    sync_status_text = "Включена ✅" if settings.get('sync_enabled') else "Выключена ❌"
    sync_period_display = settings.get('sync_period_hours', 24)
    keyboard = [[InlineKeyboardButton(f"Синхронизация: {sync_status_text}", callback_data="toggle_sync_action")],
                [InlineKeyboardButton(f"👤 Username: {sc_username_display}", callback_data="set_sc_username_action")],
                [InlineKeyboardButton(f"⏱️ Период: {sync_period_display} ч.", callback_data="set_sync_period_action")],
                [InlineKeyboardButton("🔙 Назад в главное меню", callback_data="back_to_main_menu_nav")]]
    reply_markup = InlineKeyboardMarkup(keyboard);
    message_text = "⚙️ Настройки синхронизации лайков:"
    target_message_id = context.user_data.get('last_menu_message_id')
    try:
        if query and query.message:
            await query.edit_message_text(message_text, reply_markup=reply_markup)
        elif update.message and target_message_id:
            await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=target_message_id,
                                                text=message_text, reply_markup=reply_markup)
        elif update.message:
            msg = await update.message.reply_text(message_text, reply_markup=reply_markup); context.user_data[
                'last_menu_message_id'] = msg.message_id
    except telegram.error.BadRequest as e:
        if "Message is not modified" not in str(
                e) and update.effective_message:  # Отправляем новое, если редактирование не удалось по другой причине
            msg = await update.effective_message.reply_text(message_text, reply_markup=reply_markup)
            context.user_data['last_menu_message_id'] = msg.message_id
    return SETTINGS_MENU


async def settings_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    query = update.callback_query;
    await query.answer();
    user_id = query.from_user.id;
    choice = query.data
    if query.message: context.user_data['last_menu_message_id'] = query.message.message_id
    if choice == "toggle_sync_action":
        settings = db.get_user_settings(user_id)
        if not settings: db.update_user_settings(user_id, is_new_user_setup=True); settings = db.get_user_settings(
            user_id)
        if not settings or not settings.get('soundcloud_username'):
            await query.answer("Сначала укажите SoundCloud Username!", show_alert=True);
            return SETTINGS_MENU
        new_status = not settings.get('sync_enabled', False);
        db.update_user_settings(user_id, sync_enabled=new_status)
        return await display_settings_menu(update, context, query)
    elif choice == "set_sc_username_action":
        kb = [[InlineKeyboardButton("🔙 Назад в настройки", callback_data="back_to_settings_from_input")]]
        await query.edit_message_text("Отправьте ваш SoundCloud Username (только имя, без ссылки).",
                                      reply_markup=InlineKeyboardMarkup(kb))
        return AWAIT_SC_USERNAME
    elif choice == "set_sync_period_action":
        kb = [[InlineKeyboardButton("6 ч", callback_data="period_6h"),
               InlineKeyboardButton("12 ч", callback_data="period_12h")],
              [InlineKeyboardButton("24 ч", callback_data="period_24h"),
               InlineKeyboardButton("48 ч", callback_data="period_48h")],
              [InlineKeyboardButton("📝 Другой", callback_data="period_custom_input")],
              [InlineKeyboardButton("🔙 Назад", callback_data="back_to_settings_nav")]]
        await query.edit_message_text("Выберите период синхронизации:", reply_markup=InlineKeyboardMarkup(kb))
        return SETTINGS_MENU
    elif choice.startswith("period_") and choice.endswith("h"):
        try:
            period = int(choice.replace("period_", "").replace("h", "")); db.update_user_settings(user_id,
                                                                                                  sync_period_hours=period)
        except ValueError:
            logger.warning(f"Invalid period cb: {choice}")
        return await display_settings_menu(update, context, query)
    elif choice == "period_custom_input":
        kb = [[InlineKeyboardButton("🔙 Назад в настройки", callback_data="back_to_settings_from_input")]]
        await query.edit_message_text("Введите период в часах (1-720).", reply_markup=InlineKeyboardMarkup(kb))
        return AWAIT_SYNC_PERIOD
    elif choice == "back_to_main_menu_nav":
        context.user_data.pop('last_menu_message_id', None); return await menu_command(update, context)
    elif choice == "back_to_settings_nav":
        return await display_settings_menu(update, context, query)
    return SETTINGS_MENU


async def received_sc_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    user_id = update.effective_user.id;
    sc_username = update.message.text.strip()
    target_msg_id = context.user_data.get('last_menu_message_id')
    if update.message: await update.message.delete()
    kb_err = [[InlineKeyboardButton("🔙 Назад в настройки", callback_data="back_to_settings_from_input")]];
    rm_err = InlineKeyboardMarkup(kb_err)
    if not sc_username:
        if target_msg_id: await context.bot.edit_message_text("Имя пользователя пустое. Попробуйте еще.",
                                                              chat_id=user_id, message_id=target_msg_id,
                                                              reply_markup=rm_err)
        return AWAIT_SC_USERNAME
    if not re.match(r"^[a-zA-Z0-9\-_]+$", sc_username):
        if target_msg_id: await context.bot.edit_message_text("Недопустимые символы в имени. Введите только имя.",
                                                              chat_id=user_id, message_id=target_msg_id,
                                                              reply_markup=rm_err, parse_mode="Markdown")
        return AWAIT_SC_USERNAME
    db.update_user_settings(user_id, soundcloud_username=sc_username)
    return await display_settings_menu(update, context)  # query будет None, display_settings_menu обработает


async def received_sync_period(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    user_id = update.effective_user.id;
    period_input = update.message.text.strip()
    target_msg_id = context.user_data.get('last_menu_message_id')
    if update.message: await update.message.delete()
    kb_err = [[InlineKeyboardButton("🔙 Назад в настройки", callback_data="back_to_settings_from_input")]];
    rm_err = InlineKeyboardMarkup(kb_err)
    try:
        period = int(period_input)
        if not (1 <= period <= 720):
            if target_msg_id: await context.bot.edit_message_text("Период от 1 до 720 ч.", chat_id=user_id,
                                                                  message_id=target_msg_id, reply_markup=rm_err)
            return AWAIT_SYNC_PERIOD
        db.update_user_settings(user_id, sync_period_hours=period)
    except ValueError:
        if target_msg_id: await context.bot.edit_message_text("Неверный формат периода. Введите число.",
                                                              chat_id=user_id, message_id=target_msg_id,
                                                              reply_markup=rm_err)
        return AWAIT_SYNC_PERIOD
    return await display_settings_menu(update, context)  # query будет None


async def back_to_settings_from_input_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    query = update.callback_query;
    await query.answer()
    if query.message: context.user_data['last_menu_message_id'] = query.message.message_id
    return await display_settings_menu(update, context, query)


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        logger.critical("TELEGRAM_BOT_TOKEN не найден в config.py!")
        return
    db.initialize_db()

    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .connect_timeout(10.0)
        .read_timeout(60.0)
        .write_timeout(60.0)
        .media_write_timeout(300.0)  # 5 минут для отправки медиа
        .build()
    )

    application.add_handler(CommandHandler("start", start_command))
    menu_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("menu", menu_command)],
        states={
            MAIN_MENU: [CallbackQueryHandler(main_menu_callback,
                                             pattern="^(settings_menu_nav|info_bot_nav|close_menu_nav|back_to_main_menu_nav)$")],
            INFO_MENU: [CallbackQueryHandler(info_menu_callback, pattern="^back_to_main_menu_nav$")],
            SETTINGS_MENU: [CallbackQueryHandler(settings_menu_callback)],  # Ловит все из меню настроек
            AWAIT_SC_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_sc_username),
                                CallbackQueryHandler(back_to_settings_from_input_callback,
                                                     pattern="^back_to_settings_from_input$")],
            AWAIT_SYNC_PERIOD: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_sync_period),
                                CallbackQueryHandler(back_to_settings_from_input_callback,
                                                     pattern="^back_to_settings_from_input$")],
        }, fallbacks=[CommandHandler("menu", menu_command)], name="user_menu_conversation", )
    application.add_handler(menu_conv_handler)

    soundcloud_link_filter = filters.Regex(r'soundcloud\.com/[^\s]+')
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & soundcloud_link_filter, handle_soundcloud_link))
    application.add_handler(CommandHandler("synclikesnow", sync_user_likes_command))

    logger.info("Бот запускается...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()