"""Хендлеры FoxBot, сгруппированные по темам.

Импорт пакета регистрирует все обработчики (декораторы @bot.message_handler
отрабатывают при импорте модулей).

ВАЖНО: порядок импорта = порядок регистрации, а pyTelegramBotAPI отдаёт
сообщение ПЕРВОМУ подошедшему хендлеру. say_hi ловит любой текст, поэтому
модуль chat (где он теперь в самом конце) импортируется последним — иначе
он перехватит все команды, объявленные после него.
"""
from . import fun      # погода, игры
from . import apps     # «Игры APK»
from . import fox      # /start, лисы, лиса дня, /foxfact, /forget
from . import admin    # /stats, /broadcast, /updatekb, /feedback
from . import chat     # текст, голос, фото, /translate, /summary, /mood (say_hi — в конце!)
