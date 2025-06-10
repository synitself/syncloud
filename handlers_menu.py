import logging
import asyncio
from typing import Optional, cast
import re
from datetime import datetime, timezone, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery, Message, Bot
from telegram.ext import ContextTypes, ConversationHandler
from telegram.constants import ParseMode
import telegram.error

import db
import ui_texts
from utils import escape_markdown_v2, escape_markdown_legacy, create_progress_bar

logger = logging.getLogger(__name__)

(MAIN_MENU, SETTINGS_MENU, AWAIT_SC_USERNAME, AWAIT_SYNC_PERIOD, INFO_MENU, ERROR_LOG_MENU) = map(str, range(6))
AWAITING_TEXT_INPUT_KEY = "menu_awaiting_text_input"
LAST_MENU_MSG_ID_KEY = "last_interactive_menu_message_id"
ERROR_LOG_CURRENT_PAGE_KEY = "error_log_current_page"
ERRORS_PER_PAGE = 5

MAX_STATUS_UPDATE_RETRIES = 3
STATUS_UPDATE_RETRY_BUFFER = 1.0


async def generate_status_text(user_id: int, bot_data: dict) -> str:
    settings = db.get_user_settings(user_id)
    if not settings:
        return ui_texts.STATUS_LOADING_SETTINGS_ERROR

    user_sync_locks = bot_data.setdefault("user_sync_locks", {})
    sync_lock: Optional[asyncio.Lock] = user_sync_locks.get(user_id)

    if sync_lock and sync_lock.locked():
        sc_username_raw = settings.get('soundcloud_username', '')
        sc_username_escaped = escape_markdown_v2(sc_username_raw if sc_username_raw else "SC User")
        return ui_texts.STATUS_SYNC_IN_PROGRESS_FORMAT.format(sc_username=sc_username_escaped)

    if not settings.get('sync_enabled'):
        return ui_texts.STATUS_AUTOSYNC_OFF

    sc_username = settings.get('soundcloud_username')
    if not sc_username:
        return ui_texts.STATUS_AUTOSYNC_ON_NO_USERNAME

    sc_username_escaped = escape_markdown_v2(sc_username)
    last_sync = settings.get('last_sync_timestamp')
    period_hours = settings.get('sync_period_hours', 24)

    if last_sync and isinstance(last_sync, datetime):
        if not last_sync.tzinfo:
            last_sync = last_sync.replace(tzinfo=timezone.utc)
        else:
            last_sync = last_sync.astimezone(timezone.utc)

        next_sync_utc = last_sync + timedelta(hours=period_hours)
        msk_tz = timezone(timedelta(hours=3))
        next_sync_msk = next_sync_utc.astimezone(msk_tz)
        now_msk = datetime.now(msk_tz)

        if next_sync_msk < now_msk:  # Time for sync has passed, waiting for scheduler
            return ui_texts.STATUS_AUTOSYNC_ON_WAITING_NEXT_CYCLE_FORMAT.format(
                sc_username=sc_username_escaped, period_hours=period_hours
            )
        else:
            formatted_time = next_sync_msk.strftime('%H:%M %d.%m.%Y')
            escaped_formatted_time = escape_markdown_v2(formatted_time)
            return ui_texts.STATUS_AUTOSYNC_ON_NEXT_SYNC_APPROX_FORMAT.format(
                sc_username=sc_username_escaped, next_sync_time=escaped_formatted_time
            )
    else:  # No last_sync timestamp, means first sync is pending
        return ui_texts.STATUS_AUTOSYNC_ON_FIRST_SYNC_FORMAT.format(
            sc_username=sc_username_escaped, period_hours=period_hours
        )


async def update_or_create_status_message(
        user_id: int,
        chat_id: int,
        bot_data: dict,
        bot: Bot,
        custom_text: Optional[str] = None,
        parse_mode: Optional[str] = ParseMode.MARKDOWN_V2,
        pin_message: bool = True
):
    text_to_display = custom_text if custom_text is not None else await generate_status_text(user_id, bot_data)
    settings = db.get_user_settings(user_id)
    msg_id_in_db = settings.get('status_message_id') if settings else None
    actual_msg_id_for_operation: Optional[int] = None
    edit_successful = False
    send_successful = False

    if msg_id_in_db:
        for attempt in range(1, MAX_STATUS_UPDATE_RETRIES + 1):
            try:
                await bot.edit_message_text(
                    chat_id=chat_id, message_id=msg_id_in_db,
                    text=text_to_display, parse_mode=parse_mode, reply_markup=None
                )
                logger.debug(
                    f"Статусное сообщение {msg_id_in_db} для user {user_id} успешно обновлено (попытка {attempt}).")
                actual_msg_id_for_operation = msg_id_in_db
                edit_successful = True;
                break
            except telegram.error.RetryAfter as e_retry:
                wait_time = e_retry.retry_after + STATUS_UPDATE_RETRY_BUFFER
                logger.warning(
                    f"Flood control при редактировании статусного сообщения {msg_id_in_db} для user {user_id} (попытка {attempt}/{MAX_STATUS_UPDATE_RETRIES}). Ожидание {wait_time:.2f} сек.")
                if attempt == MAX_STATUS_UPDATE_RETRIES: logger.error(
                    f"Превышено макс. количество попыток ({MAX_STATUS_UPDATE_RETRIES}) для редактирования статуса {msg_id_in_db} user {user_id} из-за RetryAfter. Ошибка: {e_retry}"); break
                await asyncio.sleep(wait_time)
            except telegram.error.BadRequest as e_bad_req:
                if "message is not modified" in str(e_bad_req).lower():
                    logger.debug(
                        f"Статусное сообщение {msg_id_in_db} для user {user_id} не изменено (текст и разметка совпали).")
                    actual_msg_id_for_operation = msg_id_in_db;
                    edit_successful = True
                elif "message to edit not found" in str(e_bad_req).lower():
                    logger.warning(
                        f"Не удалось отредактировать статусное сообщение {msg_id_in_db} для user {user_id} (не найдено). Будет отправлено новое.")
                else:
                    logger.error(
                        f"Необрабатываемая BadRequest при редактировании статусного сообщения {msg_id_in_db} для user {user_id}: {e_bad_req}")
                break
            except telegram.error.TelegramError as e_telegram:
                logger.error(
                    f"Ошибка Telegram при редактировании статусного сообщения {msg_id_in_db} для user {user_id} (попытка {attempt}): {e_telegram}")
                if attempt == MAX_STATUS_UPDATE_RETRIES: logger.error(
                    f"Превышено макс. количество попыток ({MAX_STATUS_UPDATE_RETRIES}) для редактирования статуса {msg_id_in_db} user {user_id}. Ошибка: {e_telegram}")
                await asyncio.sleep(STATUS_UPDATE_RETRY_BUFFER * attempt)
            if edit_successful: break

    if not edit_successful:
        if msg_id_in_db:  # If edit failed and there was an old ID, try to clean it up
            try:
                await bot.delete_message(chat_id=chat_id, message_id=msg_id_in_db)
                logger.info(
                    f"Старое статусное сообщение {msg_id_in_db} удалено перед отправкой нового из-за ошибки/невозможности редактирования.")
            except telegram.error.TelegramError:
                logger.warning(f"Не удалось удалить старое статусное сообщение {msg_id_in_db} при замене.")
            finally:  # Always clear from DB if we're about to send a new one
                db.update_user_settings(user_id, status_message_id=None, set_status_msg_id_to_null=True)

        sent_new_msg_obj = None
        for attempt in range(1, MAX_STATUS_UPDATE_RETRIES + 1):
            try:
                sent_new_msg_obj = await bot.send_message(
                    chat_id=chat_id, text=text_to_display,
                    parse_mode=parse_mode, reply_markup=None
                )
                logger.info(
                    f"Новое статусное сообщение {sent_new_msg_obj.message_id} для user {user_id} отправлено и сохранено (попытка {attempt}).")
                actual_msg_id_for_operation = sent_new_msg_obj.message_id
                db.update_user_settings(user_id, status_message_id=actual_msg_id_for_operation)
                send_successful = True;
                break
            except telegram.error.RetryAfter as e_retry:
                wait_time = e_retry.retry_after + STATUS_UPDATE_RETRY_BUFFER
                logger.warning(
                    f"Flood control при отправке нового статусного сообщения для user {user_id} (попытка {attempt}/{MAX_STATUS_UPDATE_RETRIES}). Ожидание {wait_time:.2f} сек.")
                if attempt == MAX_STATUS_UPDATE_RETRIES: logger.error(
                    f"Превышено макс. количество попыток ({MAX_STATUS_UPDATE_RETRIES}) для отправки нового статусного сообщения user {user_id} из-за RetryAfter: {e_retry}"); return
                await asyncio.sleep(wait_time)
            except telegram.error.TelegramError as e_telegram:
                logger.error(
                    f"Критическая ошибка Telegram при отправке нового статусного сообщения для user {user_id} (попытка {attempt}): {e_telegram}")
                if attempt == MAX_STATUS_UPDATE_RETRIES: logger.error(
                    f"Превышено макс. количество попыток ({MAX_STATUS_UPDATE_RETRIES}) для отправки нового статуса user {user_id}: {e_telegram}"); return
                await asyncio.sleep(STATUS_UPDATE_RETRY_BUFFER * attempt)
            if send_successful: break

        if not send_successful:
            logger.error(f"Не удалось ни отредактировать, ни отправить статусное сообщение для user {user_id}.")
            return

    if actual_msg_id_for_operation and pin_message:
        try:
            await bot.pin_chat_message(chat_id=chat_id, message_id=actual_msg_id_for_operation,
                                       disable_notification=True)
            logger.info(
                f"Статусное сообщение {actual_msg_id_for_operation} для user {user_id} закреплено (или уже было).")
        except telegram.error.BadRequest as e_pin_br:
            if "message to pin not found" in str(e_pin_br).lower():
                logger.warning(
                    f"Не удалось закрепить статусное сообщение {actual_msg_id_for_operation}: оно не найдено.")
                if actual_msg_id_for_operation == (
                settings.get('status_message_id') if settings else None):  # Check if DB ID was for this message
                    db.update_user_settings(user_id, status_message_id=None, set_status_msg_id_to_null=True)
            elif "CHAT_NOT_MODIFIED" in str(e_pin_br).upper() or "message is already pinned" in str(e_pin_br).lower():
                logger.debug(f"Статусное сообщение {actual_msg_id_for_operation} уже было закреплено.")
            elif "not enough rights to pin a message" in str(e_pin_br).lower():
                logger.warning(
                    f"Недостаточно прав для закрепления сообщения {actual_msg_id_for_operation} в чате {chat_id}.")
            else:
                logger.warning(
                    f"Не удалось закрепить статусное сообщение {actual_msg_id_for_operation} (BadRequest): {e_pin_br}")
        except telegram.error.TelegramError as e_pin:
            logger.error(
                f"Ошибка Telegram при закреплении статусного сообщения {actual_msg_id_for_operation} для user {user_id}: {e_pin}")


async def update_user_status_message(user_id: int, chat_id: int, bot_data: dict, bot: Bot, pin: bool = True):
    await update_or_create_status_message(user_id, chat_id, bot_data, bot, pin_message=pin)


async def _edit_or_reply_menu_message(
        update: Update, context: ContextTypes.DEFAULT_TYPE, query: Optional[CallbackQuery],
        text: str, reply_markup: Optional[InlineKeyboardMarkup], parse_mode: Optional[str] = None
):
    target_msg_id = context.user_data.get(LAST_MENU_MSG_ID_KEY)
    effective_chat_id = update.effective_chat.id if update.effective_chat else (
        query.message.chat.id if query and query.message else None)
    if not effective_chat_id: logger.error("_edit_or_reply_menu_message: No chat_id."); return

    current_message_to_handle: Optional[Message] = query.message if query else update.message

    try:
        if query and query.message:
            if query.message.text == text and query.message.reply_markup == reply_markup and query.message.parse_mode == parse_mode:  # check parse_mode too
                await query.answer();
                return
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
            context.user_data[LAST_MENU_MSG_ID_KEY] = query.message.message_id
        elif update.message and target_msg_id:  # Edit previous menu message if /start is used again
            await context.bot.edit_message_text(
                chat_id=effective_chat_id, message_id=target_msg_id,
                text=text, reply_markup=reply_markup, parse_mode=parse_mode
            )
        elif update.message:  # Send new menu message
            msg = await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
            context.user_data[LAST_MENU_MSG_ID_KEY] = msg.message_id
    except telegram.error.BadRequest as e:
        if "message is not modified" in str(e).lower():
            logger.info(f"Сообщение меню не изменено (ожидаемо): {e}")
            if query: await query.answer()
        elif "message to edit not found" in str(e).lower():
            logger.warning(
                f"Original menu message (id: {target_msg_id or (query.message.message_id if query and query.message else 'unknown')}) not found, sending new one.")
            new_msg_sent = False
            if update.message:  # If triggered by /start command
                new_msg = await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
                context.user_data[LAST_MENU_MSG_ID_KEY] = new_msg.message_id
                new_msg_sent = True
            elif query and query.message:  # If triggered by callback but original message gone
                try:
                    new_msg = await context.bot.send_message(chat_id=effective_chat_id, text=text,
                                                             reply_markup=reply_markup, parse_mode=parse_mode)
                    context.user_data[LAST_MENU_MSG_ID_KEY] = new_msg.message_id
                    new_msg_sent = True
                except Exception as e_send_new_cb:
                    logger.error(f"Failed to send new menu message after edit_not_found (callback): {e_send_new_cb}")

            if not new_msg_sent: logger.error(
                f"Cannot send new menu: no update.message and no query.message after edit_not_found, or send failed.")

        else:  # Other BadRequest errors
            logger.error(f"BadRequest editing/sending menu: {e} (Text: '{text[:100]}...', ParseMode: {parse_mode})")
            if current_message_to_handle and parse_mode:  # Try sending without parse_mode as fallback
                try:
                    fallback_text = f"{text}\n(Ошибка форматирования, меню отображено без него)"
                    msg = await current_message_to_handle.reply_text(fallback_text, reply_markup=reply_markup,
                                                                     parse_mode=None)
                    context.user_data[LAST_MENU_MSG_ID_KEY] = msg.message_id
                except Exception as e_send_fallback:
                    logger.error(f"Fallback send_message after BadRequest also failed: {e_send_fallback}")
            elif not parse_mode and current_message_to_handle:  # Error even without parse_mode
                logger.error(f"Error even without parse_mode or no current_message_to_handle for fallback.")
    except Exception as e_other:
        logger.error(f"Unexpected error in _edit_or_reply_menu_message: {e_other}")


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    logger.info(f"User {user_id} executed /start or /menu command")

    if update.message:
        try:
            await update.message.delete()
            logger.debug(f"Command message {update.message.message_id} deleted for user {user_id}.")
        except telegram.error.TelegramError as e:
            logger.warning(f"Could not delete command message {update.message.message_id} for user {user_id}: {e}")

    if not db.get_user_settings(user_id):
        db.update_user_settings(user_id, is_new_user_setup=True)
        await update_user_status_message(user_id, chat_id, context.bot_data, context.bot)  # Pin status on first setup
    else:
        await update_user_status_message(user_id, chat_id, context.bot_data,
                                         context.bot)  # Ensure status is up and pinned

    context.user_data.pop(AWAITING_TEXT_INPUT_KEY, None)
    context.user_data.pop(ERROR_LOG_CURRENT_PAGE_KEY, None)

    keyboard = [
        [InlineKeyboardButton(ui_texts.BUTTON_SYNC_NOW, callback_data="sync_now_nav")],
        [InlineKeyboardButton(ui_texts.BUTTON_SETTINGS, callback_data="settings_menu_nav")],
        [InlineKeyboardButton(ui_texts.BUTTON_ERROR_LOG, callback_data="error_log_nav")],
        [InlineKeyboardButton(ui_texts.BUTTON_INFO, callback_data="info_bot_nav")],
        [InlineKeyboardButton(ui_texts.BUTTON_CLOSE_MENU, callback_data="close_menu_nav")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    current_query = cast(CallbackQuery, update.callback_query) if update.callback_query else None

    # Delete previous menu message if /start is used and an old menu message ID exists
    if update.message and context.user_data.get(LAST_MENU_MSG_ID_KEY):
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=context.user_data[LAST_MENU_MSG_ID_KEY])
            logger.debug(
                f"Предыдущее меню {context.user_data[LAST_MENU_MSG_ID_KEY]} удалено перед показом нового главного меню.")
            context.user_data.pop(LAST_MENU_MSG_ID_KEY, None)  # Clear the key for the deleted message
        except telegram.error.TelegramError as e_del_old:
            logger.warning(f"Не удалось удалить старое меню {context.user_data.get(LAST_MENU_MSG_ID_KEY)}: {e_del_old}")
            # If deletion failed, _edit_or_reply_menu_message will attempt to edit it.

    await _edit_or_reply_menu_message(update, context, current_query, ui_texts.MENU_TITLE, reply_markup,
                                      ParseMode.MARKDOWN_V2)
    return MAIN_MENU


async def main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    from handlers_sync import sync_user_likes_command  # Local import to avoid circular dependency at module level

    query = cast(CallbackQuery, update.callback_query)
    user_id = query.from_user.id
    chat_id = query.message.chat_id if query.message else user_id  # Should always have query.message here
    choice = query.data

    if query.message: context.user_data[LAST_MENU_MSG_ID_KEY] = query.message.message_id
    next_state = MAIN_MENU

    if choice == "settings_menu_nav":
        await query.answer()
        next_state = await display_settings_menu(update, context, query)
    elif choice == "info_bot_nav":
        await query.answer()
        bot_version = context.bot_data.get("BOT_VERSION", "N/A")
        info_text = ui_texts.INFO_BOT_TEXT_FORMAT.format(
            version=escape_markdown_legacy(bot_version))  # Legacy for simple info
        keyboard = [[InlineKeyboardButton(ui_texts.BUTTON_BACK_TO_MAIN, callback_data="back_to_main_menu_nav")]]
        await _edit_or_reply_menu_message(update, context, query, info_text, InlineKeyboardMarkup(keyboard),
                                          ParseMode.MARKDOWN)
        next_state = INFO_MENU
    elif choice == "sync_now_nav":
        logger.info(f"User {user_id} initiated sync from menu button.")
        await query.answer(text=ui_texts.SYNC_NOW_STARTED_ALERT, show_alert=False)  # Give feedback that it started
        asyncio.create_task(sync_user_likes_command(update, context, direct_user_id=user_id, direct_chat_id=chat_id))
        # Menu remains open, status message will update with progress
    elif choice == "error_log_nav":
        await query.answer()
        context.user_data[ERROR_LOG_CURRENT_PAGE_KEY] = 0
        next_state = await display_error_log_menu(update, context, query)
    elif choice == "close_menu_nav":
        menu_message_to_handle = query.message
        if menu_message_to_handle:
            try:
                await context.bot.delete_message(chat_id=menu_message_to_handle.chat_id,
                                                 message_id=menu_message_to_handle.message_id)
                await query.answer(text=ui_texts.MENU_CLOSED_CONFIRMATION_SHORT, show_alert=False)
            except telegram.error.TelegramError as e_del:
                logger.warning(
                    f"Не удалось удалить меню ({menu_message_to_handle.message_id}) при закрытии: {e_del}. Попытка отредактировать...")
                try:
                    await query.edit_message_text(text=ui_texts.MENU_CLOSED, reply_markup=None,
                                                  parse_mode=ParseMode.MARKDOWN_V2)
                    await query.answer()
                except telegram.error.TelegramError as e_edit_fallback:
                    logger.error(f"Не удалось отредактировать меню после неудачного удаления: {e_edit_fallback}")
                    await query.answer(ui_texts.MENU_CLOSE_ERROR_ALERT, show_alert=True)
        else:
            await query.answer(ui_texts.MENU_CLOSED_CONFIRMATION_SHORT, show_alert=True)

        await update_user_status_message(user_id, chat_id, context.bot_data,
                                         context.bot)  # Ensure status is current and pinned

        context.user_data.pop(LAST_MENU_MSG_ID_KEY, None)
        context.user_data.pop(AWAITING_TEXT_INPUT_KEY, None)
        context.user_data.pop(ERROR_LOG_CURRENT_PAGE_KEY, None)
        next_state = ConversationHandler.END
    elif choice == "back_to_main_menu_nav":
        await query.answer()
        context.user_data.pop(ERROR_LOG_CURRENT_PAGE_KEY, None)  # Clear error log page on going back to main
        next_state = await menu_command(update, context)
        return next_state  # Return early as menu_command handles status update

    # For other main menu actions (settings, info, error log) that keep menu open, ensure status is up-to-date
    if choice not in ["close_menu_nav", "back_to_main_menu_nav", "sync_now_nav"]:
        await update_user_status_message(user_id, chat_id, context.bot_data, context.bot)

    return next_state


async def info_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    query = cast(CallbackQuery, update.callback_query)
    await query.answer()
    if query.data == "back_to_main_menu_nav":
        return await menu_command(update, context)
    return INFO_MENU


async def display_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                query: Optional[CallbackQuery] = None) -> str:
    effective_user_id = update.effective_user.id if update.effective_user else (query.from_user.id if query else None)
    if not effective_user_id: return ConversationHandler.END  # Should not happen

    settings = db.get_user_settings(effective_user_id)
    if not settings:  # Should have been created by menu_command if new
        logger.error(f"Settings not found for user {effective_user_id} in display_settings_menu. This is unexpected.")
        db.update_user_settings(effective_user_id, is_new_user_setup=True)  # Attempt to recover
        settings = db.get_user_settings(effective_user_id)
        if not settings:  # Still not found
            await _edit_or_reply_menu_message(update, context, query, ui_texts.SETTINGS_DB_ERROR, None, parse_mode=None)
            return await menu_command(update, context)  # Go back to main menu

    sc_username_raw = settings.get('soundcloud_username', '')
    sc_username_display = sc_username_raw if sc_username_raw else ui_texts.SETTINGS_SC_USERNAME_NOT_SET
    sync_status_text = ui_texts.SETTINGS_SYNC_ENABLED_ON if settings.get(
        'sync_enabled') else ui_texts.SETTINGS_SYNC_ENABLED_OFF
    sync_period_display = settings.get('sync_period_hours', 24)
    sync_order_db = settings.get('sync_order', 'old_first')
    sync_order_text = ui_texts.SETTINGS_SYNC_ORDER_OLD_FIRST if sync_order_db == 'old_first' else ui_texts.SETTINGS_SYNC_ORDER_NEW_FIRST

    keyboard = [
        [InlineKeyboardButton(
            ui_texts.SETTINGS_SC_USERNAME_BUTTON_TEXT_FORMAT.format(escape_markdown_legacy(sc_username_display[:20])),
            callback_data="set_sc_username_action")],
        [InlineKeyboardButton(ui_texts.SETTINGS_SYNC_ENABLED_LABEL_FORMAT.format(sync_status_text),
                              callback_data="toggle_sync_action")],
        [InlineKeyboardButton(ui_texts.SETTINGS_SYNC_PERIOD_LABEL_FORMAT.format(sync_period_display),
                              callback_data="set_sync_period_action")],
        [InlineKeyboardButton(ui_texts.SETTINGS_SYNC_ORDER_LABEL_FORMAT.format(sync_order_text),
                              callback_data="toggle_sync_order_action")],
        [InlineKeyboardButton(ui_texts.BUTTON_BACK_TO_MAIN, callback_data="back_to_main_menu_nav")]
    ]
    await _edit_or_reply_menu_message(update, context, query, ui_texts.SETTINGS_TITLE, InlineKeyboardMarkup(keyboard),
                                      ParseMode.MARKDOWN_V2)
    return SETTINGS_MENU


async def settings_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    query = cast(CallbackQuery, update.callback_query)
    await query.answer()  # Answer immediately
    uid = query.from_user.id
    chat_id = query.message.chat_id if query.message else uid
    choice = query.data

    if query.message: context.user_data[LAST_MENU_MSG_ID_KEY] = query.message.message_id
    settings = db.get_user_settings(uid)  # Get fresh settings
    if not settings:  # Should not happen
        logger.error(f"Settings not found for user {uid} in settings_menu_callback. This is unexpected.")
        if query.message: await query.message.reply_text(ui_texts.SETTINGS_DB_ERROR);  # Simple reply if menu edit fails
        return await menu_command(update, context)

    action_taken_requires_settings_redraw = False
    next_state = SETTINGS_MENU

    if choice == "toggle_sync_action":
        if not settings.get('soundcloud_username'):
            await query.answer(ui_texts.SETTINGS_USERNAME_NOT_SET_ALERT, show_alert=True)
        else:
            new_sync_status = not settings.get('sync_enabled', False)
            db.update_user_settings(uid, sync_enabled=new_sync_status)
            action_taken_requires_settings_redraw = True
    elif choice == "toggle_sync_order_action":
        current_order = settings.get('sync_order', 'old_first')
        new_order = 'new_first' if current_order == 'old_first' else 'old_first'
        db.update_user_settings(uid, sync_order=new_order)
        action_taken_requires_settings_redraw = True
    elif choice == "set_sc_username_action":
        kb_list = [
            [InlineKeyboardButton(ui_texts.BUTTON_BACK_TO_SETTINGS, callback_data="back_to_settings_from_input")]]
        await _edit_or_reply_menu_message(update, context, query, ui_texts.SETTINGS_SC_USERNAME_PROMPT,
                                          InlineKeyboardMarkup(kb_list), ParseMode.MARKDOWN_V2)
        context.user_data[AWAITING_TEXT_INPUT_KEY] = "sc_username"
        next_state = AWAIT_SC_USERNAME
    elif choice == "set_sync_period_action":
        kb_list = [
            [InlineKeyboardButton(ui_texts.BUTTON_PERIOD_HOURS_FORMAT.format(6), callback_data="period_6h"),
             InlineKeyboardButton(ui_texts.BUTTON_PERIOD_HOURS_FORMAT.format(12), callback_data="period_12h")],
            [InlineKeyboardButton(ui_texts.BUTTON_PERIOD_HOURS_FORMAT.format(24), callback_data="period_24h"),
             InlineKeyboardButton(ui_texts.BUTTON_PERIOD_HOURS_FORMAT.format(48), callback_data="period_48h")],
            [InlineKeyboardButton(ui_texts.BUTTON_PERIOD_CUSTOM_INPUT, callback_data="period_custom_input")],
            [InlineKeyboardButton(ui_texts.BUTTON_BACK_TO_SETTINGS, callback_data="back_to_settings_nav")]
            # Back to settings menu (redraws it)
        ]
        await _edit_or_reply_menu_message(update, context, query, ui_texts.SETTINGS_SYNC_PERIOD_PROMPT,
                                          InlineKeyboardMarkup(kb_list), parse_mode=None)
    elif choice.startswith("period_") and choice.endswith("h"):
        try:
            period_hours = int(choice.replace("period_", "").replace("h", ""))
            db.update_user_settings(uid, sync_period_hours=period_hours)
            action_taken_requires_settings_redraw = True
        except ValueError:
            logger.warning(f"Invalid period value from callback: {choice}")
            await query.answer("Неверное значение периода!", show_alert=True)
    elif choice == "period_custom_input":
        kb_list = [
            [InlineKeyboardButton(ui_texts.BUTTON_BACK_TO_SETTINGS, callback_data="back_to_settings_from_input")]]
        await _edit_or_reply_menu_message(update, context, query, ui_texts.SETTINGS_SYNC_PERIOD_INPUT_PROMPT,
                                          InlineKeyboardMarkup(kb_list), ParseMode.MARKDOWN_V2)
        context.user_data[AWAITING_TEXT_INPUT_KEY] = "sync_period"
        next_state = AWAIT_SYNC_PERIOD
    elif choice == "back_to_main_menu_nav":
        context.user_data.pop(AWAITING_TEXT_INPUT_KEY, None)  # Clear input flag
        next_state = await menu_command(update, context)
        return next_state  # Return early, menu_command handles status
    elif choice == "back_to_settings_nav":  # From period selection back to settings
        action_taken_requires_settings_redraw = True

    if action_taken_requires_settings_redraw and next_state == SETTINGS_MENU:
        await display_settings_menu(update, context, query)  # Redraw if still in settings

    # Update status after any settings change or if returning to settings menu
    if next_state == SETTINGS_MENU:
        await update_user_status_message(uid, chat_id, context.bot_data, context.bot)

    return next_state


async def received_sc_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    context.user_data.pop(AWAITING_TEXT_INPUT_KEY, None)
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    sc_user_input = cast(Message, update.message).text.strip()
    msg_to_delete = cast(Message, update.message)

    kb_err_list = [
        [InlineKeyboardButton(ui_texts.BUTTON_BACK_TO_SETTINGS, callback_data="back_to_settings_from_input")]]
    rm_err = InlineKeyboardMarkup(kb_err_list)
    error_text_to_show: Optional[str] = None

    if not sc_user_input:
        error_text_to_show = ui_texts.SETTINGS_USERNAME_EMPTY_ERROR
    elif not re.match(r"^[a-zA-Z0-9\-_]+$", sc_user_input):
        error_text_to_show = ui_texts.SETTINGS_USERNAME_INVALID_CHARS_ERROR
    elif len(sc_user_input) < 3 or len(sc_user_input) > 30:
        error_text_to_show = ui_texts.SETTINGS_USERNAME_LENGTH_ERROR

    if msg_to_delete:  # Delete user's input message
        try:
            await msg_to_delete.delete()
        except Exception as e_del:
            logger.warning(f"Could not delete user input message: {e_del}")

    if error_text_to_show:
        await _edit_or_reply_menu_message(update, context, None, error_text_to_show, rm_err, ParseMode.MARKDOWN_V2)
        context.user_data[AWAITING_TEXT_INPUT_KEY] = "sc_username"  # Set flag again
        return AWAIT_SC_USERNAME

    db.update_user_settings(uid, soundcloud_username=sc_user_input)
    # Status will be updated by display_settings_menu or back_to_settings_from_input_callback
    return await display_settings_menu(cast(Update, update), context,
                                       None)  # Pass None for query as it's from MessageHandler


async def received_sync_period(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    context.user_data.pop(AWAITING_TEXT_INPUT_KEY, None)
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    period_input = cast(Message, update.message).text.strip()
    msg_to_delete = cast(Message, update.message)

    kb_err_list = [
        [InlineKeyboardButton(ui_texts.BUTTON_BACK_TO_SETTINGS, callback_data="back_to_settings_from_input")]]
    rm_err = InlineKeyboardMarkup(kb_err_list)
    error_text_to_show: Optional[str] = None

    try:
        period_hours = int(period_input)
        if not (1 <= period_hours <= 720): raise ValueError("Period out of range")
        db.update_user_settings(uid, sync_period_hours=period_hours)
    except ValueError:
        error_text_to_show = ui_texts.SETTINGS_PERIOD_INVALID_FORMAT_ERROR

    if msg_to_delete:  # Delete user's input message
        try:
            await msg_to_delete.delete()
        except Exception as e_del:
            logger.warning(f"Could not delete user input message: {e_del}")

    if error_text_to_show:
        await _edit_or_reply_menu_message(update, context, None, error_text_to_show, rm_err, ParseMode.MARKDOWN_V2)
        context.user_data[AWAITING_TEXT_INPUT_KEY] = "sync_period"  # Set flag again
        return AWAIT_SYNC_PERIOD

    # Status will be updated by display_settings_menu or back_to_settings_from_input_callback
    return await display_settings_menu(cast(Update, update), context, None)


async def back_to_settings_from_input_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    query = cast(CallbackQuery, update.callback_query)
    await query.answer()
    user_id = query.from_user.id
    chat_id = query.message.chat_id if query.message else user_id
    context.user_data.pop(AWAITING_TEXT_INPUT_KEY, None)  # Clear the input flag

    if query.message: context.user_data[LAST_MENU_MSG_ID_KEY] = query.message.message_id
    # display_settings_menu will be called, which will then call update_user_status_message
    return await display_settings_menu(update, context, query)


async def display_error_log_menu(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                 query: Optional[CallbackQuery] = None) -> str:
    user_id = query.from_user.id if query else update.effective_user.id
    current_page_0_indexed = context.user_data.get(ERROR_LOG_CURRENT_PAGE_KEY, 0)
    total_errors = db.count_user_errors(user_id)
    total_pages = (total_errors + ERRORS_PER_PAGE - 1) // ERRORS_PER_PAGE if total_errors > 0 else 1
    current_page_0_indexed = max(0, min(current_page_0_indexed, total_pages - 1))
    context.user_data[ERROR_LOG_CURRENT_PAGE_KEY] = current_page_0_indexed

    offset = current_page_0_indexed * ERRORS_PER_PAGE
    errors_on_page = db.get_user_errors(user_id, limit=ERRORS_PER_PAGE, offset=offset)

    error_list_text = ui_texts.ERROR_LOG_TITLE
    if not errors_on_page and total_errors == 0:
        error_list_text += ui_texts.ERROR_LOG_EMPTY
    else:
        for err in errors_on_page:
            timestamp_dt = err.get('timestamp')
            timestamp_str = "N/A"
            if isinstance(timestamp_dt, datetime):
                try:
                    timestamp_str = timestamp_dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                except Exception:
                    timestamp_str = timestamp_dt.strftime("%Y-%m-%d %H:%M:%S")  # Fallback

            escaped_error_message = escape_markdown_v2(err.get('error_message', 'Нет описания'))
            escaped_context_info = escape_markdown_v2(err.get('context_info', ''))
            error_list_text += ui_texts.ERROR_LOG_ENTRY_TIMESTAMP_FORMAT.format(timestamp=timestamp_str)
            error_list_text += ui_texts.ERROR_LOG_ENTRY_MESSAGE_FORMAT.format(error_message=escaped_error_message)
            if escaped_context_info: error_list_text += ui_texts.ERROR_LOG_ENTRY_CONTEXT_FORMAT.format(
                context_info=escaped_context_info)
            error_list_text += ui_texts.ERROR_LOG_SEPARATOR

    page_display_text = ui_texts.ERROR_LOG_PAGE_X_OF_Y_FORMAT.format(current_page_display=current_page_0_indexed + 1,
                                                                     total_pages_display=total_pages)
    error_list_text += page_display_text

    keyboard_row_nav = []
    if current_page_0_indexed > 0: keyboard_row_nav.append(
        InlineKeyboardButton(ui_texts.BUTTON_ERROR_LOG_PREV_PAGE, callback_data="err_log_prev_page"))
    if current_page_0_indexed < total_pages - 1: keyboard_row_nav.append(
        InlineKeyboardButton(ui_texts.BUTTON_ERROR_LOG_NEXT_PAGE, callback_data="err_log_next_page"))

    keyboard = []
    if keyboard_row_nav: keyboard.append(keyboard_row_nav)
    if total_errors > 0: keyboard.append(
        [InlineKeyboardButton(ui_texts.BUTTON_CLEAR_ERROR_LOG, callback_data="clear_error_log")])
    keyboard.append([InlineKeyboardButton(ui_texts.BUTTON_BACK_TO_MAIN, callback_data="back_to_main_from_log")])

    await _edit_or_reply_menu_message(update, context, query, error_list_text, InlineKeyboardMarkup(keyboard),
                                      ParseMode.MARKDOWN_V2)
    return ERROR_LOG_MENU


async def error_log_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    query = cast(CallbackQuery, update.callback_query)
    await query.answer()
    user_id = query.from_user.id
    chat_id = query.message.chat_id if query.message else user_id  # Should have query.message
    choice = query.data
    current_page = context.user_data.get(ERROR_LOG_CURRENT_PAGE_KEY, 0)

    if choice == "clear_error_log":
        db.clear_user_errors(user_id)
        await query.answer(ui_texts.ERROR_LOG_CLEARED_ALERT, show_alert=True)
        context.user_data[ERROR_LOG_CURRENT_PAGE_KEY] = 0  # Reset to first page
        await display_error_log_menu(update, context, query)
        # No need to update status message here, it's not directly affected by error log
    elif choice == "back_to_main_from_log":
        context.user_data.pop(ERROR_LOG_CURRENT_PAGE_KEY, None)
        return await menu_command(update, context)  # menu_command handles status update
    elif choice == "err_log_prev_page":
        if current_page > 0: context.user_data[ERROR_LOG_CURRENT_PAGE_KEY] = current_page - 1
        await display_error_log_menu(update, context, query)
    elif choice == "err_log_next_page":
        total_errors = db.count_user_errors(user_id)
        total_pages = (total_errors + ERRORS_PER_PAGE - 1) // ERRORS_PER_PAGE if total_errors > 0 else 1
        if current_page < total_pages - 1: context.user_data[ERROR_LOG_CURRENT_PAGE_KEY] = current_page + 1
        await display_error_log_menu(update, context, query)

    return ERROR_LOG_MENU