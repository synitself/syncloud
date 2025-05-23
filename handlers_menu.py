# handlers_menu.py
import logging
import asyncio
from typing import Optional, cast
import re
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery, Message
from telegram.ext import ContextTypes, ConversationHandler
from telegram.constants import ParseMode
import telegram.error

import db
from handlers_sync import sync_user_likes_command

logger = logging.getLogger(__name__)

(MAIN_MENU, SETTINGS_MENU,
 AWAIT_SC_USERNAME, AWAIT_SYNC_PERIOD, INFO_MENU) = map(str, range(5))
AWAITING_TEXT_INPUT_KEY = "menu_awaiting_text_input"
LAST_MENU_MSG_ID_KEY = "last_interactive_menu_message_id"


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
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
            context.user_data[LAST_MENU_MSG_ID_KEY] = query.message.message_id
        elif update.message and target_msg_id:
            await context.bot.edit_message_text(chat_id=effective_chat_id, message_id=target_msg_id, text=text,
                                                reply_markup=reply_markup, parse_mode=parse_mode)
        elif update.message:
            msg = await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
            context.user_data[LAST_MENU_MSG_ID_KEY] = msg.message_id
    except telegram.error.BadRequest as e:
        if "Message is not modified" in str(e).lower():
            pass
        elif "message to edit not found" in str(e).lower():
            logger.warning(
                f"Original menu message (id: {target_msg_id or (query.message.message_id if query and query.message else 'unknown')}) not found, sending new.")
            if current_message_to_handle:
                try:
                    msg = await current_message_to_handle.reply_text(text, reply_markup=reply_markup,
                                                                     parse_mode=parse_mode)
                    context.user_data[LAST_MENU_MSG_ID_KEY] = msg.message_id
                except Exception as e_send_new:
                    logger.error(f"Failed to send new menu message: {e_send_new}")
        else:
            logger.error(f"BadRequest editing/sending menu: {e}")
            if current_message_to_handle:
                try:
                    msg = await current_message_to_handle.reply_text(f"{text}\n(Err upd prev menu)",
                                                                     reply_markup=reply_markup, parse_mode=parse_mode)
                    context.user_data[LAST_MENU_MSG_ID_KEY] = msg.message_id
                except Exception as e_send_fallback:
                    logger.error(f"Fallback send_message failed: {e_send_fallback}")
    except Exception as e_other:
        logger.error(f"Unexpected error in _edit_or_reply_menu_message: {e_other}")


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    user_id = update.effective_user.id;
    logger.info(f"User {user_id} /menu")
    if not db.get_user_settings(user_id): db.update_user_settings(user_id, is_new_user_setup=True)
    context.user_data.pop(AWAITING_TEXT_INPUT_KEY, None)
    keyboard = [[InlineKeyboardButton("🔄 𝙎𝙔𝙉𝘾", callback_data="sync_now_nav")],
                [InlineKeyboardButton("⚙️ 𝙎𝙀𝙏𝙏𝙄𝙉𝙂𝙎", callback_data="settings_menu_nav")],
                [InlineKeyboardButton("ℹ️ 𝙄𝙉𝙁𝙊", callback_data="info_bot_nav")],
                [InlineKeyboardButton("❌ 𝘾𝙇𝙊𝙎𝙀", callback_data="close_menu_nav")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text_to_send = "𝙐𝙎𝙀 𝙏𝙃𝙀 𝙎𝙔𝙉𝘾𝙇𝙊𝙐𝘿 𝘽𝙊𝙏 𝙏𝙊 𝙃𝘼𝙉𝘿 𝙊𝙑𝙀𝙍  𝙏𝙃𝙀 𝘾𝙊𝙉𝙏𝙍𝙊𝙇"
    current_query = cast(CallbackQuery, update.callback_query) if update.callback_query else None
    await _edit_or_reply_menu_message(update, context, current_query, text_to_send, reply_markup)
    return MAIN_MENU


async def main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    query = cast(CallbackQuery, update.callback_query);
    await query.answer();
    user_id = query.from_user.id;
    choice = query.data
    if query.message: context.user_data[LAST_MENU_MSG_ID_KEY] = query.message.message_id
    if choice == "settings_menu_nav":
        return await display_settings_menu(update, context, query)
    elif choice == "info_bot_nav":
        info_text = ("Бот для скачивания треков и автоматической синхронизации вашей медиатеки.\nОтправь мне ссылку на [soundcloud.com] и я отвечу прикреплённым аудиофайлом.\nДля настройки синхронизации перейди в пункт [⚙️ Настройки]\nКонтакт: @synitself \nВерсия: 0.4.6")
        keyboard = [[InlineKeyboardButton("🔙 𝘽𝘼𝘾𝙆", callback_data="back_to_main_menu_nav")]]
        await _edit_or_reply_menu_message(update, context, query, info_text, InlineKeyboardMarkup(keyboard),
                                          ParseMode.MARKDOWN)
        return INFO_MENU
    elif choice == "sync_now_nav":
        logger.info(f"User {user_id} initiated sync from menu via button.")
        asyncio.create_task(sync_user_likes_command(update, context))
        await query.answer("𝙎𝙔𝙉𝘾 𝙎𝙏𝘼𝙍𝙏𝙎", show_alert=True)
        return MAIN_MENU
    elif choice == "close_menu_nav":
        await _edit_or_reply_menu_message(update, context, query, "𝙈𝙀𝙉𝙐 𝙄𝙎 𝘾𝙇𝙊𝙎𝙀𝘿", None)
        context.user_data.pop(LAST_MENU_MSG_ID_KEY, None);
        context.user_data.pop(AWAITING_TEXT_INPUT_KEY, None)
        return ConversationHandler.END
    return MAIN_MENU


async def info_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    query = cast(CallbackQuery, update.callback_query);
    await query.answer()
    if query.data == "back_to_main_menu_nav": return await menu_command(update, context)
    return INFO_MENU


async def display_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                query: Optional[CallbackQuery] = None) -> str:
    effective_user_id = update.effective_user.id if update.effective_user else (query.from_user.id if query else None)
    if not effective_user_id: return ConversationHandler.END
    settings = db.get_user_settings(effective_user_id)
    if not settings: db.update_user_settings(effective_user_id,
                                             is_new_user_setup=True); settings = db.get_user_settings(effective_user_id)
    if not settings:
        await _edit_or_reply_menu_message(update, context, query, "Ошибка получения настроек. Попробуйте /menu позже.",
                                          None)
        return ConversationHandler.END
    sc_username_display = settings.get('soundcloud_username') or "𝙀𝙈𝙋𝙏𝙔"
    sync_status_text = "𝙊𝙉 ✅" if settings.get('sync_enabled') else "𝙊𝙁𝙁 ❌"
    sync_period_display = settings.get('sync_period_hours', 24)
    sync_order_db = settings.get('sync_order', 'old_first')
    sync_order_text = "𝘼𝙎𝘾𝙀𝙉𝘿𝙄𝙉𝙂 🔼" if sync_order_db == 'old_first' else "𝘿𝙀𝙎𝘾𝙀𝙉𝘿𝙄𝙉𝙂 🔽"
    keyboard = [[InlineKeyboardButton(f"🔄 𝙎𝙔𝙉𝘾: {sync_status_text}", callback_data="toggle_sync_action")],
                [InlineKeyboardButton(f"👤 𝙐𝙎𝙀𝙍𝙉𝘼𝙈𝙀: {sc_username_display}", callback_data="set_sc_username_action")],
                [InlineKeyboardButton(f"⏱️ 𝙎𝙔𝙉𝘾 𝙋𝙀𝙍𝙄𝙊𝘿: {sync_period_display}h", callback_data="set_sync_period_action")],
                [InlineKeyboardButton(f"📊 𝙊𝙍𝘿𝙀𝙍: {sync_order_text}", callback_data="toggle_sync_order_action")],
                [InlineKeyboardButton("🔙 𝘽𝘼𝘾𝙆", callback_data="back_to_main_menu_nav")]]
    await _edit_or_reply_menu_message(update, context, query, "⚙️ 𝙎𝙀𝙏𝙏𝙄𝙉𝙂𝙎", InlineKeyboardMarkup(keyboard))
    return SETTINGS_MENU


async def settings_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    query = cast(CallbackQuery, update.callback_query);
    await query.answer();
    uid = query.from_user.id;
    choice = query.data
    if query.message: context.user_data[LAST_MENU_MSG_ID_KEY] = query.message.message_id

    if choice == "toggle_sync_action":
        s = db.get_user_settings(uid);
        if not s: db.update_user_settings(uid, is_new_user_setup=True); s = db.get_user_settings(uid)
        if not s or not s.get('soundcloud_username'): await query.answer("𝙋𝙇𝙀𝘼𝙎𝙀 𝙀𝙉𝙏𝙀𝙍 𝙏𝙃𝙀 𝙐𝙎𝙀𝙍𝙉𝘼𝙈𝙀 𝙁𝙄𝙍𝙎𝙏",
                                                                         show_alert=True); return SETTINGS_MENU
        new_s = not s.get('sync_enabled', False);
        db.update_user_settings(uid, sync_enabled=new_s)
        return await display_settings_menu(update, context, query)
    elif choice == "toggle_sync_order_action":
        s = db.get_user_settings(uid)
        if not s: db.update_user_settings(uid, is_new_user_setup=True); s = db.get_user_settings(uid)
        if s:
            current_order = s.get('sync_order', 'old_first')
            new_order = 'new_first' if current_order == 'old_first' else 'old_first'
            db.update_user_settings(uid, sync_order=new_order)
        else:
            await query.answer("Ошибка настроек!", show_alert=True)
        return await display_settings_menu(update, context, query)
    elif choice == "set_sc_username_action":
        kb_list = [[InlineKeyboardButton("🔙 𝘽𝘼𝘾𝙆", callback_data="back_to_settings_from_input")]];
        await _edit_or_reply_menu_message(update, context, query, "𝙎𝙊𝙐𝙉𝘿𝘾𝙇𝙊𝙐𝘿 𝙐𝙎𝙀𝙍𝙉𝘼𝙈𝙀:",
                                          InlineKeyboardMarkup(kb_list));
        context.user_data[AWAITING_TEXT_INPUT_KEY] = True
        return AWAIT_SC_USERNAME
    elif choice == "set_sync_period_action":
        kb_list = [[InlineKeyboardButton("6h", callback_data="period_6h"),
                    InlineKeyboardButton("12h", callback_data="period_12h")],
                   [InlineKeyboardButton("24h", callback_data="period_24h"),
                    InlineKeyboardButton("48h", callback_data="period_48h")],
                   [InlineKeyboardButton("📝 𝘾𝙐𝙎𝙏𝙊𝙈", callback_data="period_custom_input")],
                   [InlineKeyboardButton("🔙 𝘽𝘼𝘾𝙆", callback_data="back_to_settings_nav")]];
        await _edit_or_reply_menu_message(update, context, query, "𝙎𝙀𝙇𝙀𝘾𝙏 𝙎𝙔𝙉𝘾 𝙋𝙀𝙍𝙄𝙊𝘿:",
                                          InlineKeyboardMarkup(kb_list));
        return SETTINGS_MENU
    elif choice.startswith("period_") and choice.endswith("h"):
        try:
            p = int(choice.replace("period_", "").replace("h", "")); db.update_user_settings(uid, sync_period_hours=p)
        except ValueError:
            logger.warning(f"Invalid period value from callback: {choice}")
        return await display_settings_menu(update, context, query)
    elif choice == "period_custom_input":
        kb_list = [[InlineKeyboardButton("🔙 𝘽𝘼𝘾𝙆", callback_data="back_to_settings_from_input")]];
        await _edit_or_reply_menu_message(update, context, query, "𝙄𝙉 𝙏𝙃𝙀 𝙍𝘼𝙉𝙂𝙀 1–720:",
                                          InlineKeyboardMarkup(kb_list));
        context.user_data[AWAITING_TEXT_INPUT_KEY] = True
        return AWAIT_SYNC_PERIOD
    elif choice == "back_to_main_menu_nav":
        context.user_data.pop(LAST_MENU_MSG_ID_KEY, None);
        context.user_data.pop(AWAITING_TEXT_INPUT_KEY, None);
        return await menu_command(update, context)
    elif choice == "back_to_settings_nav":
        return await display_settings_menu(update, context, query)

    logger.warning(f"Необработанный callback '{choice}' в settings_menu_callback.")
    return await display_settings_menu(update, context, query)  # По умолчанию перерисовать


async def received_sc_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    context.user_data.pop(AWAITING_TEXT_INPUT_KEY, None)
    uid = update.effective_user.id;
    sc_user = cast(Message, update.message).text.strip();
    target_id = context.user_data.get(LAST_MENU_MSG_ID_KEY)
    msg_to_delete = cast(Message, update.message)

    kb_err_list = [[InlineKeyboardButton("🔙 𝘽𝘼𝘾𝙆", callback_data="back_to_settings_from_input")]];
    rm_err = InlineKeyboardMarkup(kb_err_list)
    error_text_to_show: Optional[str] = None
    if not sc_user:
        error_text_to_show = "Имя пользователя пустое. Попробуйте еще."
    elif not re.match(r"^[a-zA-Z0-9\-_]+$", sc_user):
        error_text_to_show = "Недопустимые символы. Введите только имя."

    if error_text_to_show:
        if target_id and update.effective_chat:
            await _edit_or_reply_menu_message(update, context, None, error_text_to_show, rm_err)
        context.user_data[AWAITING_TEXT_INPUT_KEY] = True
        if msg_to_delete:
            try:
                await msg_to_delete.delete()
            except Exception:
                pass
        return AWAIT_SC_USERNAME

    db.update_user_settings(uid, soundcloud_username=sc_user)
    if msg_to_delete:
        try:
            await msg_to_delete.delete()
        except Exception:
            pass
    return await display_settings_menu(cast(Update, update), context, None)  # query = None


async def received_sync_period(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    context.user_data.pop(AWAITING_TEXT_INPUT_KEY, None)
    uid = update.effective_user.id;
    p_in = cast(Message, update.message).text.strip();
    target_id = context.user_data.get(LAST_MENU_MSG_ID_KEY)
    msg_to_delete = cast(Message, update.message)

    kb_err_list = [[InlineKeyboardButton("🔙 𝘽𝘼𝘾𝙆", callback_data="back_to_settings_from_input")]];
    rm_err = InlineKeyboardMarkup(kb_err_list)
    error_text_to_show: Optional[str] = None
    try:
        p = int(p_in);
        assert 1 <= p <= 720
        db.update_user_settings(uid, sync_period_hours=p)
    except(ValueError, AssertionError):
        error_text_to_show = "𝙄𝙉𝙑𝘼𝙇𝙄𝘿 𝙁𝙊𝙍𝙈𝘼𝙏."

    if error_text_to_show:
        if target_id and update.effective_chat:
            await _edit_or_reply_menu_message(update, context, None, error_text_to_show, rm_err)
        context.user_data[AWAITING_TEXT_INPUT_KEY] = True
        if msg_to_delete:
            try:
                await msg_to_delete.delete()
            except Exception:
                pass
        return AWAIT_SYNC_PERIOD

    if msg_to_delete:
        try:
            await msg_to_delete.delete()
        except Exception:
            pass
    return await display_settings_menu(cast(Update, update), context, None)  # query = None


async def back_to_settings_from_input_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    query = cast(CallbackQuery, update.callback_query);
    await query.answer()
    context.user_data.pop(AWAITING_TEXT_INPUT_KEY, None)
    if query.message: context.user_data[LAST_MENU_MSG_ID_KEY] = query.message.message_id
    return await display_settings_menu(update, context, query)