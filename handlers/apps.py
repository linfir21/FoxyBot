"""Приложения: «🎮 Игры APK» — описание всех игр со ссылкой на скачивание."""
import config
import content

from core import bot, logger


@bot.message_handler(commands=['abc', 'alphabet', 'games', 'gamesapk'])
def send_games(message):
    """«Игры APK»: описание всех игр + ссылка на Яндекс.Диск.

    Кнопка так называется, потому что игр больше одной (2048 + Азбука),
    а APK весит больше лимита Telegram — отдаём описание и ссылку.
    """
    bot.send_message(
        message.chat.id,
        content.games_caption(config.DOWNLOAD_URL),
        parse_mode='HTML',
        disable_web_page_preview=True,
    )
    logger.info(f'Отправлено описание игр в чат {message.chat.id}')
