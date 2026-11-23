"""Лисы во всех проявлениях: /start, /newfox, ежедневная «лиса дня», /foxfact, /forget."""
import random
import threading
import time

import requests

import config
import content

from core import bot, logger
from keyboards import build_main_keyboard
from kimi import ask_ai_once
from storage import (
    _is_gone_user_error,
    _meta_get,
    _meta_set,
    _save_history,
    histories,
    last_ai_activity,
    load_users,
    remove_user,
    save_user,
)


def get_new_image():
    """Получает ссылку на случайное фото лисы."""
    try:
        response = requests.get(content.URL, timeout=10)
        response.raise_for_status()
        data = response.json()
        image_url = data.get('image')
        if image_url:
            logger.info(f'Получено фото лисы: {image_url}')
            return image_url
        raise ValueError('В ответе API нет ключа image')
    except Exception as error:
        logger.error(f'Ошибка при запросе к основному API лис: {error}')
        try:
            response = requests.get(content.FALLBACK_URL, timeout=10)
            response.raise_for_status()
            data = response.json()
            fallback_image = data[0].get('url')
            logger.info(f'Использован fallback (кот): {fallback_image}')
            return fallback_image
        except Exception as fallback_error:
            logger.critical(f'Fallback API тоже недоступен: {fallback_error}')
            return None


@bot.message_handler(commands=['newfox'])
def new_fox(message):
    """Обработчик команды /newfox — отправляет фото лисы."""
    chat_id = message.chat.id
    image_url = get_new_image()
    if image_url:
        bot.send_photo(chat_id, image_url)
        logger.info(f'Отправлено фото в чат {chat_id}')
    else:
        bot.send_message(
            chat_id,
            'Не удалось достать фото... Попробуй позже, ладно? 🦊',
        )
        logger.warning(f'Не удалось отправить фото в чат {chat_id}')


@bot.message_handler(commands=['start'])
def wake_up(message):
    """Обработчик команды /start — приветствие и первая лиса."""
    chat_id = message.chat.id
    save_user(chat_id)
    name = message.from_user.first_name
    keyboard = build_main_keyboard()

    bot.send_message(
        chat_id=chat_id,
        text=f'Привет, {name}! Я твоя Лисёночек — девушка-лиса, которая обожает '
             f'лис... и тебя 🦊 Посмотри, какую лисичку я тебе нашла!',
        reply_markup=keyboard,
    )
    logger.info(f'Пользователь {name} ({chat_id}) запустил бота')

    image_url = get_new_image()
    if image_url:
        bot.send_photo(chat_id, image_url)
    else:
        bot.send_message(
            chat_id,
            'К сожалению, не удалось загрузить фото. Нажми /newfox ещё раз!',
        )


@bot.message_handler(commands=['forget'])
def forget_history(message):
    """Обработчик команды /forget — сбрасывает историю диалога с ИИ."""
    histories.pop(message.chat.id, None)
    last_ai_activity.pop(message.chat.id, None)
    _save_history(message.chat.id)  # удаляет запись из SQLite
    bot.send_message(message.chat.id, 'Фыр! Я всё забыла. Начнём с чистого листа:3')
    logger.info(f'История ИИ очищена для чата {message.chat.id}')


def _random_fox_fact() -> str | None:
    "Один случайный факт о лисах через ИИ (None при ошибке)."
    # Тема + случайное число в промпте: модель детерминирована и без
    # этого выдаёт один и тот же факт про магнитное поле Земли
    topic = random.choice([
        'строение тела и хвост', 'лисята и семья', 'звуки и общение',
        'питание', 'зимняя жизнь', 'разные виды лис', 'скорость и охота',
        'отношения с людьми', 'способности чувств', 'самые необычные виды',
    ])
    return ask_ai_once(
        f'Расскажи один короткий интересный факт о лисах, '
        f'тема: {topic}. '
        f'Только сам факт, без вступлений, до 300 символов. '
        f'Случайное число для разнообразия: {random.randint(1, 10**9)}.'
    )


@bot.message_handler(commands=['foxfact'])
def fox_fact(message):
    """Обработчик команды /foxfact — случайный факт о лисах через ИИ."""
    chat_id = message.chat.id
    if not config.MOONSHOT_KEY:
        bot.send_message(chat_id, 'Фыр... без API-ключа я не знаю фактов:3')
        return
    bot.send_chat_action(chat_id, 'typing')
    try:
        fact = _random_fox_fact()
    except Exception as error:
        logger.error(f'Ошибка запроса факта: {error}')
        fact = None
    if fact:
        bot.send_message(chat_id, f'🦊 Лисий факт: {fact}')
    else:
        bot.send_message(chat_id, 'Фыр... факты закончились, попробуй позже:3')


# ==== Ежедневная «лиса дня» ====
# Всем, кто хоть раз написал боту, в DAILY_FOX_HOUR:DAILY_FOX_MINUTE приходит
# фото лисы + нежная фраза (и лисий факт, если настроен ИИ). Без подписок и команд.

def _send_daily_fox():
    "Рассылает лису дня всем пользователям. Заблокировавших — удаляет из базы."
    users = load_users()
    if not users:
        return
    image_url = get_new_image()
    caption = random.choice(content.DAILY_GREETINGS)
    if config.MOONSHOT_KEY:
        try:
            fact = _random_fox_fact()
            if fact:
                caption += f'\n\n🦊 {fact}'
        except Exception as error:
            logger.error(f'Лиса дня: не удалось получить факт: {error}')
    sent, failed = 0, 0
    for user_id in users:
        try:
            if image_url:
                bot.send_photo(user_id, image_url, caption=caption)
            else:
                bot.send_message(user_id, caption)
            sent += 1
        except Exception as error:
            failed += 1
            if _is_gone_user_error(error):
                remove_user(user_id)
            logger.warning(f'Лиса дня: не доставлено {user_id}: {error}')
        time.sleep(0.05)  # ~20 сообщений/сек, лимит Telegram ~30/сек
    logger.info(f'Лиса дня: отправлено {sent}, ошибок {failed}')


def _daily_fox_loop():
    """Фоновый поток: раз в сутки шлёт лису всем. Дата последней отправки — в
    meta, так что рестарт в тот же день не приведёт к повторной рассылке, а
    запуск после назначенного времени — к немедленной (catch-up)."""
    while True:
        now = time.localtime()
        today = time.strftime('%Y-%m-%d')
        due = (now.tm_hour, now.tm_min) >= (config.DAILY_FOX_HOUR, config.DAILY_FOX_MINUTE)
        if due and _meta_get('last_daily_fox') != today:
            # фиксируем до отправки: при падении посреди рассылки не спамим повтором
            _meta_set('last_daily_fox', today)
            try:
                _send_daily_fox()
            except Exception as error:
                logger.error(f'Лиса дня: рассылка упала: {error}')
        time.sleep(30)


def start_daily_fox_thread():
    "Запускает фоновую рассылку «лиса дня» (вызывается из main)."
    threading.Thread(target=_daily_fox_loop, daemon=True).start()
