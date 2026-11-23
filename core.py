"""Ядро FoxBot: логирование и общий экземпляр бота.

Все модули импортируют bot/logger отсюда — это единственный «синглтон» проекта
и заодно разрыв потенциальных циклических импортов.
"""
import logging
import os

from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv
from telebot import TeleBot

load_dotenv()  # .env должен быть загружен до чтения TOKEN

token = os.getenv('TOKEN')
if not token:
    raise ValueError(
        'Не найден токен бота! Убедитесь, что в файле .env есть переменная TOKEN'
    )

bot = TeleBot(token=token)

# Настройка глобального логирования
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
)

# Настройка логгера с ротацией
logger = logging.getLogger('foxbot')
logger.setLevel(logging.INFO)
handler = RotatingFileHandler(
    'foxbot.log', maxBytes=50000000, backupCount=5, encoding='utf-8'
)
formatter = logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
handler.setFormatter(formatter)
logger.addHandler(handler)
