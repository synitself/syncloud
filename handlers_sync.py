import logging
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional, cast

from telegram import Update, Message, Bot
from telegram.ext import ContextTypes
import telegram.error
from telegram.constants import ParseMode

import db
import ui_texts
from utils import create_progress_bar, escape_markdown_v2
from handlers_direct_download import modified_handle_soundcloud_link
from handlers_menu import update_or_create_status_message, \
    update_user_status_message

logger = logging.getLogger(__name__)


async def sync_user_likes_command(
        update: Optional[Update],
        context: ContextTypes.DEFAULT_TYPE,
        direct_user_id: Optional[int] = None,
        direct_chat_id: Optional[int] = None
) -> None:
    user_id: int
    chat_id: int
    effective_message: Optional[Message] = None
    source_of_call = "unknown"

    if update:
        if update.message:  # Command /synclikesnow
            user_id = update.message.from_user.id
            chat_id = update.message.chat_id
            effective_message = update.message
            source_of_call = "command"
            try:  # Delete command message
                await update.message.delete()
                logger.debug(
                    f"Command message {update.message.message_id} for synclikesnow deleted for user {user_id}.")
            except telegram.error.TelegramError as e_del_cmd:
                logger.warning(f"Could not delete synclikesnow command message for user {user_id}: {e_del_cmd}")
        elif update.callback_query and update.callback_query.message:  # Button press
            user_id = update.callback_query.from_user.id
            chat_id = update.callback_query.message.chat_id
            effective_message = update.callback_query.message
            source_of_call = f"callback_query ({update.callback_query.data})"
            try:
                await update.callback_query.answer()  # Answer callback immediately
            except telegram.error.TelegramError:
                pass
        else:
            logger.warning("sync_user_likes_command вызван с неизвестным типом Update.");
            return
    elif direct_user_id and direct_chat_id:  # Scheduler
        user_id = direct_user_id
        chat_id = direct_chat_id
        source_of_call = "scheduler"
    else:
        logger.error("sync_user_likes_command вызван без Update и без direct_user_id/chat_id.");
        return

    logger.info(f"sync_user_likes_command вызвана для user_id: {user_id} из: {source_of_call}")

    user_sync_locks = context.bot_data.setdefault("user_sync_locks", {})
    if user_id not in user_sync_locks: user_sync_locks[user_id] = asyncio.Lock()
    sync_lock = user_sync_locks[user_id]

    if sync_lock.locked():
        logger.info(
            f"Синхронизация для пользователя {user_id} уже запущена (лок захвачен). Новый вызов ({source_of_call}) проигнорирован.")
        if source_of_call != "scheduler" and source_of_call != "callback_query (sync_now_nav)":  # Don't send if from button or scheduler
            await update_or_create_status_message(user_id, chat_id, context.bot_data, context.bot,
                                                  custom_text=ui_texts.SYNC_ALREADY_RUNNING, parse_mode=None)
        return

    if not await sync_lock.acquire():  # Should not happen if not locked, but as a safeguard
        logger.warning(
            f"Не удалось захватить лок для пользователя {user_id} (source: {source_of_call}), хотя он не был заблокирован. Пропускаем.");
        return
    logger.debug(f"Лок для user_id: {user_id} захвачен (source: {source_of_call}).")

    current_status_message_text_for_finally: Optional[str] = None
    track_urls_from_likes = []

    try:
        settings = db.get_user_settings(user_id)
        if not settings or not settings.get('sync_enabled') or not str(settings.get('soundcloud_username', '')).strip():
            logger.info(
                f"Синхронизация для user_id {user_id} не будет запущена (проверка после захвата лока): sync_enabled={settings.get('sync_enabled') if settings else 'N/A'}, sc_username='{settings.get('soundcloud_username') if settings else 'N/A'}'")
            if source_of_call != "scheduler":  # Only notify user if it's a direct command/action
                await update_or_create_status_message(user_id, chat_id, context.bot_data, context.bot,
                                                      custom_text=ui_texts.SYNC_SETTINGS_NOT_CONFIGURED,
                                                      parse_mode=ParseMode.MARKDOWN_V2)
            else:  # For scheduler, just ensure status is up-to-date if settings changed
                await update_user_status_message(user_id, chat_id, context.bot_data, context.bot)
            return  # Exits the try block, finally will release lock

        sc_username_raw = cast(str, settings['soundcloud_username'])
        sc_username_escaped = escape_markdown_v2(sc_username_raw)

        initial_sync_status_text = ui_texts.SYNC_GETTING_LIKES_FOR_FORMAT.format(sc_username=sc_username_escaped)
        await update_or_create_status_message(user_id, chat_id, context.bot_data, context.bot,
                                              custom_text=initial_sync_status_text, parse_mode=ParseMode.MARKDOWN_V2)

        # Re-fetch settings in case status_message_id was created/updated
        settings_after_initial_update = db.get_user_settings(user_id)
        status_message_id_for_sync_progress = settings_after_initial_update.get(
            'status_message_id') if settings_after_initial_update else None

        sync_order = settings.get('sync_order', 'old_first')
        logger.info(
            f"Начало реальной логики синхронизации лайков для user {user_id} (SC: {sc_username_raw}, Order: {sync_order})")

        soundcloud_likes_url = f"https://soundcloud.com/{sc_username_raw}/likes"
        ytdlp_cmd = ["yt-dlp", "--flat-playlist", "--print", "%(url)s", "--no-warnings", "-q", soundcloud_likes_url]

        try:
            logger.debug(f"Запуск yt-dlp для {sc_username_raw}: {' '.join(ytdlp_cmd)}")
            process_ytdlp = await asyncio.create_subprocess_exec(*ytdlp_cmd, stdout=asyncio.subprocess.PIPE,
                                                                 stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await asyncio.wait_for(process_ytdlp.communicate(),
                                                    timeout=300)  # 5 min timeout for yt-dlp
            output_str = stdout.decode(errors='ignore').strip()
            stderr_str = stderr.decode(errors='ignore').strip()

            if process_ytdlp.returncode == 0 and output_str:
                track_urls_from_likes = [url.strip() for url in output_str.splitlines() if
                                         url.strip().startswith("https://soundcloud.com/")]
                if stderr_str: logger.info(f"yt-dlp stderr (успех) для {sc_username_raw}: {stderr_str[:200]}")
            elif process_ytdlp.returncode != 0:
                err_yt = stderr_str[:200] if stderr_str else output_str[:200]  # Prioritize stderr
                logger.error(f"yt-dlp failed for {sc_username_raw}. RC: {process_ytdlp.returncode}. Error: {err_yt}")
                db.log_user_error(user_id, f"Ошибка yt-dlp при получении лайков: {err_yt}",
                                  context_info=soundcloud_likes_url)
                current_status_message_text_for_finally = ui_texts.SYNC_ERROR_GETTING_LIKES_FORMAT.format(
                    sc_username=sc_username_escaped)
                return  # Exits try, goes to finally
            elif not output_str:  # Successful return code, but empty output
                logger.info(
                    f"yt-dlp не вернул URL для {sc_username_raw} (возможно, нет лайков или приватный профиль). stderr: {stderr_str[:200]}")
                # track_urls_from_likes will remain empty
        except (asyncio.TimeoutError, RuntimeError) as e_ytdlp:
            logger.error(f"Ошибка или таймаут yt-dlp для {sc_username_raw}: {e_ytdlp}")
            db.log_user_error(user_id, f"Ошибка yt-dlp (таймаут/runtime): {str(e_ytdlp)[:150]}",
                              context_info=soundcloud_likes_url)
            current_status_message_text_for_finally = ui_texts.SYNC_ERROR_GETTING_LIKES_TIMEOUT_FORMAT.format(
                sc_username=sc_username_escaped, error_details=escape_markdown_v2(str(e_ytdlp)[:100]))
            return  # Exits try, goes to finally

        def get_next_sync_time_display_text(current_user_id: int) -> str:
            _settings = db.get_user_settings(current_user_id)
            if not _settings or not _settings.get('sync_enabled'): return escape_markdown_v2(
                "автосинхронизация выключена")
            last_sync_dt = _settings.get('last_sync_timestamp')  # This will be fresh after db.update_user_settings
            period_hours = _settings.get('sync_period_hours', 24)
            if last_sync_dt and isinstance(last_sync_dt, datetime):
                if not last_sync_dt.tzinfo:
                    last_sync_dt = last_sync_dt.replace(tzinfo=timezone.utc)
                else:
                    last_sync_dt = last_sync_dt.astimezone(timezone.utc)
                next_sync_utc = last_sync_dt + timedelta(hours=period_hours)
                msk_tz = timezone(timedelta(hours=3))
                next_sync_msk = next_sync_utc.astimezone(msk_tz)
                return escape_markdown_v2(f"{next_sync_msk.strftime('%H:%M %d.%m.%Y')} (МСК)")
            return escape_markdown_v2(
                "после текущего цикла")  # Should ideally not happen if last_sync_timestamp updated

        if not track_urls_from_likes:
            db.update_user_settings(user_id, last_sync_timestamp=datetime.now(timezone.utc))
            next_sync_time_str = get_next_sync_time_display_text(user_id)
            current_status_message_text_for_finally = ui_texts.SYNC_NO_LIKES_FOUND_FORMAT.format(
                sc_username=sc_username_escaped, next_sync_time=next_sync_time_str)
        else:
            if sync_order == 'old_first': track_urls_from_likes.reverse()
            total_liked_tracks_count = len(track_urls_from_likes)
            new_urls_to_process = [url for url in track_urls_from_likes if
                                   not db.is_track_downloaded(user_id, url) and not db.is_track_failed(user_id, url)]
            logger.debug(
                f"Для user {user_id} найдено {total_liked_tracks_count} лайков, из них {len(new_urls_to_process)} новых для обработки.")

            if not new_urls_to_process:
                db.update_user_settings(user_id, last_sync_timestamp=datetime.now(timezone.utc))
                next_sync_time_str = get_next_sync_time_display_text(user_id)
                current_status_message_text_for_finally = ui_texts.SYNC_ALL_TRACKS_SYNCED_OR_SKIPPED_FORMAT.format(
                    total_tracks=total_liked_tracks_count, sc_username=sc_username_escaped,
                    next_sync_time=next_sync_time_str)
            else:
                total_new_to_process_count = len(new_urls_to_process)
                sent_successfully_count = 0
                errors_during_sync_count = 0
                delay_between_sends = 1.2  # Increased slightly

                for i, track_url_to_process in enumerate(new_urls_to_process):
                    track_short_name = track_url_to_process.split('/')[-1][:25]
                    processed_count_so_far = sent_successfully_count + errors_during_sync_count
                    overall_status_prefix_for_track = ui_texts.SYNC_PROGRESS_OVERALL_STATUS_PREFIX_FORMAT.format(
                        sc_username=sc_username_escaped,
                        processed_count=processed_count_so_far,
                        total_new_count=total_new_to_process_count,
                        current_track_num=i + 1,
                        track_short_name=escape_markdown_v2(track_short_name)
                    )

                    # Re-fetch status_message_id inside loop in case it gets recreated by user interaction / error
                    current_settings_iter = db.get_user_settings(user_id)
                    status_msg_id_for_track_dl = current_settings_iter.get(
                        'status_message_id') if current_settings_iter else None
                    if not status_msg_id_for_track_dl and status_message_id_for_sync_progress:  # Fallback to earlier ID
                        status_msg_id_for_track_dl = status_message_id_for_sync_progress
                    if not status_msg_id_for_track_dl:
                        logger.error(
                            f"Критично: status_message_id не найден для user {user_id} во время обработки трека. Прогресс не будет показан.")

                    success, sent_msg_id = await modified_handle_soundcloud_link(
                        url=track_url_to_process, user_id=user_id, chat_id=chat_id, context=context,
                        status_message_id_to_edit=status_msg_id_for_track_dl,
                        text_prefix_for_status=overall_status_prefix_for_track,
                    )
                    if success and sent_msg_id:
                        db.add_downloaded_track(user_id, track_url_to_process, sent_msg_id);
                        sent_successfully_count += 1
                    elif not success:
                        errors_during_sync_count += 1
                    if i < total_new_to_process_count - 1: await asyncio.sleep(delay_between_sends)

                db.update_user_settings(user_id, last_sync_timestamp=datetime.now(timezone.utc))
                next_sync_time_str = get_next_sync_time_display_text(user_id)
                current_status_message_text_for_finally = ui_texts.SYNC_SUMMARY_FINAL_FORMAT.format(
                    sc_username=sc_username_escaped,
                    total_liked_tracks=total_liked_tracks_count,
                    total_new_to_process=total_new_to_process_count,
                    sent_successfully=sent_successfully_count,
                    errors_count=errors_during_sync_count,
                    next_sync_time=next_sync_time_str
                )
                logger.info(
                    f"Синхронизация лайков завершена для user {user_id}. Загружено: {sent_successfully_count}, ошибок: {errors_during_sync_count}.")
    finally:
        if current_status_message_text_for_finally:
            await update_or_create_status_message(user_id, chat_id, context.bot_data, context.bot,
                                                  custom_text=current_status_message_text_for_finally,
                                                  parse_mode=ParseMode.MARKDOWN_V2)
        else:  # If no specific final message was set (e.g., early exit before message generation), update to standard status
            await update_user_status_message(user_id, chat_id, context.bot_data, context.bot)

        if sync_lock.locked(): sync_lock.release()
        logger.debug(f"Лок для user_id: {user_id} освобожден (source: {source_of_call}).")


async def scheduled_sync_task(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Планировщик: Запуск периодической проверки синхронизации...")
    users_needing_sync = db.get_users_for_scheduled_sync()

    if not users_needing_sync:
        logger.info("Планировщик: Нет пользователей для синхронизации в данный момент.");
        return

    logger.info(f"Планировщик: Найдено {len(users_needing_sync)} пользователей для синхронизации.")
    for user_data in users_needing_sync:
        user_id = user_data['user_id']
        chat_id = user_id
        sc_username = user_data['soundcloud_username']
        logger.info(f"Планировщик: Запуск синхронизации для user_id {user_id} (SC: {sc_username}).")
        try:
            await sync_user_likes_command(None, context, direct_user_id=user_id, direct_chat_id=chat_id)
            await asyncio.sleep(20)  # Increased delay between users in scheduler
        except telegram.error.Forbidden as e_forbidden:
            logger.warning(
                f"Планировщик: Бот заблокирован пользователем {user_id} или чат не найден. Ошибка: {e_forbidden}")
            db.update_user_settings(user_id, sync_enabled=False)
            settings = db.get_user_settings(user_id)  # Get current settings to find status_message_id
            if settings and settings.get('status_message_id'):
                try:
                    await context.bot.delete_message(chat_id=chat_id, message_id=settings['status_message_id'])
                except telegram.error.TelegramError:
                    pass
                db.update_user_settings(user_id, status_message_id=None, set_status_msg_id_to_null=True)
        except Exception as e_sched_sync:
            logger.error(
                f"Планировщик: Ошибка при синхронизации для user {user_id} (SC: {sc_username}): {e_sched_sync}",
                exc_info=True)
            db.log_user_error(user_id, f"Ошибка при плановой синхронизации: {str(e_sched_sync)[:200]}",
                              context_info="Планировщик")
            try:
                await update_user_status_message(user_id, chat_id, context.bot_data,
                                                 context.bot)  # Try to update status to normal
            except Exception as e_status_update:
                logger.error(
                    f"Планировщик: Не удалось обновить статусное сообщение для user {user_id} после ошибки: {e_status_update}")
    logger.info("Планировщик: Все запланированные синхронизации завершены.")