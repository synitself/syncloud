# handlers_sync.py
import logging
import asyncio
import re
from pathlib import Path
from datetime import datetime
from typing import Optional, cast

from telegram import Update, Message
from telegram.ext import ContextTypes
import telegram.error

import db
from config import DOWNLOAD_FOLDER
from utils import create_progress_bar  # Только create_progress_bar

logger = logging.getLogger(__name__)


async def sync_user_likes_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from handlers_direct_download import modified_handle_soundcloud_link

    effective_message: Optional[Message] = update.message
    user_id: int;
    chat_id: int
    if update.message:
        user_id = update.message.from_user.id; chat_id = update.message.chat_id
    elif update.callback_query and update.callback_query.message:
        user_id = update.callback_query.from_user.id;
        chat_id = update.callback_query.message.chat_id
        effective_message = update.callback_query.message;
        await update.callback_query.answer("𝙎𝙔𝙉𝘾 𝙎𝙏𝘼𝙍𝙏𝙎...")
    else:
        logger.warning("sync_user_likes_command triggered without user/chat context."); return

    settings = db.get_user_settings(user_id)
    if not settings or not settings.get('sync_enabled') or not settings.get('soundcloud_username'):
        reply_text = "Синхронизация не настроена/выключена. /menu"
        if effective_message:
            await effective_message.reply_text(reply_text)
        else:
            await context.bot.send_message(chat_id, reply_text); return

    sc_username = cast(str, settings['soundcloud_username'])
    sync_order = settings.get('sync_order', 'old_first')
    logger.info(f"Starting likes sync for {user_id} (SC: {sc_username}, Order: {sync_order})")

    # --- ИЗМЕНЕНИЕ: Используем это сообщение для общего прогресса синхронизации ---
    sync_overall_progress_message: Optional[Message] = None
    initial_sync_text = f"⏳ 𝙎𝙔𝙉𝘾𝙄𝙉𝙂 𝙁𝙊𝙍 '{sc_username}'..."
    try:
        if update.callback_query and effective_message and effective_message.text and \
                ("меню" in effective_message.text.lower() or "настройки" in effective_message.text.lower()):
            sync_overall_progress_message = await effective_message.edit_text(initial_sync_text)
        elif effective_message:
            sync_overall_progress_message = await effective_message.reply_text(initial_sync_text)
        else:
            sync_overall_progress_message = await context.bot.send_message(chat_id, initial_sync_text)
    except Exception as e_msg_init:
        logger.error(f"Could not send/edit initial sync message: {e_msg_init}")
        sync_overall_progress_message = await context.bot.send_message(chat_id, initial_sync_text)

    soundcloud_likes_url = f"https://soundcloud.com/{sc_username}/likes"
    ytdlp_cmd = ["yt-dlp", "--flat-playlist", "--print", "%(url)s", "--no-warnings", "-q", soundcloud_likes_url]
    track_urls_from_likes = []
    try:  # ... (yt-dlp как раньше)
        process_ytdlp = await asyncio.create_subprocess_exec(*ytdlp_cmd, stdout=asyncio.subprocess.PIPE,
                                                             stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(process_ytdlp.communicate(), timeout=300)
        output_str = stdout.decode(errors='ignore').strip();
        stderr_str = stderr.decode(errors='ignore').strip()
        if process_ytdlp.returncode == 0 and output_str:
            track_urls_from_likes = [url.strip() for url in output_str.splitlines() if
                                     url.strip().startswith("https://soundcloud.com/")]
            if stderr_str: logger.info(f"yt-dlp stderr (success): {stderr_str[:500]}")
        elif process_ytdlp.returncode != 0:
            raise RuntimeError(f"yt-dlp failed: {stderr_str[:200] if stderr_str else output_str[:200]}")
        elif not output_str:
            logger.info(f"yt-dlp returned no URLs. stderr: {stderr_str[:500]}")
    except (asyncio.TimeoutError, RuntimeError) as e_ytdlp:
        if sync_overall_progress_message: await sync_overall_progress_message.edit_text(
            f"🚫 Ошибка получения списка лайков: {str(e_ytdlp)[:150]}"); return

    if not track_urls_from_likes:  # ... (обработка пустого списка)
        if sync_overall_progress_message: await sync_overall_progress_message.edit_text(
            f"✅ 𝙏𝙃𝙀 '{sc_username}' 𝙇𝙄𝙆𝙀𝘿 𝙏𝙍𝘼𝘾𝙆𝙎 𝘼𝙍𝙀 𝙉𝙊𝙏 𝙁𝙊𝙐𝙉𝘿.")
        db.update_user_settings(user_id, last_sync_timestamp=datetime.now());
        return

    if sync_order == 'old_first': track_urls_from_likes.reverse()

    total_liked_tracks = len(track_urls_from_likes)
    new_urls = [url for url in track_urls_from_likes if not db.is_track_downloaded(user_id, url)]

    if not new_urls:  # ... (обработка, если нет новых)
        if sync_overall_progress_message: await sync_overall_progress_message.edit_text(
            f"✅ 𝘼𝙇𝙇 𝙏𝙃𝙀 {total_liked_tracks} 𝙏𝙍𝘼𝘾𝙆𝙎 𝘼𝙍𝙀 𝘼𝙇𝙍𝙀𝘼𝘿𝙔 𝙎𝙔𝙉𝘾𝙀𝘿.")
        db.update_user_settings(user_id, last_sync_timestamp=datetime.now());
        return

    total_new_to_process = len(new_urls)
    if sync_overall_progress_message: await sync_overall_progress_message.edit_text(
        f"𝙁𝙊𝙐𝙉𝘿 {total_new_to_process} 𝙉𝙀𝙒 𝙏𝙍𝘼𝘾𝙆𝙎 𝙊𝙁 {total_liked_tracks} 𝙏𝙍𝘼𝘾𝙆𝙎. 𝙎𝙔𝙉𝘾𝙄𝙉𝙂..."
    )

    sent_count = 0;
    error_count = 0;
    delay_between_sends = 3

    for i, track_url_to_process in enumerate(new_urls):
        # Обновляем общее сообщение о прогрессе СИНХРОНИЗАЦИИ (не каждого трека)
        if sync_overall_progress_message:
            track_short_name = track_url_to_process.split('/')[-1][:25]
            overall_progress_text = create_progress_bar(
                int(((i) / total_new_to_process) * 100 if total_new_to_process > 0 else 0),
                # Процент от общего числа НОВЫХ
                stage_message=f"𝙋𝙍𝙊𝙂𝙍𝙀𝙎𝙎 {i + 1}/{total_new_to_process}..."
            )
            try:
                await sync_overall_progress_message.edit_text(overall_progress_text)
            except telegram.error.BadRequest:
                pass  # Игнорируем "not modified"

        # modified_handle_soundcloud_link создаст СВОЙ прогресс-бар для каждого трека
        success, sent_msg_id = await modified_handle_soundcloud_link(
            url=track_url_to_process, user_id=user_id, chat_id=chat_id, context=context,
            # progress_message_to_update=None, # Убрали, чтобы каждый трек имел свой прогресс
            reply_to_message_id_for_final_audio=None  # Лайки не отвечают на конкретное сообщение
        )

        if success and sent_msg_id:
            db.add_downloaded_track(user_id, track_url_to_process, sent_msg_id)
            sent_count += 1
        elif not success:
            error_count += 1
            # Сообщение об ошибке для трека уже отправлено из modified_handle_soundcloud_link

        if i < total_new_to_process - 1: await asyncio.sleep(delay_between_sends)

    final_summary_message = (
        f"🏁 𝙎𝙔𝙉𝘾𝙄𝙉𝙂 𝙁𝙊𝙍 '{sc_username}' 𝙄𝙎 𝘿𝙊𝙉𝙀.\n"
        f"𝙏𝙊𝙏𝘼𝙇: {total_liked_tracks} 𝙏𝙍𝘼𝘾𝙆𝙎"
        f"✅ 𝙐𝙋𝙇𝙊𝘼𝘿𝙀𝘿: {sent_count} 𝙏𝙍𝘼𝘾𝙆𝙎\n❌ 𝙀𝙍𝙍𝙊𝙍𝙎: {error_count}"
    )
    if sync_overall_progress_message:
        await sync_overall_progress_message.edit_text(final_summary_message)
    else:
        await context.bot.send_message(chat_id, final_summary_message)

    db.update_user_settings(user_id, last_sync_timestamp=datetime.now())
    logger.info(f"Likes sync finished for user {user_id}. Result: {sent_count} sent, {error_count} errors.")