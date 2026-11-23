"""Развлечения: погода по геопозиции и игры («🎮 Игры»):
Угадай число, Хвостик-Мордашка-Лапка, Правда или ложь."""
import random
import requests

import config
import content

from core import bot, logger
from keyboards import (
    _games_keyboard,
    _rps_keyboard,
    _truth_keyboard,
    _truth_next_keyboard,
)
from storage import games, save_user, track_message


@bot.message_handler(content_types=['location'])
def weather_by_location(message):
    """Погода по геопозиции: кнопка «🌤 Погода» присылает location."""
    chat_id = message.chat.id
    save_user(chat_id)
    track_message(chat_id)
    lat, lon = message.location.latitude, message.location.longitude
    bot.send_chat_action(chat_id, 'typing')
    try:
        response = requests.get(
            config.WEATHER_URL,
            params={
                'latitude': lat,
                'longitude': lon,
                'current': 'temperature_2m,apparent_temperature,'
                           'relative_humidity_2m,weather_code,wind_speed_10m',
                'daily': 'temperature_2m_max,temperature_2m_min,'
                         'precipitation_probability_max',
                'timezone': 'auto',
                'forecast_days': 1,
            },
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        current, daily = data['current'], data['daily']
        description, emoji = content.WEATHER_CODES.get(
            current.get('weather_code', 0), ('непонятно что', '🦊')
        )
        bot.send_message(
            chat_id,
            f'{emoji} Сейчас {description}\n\n'
            f'🌡 {current["temperature_2m"]}°C '
            f'(ощущается как {current["apparent_temperature"]}°C)\n'
            f'💨 Ветер: {current["wind_speed_10m"]} км/ч\n'
            f'💧 Влажность: {current["relative_humidity_2m"]}%\n\n'
            f'📅 Сегодня: {daily["temperature_2m_min"][0]}…'
            f'{daily["temperature_2m_max"][0]}°C, '
            f'осадки {daily["precipitation_probability_max"][0]}%',
        )
        logger.info(f'Погода для {chat_id}: {lat}, {lon}')
    except Exception as error:
        logger.error(f'Ошибка погоды: {error}')
        bot.send_message(chat_id, 'Фыр... метеослужба молчит. Попробуй позже:3')


# ==== Меню игр ====

def send_games_menu(chat_id: int):
    "Показывает меню выбора игры (reply-кнопка «🎮 Игры»)."
    bot.send_message(
        chat_id,
        content.GAMES_MENU_TEXT,
        parse_mode='HTML',
        reply_markup=_games_keyboard(),
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith('game:'))
def games_callback(call):
    """Старт игры по кнопке меню или выход по «Хватит играть»."""
    chat_id = call.message.chat.id
    action = call.data.split(':', 1)[1]
    bot.answer_callback_query(call.id)
    if action == 'stop':
        if games.pop(chat_id, None) is not None:
            bot.send_message(chat_id, 'Ладно-ладно, заканчиваем! Фыр 🦊')
        else:
            bot.send_message(chat_id, 'Мы сейчас ни во что не играем:3')
        return
    if action == 'number':
        _start_number_game(chat_id)
    elif action == 'rps':
        games[chat_id] = {'game': 'rps', 'wins': 0, 'losses': 0, 'draws': 0}
        bot.send_message(
            chat_id, content.RPS_RULES_TEXT, reply_markup=_rps_keyboard()
        )
        logger.info(f'Хвостик-Мордашка-Лапка: старт в чате {chat_id}')
    elif action == 'truth':
        games[chat_id] = {
            'game': 'truth',
            'deck': content.shuffled_truth_deck(),
            'pos': 0,
        }
        _send_truth_statement(chat_id)
        logger.info(f'Правда или ложь: старт в чате {chat_id}')


@bot.message_handler(commands=['stopgame'])
def stop_game(message):
    "Сдаётся в любой активной игре."
    if games.pop(message.chat.id, None) is not None:
        bot.send_message(message.chat.id, 'Сдаёшься? В другой раз повезёт, фыр! 🦊')
    else:
        bot.send_message(message.chat.id, 'Мы сейчас ни во что не играем:3')


# ==== Угадай число ====

def _start_number_game(chat_id: int):
    games[chat_id] = {'game': 'number', 'number': random.randint(1, 100), 'tries': 0}
    bot.send_message(
        chat_id,
        '🎲 Я загадала число от 1 до 100. Попробуй угадать!\n'
        '(напиши /stopgame, чтобы сдаться)',
    )


def _play_number_round(chat_id: int, text: str) -> bool:
    """Ход «Угадай числа»: True — ход обработан (игра есть и это число)."""
    state = games.get(chat_id)
    if not state or state.get('game') != 'number':
        return False
    if not text.isdigit():
        bot.send_message(
            chat_id,
            'Напиши число от 1 до 100 или /stopgame, чтобы сдаться:3',
        )
        return True
    state['tries'] += 1
    guess = int(text)
    if guess < state['number']:
        bot.send_message(chat_id, 'Больше! ⬆️')
    elif guess > state['number']:
        bot.send_message(chat_id, 'Меньше! ⬇️')
    else:
        games.pop(chat_id)
        bot.send_message(
            chat_id,
            f'🎉 Угадал за {state["tries"]} попыток! Умница моя, фыр! 🦊',
        )
    return True


# ==== Хвостик-Мордашка-Лапка (лисьи «камень-ножницы-бумага») ====
# Хвост хлещет морду, морда кусает лапу, лапа бьёт хвост.

def _rps_result(player: str, fox: str) -> str:
    """Исход раунда: 'player' | 'fox' | 'draw'. Чистая логика — тестируется."""
    if player == fox:
        return 'draw'
    return 'player' if content.RPS_BEATS[player] == fox else 'fox'


@bot.callback_query_handler(func=lambda call: call.data.startswith('rps:'))
def rps_callback(call):
    """Раунд «Хвостик-Мордашка-Лапка»: считаем счёт и предлагаем ещё раз."""
    chat_id = call.message.chat.id
    state = games.get(chat_id)
    if not state or state.get('game') != 'rps':
        bot.answer_callback_query(call.id, 'Мы сейчас не играем! Жми «🎮 Игры» 🦊')
        return
    player = call.data.split(':', 1)[1]
    fox = random.choice(list(content.RPS_CHOICES))
    result = _rps_result(player, fox)
    if result == 'player':
        state['wins'] += 1
        phrase = random.choice(content.RPS_LOSE)  # проиграла я — хвали игрока
    elif result == 'fox':
        state['losses'] += 1
        phrase = random.choice(content.RPS_WIN)
    else:
        state['draws'] += 1
        phrase = random.choice(content.RPS_DRAW)
    bot.answer_callback_query(call.id)
    bot.send_message(
        chat_id,
        f'Ты выбрал: {content.RPS_CHOICES[player]}\n'
        f'Я выбрала: {content.RPS_CHOICES[fox]}\n\n'
        f'{phrase}\n\n'
        f'Счёт: ты {state["wins"]} — {state["losses"]} я, ничьих {state["draws"]}',
        reply_markup=_rps_keyboard(),
    )


# ==== Правда или ложь: 5 фактов про лис, не повторяются ====

def _send_truth_statement(chat_id: int):
    "Задаёт текущий факт из колоды."
    state = games[chat_id]
    fact = content.TRUTH_OR_LIE[state['deck'][state['pos']]]
    bot.send_message(
        chat_id,
        content.TRUTH_INTRO.format(text=fact['text']),
        parse_mode='HTML',
        reply_markup=_truth_keyboard(),
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith('truth:'))
def truth_callback(call):
    """Ответ «правда/ложь» или переход к следующему факту."""
    chat_id = call.message.chat.id
    state = games.get(chat_id)
    if not state or state.get('game') != 'truth':
        bot.answer_callback_query(call.id, 'Мы сейчас не играем! Жми «🎮 Игры» 🦊')
        return
    action = call.data.split(':', 1)[1]
    bot.answer_callback_query(call.id)
    if action == 'next':
        _next_truth(chat_id)
        return
    fact = content.TRUTH_OR_LIE[state['deck'][state['pos']]]
    guess_true = action == 'true'
    correct = guess_true == fact['true']
    verdict = 'Правда!' if fact['true'] else 'Ложь!'
    praise = random.choice(content.TRUTH_CORRECT if correct else content.TRUTH_WRONG)
    bot.send_message(
        chat_id,
        f'{verdict} {fact["why"]}\n\n{praise}',
        reply_markup=_truth_next_keyboard(),
    )


def _next_truth(chat_id: int):
    "Следующий факт; колода кончилась — перетасовываем (без повторов подряд)."
    state = games[chat_id]
    state['pos'] += 1
    if state['pos'] >= len(state['deck']):
        state['deck'] = content.reshuffled_truth_deck(state['deck'])
        state['pos'] = 0
        bot.send_message(chat_id, 'Ой, факты закончились! Перемешаю и начну сначала~ 🦊')
    _send_truth_statement(chat_id)
