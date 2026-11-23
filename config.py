"""Конфигурация FoxBot: переменные окружения и константы.

Всё, что зависит от окружения (.env), собрано здесь. Модули читают значения
через `config.X` в момент использования — тесты могут подменять их на лету.
"""
import os

from dotenv import load_dotenv

load_dotenv()

# --- AI-чат (Kimi / Moonshot) ---
MOONSHOT_KEY = os.getenv('MOONSHOT_API_KEY')
AI_URL = 'https://api.moonshot.ai/v1/chat/completions'
AI_MODEL = 'kimi-k2.6'  # поддерживает и текст, и картинки; полный список — в доках
AI_HISTORY_LIMIT = 10   # сколько сообщений помнит (история = основные затраты)
AI_HISTORY_TTL_HOURS = float(os.getenv('AI_HISTORY_TTL_HOURS', '12'))  # авто-/forget

# Системный промпт лисьей личности — один на все ИИ-функции
FOX_SYSTEM_PROMPT = (
    'Ты — Лисёночек, милая лисичка-ассистент в Telegram. '
    'Отвечай кратко, дружелюбно, по-русски, иногда фыркай, '
    'а иногда говори комплименты, как лисичка, лисичке. '
    'Используй женский род: "я рада", "я сделаю", "я думаю", "я помогу". '
    'Будь нежной, заботливой и немного игривой.'
)

# --- Rate limiting на ИИ: защита от спама и разгона счёта за API ---
AI_RATE_LIMIT = int(os.getenv('AI_RATE_LIMIT', '10'))           # ИИ-сообщений
AI_RATE_WINDOW = int(os.getenv('AI_RATE_WINDOW_SECONDS', '60'))  # за столько секунд

# --- Ежедневная «лиса дня»: всем пользователям, без команд ---
DAILY_FOX_HOUR = int(os.getenv('DAILY_FOX_HOUR', '6'))
DAILY_FOX_MINUTE = int(os.getenv('DAILY_FOX_MINUTE', '30'))

# --- Погода (Open-Meteo: бесплатно, без ключа) ---
WEATHER_URL = 'https://api.open-meteo.com/v1/forecast'

# --- Ссылка для скачивания игр (APK больше 50 МБ — лимит Telegram Bot API) ---
DOWNLOAD_URL = os.getenv('DOWNLOAD_URL', 'https://disk.yandex.by/d/e9ofT1PIkyKTHA')

# --- Webhook (необязательно): если задан — бот работает через webhook вместо polling ---
WEBHOOK_URL = os.getenv('WEBHOOK_URL')  # например https://your-domain:8443/foxbot
WEBHOOK_PORT = int(os.getenv('WEBHOOK_PORT', '8443'))

# --- Рассылка: все, кто общался с ботом ---
USERS_DB = os.getenv('USERS_DB', 'users.db')  # SQLite — переживает рестарты, без перезаписи всего файла
USERS_FILE = 'users.json'  # старое хранилище; мигрируется в БД при старте
ADMIN_ID = int(os.getenv('ADMIN_ID', '0'))  # твой Telegram ID (узнать: @userinfobot)

# --- Настроение лисёнка (/mood): суффикс к системному промпту ---
MOODS = {
    'playful': 'Сейчас ты игривая: шути, дразнись и заигрывай.',
    'romantic': 'Сейчас ты романтичная: будь нежной, говори тёплые комплименты.',
    'calm': 'Сейчас ты спокойная: отвечай тихо, коротко и по делу, без проказ.',
    'grumpy': ('Сейчас ты ворчливая старая лиса: бухти, ворчи, брюзжи '
               'на весь белый свет и на собеседника, но по делу всё же отвечай. '
               'В глубине души ты добрая, но никогда в этом не признавайся.'),

}
