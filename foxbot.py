# foxbot/foxbot.py
import logging
import os
import requests

from dotenv import load_dotenv
from logging.handlers import RotatingFileHandler
from telebot import TeleBot, types


load_dotenv()

secret_token = os.getenv('TOKEN')
if not secret_token:
    raise ValueError(
        'Не найден токен бота! Убедитесь, что в файле .env есть переменная TOKEN'
    )

bot = TeleBot(token=secret_token)

# Настройка глобального логирования
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
)

# Настройка логгера для текущего файла с ротацией
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
handler = RotatingFileHandler(
    'foxbot.log', maxBytes=50000000, backupCount=5, encoding='utf-8'
)
formatter = logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
handler.setFormatter(formatter)
logger.addHandler(handler)

# API для случайных фото лис
URL = 'https://randomfox.ca/floof/'
# Fallback API (коты)
FALLBACK_URL = 'https://api.thecatapi.com/v1/images/search'

# ==== erazeChat 2048: раздача приложения и создание комнат ====
# Путь к APK на сервере (обновление = просто заменить этот файл)
APK_PATH = os.getenv('APK_PATH', 'erazechat_2048.apk')
# Куда бот стучится за кодом комнаты (Django)
DJANGO_API = os.getenv('DJANGO_API_URL', 'http://127.0.0.1:8000/api')

# --- AI-чат (Kimi / Moonshot) ---
MOONSHOT_KEY = os.getenv('MOONSHOT_API_KEY')
AI_URL = 'https://api.moonshot.ai/v1/chat/completions'
AI_MODEL = 'kimi-k2.6'  # дешёвая модель для чата; полный список — в доках
AI_HISTORY_LIMIT = 10   # сколько сообщений помнит (история = основные затраты)

# история диалогов: chat_id -> список сообщений
histories: dict[int, list] = {}


def ask_ai(chat_id: int, text: str) -> str:
    """Отправляет сообщение пользователя в Kimi API и возвращает ответ."""
    history = histories.setdefault(chat_id, [
        {'role': 'system', 'content':
         'Ты — Лисёночек, милый лис-ассистент в Telegram. '
         'Отвечай кратко, дружелюбно, по-русски, иногда фыркай,'
         'а иногда говори комплименты, как лисенок, лисенку'}
    ])
    history.append({'role': 'user', 'content': text})
    # режем историю: системный промпт + последние N сообщений
    history[:] = history[:1] + history[-AI_HISTORY_LIMIT:]

    response = requests.post(
        AI_URL,
        headers={'Authorization': f'Bearer {MOONSHOT_KEY}'},
        json={
            'model': AI_MODEL,
            'messages': history,
        },
        timeout=60,
    )
    response.raise_for_status()
    answer = response.json()['choices'][0]['message']['content']
    history.append({'role': 'assistant', 'content': answer})
    return answer

APK_CAPTION = (
    'Игра 2048 со скрытым зашифрованным чатом (E2E).\n\n'
    '1. Открой этот файл — Android попросит разрешить установку из Telegram (один раз)\n'
    '2. Установи и открой игру\n'
    '3. Нажми 5 раз на надпись «2048» — откроется скрытый чат\n'
    '4. Введи имя и код комнаты (код создаёт кнопка ниже ⬇️)\n\n'
    'Кто-то должен зайти в ту же комнату — только тогда чат заработает.'
)


def _rooms_keyboard() -> types.InlineKeyboardMarkup:
    """Кнопка создания комнаты под сообщением с APK."""
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        types.InlineKeyboardButton(
            'Создать комнату для чата 🗝️', callback_data='eraze_create_room'
        )
    )
    return keyboard


@bot.message_handler(commands=['app', 'game'])
def send_app(message):
    """Отправляет APK игры и кнопку создания комнаты."""
    chat_id = message.chat.id
    keyboard = _rooms_keyboard()
    if not os.path.exists(APK_PATH):
        bot.send_message(
            chat_id,
            'APK пока не загружен на сервер. Попробуй позже.',
            reply_markup=keyboard,
        )
        logger.warning(f'APK не найден по пути {APK_PATH}')
        return
    # Читаем файл при каждом запросе: замена файла = обновление для всех
    with open(APK_PATH, 'rb') as apk_file:
        bot.send_document(
            chat_id,
            apk_file,
            caption=APK_CAPTION,
            reply_markup=keyboard,
        )
    logger.info(f'Отправлен APK в чат {chat_id}')


@bot.callback_query_handler(func=lambda call: call.data == 'eraze_create_room')
def create_chat_room(call):
    """Создаёт комнату через Django API и присылает код."""
    chat_id = call.message.chat.id
    bot.answer_callback_query(call.id)
    try:
        response = requests.post(f'{DJANGO_API}/room/create/', timeout=10)
        response.raise_for_status()
        room_id = response.json().get('room_id')
    except Exception as error:
        logger.error(f'Не удалось создать комнату: {error}')
        bot.send_message(chat_id, 'Не удалось создать комнату. Сервер спит, попробуй позже.')
        return
    if not room_id:
        bot.send_message(chat_id, 'Сервер вернул пустой код комнаты.')
        return
    bot.send_message(
        chat_id,
        f'Код комнаты: <b>{room_id}</b>\n\n'
        'Отправь его тому, с кем хочешь чатиться, '
        'и оба введите этот код в игре (5 тапов по «2048»).',
        parse_mode='HTML',
    )
    logger.info(f'Создана комната {room_id} для чата {chat_id}')


def get_new_image():
    """Получает ссылку на случайное фото лисы."""
    try:
        response = requests.get(URL, timeout=10)
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
            response = requests.get(FALLBACK_URL, timeout=10)
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
            'Не удалось получить фото. Попробуйте позже.',
        )
        logger.warning(f'Не удалось отправить фото в чат {chat_id}')


@bot.message_handler(commands=['forget'])
def forget_history(message):
    """Обработчик команды /forget — сбрасывает историю диалога с ИИ."""
    histories.pop(message.chat.id, None)
    bot.send_message(message.chat.id, 'Фыр! Я всё забыл. Начнём с чистого листа:3')
    logger.info(f'История ИИ очищена для чата {message.chat.id}')


@bot.message_handler(commands=['start'])
def wake_up(message):
    """Обработчик команды /start — приветствие и первая лиса."""
    chat_id = message.chat.id
    name = message.from_user.first_name
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    button_fox = types.KeyboardButton('Лисёночек:3')
    button_sun = types.KeyboardButton('Ты моё солнышко ☀️')
    button_hug = types.KeyboardButton('Обнимашки 🤗')
    button_mur = types.KeyboardButton('Фыр ❤️')
    button_spicy = types.KeyboardButton('Горяченькое 🔥')
    button_game = types.KeyboardButton('🎮 Игра 2048 + чат')
    keyboard.add(button_fox, button_sun, button_hug, button_mur, button_spicy, button_game)

    bot.send_message(
        chat_id=chat_id,
        text=f'Привет, {name}! Я твой Лисёночек — бот, который любит лис. '
             f'Посмотри, какую лисичку я тебе нашёл!',
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


# Нежные фразы для кнопок
TENDER_PHRASES = {
    'Ты моё солнышко ☀️': [
        'Ты моё солнышко, без тебя мой мир холодный и серый! ☀️',
        'Солнышко моё, ты согреваешь меня своей улыбкой!',
        'Ты ярче любого солнышка, люблю тебя! ☀️❤️',
        'Когда ты рядом, даже тучки разбегаются! ☀️',
        'Ты — мой личный источник тепла и света! 🔥',
        'С тобой всегда лето в душе, даже если за окном метель! ☀️',
        'Ты самый замечательный человечек во всей вселенной! ✨❤️',
        'Ты как лучший закат — красивый, тёплый, незабываемый! 🌇✨',
        'Твой свет освещает даже самые тёмные уголки моего мира! ✨☀️',
        'Я бы смотрел на тебя, как на рассвет, вечно! 🌅',
        'Ты такой яркий, что мне нужны очки от любви! 😎❤️',
        'Моё солнышко, ты заставляешь моё сердце петь! ☀️🎶',
    ],
    'Обнимашки 🤗': [
        'Крепко-крепко обнимаю тебя! 🤗🤗🤗',
        'Обнимаю так, что никакие проблемы не пролезут между нами!',
        'Посылаю тебе воздушный обнимашку! Лови! 🤗💕',
        'Заворачиваю тебя в пледик и обнимаю до упаду! 🛋️🤗',
        'Мои руки скучают по тебе, приходи на обнимашки! 🤗',
        'Обнимаю тебя так крепко, что слышу твоё сердцебиение! 💓',
        'Шлю тебе сквозь-вселенский обнимашку! Лови! 🌌🤗',
        'В мире миллионы людей, но только ты — мой самый особенный! ✨',
        'Обнимаю тебя с ног до головы, ты мой сладкий! 🤗❤️',
        'Ты заслуживаешь тысячу обнимашек в день! Вот первая! 🤗',
        'Представь, что я рядом и крепко обнимаю! Не отпускаю! 🤗',
    ],
    'Фыр ❤️': [
        'Фыр-фыр, ты мой сладенький лисёнок! ❤️🦊',
        'Фыр ❤️ Люблю тебя больше всех лисичек на свете!',
        'Фыр-фыр, давай свернёмся клубочком и будем дремать вместе! 🦊❤️',
        'Мой хвостик виляет только для тебя! Фыр! 🦊',
        'Фыр! Ты пахнешь так вкусно, что я хочу тебя обнюхать! 👃❤️',
        'Фыр! Ты пахнешь так вкусно, что я хочу тебя облизать! 🤤❤️',
        'Я бы подарил тебе целую гору печенек! Фыр-фыр! 🍪🦊',
        'Ты моя самая любимая норка! Фыр! 🕳️❤️',
        'Фыр-фыр, мои ушки настроены только на твой голос! 🦊👂',
        'Если бы я был лисой, я бы украл твоё сердце! Фыр! 🦊❤️',
        'Фыр! Ты заставляешь мой носик мокреть от волнения! 🦊💧❤️',
    ],
    'Горяченькое 🔥': [
        'Если бы я был лисом, я бы уже утащил тебя в норку... и не для сна 😏🦊',
        'Мой хвост шевелится не только когда я доволен...и не только хвост 🔥',
        'Хочу, чтобы ты проверил, мягкие ли у меня ушки... ручками 😏',
        'Мой носик уже всё обнюхал... теперь хочу изучить тебя язычком, медленно и тщательно:3',
        'У меня для тебя есть подарок... но снимать его нужно медленно 🎀😏',
        'Я такая голодная лиса... и ты выглядишь очень аппетитно 🔥🦊',
        'Представляю, как ты гладишь мой мех... а я твой 😏',
        'Может, перестанем фыркать и начнём тяжело дышать? 🔥💋',
        'Может, я залезу к тебе под одеялочко и покажу, чему лисички учатся по ночам?',
        'Ты заставляешь моё сердце биться чаще... и не только сердце 💓🔥',
    ],
}


@bot.message_handler(content_types=['text'])
def say_hi(message):
    """Обработчик текстовых сообщений."""
    chat = message.chat
    chat_id = chat.id
    text = message.text
    logger.info(f'Получено сообщение от {chat_id}: {text}')

    if text in TENDER_PHRASES:
        import random
        phrase = random.choice(TENDER_PHRASES[text])
        bot.send_message(chat_id=chat_id, text=phrase)
        logger.info(f'Отправлена нежность в чат {chat_id}')
        return

    if text == 'Лисёночек:3':
        new_fox(message)
        return

    if text in ('🎮 Игра 2048 + чат', '/app', '/game'):
        send_app(message)
        return

    # Всё остальное уходит в ИИ-чат
    if not MOONSHOT_KEY:
        bot.send_message(
            chat_id=chat_id,
            text=('Привет, я FoxBot! Нажми Лисёночек:3, и я покажу тебе лису. '
                  'Или нажми на кнопочку, и я покажу тебе любовь:3'),
        )
        return

    bot.send_chat_action(chat_id, 'typing')
    try:
        answer = ask_ai(chat_id, text)
    except Exception as error:
        logger.error(f'Ошибка AI-запроса: {error}')
        answer = 'Фыр... я запутался в мыслях. Попробуй ещё раз:3'
    # лимит Telegram — 4096 символов на сообщение
    for i in range(0, len(answer), 4096):
        bot.send_message(chat_id, answer[i:i + 4096])


def main():
    logger.info('FoxBot запущен')
    bot.polling(none_stop=True)


if __name__ == '__main__':
    main()
