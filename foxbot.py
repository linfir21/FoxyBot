# foxbot/foxbot.py
"""FoxBot «Лисёночек» — точка входа.

Проект разбит на модули:
- config.py      — .env, константы, промпты, настроения
- core.py        — логирование и общий экземпляр TeleBot
- content.py     — фразы кнопок, приветствия, подписи, справочники
- storage.py     — SQLite и разделяемое состояние (истории, игры, рассылки)
- kimi.py        — запросы к ИИ и rate limiting
- keyboards.py   — все клавиатуры
- handlers/      — обработчики по темам (fox, chat, apps, fun, admin)

Этот модуль — фасад: импортирует всё нужное (тесты работают через foxbot.X)
и содержит main() с запуском бота.
"""
import random  # noqa: F401  (используется тестами через foxbot.random)
import requests  # noqa: F401  (подменяется в тестах через foxbot.requests)
import threading
import time  # noqa: F401  (используется тестами через foxbot.time)

import config
import handlers  # noqa: F401  регистрирует все обработчики при импорте

from config import *  # noqa: F401,F403  константы для удобства и тестов
from content import *  # noqa: F401,F403  фразы и справочники
from core import bot, logger
from keyboards import *  # noqa: F401,F403
from kimi import _ai_rate_ok, ask_ai, ask_ai_image, ask_ai_once  # noqa: F401
from storage import (  # noqa: F401
    _apply_mood,
    _get_mood,
    _is_gone_user_error,
    _load_history,
    _meta_get,
    _meta_set,
    _save_history,
    _set_mood,
    _system_prompt,
    _users_db,
    ai_requests,
    feedback_relay,
    games,
    get_stats,
    histories,
    last_ai_activity,
    load_users,
    migrate_users_json,
    pending_broadcasts,
    remove_user,
    save_user,
    track_message,
)

# Хендлеры и функции, к которым обращаются тесты (и другие модули)
from handlers.admin import (  # noqa: F401
    broadcast,
    broadcast_confirm,
    feedback,
    push_keyboard_to_all,
    schedule_keyboard_push_on_startup,
    stats,
    update_keyboards,
)
from handlers.apps import send_games  # noqa: F401
from handlers.chat import (  # noqa: F401
    handle_photo,
    handle_voice,
    mood_callback,
    mood_cmd,
    say_hi,
    summary_cmd,
    translate_cmd,
)
from handlers.fox import (  # noqa: F401
    _daily_fox_loop,
    _random_fox_fact,
    _send_daily_fox,
    forget_history,
    fox_fact,
    get_new_image,
    new_fox,
    start_daily_fox_thread,
    wake_up,
)
from handlers.fun import (  # noqa: F401
    _play_number_round,
    _rps_result,
    _start_number_game,
    games_callback,
    rps_callback,
    send_games_menu,
    stop_game,
    truth_callback,
    weather_by_location,
)
from telebot import types


def main():
    migrate_users_json()
    # Меню команд в интерфейсе Telegram (не критично, если не получилось)
    try:
        bot.set_my_commands([
            types.BotCommand('newfox', 'Прислать лисичку'),
            types.BotCommand('foxfact', 'Факт о лисах'),
            types.BotCommand('translate', 'Перевести текст'),
            types.BotCommand('summary', 'Короткий пересказ текста'),
            types.BotCommand('feedback', 'Написать админу'),
            types.BotCommand('mood', 'Настроение лисёнка'),
            types.BotCommand('forget', 'Забыть наш диалог'),
            types.BotCommand('games', 'Игры APK (описание и ссылка)'),
        ])
    except Exception as error:
        logger.warning(f'Не удалось зарегистрировать меню команд: {error}')
    # Ежедневная «лиса дня» всем пользователям (DAILY_FOX_HOUR:DAILY_FOX_MINUTE)
    start_daily_fox_thread()
    # После деплоя сам обновим клавиатуру у всех (бесшумно)
    schedule_keyboard_push_on_startup()
    if WEBHOOK_URL:
        logger.info(f'FoxBot запущен в режиме webhook: {WEBHOOK_URL}')
        bot.run_webhooks(
            listen='0.0.0.0',
            port=WEBHOOK_PORT,
            url_path=WEBHOOK_URL.rstrip('/').rsplit('/', 1)[-1],
            webhook_url=WEBHOOK_URL,
        )
    else:
        logger.info('FoxBot запущен в режиме polling')
        bot.polling(none_stop=True, timeout=30, long_polling_timeout=30)


if __name__ == '__main__':
    main()
