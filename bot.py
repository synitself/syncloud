# bot.py
import logging
from pathlib import Path  # Не используется здесь напрямую, но может быть в других модулях
from typing import Optional, cast

from telegram import Update, Message
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    CallbackQueryHandler, ContextTypes, ConversationHandler, Defaults
)

import telegram.error # Не используется здесь напрямую

try:
    from config import TELEGRAM_BOT_TOKEN, DOWNLOAD_FOLDER
except ImportError:
    print("Критическая ошибка: Файл config.py не найден...")
    exit(1)

import db
# utils не импортируем здесь, он используется внутри других хендлеров
from handlers_direct_download import handle_soundcloud_link
# modified_handle_soundcloud_link не нужен для прямого импорта в bot.py
from handlers_menu import (
    MAIN_MENU, SETTINGS_MENU, AWAIT_SC_USERNAME, AWAIT_SYNC_PERIOD, INFO_MENU,
    AWAITING_TEXT_INPUT_KEY,  # Этот ключ используется в handle_soundcloud_link
    menu_command, main_menu_callback, info_menu_callback,
    display_settings_menu, settings_menu_callback,
    received_sc_username, received_sync_period,
    back_to_settings_from_input_callback
)
from handlers_sync import sync_user_likes_command

# --- Настройка логирования ---
log_formatter = logging.Formatter("%(asctime)s - %(name)s [%(levelname)s] - %(message)s (%(filename)s:%(lineno)d)")
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
for handler in root_logger.handlers[:]: root_logger.removeHandler(handler)  # Очищаем предыдущие хендлеры
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
root_logger.addHandler(console_handler)  # Добавляем наш
# Уровни для библиотек
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.INFO)
logger = logging.getLogger(__name__)  # Логгер для текущего файла bot.py


# --- Конец настройки логирования ---

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Логирует ошибки и информирует пользователя, если это возможно и уместно."""
    logger.error("Исключение при обработке обновления:", exc_info=context.error)

    # Пытаемся отправить сообщение пользователю, если это возможно
    if isinstance(update, Update) and update.effective_chat and update.effective_user:
        user_id_for_db = update.effective_user.id
        error_message_to_user = "𝙄𝙉𝙏𝙀𝙍𝙉𝘼𝙇 𝙀𝙍𝙍𝙊𝙍. 𝘾𝙊𝙉𝙏𝘼𝘾𝙏 𝙏𝙃𝙀 𝘼𝘿𝙈𝙄𝙉 𝙊𝙍 𝙏𝙍𝙔 𝘼𝙂𝘼𝙄𝙉 𝙇𝘼𝙏𝙀𝙍..."

        if isinstance(context.error, telegram.error.Forbidden):
            if "bot was blocked by the user" in str(context.error).lower() or \
                    "user is deactivated" in str(context.error).lower() or \
                    "chat not found" in str(context.error).lower():
                if user_id_for_db:
                    logger.info(f"Bot blocked or chat/user not found for user {user_id_for_db}. Disabling sync.")
                    db.update_user_settings(user_id_for_db, sync_enabled=False)  # Просто выключаем синхронизацию
                return  # Не отправляем сообщение пользователю, т.к. он заблокировал бота или чат не найден
        elif isinstance(context.error, telegram.error.NetworkError):
            error_message_to_user = "𝙉𝙀𝙏𝙒𝙊𝙍𝙆 𝙀𝙍𝙍𝙊𝙍. 𝙋𝙇𝙀𝘼𝙎𝙀 𝘾𝙃𝙀𝘾𝙆 𝙔𝙊𝙐𝙍 𝘾𝙊𝙉𝙉𝙀𝘾𝙏𝙄𝙊𝙉 𝙊𝙍 𝙏𝙍𝙔 𝘼𝙂𝘼𝙄𝙉 𝙇𝘼𝙏𝙀𝙍..."
        elif isinstance(context.error, telegram.error.TimedOut):
            error_message_to_user = "𝙏𝙃𝙀 𝙊𝙋𝙀𝙍𝘼𝙏𝙄𝙊𝙉 𝙏𝙊𝙊𝙆 𝙏𝙊𝙊 𝙇𝙊𝙉𝙂 𝘼𝙉𝘿 𝙒𝘼𝙎 𝙄𝙉𝙏𝙀𝙍𝙍𝙐𝙋𝙏𝙀𝘿. 𝙋𝙇𝙀𝘼𝙎𝙀 𝙏𝙍𝙔 𝘼𝙂𝘼𝙄𝙉 𝙇𝘼𝙏𝙀𝙍..."
        # Добавить другие специфичные ошибки по необходимости

        try:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=error_message_to_user)
        except Exception as e_report:
            logger.error(
                f"Не удалось отправить сообщение об ошибке пользователю {update.effective_user.id}: {e_report}")
    elif isinstance(context.error, telegram.error.TimedOut):
        logger.warning(f"Общий таймаут запроса (не связан с конкретным чатом): {context.error}")
    # Для других типов ошибок, не связанных с Update, просто логируем.


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

    # Добавляем обработчик ошибок
    application.add_error_handler(error_handler)

    menu_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", menu_command)],
        states={
            MAIN_MENU: [CallbackQueryHandler(main_menu_callback,
                                             pattern="^(settings_menu_nav|info_bot_nav|sync_now_nav|close_menu_nav|back_to_main_menu_nav)$")],
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
        },
        fallbacks=[CommandHandler("menu", menu_command)],
        name="user_menu_conversation",
    )
    application.add_handler(menu_conv_handler)  # group=0 по умолчанию

    soundcloud_link_filter = filters.Regex(r'soundcloud\.com/[^\s]+')
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & soundcloud_link_filter, handle_soundcloud_link), group=1)

    application.add_handler(CommandHandler("synclikesnow", sync_user_likes_command))

    logger.info("Бот запускается...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()