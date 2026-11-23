"""Все клавиатуры FoxBot: главная reply-клавиатура и inline-меню.

Кнопки легко переставлять/переименовывать — тексты фраз живут в content.py,
а обработка нажатий — в handlers/. Клавиатуры и кнопки разнесены по файлам,
чтобы было проще ориентироваться.
"""
from telebot import types

from content import MOOD_LABELS, RPS_CHOICES


def build_main_keyboard() -> types.ReplyKeyboardMarkup:
    """Главная reply-клавиатура. Отправка любого сообщения с этой разметкой
    обновляет кнопки у пользователя — старая клавиатура заменяется автоматически."""
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    keyboard.add(
        types.KeyboardButton('Лисёночек:3'),
        types.KeyboardButton('Фыр ❤️'),
        types.KeyboardButton('Горяченькое 🔥'),
        types.KeyboardButton('🎮 Игры APK'),
        types.KeyboardButton('Лисий факт 🦊📚'),
        # Telegram пришлёт геопозицию пользователя — бот ответит погодой
        types.KeyboardButton('🌤 Погода', request_location=True),
        types.KeyboardButton('🎮 Игры'),
    )
    return keyboard


def _mood_keyboard(current: str | None) -> types.InlineKeyboardMarkup:
    """Inline-меню выбора настроения; текущее отмечено галочкой."""
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    buttons = [
        types.InlineKeyboardButton(
            f'{"✅ " if mood == current else ""}{label}',
            callback_data=f'mood:{mood}',
        )
        for mood, label in MOOD_LABELS.items()
    ]
    buttons.append(types.InlineKeyboardButton(
        f'{"✅ " if current is None else ""}🦊 Обычное', callback_data='mood:none'
    ))
    keyboard.add(*buttons)
    return keyboard


# ==== Игры: меню выбора и клавиатуры раундов ====

def _games_keyboard() -> types.InlineKeyboardMarkup:
    """Меню выбора игры (под reply-кнопкой «🎮 Игры»)."""
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        types.InlineKeyboardButton('🎲 Угадай число', callback_data='game:number'),
        types.InlineKeyboardButton(
            '🦊 Хвостик-Мордашка-Лапка', callback_data='game:rps'
        ),
        types.InlineKeyboardButton('❓ Правда или ложь', callback_data='game:truth'),
    )
    return keyboard


def _stop_game_button() -> types.InlineKeyboardButton:
    "Кнопка выхода из игры — одинаковая во всех игровых клавиатурах."
    return types.InlineKeyboardButton('Хватит играть ❌', callback_data='game:stop')


def _rps_keyboard() -> types.InlineKeyboardMarkup:
    """Выбор хода в «Хвостик-Мордашка-Лапка»."""
    keyboard = types.InlineKeyboardMarkup(row_width=3)
    keyboard.add(
        *[
            types.InlineKeyboardButton(label, callback_data=f'rps:{key}')
            for key, label in RPS_CHOICES.items()
        ]
    )
    keyboard.add(_stop_game_button())
    return keyboard


def _truth_keyboard() -> types.InlineKeyboardMarkup:
    """Ответ в «Правда или ложь»."""
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton('✅ Правда', callback_data='truth:true'),
        types.InlineKeyboardButton('❌ Ложь', callback_data='truth:false'),
    )
    keyboard.add(_stop_game_button())
    return keyboard


def _truth_next_keyboard() -> types.InlineKeyboardMarkup:
    """Следующий факт в «Правда или ложь»."""
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        types.InlineKeyboardButton('Дальше ➡️', callback_data='truth:next'),
        _stop_game_button(),
    )
    return keyboard
