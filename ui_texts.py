MENU_TITLE = "🤖 *Меню*"
MENU_CLOSED = "Меню закрыто\\." # For Markdown V2 edit fallback
MENU_CLOSED_CONFIRMATION_SHORT = "Меню закрыто." # For query.answer simple text
MENU_CLOSE_ERROR_ALERT = "Ошибка при закрытии меню."


PROCESSING_ERROR_DIRECT_FORMAT = "🚫 Ошибка обработки \\({filename_short}\\.\\.\\.\\): {error_details}"
PROCESSING_ERROR_UNKNOWN_DIRECT_FORMAT = "🚫 Неожиданная ошибка при обработке трека \\({filename_short}\\.\\.\\.\\)\\. Подробности в журнале\\."
TELEGRAM_ERROR_DIRECT_FORMAT = "🚫 Ошибка Telegram \\({filename_short}\\.\\.\\.\\): {error_details}"
ACTION_SUCCESS_ALERT = "Успешно!"

BUTTON_SYNC_NOW = "🔄 Синхронизировать сейчас"
BUTTON_SETTINGS = "⚙️ Настройки"
BUTTON_ERROR_LOG = "📜 Журнал ошибок"
BUTTON_INFO = "ℹ️ Информация"
BUTTON_CLOSE_MENU = "❌ Закрыть меню"
BUTTON_BACK_TO_MAIN = "🔙 Назад"
BUTTON_BACK_TO_SETTINGS = "🔙 Назад"

INFO_BOT_TEXT_FORMAT = (
    "ℹ️ *Информация о боте*\n\n"
    "Этот бот поможет вам скачивать треки с SoundCloud и автоматически синхронизировать вашу медиатеку лайков.\n\n"
    "➡️ *Как пользоваться:*\n" # Оставляем стрелку, если она была в вашем оригинале
    "1. Отправьте боту ссылку на трек с `soundcloud.com`.\n"
    "2. Для настройки автоматической синхронизации лайков перейдите в `⚙️ Настройки` (`/start` или `/menu`).\n"
    "3. Проверить ошибки обработки можно в `📜 Журнал ошибок`.\n\n"
    "📞 Контакт: @synitself\n"
    "💎 Версия: {version}"
)

SETTINGS_TITLE = "⚙️ *Настройки*"
SETTINGS_SC_USERNAME_PROMPT = "Введите Ваше имя пользователя SoundCloud"
SETTINGS_SC_USERNAME_NOT_SET = "не задано"
SETTINGS_SC_USERNAME_BUTTON_TEXT_FORMAT = "👤 Имя пользователя: {}"
SETTINGS_SYNC_ENABLED_ON = "Вкл ✅"
SETTINGS_SYNC_ENABLED_OFF = "Выкл ❌"
SETTINGS_SYNC_ENABLED_LABEL_FORMAT = "🔄 Авто-синхронизация: {}"
SETTINGS_SYNC_PERIOD_LABEL_FORMAT = "⏱️ Период синхронизации: {} ч."
SETTINGS_SYNC_ORDER_OLD_FIRST = "Сначала старые 🔼"
SETTINGS_SYNC_ORDER_NEW_FIRST = "Сначала новые 🔽"
SETTINGS_SYNC_ORDER_LABEL_FORMAT = "📊 Порядок синхронизации: {}"
SETTINGS_SYNC_PERIOD_PROMPT = "Выберите период автоматической синхронизации:"
SETTINGS_SYNC_PERIOD_INPUT_PROMPT = "Введите период синхронизации в часах \\(например, `12` для 12 часов, от 1 до 720\\)\\:"
SETTINGS_USERNAME_NOT_SET_ALERT = "Сначала укажите имя пользователя SoundCloud!"
SETTINGS_USERNAME_EMPTY_ERROR = "Имя пользователя не может быть пустым\\. Попробуйте еще раз\\."
SETTINGS_USERNAME_INVALID_CHARS_ERROR = "Имя пользователя содержит недопустимые символы\\. Используйте только латинские буквы \\(a\\-z, A\\-Z\\), цифры \\(0\\-9\\), дефис \\(\\-\\) и подчеркивание \\(\\_\\)\\."
SETTINGS_USERNAME_LENGTH_ERROR = "Длина имени пользователя должна быть от 3 до 30 символов\\."
SETTINGS_PERIOD_INVALID_FORMAT_ERROR = "Неверный формат периода\\. Введите число от 1 до 720 \\(часы\\)\\."
SETTINGS_DB_ERROR = "Ошибка получения/сохранения настроек\\. Попробуйте /start позже\\."
BUTTON_PERIOD_HOURS_FORMAT = "{} ч."
BUTTON_PERIOD_CUSTOM_INPUT = "📝 Ввести вручную"

ERROR_LOG_TITLE = "*📜 Журнал ошибок*\n\n"
ERROR_LOG_EMPTY = "Журнал ошибок пуст\\."
ERROR_LOG_ENTRY_TIMESTAMP_FORMAT = "📅 `{timestamp}`\n"
ERROR_LOG_ENTRY_MESSAGE_FORMAT = "💬 _{error_message}_\n"
ERROR_LOG_ENTRY_CONTEXT_FORMAT = "🔗 `{context_info}`\n"
ERROR_LOG_SEPARATOR = "\\_ \\_ \\_ \\_ \\_ \\_ \\_ \\_ \\_ \\_ \\_ \\_ \\_ \\_ \\_ \\_ \\_ \\_ \\_ \\_ \n\n"
BUTTON_CLEAR_ERROR_LOG = "🗑️ Очистить журнал"
ERROR_LOG_CLEARED_ALERT = "Журнал ошибок очищен!"
BUTTON_ERROR_LOG_NEXT_PAGE = "➡️ След. стр." # Оставляем стрелку
BUTTON_ERROR_LOG_PREV_PAGE = "⬅️ Пред. стр." # Оставляем стрелку
ERROR_LOG_PAGE_X_OF_Y_FORMAT = "\n\n📄 Стр\\. {current_page_display} из {total_pages_display}"

STATUS_LOADING_SETTINGS_ERROR = "❌ Не удалось загрузить ваши настройки\\."
STATUS_SYNC_IN_PROGRESS_FORMAT = "⏳"
STATUS_AUTOSYNC_OFF = "❌ Синхронизация отключена\\. "
STATUS_AUTOSYNC_ON_NO_USERNAME = "❌ Синхронизация включена, но не указано имя пользователя SoundCloud\\." # ИСПРАВЛЕНО
STATUS_AUTOSYNC_ON_NEXT_SYNC_APPROX_FORMAT = "✅ Синхронизация вкл\\.\n\n🕒 {next_sync_time} МСК"
STATUS_AUTOSYNC_ON_FIRST_SYNC_FORMAT = "✅ Синхронизация вкл\\.\n\n🕒 Ожидание следующего цикла проверки\\.\\.\\."
STATUS_AUTOSYNC_ON_WAITING_NEXT_CYCLE_FORMAT = "✅ Синхронизация вкл\\.\n\n🕒 Ожидание следующего цикла проверки\\.\\.\\."

SYNC_NOW_STARTED_ALERT = "⏳"
SYNC_ALREADY_RUNNING = "✅ Синхронизация уже выполняется."
SYNC_SETTINGS_NOT_CONFIGURED = "🚫 Синхронизация не настроена или отключена \\(проверьте имя пользователя и статус авто\\-синхронизации\\)\\."
SYNC_GETTING_LIKES_FOR_FORMAT = "⏳"
SYNC_ERROR_GETTING_LIKES_FORMAT = "🚫 Ошибка получения медиатеки для {sc_username}\\. Подробности в журнале ошибок \\(/start \\- Журнал ошибок\\)\\." # ИСПРАВЛЕНО
SYNC_ERROR_GETTING_LIKES_TIMEOUT_FORMAT = "🚫 Ошибка получения медиатеки \\(таймаут\\) для '{sc_username}': {error_details}\\. Подробности в журнале\\."
SYNC_NO_LIKES_FOUND_FORMAT = (
    "❌ Треки не найдены или список пуст\\.\n\n"
    "🕒 {next_sync_time} "
)
SYNC_ALL_TRACKS_SYNCED_OR_SKIPPED_FORMAT = (
    "✅ Синхронизировано\n\n"
    "🕒 {next_sync_time} "
)
SYNC_PROGRESS_OVERALL_STATUS_PREFIX_FORMAT = (
    "✅ Завершено {processed_count}/{total_new_count}\n"
)
SYNC_PROGRESS_TRACK_PREPARING = ""
SYNC_SUMMARY_FINAL_FORMAT = (
    "✅ Синхронизировано\n\n"
    "🕒 {next_sync_time}"
)
SYNC_ERROR_SENDING_TRACK_PROGRESS_FORMAT = "🚫 Не удалось отправить сообщение о прогрессе для трека: {track_url}"
SYNC_ERROR_SENDING_FINAL_SUMMARY_FORMAT = "🚫 Не удалось отправить итоговое сообщение синхронизации для user {user_id}: {error_details}"

DIRECT_DL_PREPARING = ""
DIRECT_DL_ERROR_SENDING_INITIAL_PROGRESS_FORMAT = "🚫 Не удалось отправить временное сообщение о прогрессе для прямой загрузки: {error_details}"
DIRECT_DL_ERROR_START_PROCESSING_FORMAT = "🚫 Не удалось начать обработку ссылки (ошибка Telegram): {error_details}"

TRACK_STAGE_STARTING = ""
TRACK_STAGE_DOWNLOADING = ""
TRACK_STAGE_PROCESSING_METADATA = ""
TRACK_STAGE_CONVERTING = ""
TRACK_STAGE_UPLOADING = ""
TRACK_STAGE_INTERMEDIATE = ""

LOG_ERR_PROCESSING_FORMAT ="🚫 Ошибка обработки ({filename_short}...): {error_details}"
LOG_ERR_TELEGRAM_FORMAT = "🚫 Ошибка Telegram ({filename_short}...): {error_details}"
LOG_ERR_UNEXPECTED_FORMAT = "🚫 Неожиданная ошибка ({filename_short}...). Подробности в журнале."

USER_ERR_PROCESSING_DIRECT_FORMAT = "🚫 Ошибка обработки ({filename_short}...): {error_details}"
USER_ERR_TELEGRAM_DIRECT_FORMAT = "🚫 Ошибка Telegram ({filename_short}...): {error_details}"
USER_ERR_UNEXPECTED_DIRECT_FORMAT = "🚫 Неожиданная ошибка ({filename_short}...). Подробности в журнале."