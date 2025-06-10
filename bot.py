import logging
from pathlib import Path
from typing import Optional, cast
import asyncio

from telegram import Update, Message
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    CallbackQueryHandler, ContextTypes, ConversationHandler, Defaults
)
import telegram.error

try:
    from config import TELEGRAM_BOT_TOKEN, DOWNLOAD_FOLDER, BOT_VERSION
except ImportError:
    print("Критическая ошибка: Файл config.py не найден или отсутствует BOT_VERSION...")
    TELEGRAM_BOT_TOKEN = None
    DOWNLOAD_FOLDER = "downloads"
    BOT_VERSION = "N/A"
    if not TELEGRAM_BOT_TOKEN: exit(1)

import db
import ui_texts
from handlers_direct_download import handle_soundcloud_link
from handlers_menu import (
    MAIN_MENU, SETTINGS_MENU, AWAIT_SC_USERNAME, AWAIT_SYNC_PERIOD, INFO_MENU, ERROR_LOG_MENU,
    AWAITING_TEXT_INPUT_KEY,
    menu_command, main_menu_callback, info_menu_callback,
    display_settings_menu, settings_menu_callback,
    received_sc_username, received_sync_period,
    back_to_settings_from_input_callback,
    display_error_log_menu, error_log_menu_callback,
    update_user_status_message
)
from handlers_sync import sync_user_likes_command, scheduled_sync_task

log_formatter = logging.Formatter("%(asctime)s - %(name)s [%(levelname)s] - %(message)s (%(filename)s:%(lineno)d)")
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
for handler in root_logger.handlers[:]: root_logger.removeHandler(handler)
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
root_logger.addHandler(console_handler)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.INFO)
logger = logging.getLogger(__name__)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Исключение при обработке обновления:", exc_info=context.error)
    if isinstance(update, Update) and update.effective_chat and update.effective_user:
        user_id_for_db = update.effective_user.id
        if isinstance(context.error, telegram.error.Forbidden):
            error_message_lower = str(context.error).lower()
            if "bot was blocked by the user" in error_message_lower or \
                    "user is deactivated" in error_message_lower or \
                    "chat not found" in error_message_lower or \
                    "forbidden: bot can't initiate conversation with a user" in error_message_lower or \
                    "group chat was deactivated" in error_message_lower:
                if user_id_for_db:
                    logger.info(
                        f"Bot blocked, user/chat deactivated or not found for user {user_id_for_db}. Disabling sync and cleaning up status message.")
                    db.update_user_settings(user_id_for_db, sync_enabled=False)
                    settings = db.get_user_settings(user_id_for_db)
                    if settings and settings.get('status_message_id'):
                        try:
                            await context.bot.delete_message(chat_id=user_id_for_db,
                                                             message_id=settings['status_message_id'])
                            logger.info(
                                f"Status message {settings['status_message_id']} deleted for user {user_id_for_db} due to blocking/deactivation.")
                        except telegram.error.TelegramError as e_del_status:
                            logger.warning(
                                f"Could not delete status message for user {user_id_for_db} (likely chat inaccessible): {e_del_status}")
                        db.update_user_settings(user_id_for_db, status_message_id=None, set_status_msg_id_to_null=True)
                return
    elif isinstance(context.error, telegram.error.TimedOut):
        logger.warning(f"Общий таймаут запроса (не связан с конкретным чатом): {context.error}")
    elif isinstance(context.error, telegram.error.NetworkError):
        logger.warning(f"Сетевая ошибка при обработке обновления: {context.error}")


async def post_init(application: Application) -> None:
    application.bot_data["BOT_VERSION"] = BOT_VERSION
    logger.info(f"Bot post_init: Установлена версия бота: {BOT_VERSION}")
    logger.info("Bot post_init: Обновление статусных сообщений для активных пользователей...")
    all_users_with_status_msg = db.get_all_users_with_status_message()

    USER_PROCESSING_DELAY = 0.8
    RETRY_AFTER_BUFFER = 1.0

    for i, user_data in enumerate(all_users_with_status_msg):
        user_id = user_data['user_id']
        try:
            await update_user_status_message(user_id, user_id, application.bot_data, application.bot)
            await asyncio.sleep(USER_PROCESSING_DELAY)
        except telegram.error.RetryAfter as e_retry:
            wait_time = e_retry.retry_after + RETRY_AFTER_BUFFER
            logger.warning(
                f"post_init: Flood control для user {user_id}. Ожидание {wait_time:.2f}с. ({i + 1}/{len(all_users_with_status_msg)})"
            )
            if wait_time > 60:
                logger.warning(
                    f"post_init: Telegram запросил долгую задержку ({e_retry.retry_after}s) для user {user_id}. "
                    f"Пропускаем этого пользователя в post_init, чтобы не блокировать запуск."
                )
                continue
            await asyncio.sleep(wait_time)
        except telegram.error.Forbidden as e_forbidden:
            logger.warning(
                f"post_init: Бот заблокирован пользователем {user_id} или чат не найден. Ошибка: {e_forbidden}")
            db.update_user_settings(user_id, sync_enabled=False)
            if user_data.get('status_message_id'):
                db.update_user_settings(user_id, status_message_id=None, set_status_msg_id_to_null=True)
        except Exception as e:
            logger.error(f"Ошибка в post_init при обновлении статуса для user {user_id}: {e}")
            await asyncio.sleep(USER_PROCESSING_DELAY * 2.5)  # Increased delay for general errors

    logger.info(
        f"Bot post_init: Обновление статусных сообщений ({len(all_users_with_status_msg)} пользователей) завершено.")


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        logger.critical("TELEGRAM_BOT_TOKEN не найден в config.py!")
        return

    db.initialize_db()

    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .connect_timeout(30.0)
        .read_timeout(130.0)
        .write_timeout(130.0)
        .media_write_timeout(360.0)
        .post_init(post_init)
        .build()
    )

    application.bot_data["user_sync_locks"] = {}
    application.add_error_handler(error_handler)

    menu_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", menu_command), CommandHandler("menu", menu_command)],
        states={
            MAIN_MENU: [CallbackQueryHandler(main_menu_callback,
                                             pattern="^(settings_menu_nav|info_bot_nav|sync_now_nav|error_log_nav|close_menu_nav|back_to_main_menu_nav)$")],
            INFO_MENU: [CallbackQueryHandler(info_menu_callback, pattern="^back_to_main_menu_nav$")],
            SETTINGS_MENU: [CallbackQueryHandler(settings_menu_callback)],
            AWAIT_SC_USERNAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, received_sc_username),
                CallbackQueryHandler(back_to_settings_from_input_callback, pattern="^back_to_settings_from_input$")
            ],
            AWAIT_SYNC_PERIOD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, received_sync_period),
                CallbackQueryHandler(back_to_settings_from_input_callback, pattern="^back_to_settings_from_input$")
            ],
            ERROR_LOG_MENU: [CallbackQueryHandler(error_log_menu_callback,
                                                  pattern="^(clear_error_log|back_to_main_from_log|err_log_prev_page|err_log_next_page)$")]
        },
        fallbacks=[CommandHandler("start", menu_command), CommandHandler("menu", menu_command)],
        name="user_menu_conversation",
        persistent=False
    )
    application.add_handler(menu_conv_handler)

    soundcloud_link_filter = filters.Regex(r'soundcloud\.com/[^\s]+')
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & soundcloud_link_filter, handle_soundcloud_link), group=1)

    application.add_handler(CommandHandler("synclikesnow", sync_user_likes_command))

    job_queue = application.job_queue
    num_users_for_post_init_estimate = len(db.get_all_users_with_status_message())
    post_init_base_time = 15
    post_init_per_user_delay = 1.5
    post_init_estimated_duration = post_init_base_time + (num_users_for_post_init_estimate * post_init_per_user_delay)
    job_queue_first_run_delay = post_init_estimated_duration + 30

    job_queue.run_repeating(scheduled_sync_task, interval=600   , first=job_queue_first_run_delay)
    logger.info(
        f"Планировщик задач запущен (проверка каждый час, первая через ~{job_queue_first_run_delay:.0f} сек, "
        f"исходя из {num_users_for_post_init_estimate} пользователей в post_init).")

    logger.info("Бот запускается...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()