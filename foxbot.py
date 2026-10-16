# foxbot/foxbot.py
import json
import logging
import os
import random
import sqlite3
import time
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
APK_PATH = os.getenv('APK_PATH', '2048.apk')
# ==== Азбука для малышей: обучающая игра (прототип) ====
ABC_APK_PATH = os.getenv('ABC_APK_PATH', 'app-release.apk')
# Куда бот стучится за кодом комнаты (Django)
DJANGO_API = os.getenv('DJANGO_API_URL', 'http://127.0.0.1:8000/api')
# Токен бэкенда (заголовок X-Bot-Token) — должен совпадать с ERAZE_BOT_TOKEN на сервере
BOT_API_TOKEN = os.getenv('BOT_API_TOKEN')

# --- AI-чат (Kimi / Moonshot) ---
MOONSHOT_KEY = os.getenv('MOONSHOT_API_KEY')
AI_URL = 'https://api.moonshot.ai/v1/chat/completions'
AI_MODEL = 'kimi-k2.6'  # дешёвая модель для чата; полный список — в доках
AI_HISTORY_LIMIT = 10   # сколько сообщений помнит (история = основные затраты)
AI_HISTORY_TTL_HOURS = float(os.getenv('AI_HISTORY_TTL_HOURS', '12'))  # авто-/forget

# история диалогов: chat_id -> список сообщений
histories: dict[int, list] = {}
# время последнего сообщения: для авто-забывания истории (TTL)
last_ai_activity: dict[int, float] = {}

# --- Webhook (необязательно): если задан — бот работает через webhook вместо polling ---
WEBHOOK_URL = os.getenv('WEBHOOK_URL')  # например https://your-domain:8443/foxbot
WEBHOOK_PORT = int(os.getenv('WEBHOOK_PORT', '8443'))

# --- Рассылка: все, кто общался с ботом ---
USERS_DB = os.getenv('USERS_DB', 'users.db')  # SQLite — переживает рестарты, без перезаписи всего файла
USERS_FILE = 'users.json'  # старое хранилище; мигрируется в БД при старте
ADMIN_ID = int(os.getenv('ADMIN_ID', '0'))  # твой Telegram ID (узнать: @userinfobot)


def _users_db() -> sqlite3.Connection:
    "Открывает БД пользователей (короткое соединение на операцию — безопасно для тредов)."
    conn = sqlite3.connect(USERS_DB)
    conn.execute(
        'CREATE TABLE IF NOT EXISTS users ('
        'chat_id INTEGER PRIMARY KEY, '
        'added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)'
    )
    conn.execute(
        'CREATE TABLE IF NOT EXISTS daily_stats '
        '(day TEXT PRIMARY KEY, messages INTEGER)'
    )
    return conn


def load_users() -> set:
    "Читает сохранённые chat_id из БД."
    with _users_db() as conn:
        return {row[0] for row in conn.execute('SELECT chat_id FROM users')}


def save_user(chat_id: int):
    "Добавляет chat_id в список для рассылки."
    with _users_db() as conn:
        cursor = conn.execute(
            'INSERT OR IGNORE INTO users (chat_id) VALUES (?)', (chat_id,)
        )
        if cursor.rowcount:
            logger.info(f'Новый пользователь для рассылки: {chat_id}')


def track_message():
    "Считает сообщения за сегодня (для /stats)."
    day = time.strftime('%Y-%m-%d')
    with _users_db() as conn:
        conn.execute(
            'CREATE TABLE IF NOT EXISTS daily_stats '
            '(day TEXT PRIMARY KEY, messages INTEGER)'
        )
        conn.execute(
            'INSERT INTO daily_stats (day, messages) VALUES (?, 1) '
            'ON CONFLICT(day) DO UPDATE SET messages = messages + 1',
            (day,),
        )


def get_stats() -> tuple[int, int]:
    "Возвращает (всего пользователей, сообщений сегодня)."
    day = time.strftime('%Y-%m-%d')
    with _users_db() as conn:
        total_users = conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]
        row = conn.execute(
            'SELECT messages FROM daily_stats WHERE day = ?', (day,)
        ).fetchone()
    return total_users, row[0] if row else 0


def migrate_users_json():
    "Одноразово переносит users.json (старый формат) в SQLite."
    if not os.path.exists(USERS_FILE):
        return
    try:
        with open(USERS_FILE, encoding='utf-8') as f:
            old_users = set(json.load(f))
    except (json.JSONDecodeError, OSError) as error:
        logger.warning(f'Не удалось прочитать {USERS_FILE}: {error}')
        return
    with _users_db() as conn:
        conn.executemany(
            'INSERT OR IGNORE INTO users (chat_id) VALUES (?)',
            [(chat_id,) for chat_id in old_users],
        )
    os.replace(USERS_FILE, USERS_FILE + '.bak')
    logger.info(f'Мигрировано {len(old_users)} пользователей из {USERS_FILE} в {USERS_DB}')


def ask_ai(chat_id: int, text: str) -> str:
    """Отправляет сообщение пользователя в Kimi API и возвращает ответ."""
    # авто-/forget: история протухает после AI_HISTORY_TTL_HOURS неактивности
    last = last_ai_activity.get(chat_id, 0)
    if time.time() - last > AI_HISTORY_TTL_HOURS * 3600:
        histories.pop(chat_id, None)
    last_ai_activity[chat_id] = time.time()
    history = histories.setdefault(chat_id, [
        {'role': 'system', 'content':
         'Ты — Лисёночек, милый лис-ассистент в Telegram. '
         'Отвечай кратко, дружелюбно, по-русски, иногда фыркай,'
         'а иногда говори комплименты, как лисенок, лисенку'}
    ])
    history.append({'role': 'user', 'content': text})
    # режем историю: системный промпт + последние N сообщений
    # (history[1:], а не history, иначе системный промпт продублируется)
    history[:] = history[:1] + history[1:][-AI_HISTORY_LIMIT:]

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


def ask_ai_once(prompt: str, max_tokens: int = 2048) -> str:
    """Одноразовый запрос к ИИ без истории и системного промпта."""
    response = requests.post(
        AI_URL,
        headers={'Authorization': f'Bearer {MOONSHOT_KEY}'},
        json={
            'model': AI_MODEL,
            'messages': [{'role': 'user', 'content': prompt}],
            'max_tokens': max_tokens,
            # temperature не трогаем: kimi-k2.6 принимает только значение 1
        },
        timeout=60,
    )
    response.raise_for_status()
    return response.json()['choices'][0]['message']['content'].strip()

APK_CAPTION = (
    'Игра 2048 со скрытым зашифрованным чатом (E2E).\n\n'
    '1. Открой этот файл — Android попросит разрешить установку из Telegram (один раз)\n'
    '2. Установи и открой игру\n'
    '3. Нажми 5 раз на надпись «2048» — откроется скрытый чат\n'
    '4. Введи имя и код комнаты (код создаёт кнопка ниже ⬇️)\n\n'
    'Кто-то должен зайти в ту же комнату — только тогда чат заработает.'
)

ABC_CAPTION = (
    'Обучающая игра «Азбука» для детей (прототип).\n\n'
    'Ребёнок учит буквы алфавита в игровой форме: знакомится с каждой буквой, '
    'повторяет её звук и подбирает слова на эту букву.\n\n'
    '1. Открой этот файл — Android попросит разрешить установку из Telegram (один раз)\n'
    '2. Установи и открой игру\n'
    '3. Выбери букву и проходи занятие вместе с малышом\n\n'
    'Прототип: часть букв может отсутствовать, а интерфейс меняться.'
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


def _send_apk(message, apk_path: str, caption: str, keyboard=None):
    """Отправляет APK-файл с описанием; при отсутствии файла — сообщение об этом."""
    chat_id = message.chat.id
    if not os.path.exists(apk_path):
        bot.send_message(
            chat_id,
            'APK пока не загружен на сервер. Попробуй позже.',
            reply_markup=keyboard,
        )
        logger.warning(f'APK не найден по пути {apk_path}')
        return
    # Читаем файл при каждом запросе: замена файла = обновление для всех
    with open(apk_path, 'rb') as apk_file:
        bot.send_document(
            chat_id,
            apk_file,
            caption=caption,
            reply_markup=keyboard,
        )
    logger.info(f'Отправлен {apk_path} в чат {chat_id}')


@bot.message_handler(commands=['app', 'game'])
def send_app(message):
    """Отправляет APK игры и кнопку создания комнаты."""
    _send_apk(message, APK_PATH, APK_CAPTION, keyboard=_rooms_keyboard())


@bot.message_handler(commands=['abc', 'alphabet'])
def send_alphabet_app(message):
    """Отправляет APK обучающей игры «Азбука» для детей."""
    _send_apk(message, ABC_APK_PATH, ABC_CAPTION)


@bot.callback_query_handler(func=lambda call: call.data == 'eraze_create_room')
def create_chat_room(call):
    """Создаёт комнату через Django API и присылает код."""
    chat_id = call.message.chat.id
    bot.answer_callback_query(call.id)
    if not BOT_API_TOKEN:
        logger.error('BOT_API_TOKEN не задан в .env')
        bot.send_message(chat_id, 'Бот не настроен (нет токена API). Сообщи админу.')
        return
    try:
        response = requests.post(
            f'{DJANGO_API}/room/create/',
            headers={'X-Bot-Token': BOT_API_TOKEN},
            timeout=10,
        )
        if response.status_code == 403:
            logger.error('Бэкенд отклонил токен (403)')
            bot.send_message(chat_id, 'Ошибка авторизации на сервере. Сообщи админу.')
            return
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
    last_ai_activity.pop(message.chat.id, None)
    bot.send_message(message.chat.id, 'Фыр! Я всё забыл. Начнём с чистого листа:3')
    logger.info(f'История ИИ очищена для чата {message.chat.id}')


@bot.message_handler(commands=['foxfact'])
def fox_fact(message):
    """Обработчик команды /foxfact — случайный факт о лисах через ИИ."""
    chat_id = message.chat.id
    if not MOONSHOT_KEY:
        bot.send_message(chat_id, 'Фыр... без API-ключа я не знаю фактов:3')
        return
    bot.send_chat_action(chat_id, 'typing')
    try:
        # Тема + случайное число в промпте: модель детерминирована и без
        # этого выдаёт один и тот же факт про магнитное поле Земли
        topic = random.choice([
            'строение тела и хвост', 'лисята и семья', 'звуки и общение',
            'питание', 'зимняя жизнь', 'разные виды лис', 'скорость и охота',
            'отношения с людьми', 'способности чувств', 'самые необычные виды',
        ])
        fact = ask_ai_once(
            f'Расскажи один короткий интересный факт о лисах, '
            f'тема: {topic}. '
            f'Только сам факт, без вступлений, до 300 символов. '
            f'Случайное число для разнообразия: {random.randint(1, 10**9)}.'
        )
    except Exception as error:
        logger.error(f'Ошибка запроса факта: {error}')
        fact = None
    if fact:
        bot.send_message(chat_id, f'🦊 Лисий факт: {fact}')
    else:
        bot.send_message(chat_id, 'Фыр... факты закончились, попробуй позже:3')


@bot.message_handler(commands=['stats'])
def stats(message):
    """Статистика для админа: пользователи и сообщения за сегодня."""
    if message.from_user.id != ADMIN_ID:
        return
    total_users, today_messages = get_stats()
    bot.send_message(
        message.chat.id,
        f'📊 Статистика\n\n'
        f'Всего пользователей: {total_users}\n'
        f'Сообщений сегодня: {today_messages}',
    )
    logger.info(f'/stats: {total_users} пользователей, {today_messages} сообщений сегодня')


@bot.message_handler(commands=['updatekb'])
def update_keyboards(message):
    """Обновляет reply-клавиатуру у всех пользователей (только админ).

    Reply-клавиатуру нельзя отредактировать, но сообщение с новой разметкой
    заменяет старую клавиатуру у получателя автоматически.
    """
    if message.from_user.id != ADMIN_ID:
        return
    users = load_users()
    keyboard = build_main_keyboard()
    sent, failed = 0, 0
    for user_id in users:
        try:
            bot.send_message(
                user_id,
                'Фыр! У меня новые кнопочки 🦊✨',
                reply_markup=keyboard,
            )
            sent += 1
        except Exception as error:
            failed += 1
            logger.warning(f'/updatekb: не доставлено {user_id}: {error}')
        time.sleep(0.05)  # ~20 сообщений/сек, лимит Telegram ~30/сек
    bot.send_message(
        message.chat.id,
        f'Клавиатуры обновлены: отправлено {sent}, ошибок {failed}.',
    )
    logger.info(f'/updatekb: отправлено {sent}, ошибок {failed}')


# Текст ожидающей подтверждения рассылки: admin_id -> текст
pending_broadcasts: dict[int, str] = {}


@bot.message_handler(commands=['broadcast'])
def broadcast(message):
    "Рассылка с подтверждением. Формат: /broadcast текст"
    if message.from_user.id != ADMIN_ID:
        return
    text = message.text[len('/broadcast'):].strip()
    if not text:
        bot.send_message(message.chat.id, 'Формат: /broadcast текст сообщения')
        return

    pending_broadcasts[ADMIN_ID] = text
    users_count = len(load_users())
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton('✅ Отправить', callback_data='bc_send'),
        types.InlineKeyboardButton('❌ Отмена', callback_data='bc_cancel'),
    )
    preview = text if len(text) <= 200 else text[:200] + '…'
    bot.send_message(
        message.chat.id,
        f'Разослать это {users_count} пользователям?\n\n«{preview}»',
        reply_markup=keyboard,
    )


@bot.callback_query_handler(func=lambda call: call.data in ('bc_send', 'bc_cancel'))
def broadcast_confirm(call):
    """Подтверждение/отмена рассылки по inline-кнопке."""
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, 'Это не твоя рассылка, фыр!')
        return
    bot.answer_callback_query(call.id)
    chat_id = call.message.chat.id
    if call.data == 'bc_cancel':
        pending_broadcasts.pop(ADMIN_ID, None)
        bot.send_message(chat_id, 'Рассылка отменена.')
        return

    text = pending_broadcasts.pop(ADMIN_ID, None)
    if text is None:
        bot.send_message(chat_id, 'Текст рассылки протух — отправь /broadcast заново.')
        return

    users = load_users()
    sent, failed = 0, 0
    for user_id in users:
        try:
            bot.send_message(user_id, text)
            sent += 1
        except Exception as error:
            failed += 1
            logger.warning(f'Рассылка: не доставлено {user_id}: {error}')
        time.sleep(0.05)  # ~20 сообщений/сек, лимит Telegram ~30/сек
    bot.send_message(
        chat_id,
        f'Рассылка завершена: отправлено {sent}, ошибок {failed}. Всего в базе: {len(users)}',
    )
    logger.info(f'Рассылка: отправлено {sent}, ошибок {failed}')


def build_main_keyboard() -> types.ReplyKeyboardMarkup:
    """Главная reply-клавиатура. Отправка любого сообщения с этой разметкой
    обновляет кнопки у пользователя — старая клавиатура заменяется автоматически."""
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    keyboard.add(
        types.KeyboardButton('Лисёночек:3'),
        types.KeyboardButton('Ты моё солнышко ☀️'),
        types.KeyboardButton('Фыр ❤️'),
        types.KeyboardButton('Горяченькое 🔥'),
        types.KeyboardButton('🎮 Игра 2048 + чат'),
        types.KeyboardButton('🔤 Азбука для малышей'),
        types.KeyboardButton('Лисий факт 🦊📚'),
    )
    return keyboard


@bot.message_handler(commands=['start'])
def wake_up(message):
    """Обработчик команды /start — приветствие и первая лиса."""
    chat_id = message.chat.id
    save_user(chat_id)
    name = message.from_user.first_name
    keyboard = build_main_keyboard()

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
        'Крепко-крепко обнимаю тебя! 🤗🤗🤗',
        'Заворачиваю тебя в пледик и обнимаю до упаду! 🛋️🤗',
        'Обнимаю тебя так крепко, что слышу твоё сердцебиение! 💓',
        'Шлю тебе сквозь-вселенский обнимашку! Лови! 🌌🤗',
        'В мире миллионы людей, но только ты — мой самый особенный! ✨',
        'Ты заслуживаешь тысячу обнимашек в день! Вот первая! 🤗',
        'Представь, что я рядом и крепко обнимаю! Не отпускаю! 🤗',
        'Ты такой яркий, что мне нужны очки от любви! 😎❤️',
        'Моё солнышко, ты заставляешь моё сердце петь! ☀️🎶',
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
    save_user(chat_id)
    track_message()
    logger.info(f'Получено сообщение от {chat_id}: {text}')

    if text in TENDER_PHRASES:
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

    if text in ('🔤 Азбука для малышей', '/abc', '/alphabet'):
        send_alphabet_app(message)
        return

    if text == 'Лисий факт 🦊📚':
        fox_fact(message)
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
    migrate_users_json()
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
        bot.polling(none_stop=True)


if __name__ == '__main__':
    main()
