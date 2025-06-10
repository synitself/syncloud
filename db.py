import sqlite3
from pathlib import Path
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)
DATABASE_FILE = Path(__file__).resolve().parent / "soundcloud_bot.db"


def _add_column_if_not_exists(cursor, table_name, column_name, column_type):
    cursor.execute(f"PRAGMA table_info({table_name})")
    columns = [info[1] for info in cursor.fetchall()]
    if column_name not in columns:
        try:
            cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")
            logger.info(f"Добавлена колонка {column_name} в таблицу {table_name}.")
        except sqlite3.Error as e:
            logger.error(f"Ошибка при добавлении колонки {column_name} в {table_name}: {e}")


def _drop_column_if_exists(cursor, table_name, column_name):
    cursor.execute(f"PRAGMA table_info({table_name})")
    columns = [info[1] for info in cursor.fetchall()]
    if column_name in columns:
        logger.warning(f"Колонка {column_name} в таблице {table_name} существует. "
                       f"Если она больше не нужна, рассмотрите возможность ее удаления вручную или через миграцию.")


def initialize_db():
    try:
        conn = sqlite3.connect(DATABASE_FILE, detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
        sqlite3.register_adapter(datetime, lambda val: val.isoformat() if val else None)

        def datetime_converter(val_bytes):
            if not val_bytes: return None
            val_str = val_bytes.decode()
            try:
                if '+' in val_str or '-' in val_str[10:] or 'Z' in val_str:
                    return datetime.fromisoformat(val_str.replace('Z', '+00:00'))
                elif '.' in val_str:  # Handle cases with microseconds but no explicit timezone
                    dt_obj = datetime.fromisoformat(val_str)
                    if dt_obj.tzinfo is None:
                        return dt_obj.replace(tzinfo=timezone.utc)
                    return dt_obj
                # Fallback for older non-ISO formats, assuming UTC
                return datetime.strptime(val_str, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
            except ValueError:
                logger.warning(f"Could not parse datetime string from DB: {val_str}")
                return None

        sqlite3.register_converter("DATETIME", datetime_converter)
        sqlite3.register_converter("TIMESTAMP", datetime_converter)

        cursor = conn.cursor()
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
                           DATETIME,
                           sync_order
                           TEXT
                           DEFAULT
                           'old_first',
                           status_message_id
                           INTEGER
                       )
                       """)
        _add_column_if_not_exists(cursor, "users", "status_message_id", "INTEGER")

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
        cursor.execute("""
                       CREATE TABLE IF NOT EXISTS user_errors
                       (
                           id
                           INTEGER
                           PRIMARY
                           KEY
                           AUTOINCREMENT,
                           user_id
                           INTEGER,
                           timestamp
                           DATETIME
                           DEFAULT
                           CURRENT_TIMESTAMP,
                           error_message
                           TEXT,
                           context_info
                           TEXT,
                           FOREIGN
                           KEY
                       (
                           user_id
                       ) REFERENCES users
                       (
                           user_id
                       ) ON DELETE CASCADE
                           )
                       """)
        cursor.execute("""
                       CREATE TABLE IF NOT EXISTS failed_tracks
                       (
                           user_id
                           INTEGER,
                           track_identifier
                           TEXT,
                           timestamp
                           DATETIME
                           DEFAULT
                           CURRENT_TIMESTAMP,
                           reason
                           TEXT,
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
    except sqlite3.Error as e:
        logger.error(f"Ошибка БД (init): {e}")
    finally:
        if conn: conn.close()


def get_user_settings(user_id: int) -> dict | None:
    try:
        conn = sqlite3.connect(DATABASE_FILE, detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        user_data = cursor.fetchone()
        return dict(user_data) if user_data else None
    except sqlite3.Error as e:
        logger.error(f"Ошибка БД (get_user {user_id}): {e}");
        return None
    finally:
        if conn: conn.close()


def update_user_settings(user_id: int, soundcloud_username: str | None = None,
                         sync_enabled: bool | None = None, sync_period_hours: int | None = None,
                         last_sync_timestamp: datetime | None = None,
                         sync_order: str | None = None,
                         status_message_id: int | None = None,
                         is_new_user_setup: bool = False,
                         set_status_msg_id_to_null: bool = False):
    conn = sqlite3.connect(DATABASE_FILE);
    cursor = conn.cursor()
    try:
        current_settings = get_user_settings(user_id)
        fields_to_update = {}

        def add_field(name, value, default_if_new=None):
            if value is not None:
                fields_to_update[name] = value
            elif name == 'status_message_id' and set_status_msg_id_to_null:
                fields_to_update[name] = None
            elif is_new_user_setup and name not in (current_settings or {}):
                fields_to_update[name] = default_if_new

        add_field('soundcloud_username', soundcloud_username)
        add_field('sync_enabled', sync_enabled, False if is_new_user_setup else None)
        add_field('sync_period_hours', sync_period_hours, 24 if is_new_user_setup else None)

        if last_sync_timestamp is not None:
            if not last_sync_timestamp.tzinfo:
                fields_to_update['last_sync_timestamp'] = last_sync_timestamp.replace(tzinfo=timezone.utc)
            else:
                fields_to_update['last_sync_timestamp'] = last_sync_timestamp.astimezone(timezone.utc)
        elif is_new_user_setup and 'last_sync_timestamp' not in (current_settings or {}):
            fields_to_update['last_sync_timestamp'] = None

        add_field('sync_order', sync_order, 'old_first' if is_new_user_setup else None)

        if status_message_id is not None:
            fields_to_update['status_message_id'] = status_message_id
        elif set_status_msg_id_to_null:
            fields_to_update['status_message_id'] = None
        elif is_new_user_setup and 'status_message_id' not in (current_settings or {}):
            fields_to_update['status_message_id'] = None

        if current_settings:
            if fields_to_update:
                set_clause_parts = []
                values_list = []
                for key, value_to_set in fields_to_update.items():
                    set_clause_parts.append(f"{key} = ?")
                    values_list.append(value_to_set)

                if not set_clause_parts:
                    return

                set_clause = ", ".join(set_clause_parts)
                values_list.append(user_id)
                cursor.execute(f"UPDATE users SET {set_clause} WHERE user_id = ?", tuple(values_list))
        else:
            final_sc_username = fields_to_update.get('soundcloud_username', None)
            final_sync_enabled = fields_to_update.get('sync_enabled', False)
            final_sync_period = fields_to_update.get('sync_period_hours', 24)
            final_last_sync = fields_to_update.get('last_sync_timestamp', None)
            final_sync_order = fields_to_update.get('sync_order', 'old_first')
            final_status_msg_id = fields_to_update.get('status_message_id', None)
            cursor.execute("""INSERT INTO users (user_id, soundcloud_username, sync_enabled, sync_period_hours,
                                                 last_sync_timestamp, sync_order, status_message_id)
                              VALUES (?, ?, ?, ?, ?, ?, ?)""",
                           (user_id, final_sc_username, final_sync_enabled, final_sync_period, final_last_sync,
                            final_sync_order, final_status_msg_id))
            logger.info(f"Создан пользователь {user_id} в БД.")
        conn.commit()
    except sqlite3.Error as e:
        logger.error(f"Ошибка БД (update_user {user_id}): {e}")
    finally:
        if conn: conn.close()


def add_downloaded_track(user_id: int, track_identifier: str, telegram_message_id: int | None):
    conn = sqlite3.connect(DATABASE_FILE);
    cursor = conn.cursor()
    try:
        cursor.execute("""INSERT OR REPLACE INTO downloaded_tracks
                       (user_id,track_identifier,telegram_message_id,download_timestamp) VALUES (?,?,?,?)""",
                       (user_id, track_identifier, telegram_message_id, datetime.now(timezone.utc)))
        conn.commit()
    except sqlite3.Error as e:
        logger.error(f"Ошибка БД (add_track '{track_identifier}' for {user_id}): {e}")
    finally:
        if conn: conn.close()


def is_track_downloaded(user_id: int, track_identifier: str) -> bool:
    conn = sqlite3.connect(DATABASE_FILE);
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT 1 FROM downloaded_tracks WHERE user_id = ? AND track_identifier = ?",
                       (user_id, track_identifier))
        result = cursor.fetchone();
        return result is not None
    except sqlite3.Error as e:
        logger.error(f"Ошибка БД (is_track_downloaded '{track_identifier}' for {user_id}): {e}");
        return False
    finally:
        if conn: conn.close()


def log_user_error(user_id: int, error_message: str, context_info: str | None = None):
    conn = sqlite3.connect(DATABASE_FILE);
    cursor = conn.cursor()
    try:
        cursor.execute("""INSERT INTO user_errors (user_id, error_message, context_info, timestamp)
                          VALUES (?, ?, ?, ?)""",
                       (user_id, error_message, context_info, datetime.now(timezone.utc)))
        conn.commit()
    except sqlite3.Error as e:
        logger.error(f"Ошибка БД (log_user_error for {user_id}): {e}")
    finally:
        if conn: conn.close()


def get_user_errors(user_id: int, limit: int = 10, offset: int = 0) -> list[dict]:
    conn = sqlite3.connect(DATABASE_FILE, detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    try:
        cursor.execute("""SELECT id, timestamp, error_message, context_info
                          FROM user_errors
                          WHERE user_id = ?
                          ORDER BY timestamp DESC LIMIT ?
                          OFFSET ?""", (user_id, limit, offset))
        return [dict(row) for row in cursor.fetchall()]
    except sqlite3.Error as e:
        logger.error(f"Ошибка БД (get_user_errors for {user_id}): {e}")
        return []
    finally:
        if conn: conn.close()


def count_user_errors(user_id: int) -> int:
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT COUNT(*) FROM user_errors WHERE user_id = ?", (user_id,))
        result = cursor.fetchone()
        return result[0] if result else 0
    except sqlite3.Error as e:
        logger.error(f"Ошибка БД (count_user_errors for {user_id}): {e}")
        return 0
    finally:
        if conn: conn.close()


def clear_user_errors(user_id: int):
    conn = sqlite3.connect(DATABASE_FILE);
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM user_errors WHERE user_id = ?", (user_id,))
        conn.commit()
    except sqlite3.Error as e:
        logger.error(f"Ошибка БД (clear_user_errors for {user_id}): {e}")
    finally:
        if conn: conn.close()


def add_failed_track(user_id: int, track_identifier: str, reason: str | None = None):
    conn = sqlite3.connect(DATABASE_FILE);
    cursor = conn.cursor()
    try:
        cursor.execute("""INSERT
        OR IGNORE INTO failed_tracks
                          (user_id, track_identifier, reason, timestamp) VALUES (?, ?, ?, ?)""",
                       (user_id, track_identifier, reason, datetime.now(timezone.utc)))
        conn.commit()
    except sqlite3.Error as e:
        logger.error(f"Ошибка БД (add_failed_track for {user_id}, {track_identifier}): {e}")
    finally:
        if conn: conn.close()


def is_track_failed(user_id: int, track_identifier: str) -> bool:
    conn = sqlite3.connect(DATABASE_FILE);
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT 1 FROM failed_tracks WHERE user_id = ? AND track_identifier = ?",
                       (user_id, track_identifier))
        result = cursor.fetchone();
        return result is not None
    except sqlite3.Error as e:
        logger.error(f"Ошибка БД (is_track_failed for {user_id}, {track_identifier}): {e}");
        return False
    finally:
        if conn: conn.close()


def get_users_for_scheduled_sync() -> list[dict]:
    conn = sqlite3.connect(DATABASE_FILE, detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    users_to_sync = []
    try:
        logger.debug("Планировщик: Запрос всех пользователей с sync_enabled=TRUE и непустым soundcloud_username")
        cursor.execute(
            "SELECT user_id, soundcloud_username, sync_period_hours, last_sync_timestamp, sync_enabled FROM users WHERE sync_enabled = TRUE AND soundcloud_username IS NOT NULL AND soundcloud_username != ''")
        all_enabled_users = cursor.fetchall()
        logger.debug(
            f"Планировщик: Найдено {len(all_enabled_users)} пользователей с включенной синхронизацией и username.")

        now_utc = datetime.now(timezone.utc)
        logger.debug(f"Планировщик: Текущее время UTC: {now_utc.isoformat()}")

        for user_row in all_enabled_users:
            user = dict(user_row)
            logger.debug(f"Планировщик: Проверка пользователя {user['user_id']} (SC: {user['soundcloud_username']})")
            logger.debug(f"  sync_enabled: {user.get('sync_enabled')}")
            logger.debug(f"  sync_period_hours: {user.get('sync_period_hours')}")
            last_sync_val = user.get('last_sync_timestamp')
            logger.debug(f"  last_sync_timestamp (из БД): {last_sync_val} (тип: {type(last_sync_val)})")

            if last_sync_val is None:
                logger.debug(f"  last_sync_timestamp is None. Добавляем пользователя {user['user_id']} в очередь.")
                users_to_sync.append(user)
                continue

            if not isinstance(last_sync_val, datetime):
                logger.warning(
                    f"  Планировщик: last_sync_timestamp для user {user['user_id']} не является datetime после конвертации: {last_sync_val}. Пробуем синхронизировать.")
                users_to_sync.append(user)
                continue

            # Ensure last_sync_val is timezone-aware (UTC)
            if last_sync_val.tzinfo is None or last_sync_val.tzinfo.utcoffset(last_sync_val) is None:
                last_sync_val = last_sync_val.replace(tzinfo=timezone.utc)
                logger.debug(f"  last_sync_timestamp (приведен к UTC): {last_sync_val.isoformat()}")
            elif last_sync_val.tzinfo != timezone.utc:
                last_sync_val = last_sync_val.astimezone(timezone.utc)
                logger.debug(f"  last_sync_timestamp (конвертирован в UTC): {last_sync_val.isoformat()}")

            sync_period_hours = user.get('sync_period_hours', 24)
            if not isinstance(sync_period_hours, (int, float)) or sync_period_hours <= 0:
                logger.warning(
                    f"  Планировщик: Некорректный sync_period_hours ({sync_period_hours}) для user {user['user_id']}. Используется 24ч.")
                sync_period_hours = 24

            sync_period = timedelta(hours=sync_period_hours)
            next_sync_time = last_sync_val + sync_period
            logger.debug(
                f"  Период синхронизации: {sync_period_hours} ч. Следующая синхронизация не ранее: {next_sync_time.isoformat()}")

            if now_utc >= next_sync_time:
                logger.debug(f"  Время для синхронизации пользователя {user['user_id']} пришло. Добавляем.")
                users_to_sync.append(user)
            else:
                logger.debug(f"  Для пользователя {user['user_id']} время синхронизации еще не пришло.")

        logger.debug(f"Планировщик: Итого пользователей для синхронизации: {len(users_to_sync)}")
        return users_to_sync
    except sqlite3.Error as e:
        logger.error(f"Ошибка БД (get_users_for_scheduled_sync): {e}")
        return []
    finally:
        if conn: conn.close()


def get_all_users_with_status_message() -> list[dict]:
    conn = sqlite3.connect(DATABASE_FILE, detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT user_id, status_message_id FROM users WHERE status_message_id IS NOT NULL")
        return [dict(row) for row in cursor.fetchall()]
    except sqlite3.Error as e:
        logger.error(f"Ошибка БД (get_all_users_with_status_message): {e}")
        return []
    finally:
        if conn: conn.close()