"""Админка: /stats, /updatekb, рассылка /broadcast с подтверждением, /feedback."""
import time

import config

from core import bot, logger
from keyboards import build_main_keyboard
from storage import (
    _is_gone_user_error,
    feedback_relay,
    get_stats,
    load_users,
    pending_broadcasts,
    remove_user,
)
from telebot import types


@bot.message_handler(commands=['stats'])
def stats(message):
    """Статистика для админа: пользователи, сообщения, активность, топ часов."""
    if message.from_user.id != config.ADMIN_ID:
        return
    data = get_stats()
    top = '—'
    if data['top_hours']:
        top = ', '.join(
            f'{hour:02d}:00 ({count} сообщ.)' for hour, count in data['top_hours']
        )
    bot.send_message(
        message.chat.id,
        f'📊 Статистика\n\n'
        f'Всего пользователей: {data["total_users"]}\n'
        f'Активных сегодня: {data["today_active"]}\n'
        f'Сообщений сегодня: {data["today_messages"]}\n'
        f'Сообщений за 7 дней: {data["week_messages"]}\n'
        f'Самые активные часы: {top}',
    )
    logger.info(f'/stats: {data}')


@bot.message_handler(commands=['updatekb'])
def update_keyboards(message):
    """Обновляет reply-клавиатуру у всех пользователей (только админ).

    Reply-клавиатуру нельзя отредактировать, но сообщение с новой разметкой
    заменяет старую клавиатуру у получателя автоматически.
    """
    if message.from_user.id != config.ADMIN_ID:
        return
    users = load_users()
    keyboard = build_main_keyboard()
    sent, failed = 0, 0
    for user_id in users:
        try:
            # Клавиатуру можно обновить только сообщением, поэтому шлём
            # минимальный бесшумный стикер-пустышку: без звука и без бейджа
            bot.send_message(
                user_id,
                '🦊',
                reply_markup=keyboard,
                disable_notification=True,
            )
            sent += 1
        except Exception as error:
            failed += 1
            if _is_gone_user_error(error):
                remove_user(user_id)
            logger.warning(f'/updatekb: не доставлено {user_id}: {error}')
        time.sleep(0.05)  # ~20 сообщений/сек, лимит Telegram ~30/сек
    bot.send_message(
        message.chat.id,
        f'Клавиатуры обновлены: отправлено {sent}, ошибок {failed}.',
    )
    logger.info(f'/updatekb: отправлено {sent}, ошибок {failed}')


@bot.message_handler(commands=['broadcast'])
def broadcast(message):
    """Рассылка с подтверждением.

    Формат: /broadcast текст — рассылает текст;
    /broadcast реплаем на фото/видео/документ/голосовое — рассылает его копию.
    """
    if message.from_user.id != config.ADMIN_ID:
        return
    users_count = len(load_users())
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton('✅ Отправить', callback_data='bc_send'),
        types.InlineKeyboardButton('❌ Отмена', callback_data='bc_cancel'),
    )

    reply = message.reply_to_message
    text = message.text[len('/broadcast'):].strip()
    if reply is not None:
        # Рассылка любого контента: копируем сообщение, на которое ответили
        pending_broadcasts[config.ADMIN_ID] = ('copy', reply.chat.id, reply.message_id)
        preview = reply.text or reply.caption or f'({reply.content_type})'
        preview = preview if len(preview) <= 200 else preview[:200] + '…'
        bot.send_message(
            message.chat.id,
            f'Разослать это сообщение ({reply.content_type}) '
            f'{users_count} пользователям?\n\n«{preview}»',
            reply_markup=keyboard,
        )
        return

    if not text:
        bot.send_message(
            message.chat.id,
            'Формат: /broadcast текст сообщения\n'
            'или /broadcast реплаем на фото/видео/документ',
        )
        return
    pending_broadcasts[config.ADMIN_ID] = text
    preview = text if len(text) <= 200 else text[:200] + '…'
    bot.send_message(
        message.chat.id,
        f'Разослать это {users_count} пользователям?\n\n«{preview}»',
        reply_markup=keyboard,
    )


@bot.callback_query_handler(func=lambda call: call.data in ('bc_send', 'bc_cancel'))
def broadcast_confirm(call):
    """Подтверждение/отмена рассылки по inline-кнопке."""
    if call.from_user.id != config.ADMIN_ID:
        bot.answer_callback_query(call.id, 'Это не твоя рассылка, фыр!')
        return
    bot.answer_callback_query(call.id)
    chat_id = call.message.chat.id
    if call.data == 'bc_cancel':
        pending_broadcasts.pop(config.ADMIN_ID, None)
        bot.send_message(chat_id, 'Рассылка отменена.')
        return

    pending = pending_broadcasts.pop(config.ADMIN_ID, None)
    if pending is None:
        bot.send_message(chat_id, 'Рассылка протухла — отправь /broadcast заново.')
        return

    users = load_users()
    sent, failed = 0, 0
    for user_id in users:
        try:
            if isinstance(pending, tuple):
                # ('copy', from_chat_id, message_id): копия любого контента
                _, from_chat_id, message_id = pending
                bot.copy_message(user_id, from_chat_id, message_id)
            else:
                bot.send_message(user_id, pending)
            sent += 1
        except Exception as error:
            failed += 1
            if _is_gone_user_error(error):
                remove_user(user_id)
            logger.warning(f'Рассылка: не доставлено {user_id}: {error}')
        time.sleep(0.05)  # ~20 сообщений/сек, лимит Telegram ~30/сек
    bot.send_message(
        chat_id,
        f'Рассылка завершена: отправлено {sent}, ошибок {failed}. Всего в базе: {len(users)}',
    )
    logger.info(f'Рассылка: отправлено {sent}, ошибок {failed}')


# ==== Обратная связь: /feedback + ответ админа реплаем ====

@bot.message_handler(commands=['feedback'])
def feedback(message):
    """Пересылает сообщение админу; админ отвечает реплаем — бот доставит."""
    chat_id = message.chat.id
    text = message.text[len('/feedback'):].strip()
    if not text:
        bot.send_message(chat_id, 'Формат: /feedback твоё сообщение')
        return
    if not config.ADMIN_ID:
        bot.send_message(chat_id, 'Фыр... админ не настроен, некуда передать:3')
        return
    try:
        sent = bot.send_message(
            config.ADMIN_ID,
            f'📩 Фидбек от {message.from_user.first_name} (chat_id {chat_id}):\n\n'
            f'{text}\n\n'
            'Ответь реплаем на это сообщение — я перешлю ответ.',
        )
    except Exception as error:
        logger.error(f'Фидбек не доставлен админу: {error}')
        bot.send_message(chat_id, 'Не удалось передать сообщение. Попробуй позже:3')
        return
    feedback_relay[sent.message_id] = chat_id
    bot.send_message(chat_id, 'Передал админу! Фыр 🦊')
