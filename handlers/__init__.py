"""Хендлеры FoxBot, сгруппированные по темам.

Импорт пакета регистрирует все обработчики (декораторы @bot.message_handler
отрабатывают при импорте модулей). Порядок важен только для читаемости —
Telegram-бот сопоставляет хендлеры сам.
"""
from . import fun      # погода, «Угадай число»
from . import apps     # /app (2048), «Игры APK», комнаты
from . import fox      # /start, лисы, лиса дня, /foxfact, /forget
from . import chat     # текст, голос, фото, /translate, /summary, /mood
from . import admin    # /stats, /broadcast, /updatekb, /feedback
