# db.py
import sqlite3
from pathlib import Path
import logging
from datetime import datetime

logger = logging.getLogger(__name__)
DATABASE_FILE = Path(__file__).resolve().parent / "soundcloud_bot.db"

def initialize_db():
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            soundcloud_username TEXT,
            sync_enabled BOOLEAN DEFAULT FALSE,
            sync_period_hours INTEGER DEFAULT 24,
            last_sync_timestamp DATETIME,
            sync_order TEXT DEFAULT 'old_first' 
        )
        """)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS downloaded_tracks (
            user_id INTEGER,
            track_identifier TEXT, 
            telegram_message_id INTEGER, 
            download_timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, track_identifier),
            FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
        )
        """)
        conn.commit()
    except sqlite3.Error as e: logger.error(f"Ошибка БД (init): {e}")
    finally:
        if conn: conn.close()

def get_user_settings(user_id: int) -> dict | None:
    try:
        conn = sqlite3.connect(DATABASE_FILE); conn.row_factory = sqlite3.Row; cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        user_data = cursor.fetchone(); conn.close()
        return dict(user_data) if user_data else None
    except sqlite3.Error as e: logger.error(f"Ошибка БД (get_user {user_id}): {e}"); return None

def update_user_settings(user_id: int, soundcloud_username: str | None = None,
                         sync_enabled: bool | None = None, sync_period_hours: int | None = None,
                         last_sync_timestamp: datetime | None = None,
                         sync_order: str | None = None,
                         is_new_user_setup: bool = False):
    conn = sqlite3.connect(DATABASE_FILE); cursor = conn.cursor()
    try:
        current_settings = get_user_settings(user_id)
        fields_to_update = {}
        def add_field(name, value, default_if_new=None):
            if value is not None: fields_to_update[name] = value
            elif is_new_user_setup and name not in (current_settings or {}):
                 fields_to_update[name] = default_if_new
        add_field('soundcloud_username', soundcloud_username, None)
        add_field('sync_enabled', sync_enabled, False)
        add_field('sync_period_hours', sync_period_hours, 24)
        if last_sync_timestamp is not None: fields_to_update['last_sync_timestamp'] = last_sync_timestamp
        add_field('sync_order', sync_order, 'old_first')
        if current_settings:
            if fields_to_update:
                set_clause = ", ".join([f"{key} = ?" for key in fields_to_update.keys()])
                values = list(fields_to_update.values()) + [user_id]
                cursor.execute(f"UPDATE users SET {set_clause} WHERE user_id = ?", tuple(values))
        else:
            final_sc_username=fields_to_update.get('soundcloud_username',None)
            final_sync_enabled=fields_to_update.get('sync_enabled',False)
            final_sync_period=fields_to_update.get('sync_period_hours',24)
            final_last_sync=fields_to_update.get('last_sync_timestamp',None)
            final_sync_order=fields_to_update.get('sync_order','old_first')
            cursor.execute("""INSERT INTO users (user_id,soundcloud_username,sync_enabled,sync_period_hours,last_sync_timestamp,sync_order) 
                           VALUES (?,?,?,?,?,?)""",(user_id,final_sc_username,final_sync_enabled,final_sync_period,final_last_sync,final_sync_order))
            logger.info(f"Создан пользователь {user_id}.")
        conn.commit()
    except sqlite3.Error as e: logger.error(f"Ошибка БД (update_user {user_id}): {e}")
    finally:
        if conn: conn.close()

def add_downloaded_track(user_id: int, track_identifier: str, telegram_message_id: int | None):
    conn = sqlite3.connect(DATABASE_FILE); cursor = conn.cursor()
    try:
        cursor.execute("""INSERT OR REPLACE INTO downloaded_tracks 
                       (user_id,track_identifier,telegram_message_id,download_timestamp) VALUES (?,?,?,?)""",
                       (user_id, track_identifier, telegram_message_id, datetime.now()))
        conn.commit()
    except sqlite3.Error as e: logger.error(f"Ошибка БД (add_track '{track_identifier}' for {user_id}): {e}")
    finally:
        if conn: conn.close()

def is_track_downloaded(user_id: int, track_identifier: str) -> bool:
    conn = sqlite3.connect(DATABASE_FILE); cursor = conn.cursor()
    try:
        cursor.execute("SELECT 1 FROM downloaded_tracks WHERE user_id = ? AND track_identifier = ?", (user_id, track_identifier))
        result = cursor.fetchone(); return result is not None
    except sqlite3.Error as e: logger.error(f"Ошибка БД (is_track_downloaded '{track_identifier}' for {user_id}): {e}"); return False
    finally:
        if conn: conn.close()