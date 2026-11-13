# foxbot/foxbot.py
import base64
import json
import logging
import os
import random
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import requests

from dotenv import load_dotenv
from logging.handlers import RotatingFileHandler
from telebot import TeleBot, types

# Распознавание голосовых — опциональная зависимость (SpeechRecognition + ffmpeg).
# Без них голосовые просто отвечают «не расслышал», бот продолжает работать.
try:
    import speech_recognition as sr
except ImportError:
    sr = None


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
AI_MODEL = 'kimi-k2.6'  # поддерживает и текст, и картинки; полный список — в доках
AI_HISTORY_LIMIT = 10   # сколько сообщений помнит (история = основные затраты)
AI_HISTORY_TTL_HOURS = float(os.getenv('AI_HISTORY_TTL_HOURS', '12'))  # авто-/forget

# Системный промпт лисьей личности — один на все ИИ-функции
FOX_SYSTEM_PROMPT = (
    'Ты — Лисёночек, милый лис-ассистент в Telegram. '
    'Отвечай кратко, дружелюбно, по-русски, иногда фыркай,'
    'а иногда говори комплименты, как лисенок, лисенку'
)

# история диалогов: chat_id -> список сообщений (кэш поверх SQLite)
histories: dict[int, list] = {}
# время последнего сообщения: для авто-забывания истории (TTL)
last_ai_activity: dict[int, float] = {}

# --- Rate limiting на ИИ: защита от спама и разгона счёта за API ---
AI_RATE_LIMIT = int(os.getenv('AI_RATE_LIMIT', '10'))           # ИИ-сообщений
AI_RATE_WINDOW = int(os.getenv('AI_RATE_WINDOW_SECONDS', '60'))  # за столько секунд
# chat_id -> метки времени последних ИИ-запросов
ai_requests: dict[int, list] = {}

# --- Настроение лисёнка (/mood): суффикс к системному промпту ---
MOODS = {
    'playful': 'Сейчас ты игривый: шути, дразнись и заигрывай.',
    'romantic': 'Сейчас ты романтичный: будь нежным, говори тёплые комплименты.',
    'calm': 'Сейчас ты спокойный: отвечай тихо, коротко и по делу, без проказ.',
    'grumpy': ('Сейчас ты ворчливый старый лис: бухти, ворчи, ворись '
               'на весь белый свет и на собеседника, но по делу всё же отвечай. '
               'В глубине души ты добрый, но никогда в этом не признавайся.'),
}

# --- Ежедневная «лиса дня»: всем пользователям, без команд ---
DAILY_FOX_HOUR = int(os.getenv('DAILY_FOX_HOUR', '6'))
DAILY_FOX_MINUTE = int(os.getenv('DAILY_FOX_MINUTE', '30'))

# --- Погода (Open-Meteo: бесплатно, без ключа) ---
WEATHER_URL = 'https://api.open-meteo.com/v1/forecast'

# --- Ссылка для скачивания APK больше 50 МБ (лимит Telegram Bot API) ---
DOWNLOAD_URL = os.getenv('DOWNLOAD_URL', 'https://disk.yandex.by/d/e9ofT1PIkyKTHA')

# --- Мини-игры: chat_id -> состояние игры ---
games: dict[int, dict] = {}

# --- Фидбек: message_id сообщения админу -> chat_id автора (для ответа реплаем) ---
feedback_relay: dict[int, int] = {}

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
    # Активные пользователи по дням (для /stats)
    conn.execute(
        'CREATE TABLE IF NOT EXISTS daily_active '
        '(day TEXT, chat_id INTEGER, PRIMARY KEY (day, chat_id))'
    )
    # Сообщения по часам (топ часов активности в /stats)
    conn.execute(
        'CREATE TABLE IF NOT EXISTS hourly_stats '
        '(day TEXT, hour INTEGER, messages INTEGER, PRIMARY KEY (day, hour))'
    )
    # Служебные ключи (например, дата последней ежедневной рассылки)
    conn.execute(
        'CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)'
    )
    # История ИИ-диалогов: переживает рестарт бота
    conn.execute(
        'CREATE TABLE IF NOT EXISTS ai_history ('
        'chat_id INTEGER PRIMARY KEY, '
        'history_json TEXT, updated_at REAL)'
    )
    # Настроение лисёнка по чатам (/mood)
    conn.execute(
        'CREATE TABLE IF NOT EXISTS moods ('
        'chat_id INTEGER PRIMARY KEY, mood TEXT)'
    )
    return conn


def _meta_get(key: str) -> str | None:
    "Читает служебный ключ из таблицы meta."
    with _users_db() as conn:
        row = conn.execute('SELECT value FROM meta WHERE key = ?', (key,)).fetchone()
    return row[0] if row else None


def _meta_set(key: str, value: str):
    "Записывает служебный ключ в таблицу meta."
    with _users_db() as conn:
        conn.execute(
            'INSERT INTO meta (key, value) VALUES (?, ?) '
            'ON CONFLICT(key) DO UPDATE SET value = excluded.value',
            (key, value),
        )


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


def remove_user(chat_id: int):
    "Удаляет chat_id из базы (пользователь заблокировал бота / чат недоступен)."
    with _users_db() as conn:
        cursor = conn.execute('DELETE FROM users WHERE chat_id = ?', (chat_id,))
        if cursor.rowcount:
            logger.info(f'Пользователь {chat_id} удалён из базы (бот заблокирован)')


def _is_gone_user_error(error) -> bool:
    "True, если Telegram говорит, что пользователь недоступен (403 / чат не найден)."
    if getattr(error, 'error_code', None) == 403:
        return True
    description = str(error).lower()
    return 'bot was blocked' in description or 'chat not found' in description


def track_message(chat_id: int):
    "Считает сообщение: всего за день, активные пользователи, активность по часам."
    day = time.strftime('%Y-%m-%d')
    hour = time.localtime().tm_hour
    with _users_db() as conn:
        conn.execute(
            'INSERT INTO daily_stats (day, messages) VALUES (?, 1) '
            'ON CONFLICT(day) DO UPDATE SET messages = messages + 1',
            (day,),
        )
        conn.execute(
            'INSERT OR IGNORE INTO daily_active (day, chat_id) VALUES (?, ?)',
            (day, chat_id),
        )
        conn.execute(
            'INSERT INTO hourly_stats (day, hour, messages) VALUES (?, ?, 1) '
            'ON CONFLICT(day, hour) DO UPDATE SET messages = messages + 1',
            (day, hour),
        )


def get_stats() -> dict:
    "Расширенная статистика: пользователи, сообщения, активность, топ часов."
    day = time.strftime('%Y-%m-%d')
    with _users_db() as conn:
        total_users = conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]
        row = conn.execute(
            'SELECT messages FROM daily_stats WHERE day = ?', (day,)
        ).fetchone()
        today_messages = row[0] if row else 0
        week_messages = conn.execute(
            'SELECT COALESCE(SUM(messages), 0) FROM ('
            '  SELECT messages FROM daily_stats ORDER BY day DESC LIMIT 7'
            ')'
        ).fetchone()[0]
        today_active = conn.execute(
            'SELECT COUNT(*) FROM daily_active WHERE day = ?', (day,)
        ).fetchone()[0]
        top_hours = conn.execute(
            'SELECT hour, SUM(messages) AS total FROM hourly_stats '
            'GROUP BY hour ORDER BY total DESC LIMIT 3'
        ).fetchall()
    return {
        'total_users': total_users,
        'today_messages': today_messages,
        'week_messages': week_messages,
        'today_active': today_active,
        'top_hours': [(hour, total) for hour, total in top_hours],
    }


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


def _load_history(chat_id: int):
    "Подгружает историю диалога из SQLite, если её нет в памяти (после рестарта)."
    if chat_id in histories:
        return
    with _users_db() as conn:
        row = conn.execute(
            'SELECT history_json, updated_at FROM ai_history WHERE chat_id = ?',
            (chat_id,),
        ).fetchone()
    if row:
        try:
            histories[chat_id] = json.loads(row[0])
            last_ai_activity[chat_id] = row[1]
        except (json.JSONDecodeError, TypeError) as error:
            logger.warning(f'Битая история ИИ для {chat_id}: {error}')


def _get_mood(chat_id: int) -> str | None:
    "Текущее настроение лисёнка для чата (None — обычное)."
    with _users_db() as conn:
        row = conn.execute(
            'SELECT mood FROM moods WHERE chat_id = ?', (chat_id,)
        ).fetchone()
    return row[0] if row else None


def _set_mood(chat_id: int, mood: str):
    "Запоминает настроение лисёнка для чата."
    with _users_db() as conn:
        conn.execute(
            'INSERT INTO moods (chat_id, mood) VALUES (?, ?) '
            'ON CONFLICT(chat_id) DO UPDATE SET mood = excluded.mood',
            (chat_id, mood),
        )


def _system_prompt(chat_id: int) -> str:
    "Системный промпт с учётом настроения (/mood)."
    mood = _get_mood(chat_id)
    if mood in MOODS:
        return f'{FOX_SYSTEM_PROMPT} {MOODS[mood]}'
    return FOX_SYSTEM_PROMPT


def _ai_rate_ok(chat_id: int) -> bool:
    """Rate limiting: не больше AI_RATE_LIMIT ИИ-запросов за AI_RATE_WINDOW секунд.
    Старые метки выкидываются, новая записывается только при успехе.
    Админа не ограничиваем — он свой."""
    if chat_id == ADMIN_ID:
        return True
    now = time.time()
    stamps = [t for t in ai_requests.get(chat_id, []) if now - t < AI_RATE_WINDOW]
    if len(stamps) >= AI_RATE_LIMIT:
        ai_requests[chat_id] = stamps
        return False
    stamps.append(now)
    ai_requests[chat_id] = stamps
    return True


def _save_history(chat_id: int):
    "Сохраняет историю диалога в SQLite (None = удалить, для /forget и TTL)."
    history = histories.get(chat_id)
    with _users_db() as conn:
        if history is None:
            conn.execute('DELETE FROM ai_history WHERE chat_id = ?', (chat_id,))
        else:
            conn.execute(
                'INSERT INTO ai_history (chat_id, history_json, updated_at) '
                'VALUES (?, ?, ?) '
                'ON CONFLICT(chat_id) DO UPDATE SET '
                'history_json = excluded.history_json, '
                'updated_at = excluded.updated_at',
                (chat_id, json.dumps(history, ensure_ascii=False),
                 last_ai_activity.get(chat_id, time.time())),
            )


def ask_ai(chat_id: int, text: str) -> str:
    """Отправляет сообщение пользователя в Kimi API и возвращает ответ."""
    _load_history(chat_id)  # память переживает рестарт: история лежит в SQLite
    # авто-/forget: история протухает после AI_HISTORY_TTL_HOURS неактивности
    last = last_ai_activity.get(chat_id, 0)
    if time.time() - last > AI_HISTORY_TTL_HOURS * 3600:
        histories.pop(chat_id, None)
    last_ai_activity[chat_id] = time.time()
    history = histories.setdefault(chat_id, [
        {'role': 'system', 'content': _system_prompt(chat_id)}
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
    _save_history(chat_id)
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


def ask_ai_image(prompt: str, image_b64: str, mime: str = 'image/jpeg') -> str:
    """Запрос к ИИ с картинкой (kimi-k2.6 умеет vision: JPEG/PNG/WebP в base64).

    Намеренно вне истории диалога: base64 картинок раздул бы её до неприличия.
    Каждая картинка тарифицируется фиксировано (~1024 токена).
    """
    response = requests.post(
        AI_URL,
        headers={'Authorization': f'Bearer {MOONSHOT_KEY}'},
        json={
            'model': AI_MODEL,
            'messages': [
                {'role': 'system', 'content': FOX_SYSTEM_PROMPT},
                {'role': 'user', 'content': [
                    {'type': 'image_url', 'image_url': {
                        'url': f'data:{mime};base64,{image_b64}',
                    }},
                    {'type': 'text', 'text': prompt},
                ]},
            ],
            'max_tokens': 1024,
        },
        timeout=90,
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
    """Отправляет APK файлом, если он < 50MB (лимит Telegram API),
    иначе — ссылкой на скачивание. При отсутствии файла — сообщение об этом."""
    chat_id = message.chat.id
    if not os.path.exists(apk_path):
        bot.send_message(
            chat_id,
            'APK пока не загружен на сервер. Попробуй позже.',
            reply_markup=keyboard,
        )
        logger.warning(f'APK не найден по пути {apk_path}')
        return
    file_size = os.path.getsize(apk_path)
    size_mb = file_size / (1024 * 1024)
    if file_size > 50 * 1024 * 1024:
        # Больше лимита Telegram — отправляем ссылку (DOWNLOAD_URL из .env)
        text = f'{caption}\n\n[📥 Скачать приложение]({DOWNLOAD_URL})'
        bot.send_message(
            chat_id,
            text,
            parse_mode='Markdown',
            reply_markup=keyboard,
        )
        logger.info(f'Отправлена ссылка на {apk_path} ({size_mb:.1f}MB) в чат {chat_id}')
        return
    # Читаем файл при каждом запросе: замена файла = обновление для всех
    with open(apk_path, 'rb') as apk_file:
        bot.send_document(
            chat_id,
            apk_file,
            caption=caption,
            reply_markup=keyboard,
        )
    logger.info(f'Отправлен {apk_path} ({size_mb:.1f}MB) в чат {chat_id}')


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
    _save_history(message.chat.id)  # удаляет запись из SQLite
    bot.send_message(message.chat.id, 'Фыр! Я всё забыл. Начнём с чистого листа:3')
    logger.info(f'История ИИ очищена для чата {message.chat.id}')


@bot.message_handler(commands=['mood'])
def mood_cmd(message):
    """Настроение лисёнка: /mood playful|romantic|calm.

    Сохраняет выбор в SQLite и подмешивает суффикс к системному промпту.
    Если диалог уже идёт — обновляет промпт на лету, контекст не теряется.
    """
    chat_id = message.chat.id
    arg = message.text[len('/mood'):].strip().lower()
    if arg not in MOODS:
        current = _get_mood(chat_id)
        current = f'«{current}»' if current else 'обычное'
        bot.send_message(
            chat_id,
            f'Сейчас у меня настроение {current}. Могу быть:\n'
            f'• /mood playful — игривый, шучу и дразнюсь\n'
            f'• /mood romantic — нежный и романтичный\n'
            f'• /mood calm — спокойный и короткий\n'
            f'• /mood grumpy — ворчливый старый лис 🦊💢',
        )
        return
    _set_mood(chat_id, arg)
    if chat_id in histories and histories[chat_id]:
        histories[chat_id][0] = {'role': 'system', 'content': _system_prompt(chat_id)}
        _save_history(chat_id)
    bot.send_message(chat_id, f'Фыр! Теперь я {arg} 🦊')
    logger.info(f'Настроение чата {chat_id}: {arg}')


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
    if not MOONSHOT_KEY:
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

DAILY_GREETINGS = [
    'Доброе утро! Вот твоя лисичка на сегодня 🦊☀️',
    'Фыр-фыр! Новый день — новая лиса 🦊',
    'Просыпайся, солнышко! Лиса уже тут ☀️🦊',
    'Утро добрым не бывает? С лисой — бывает! 🦊❤️',
    'Тук-тук! Это я, принёс тебе утреннюю лисичку 🦊',
    'Лиса дня прибыла! Пусть день будет пушистым 🦊✨',
]


def _send_daily_fox():
    "Рассылает лису дня всем пользователям. Заблокировавших — удаляет из базы."
    users = load_users()
    if not users:
        return
    image_url = get_new_image()
    caption = random.choice(DAILY_GREETINGS)
    if MOONSHOT_KEY:
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
        due = (now.tm_hour, now.tm_min) >= (DAILY_FOX_HOUR, DAILY_FOX_MINUTE)
        if due and _meta_get('last_daily_fox') != today:
            # фиксируем до отправки: при падении посреди рассылки не спамим повтором
            _meta_set('last_daily_fox', today)
            try:
                _send_daily_fox()
            except Exception as error:
                logger.error(f'Лиса дня: рассылка упала: {error}')
        time.sleep(30)


@bot.message_handler(commands=['stats'])
def stats(message):
    """Статистика для админа: пользователи, сообщения, активность, топ часов."""
    if message.from_user.id != ADMIN_ID:
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
            if _is_gone_user_error(error):
                remove_user(user_id)
            logger.warning(f'/updatekb: не доставлено {user_id}: {error}')
        time.sleep(0.05)  # ~20 сообщений/сек, лимит Telegram ~30/сек
    bot.send_message(
        message.chat.id,
        f'Клавиатуры обновлены: отправлено {sent}, ошибок {failed}.',
    )
    logger.info(f'/updatekb: отправлено {sent}, ошибок {failed}')


# Ожидающая подтверждения рассылка: admin_id -> текст ИЛИ
# кортеж ('copy', from_chat_id, message_id) для рассылки реплаем любого контента
pending_broadcasts: dict[int, object] = {}


@bot.message_handler(commands=['broadcast'])
def broadcast(message):
    """Рассылка с подтверждением.

    Формат: /broadcast текст — рассылает текст;
    /broadcast реплаем на фото/видео/документ/голосовое — рассылает его копию.
    """
    if message.from_user.id != ADMIN_ID:
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
        pending_broadcasts[ADMIN_ID] = ('copy', reply.chat.id, reply.message_id)
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
    pending_broadcasts[ADMIN_ID] = text
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

    pending = pending_broadcasts.pop(ADMIN_ID, None)
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
        # Telegram пришлёт геопозицию пользователя — бот ответит погодой
        types.KeyboardButton('🌤 Погода', request_location=True),
        types.KeyboardButton('🎲 Угадай число'),
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


# ==== Погода по геопозиции (Open-Meteo, без ключа) ====

# Коды погоды WMO → (описание, эмодзи). Диапазоны свёрнуты для краткости.
_WEATHER_CODES = {
    0: ('ясно', '☀️'), 1: ('преимущественно ясно', '🌤'),
    2: ('переменная облачность', '⛅'), 3: ('пасмурно', '☁️'),
    45: ('туман', '🌫'), 48: ('туман с изморозью', '🌫'),
    51: ('морось', '🌦'), 53: ('морось', '🌦'), 55: ('сильная морось', '🌧'),
    56: ('ледяная морось', '🌧'), 57: ('ледяная морось', '🌧'),
    61: ('небольшой дождь', '🌦'), 63: ('дождь', '🌧'), 65: ('сильный дождь', '🌧'),
    66: ('ледяной дождь', '🌧'), 67: ('ледяной дождь', '🌧'),
    71: ('небольшой снег', '🌨'), 73: ('снег', '❄️'), 75: ('сильный снег', '❄️'),
    77: ('снежная крупа', '❄️'),
    80: ('ливень', '🌧'), 81: ('ливень', '🌧'), 82: ('сильный ливень', '⛈'),
    85: ('снегопад', '❄️'), 86: ('сильный снегопад', '❄️'),
    95: ('гроза', '⛈'), 96: ('гроза с градом', '⛈'), 99: ('гроза с градом', '⛈'),
}


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
            WEATHER_URL,
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
        description, emoji = _WEATHER_CODES.get(
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


# ==== Перевод и пересказ через ИИ ====

def _command_text(message, command: str) -> str:
    """Текст после команды, а если пусто — текст сообщения-реплая."""
    text = message.text[len(command):].strip()
    if not text and message.reply_to_message and message.reply_to_message.text:
        text = message.reply_to_message.text
    return text


def _ask_once_or_apology(chat_id: int, prompt: str, max_tokens: int = 2048):
    "Отправляет одноразовый ИИ-запрос и отвечает результатом (или извинением)."
    if not MOONSHOT_KEY:
        bot.send_message(chat_id, 'Фыр... без API-ключа я это не умею:3')
        return
    bot.send_chat_action(chat_id, 'typing')
    try:
        result = ask_ai_once(prompt, max_tokens=max_tokens)
    except Exception as error:
        logger.error(f'Ошибка одноразового ИИ-запроса: {error}')
        result = 'Фыр... я задремал. Попробуй ещё раз:3'
    for i in range(0, len(result), 4096):
        bot.send_message(chat_id, result[i:i + 4096])


@bot.message_handler(commands=['translate'])
def translate_cmd(message):
    """Перевод: /translate текст (или ответь командой на сообщение)."""
    text = _command_text(message, '/translate')
    if not text:
        bot.send_message(
            message.chat.id,
            'Формат: /translate текст\n(или ответь командой на сообщение)',
        )
        return
    _ask_once_or_apology(
        message.chat.id,
        'Переведи текст: если он на русском — на английский, '
        'если на любом другом языке — на русский. '
        'Ответь только переводом, без пояснений.\n\n' + text,
    )


@bot.message_handler(commands=['summary'])
def summary_cmd(message):
    """Короткий пересказ: /summary текст (или ответь командой на сообщение)."""
    text = _command_text(message, '/summary')
    if not text:
        bot.send_message(
            message.chat.id,
            'Формат: /summary текст\n(или ответь командой на длинное сообщение)',
        )
        return
    _ask_once_or_apology(
        message.chat.id,
        'Сожми текст до 3–5 коротких тезисов на русском. '
        'Только тезисы, без вступлений.\n\n' + text,
        max_tokens=1024,
    )


# ==== Голосовые сообщения: OGG → WAV (ffmpeg) → Google STT → ИИ ====

def _transcribe_voice(message) -> str | None:
    """Распознаёт голосовое. None — не получилось (или нет ffmpeg/STT)."""
    if sr is None or shutil.which('ffmpeg') is None:
        logger.warning('Голосовые недоступны: нужны SpeechRecognition и ffmpeg')
        return None
    tmp_ogg = tmp_wav = None
    try:
        file_info = bot.get_file(message.voice.file_id)
        voice_bytes = bot.download_file(file_info.file_path)
        with tempfile.NamedTemporaryFile(suffix='.ogg', delete=False) as tmp:
            tmp.write(voice_bytes)
            tmp_ogg = tmp.name
        tmp_wav = tmp_ogg + '.wav'
        subprocess.run(
            ['ffmpeg', '-y', '-i', tmp_ogg, tmp_wav],
            check=True, capture_output=True,
        )
        recognizer = sr.Recognizer()
        with sr.AudioFile(tmp_wav) as source:
            audio = recognizer.record(source)
        return recognizer.recognize_google(audio, language='ru-RU')
    except Exception as error:
        # UnknownValueError (неразборчиво) и всё остальное — одинаково: не слышим
        logger.warning(f'Не удалось распознать голосовое: {error}')
        return None
    finally:
        for path in (tmp_ogg, tmp_wav):
            if path and os.path.exists(path):
                os.remove(path)


@bot.message_handler(content_types=['voice'])
def handle_voice(message):
    """Голосовое → текст → ответ Лисёночка (история общая с текстовым чатом)."""
    chat_id = message.chat.id
    save_user(chat_id)
    track_message(chat_id)
    bot.send_chat_action(chat_id, 'typing')
    text = _transcribe_voice(message)
    if text is None:
        bot.send_message(
            chat_id, 'Фыр... не расслышал тебя. Напиши текстом, ладно? :3'
        )
        return
    logger.info(f'Распознано голосовое от {chat_id}: {text}')
    if not MOONSHOT_KEY:
        bot.send_message(chat_id, f'Ты сказал: «{text}»\n\nНо без API-ключа мне нечего ответить:3')
        return
    if not _ai_rate_ok(chat_id):
        bot.send_message(chat_id, 'Фыр-фыр, не так быстро! Дай мне перевести дух минутку:3')
        return
    try:
        answer = ask_ai(chat_id, text)
    except Exception as error:
        logger.error(f'Ошибка AI-запроса (голос): {error}')
        answer = 'Фыр... я запутался в мыслях. Попробуй ещё раз:3'
    reply = f'🎤 «{text}»\n\n{answer}'
    for i in range(0, len(reply), 4096):
        bot.send_message(chat_id, reply[i:i + 4096])


# ==== Фото: kimi-k2.6 умеет vision — обсуждаем картинки ====

@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    """Фото → ИИ (vision). Подпись к фото = вопрос о картинке."""
    chat_id = message.chat.id
    save_user(chat_id)
    track_message(chat_id)
    if not MOONSHOT_KEY:
        bot.send_message(
            chat_id,
            'Красивая картинка! Но без API-ключа я не могу её обсудить:3',
        )
        return
    if not _ai_rate_ok(chat_id):
        bot.send_message(chat_id, 'Фыр-фыр, не так быстро! Дай мне перевести дух минутку:3')
        return
    bot.send_chat_action(chat_id, 'typing')
    try:
        file_info = bot.get_file(message.photo[-1].file_id)  # максимальное качество
        image_bytes = bot.download_file(file_info.file_path)
        ext = file_info.file_path.rsplit('.', 1)[-1].lower()
        mime = {
            'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
            'png': 'image/png', 'webp': 'image/webp',
        }.get(ext, 'image/jpeg')
        prompt = message.caption or 'Что на этой картинке? Ответь коротко и по-лисьи.'
        answer = ask_ai_image(
            prompt, base64.b64encode(image_bytes).decode(), mime
        )
    except Exception as error:
        logger.error(f'Ошибка обработки фото: {error}')
        answer = 'Фыр... мои лисьи глазки подвели. Попробуй ещё раз:3'
    for i in range(0, len(answer), 4096):
        bot.send_message(chat_id, answer[i:i + 4096])


# ==== Обратная связь: /feedback + ответ админа реплаем ====

@bot.message_handler(commands=['feedback'])
def feedback(message):
    """Пересылает сообщение админу; админ отвечает реплаем — бот доставит."""
    chat_id = message.chat.id
    text = message.text[len('/feedback'):].strip()
    if not text:
        bot.send_message(chat_id, 'Формат: /feedback твоё сообщение')
        return
    if not ADMIN_ID:
        bot.send_message(chat_id, 'Фыр... админ не настроен, некуда передать:3')
        return
    try:
        sent = bot.send_message(
            ADMIN_ID,
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


# ==== Мини-игра «Угадай число» ====

def _start_number_game(chat_id: int):
    games[chat_id] = {'number': random.randint(1, 100), 'tries': 0}
    bot.send_message(
        chat_id,
        '🎲 Я загадал число от 1 до 100. Попробуй угадать!\n'
        '(напиши /stopgame, чтобы сдаться)',
    )


@bot.message_handler(commands=['stopgame'])
def stop_game(message):
    if games.pop(message.chat.id, None) is not None:
        bot.send_message(message.chat.id, 'Сдаёшься? В другой раз повезёт, фыр! 🦊')
    else:
        bot.send_message(message.chat.id, 'Мы сейчас ни во что не играем:3')


@bot.message_handler(content_types=['text'])
def say_hi(message):
    """Обработчик текстовых сообщений."""
    chat = message.chat
    chat_id = chat.id
    text = message.text
    save_user(chat_id)
    track_message(chat_id)
    logger.info(f'Получено сообщение от {chat_id}: {text}')

    # Ответ админа на фидбек (реплаем на пересланное сообщение)
    relay = message.reply_to_message
    if (relay is not None and message.from_user.id == ADMIN_ID
            and relay.message_id in feedback_relay):
        target = feedback_relay[relay.message_id]
        try:
            bot.send_message(target, f'💌 Ответ от Лисёночка:\n\n{text}')
            bot.send_message(chat_id, 'Ответ отправлен ✔️')
            logger.info(f'Ответ админа доставлен в чат {target}')
        except Exception as error:
            bot.send_message(chat_id, f'Не доставлено: {error}')
            logger.warning(f'Ответ админа не доставлен в {target}: {error}')
        return

    # Команды обрабатываются своими хендлерами; сюда — только кнопки и болтовня
    if text.startswith('/'):
        return

    if text in TENDER_PHRASES:
        phrase = random.choice(TENDER_PHRASES[text])
        bot.send_message(chat_id=chat_id, text=phrase)
        logger.info(f'Отправлена нежность в чат {chat_id}')
        return

    if text == 'Лисёночек:3':
        new_fox(message)
        return

    if text == '🎮 Игра 2048 + чат':
        send_app(message)
        return

    if text == '🔤 Азбука для малышей':
        send_alphabet_app(message)
        return

    if text == 'Лисий факт 🦊📚':
        fox_fact(message)
        return

    if text == '🎲 Угадай число':
        _start_number_game(chat_id)
        return

    # Активная игра «Угадай число»: любой текст трактуем как попытку
    if chat_id in games:
        if not text.isdigit():
            bot.send_message(
                chat_id,
                'Напиши число от 1 до 100 или /stopgame, чтобы сдаться:3',
            )
            return
        game = games[chat_id]
        game['tries'] += 1
        guess = int(text)
        if guess < game['number']:
            bot.send_message(chat_id, 'Больше! ⬆️')
        elif guess > game['number']:
            bot.send_message(chat_id, 'Меньше! ⬇️')
        else:
            games.pop(chat_id)
            bot.send_message(
                chat_id,
                f'🎉 Угадал за {game["tries"]} попыток! Умничка, фыр! 🦊',
            )
        return

    # Всё остальное уходит в ИИ-чат
    if not MOONSHOT_KEY:
        bot.send_message(
            chat_id=chat_id,
            text=('Привет, я FoxBot! Нажми Лисёночек:3, и я покажу тебе лису. '
                  'Или нажми на кнопочку, и я покажу тебе любовь:3'),
        )
        return

    if not _ai_rate_ok(chat_id):
        bot.send_message(chat_id, 'Фыр-фыр, не так быстро! Дай мне перевести дух минутку:3')
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
            types.BotCommand('app', 'Игра 2048 + скрытый чат'),
            types.BotCommand('abc', 'Азбука для малышей'),
        ])
    except Exception as error:
        logger.warning(f'Не удалось зарегистрировать меню команд: {error}')
    # Ежедневная «лиса дня» всем пользователям (DAILY_FOX_HOUR:DAILY_FOX_MINUTE)
    threading.Thread(target=_daily_fox_loop, daemon=True).start()
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
