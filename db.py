# db.py
import sqlite3
from pathlib import Path
import logging
from datetime import datetime

logger = logging.getLogger(__name__)  # Будет использовать конфигурацию логгера из bot.py

# БД будет создана в той же папке, где лежит этот скрипт db.py
# Если bot.py и db.py в одной папке, то все будет рядом.
DATABASE_FILE = Path(__file__).resolve().parent / "soundcloud_bot.db"


def initialize_db():
    """Инициализирует базу данных и создает таблицы, если их нет."""
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()

        # Таблица пользователей
        # last_sync_timestamp - время последней успешной ПОЛНОЙ синхронизации лайков
        cursor.execute("""
                       CREATE TABLE IF NOT EXISTS users
                       (
                           user_id
                           INTEGER
                           PRIMARY
                           KEY,
                           soundcloud_username
                           TEXT,
                           sync_enabled
                           BOOLEAN
                           DEFAULT
                           FALSE,
                           sync_period_hours
                           INTEGER
                           DEFAULT
                           24,
                           last_sync_timestamp
                           DATETIME
                       )
                       """)

        # Таблица скачанных/отправленных треков
        # track_identifier - URL трека SoundCloud
        # telegram_message_id - ID сообщения в Telegram, куда был отправлен трек
        cursor.execute("""
                       CREATE TABLE IF NOT EXISTS downloaded_tracks
                       (
                           user_id
                           INTEGER,
                           track_identifier
                           TEXT,
                           telegram_message_id
                           INTEGER,
                           download_timestamp
                           DATETIME
                           DEFAULT
                           CURRENT_TIMESTAMP,
                           PRIMARY
                           KEY
                       (
                           user_id,
                           track_identifier
                       ),
                           FOREIGN KEY
                       (
                           user_id
                       ) REFERENCES users
                       (
                           user_id
                       ) ON DELETE CASCADE
                           )
                       """)
        conn.commit()
        logger.info(f"База данных '{DATABASE_FILE}' инициализирована/проверена.")
    except sqlite3.Error as e:
        logger.error(f"Ошибка при инициализации БД '{DATABASE_FILE}': {e}")
    finally:
        if conn:
            conn.close()


def get_user_settings(user_id: int) -> dict | None:
    """Получает настройки пользователя из БД."""
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        user_data = cursor.fetchone()
        conn.close()
        return dict(user_data) if user_data else None
    except sqlite3.Error as e:
        logger.error(f"Ошибка БД при получении настроек пользователя {user_id}: {e}")
        return None


def update_user_settings(user_id: int, soundcloud_username: str | None = None,
                         sync_enabled: bool | None = None, sync_period_hours: int | None = None,
                         last_sync_timestamp: datetime | None = None,
                         is_new_user_setup: bool = False):  # Флаг для установки дефолтов при создании
    """Обновляет или создает настройки пользователя."""
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()

    try:
        current_settings = get_user_settings(user_id)  # Проверяем, существует ли пользователь

        fields_to_update = {}
        # Собираем поля для обновления, только если они переданы (не None)
        if soundcloud_username is not None: fields_to_update['soundcloud_username'] = soundcloud_username
        if sync_enabled is not None: fields_to_update['sync_enabled'] = sync_enabled
        if sync_period_hours is not None: fields_to_update['sync_period_hours'] = sync_period_hours
        if last_sync_timestamp is not None: fields_to_update['last_sync_timestamp'] = last_sync_timestamp
        # Для is_new_user_setup, мы не добавляем его в fields_to_update, а используем для установки дефолтов ниже

        if current_settings:  # Пользователь существует, обновляем
            if fields_to_update:  # Если есть что обновлять
                set_clause = ", ".join([f"{key} = ?" for key in fields_to_update.keys()])
                values = list(fields_to_update.values()) + [user_id]
                cursor.execute(f"UPDATE users SET {set_clause} WHERE user_id = ?", tuple(values))
                logger.info(f"Обновлены настройки для пользователя {user_id}: {fields_to_update}")
            # else:
            # logger.debug(f"Нет полей для обновления для существующего пользователя {user_id}.")
        else:  # Пользователя нет, создаем
            # Устанавливаем значения, используя переданные или дефолты, если is_new_user_setup
            final_sc_username = soundcloud_username if soundcloud_username is not None else (
                None if not is_new_user_setup else None)  # Дефолт SC username - None
            final_sync_enabled = sync_enabled if sync_enabled is not None else (
                False if not is_new_user_setup else False)  # Дефолт sync_enabled - False
            final_sync_period = sync_period_hours if sync_period_hours is not None else (
                24 if not is_new_user_setup else 24)  # Дефолт sync_period - 24
            final_last_sync = last_sync_timestamp  # Может быть None при создании, даже если is_new_user_setup

            # Если is_new_user_setup=True и какое-то поле не передано, оно возьмет дефолтное значение
            # (False для bool, 24 для int, None для text/datetime).
            # Это гарантирует, что при первом вызове /start или /menu пользователь будет создан с базовыми настройками.

            cursor.execute("""
                           INSERT INTO users (user_id, soundcloud_username, sync_enabled, sync_period_hours,
                                              last_sync_timestamp)
                           VALUES (?, ?, ?, ?, ?)
                           """, (user_id, final_sc_username, final_sync_enabled, final_sync_period, final_last_sync))
            logger.info(
                f"Создан новый пользователь {user_id} с настройками: sc_user='{final_sc_username}', sync_on={final_sync_enabled}, period={final_sync_period}h")

        conn.commit()
    except sqlite3.Error as e:
        logger.error(f"Ошибка БД при обновлении/создании настроек пользователя {user_id}: {e}")
    finally:
        if conn:
            conn.close()


def add_downloaded_track(user_id: int, track_identifier: str, telegram_message_id: int | None):
    """Добавляет информацию об отправленном треке в БД."""
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT OR REPLACE INTO downloaded_tracks 
            (user_id, track_identifier, telegram_message_id, download_timestamp) 
            VALUES (?, ?, ?, ?)
            """, (user_id, track_identifier, telegram_message_id, datetime.now()))
        # Используем INSERT OR REPLACE на случай, если трек был перезалит/обновлен и message_id изменился
        conn.commit()
        logger.info(
            f"Трек '{track_identifier}' (msg_id: {telegram_message_id}) добавлен в БД для пользователя {user_id}")
    except sqlite3.Error as e:
        logger.error(f"Ошибка БД при добавлении трека '{track_identifier}' для {user_id}: {e}")
    finally:
        if conn:
            conn.close()


def is_track_downloaded(user_id: int, track_identifier: str) -> bool:
    """Проверяет, был ли трек уже скачан и записан для пользователя."""
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT 1 FROM downloaded_tracks WHERE user_id = ? AND track_identifier = ?",
                       (user_id, track_identifier))
        result = cursor.fetchone()
        return result is not None
    except sqlite3.Error as e:
        logger.error(f"Ошибка БД при проверке трека '{track_identifier}' для {user_id}: {e}")
        return False  # В случае ошибки лучше считать, что не скачан, чтобы не пропустить
    finally:
        if conn:
            conn.close()

# Важно: initialize_db() будет вызываться из main() в bot.py,
# чтобы логгер был уже настроен к моменту первого лога из db.py.