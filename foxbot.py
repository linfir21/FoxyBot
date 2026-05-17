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


@bot.message_handler(commands=['start'])
def wake_up(message):
    """Обработчик команды /start — приветствие и первая лиса."""
    chat_id = message.chat.id
    name = message.from_user.first_name
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    button_fox = types.KeyboardButton('Лисёночек:3')
    button_sun = types.KeyboardButton('Ты моё солнышко ☀️')
    button_hug = types.KeyboardButton('Обнимашки 🤗')
    button_best = types.KeyboardButton('Ты лучше всех ✨')
    button_mur = types.KeyboardButton('Фыр ❤️')
    button_spicy = types.KeyboardButton('Горяченькое 🔥')
    keyboard.add(button_fox, button_sun, button_hug, button_best, button_mur, button_spicy)

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
        'Обнимаю тебя с ног до головы, ты мой сладкий! 🤗❤️',
        'Ты заслуживаешь тысячу обнимашек в день! Вот первая! 🤗',
        'Представь, что я рядом и крепко обнимаю! Не отпускаю! 🤗',
    ],
    'Ты лучше всех ✨': [
        'Ты лучше всех на свете, и я каждый день благодарю судьбу за тебя! ✨',
        'Никто не сравнится с тобой, ты мой идеал!',
        'Ты самый замечательный человечек во всей вселенной! ✨❤️',
        'В мире миллионы людей, но только ты — мой самый особенный! ✨',
        'Ты лучше радуги после дождя, лучше звёзд на небе! 🌈✨',
        'Если бы выбирали лучшего человека, я бы голосовал только за тебя! 🏆',
        'Ты невероятный, уникальный, неповторимый — мой! ✨',
        'Все шедевры мира блекнут по сравнению с тобой! 🎨✨',
        'Ты как лучший закат — красивый, тёплый, незабываемый! 🌇✨',
        'С тобой я понял, что идеал существует! ✨❤️',
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

    bot.send_message(
        chat_id=chat_id,
        text=(
            'Привет, я FoxBot! Нажми Лисёночек:3, и я покажу тебе лису. '
            'Или нажми на кнопочку, и я покажу тебе любовь:3'
        ),
    )


def main():
    logger.info('FoxBot запущен')
    bot.polling(none_stop=True)


if __name__ == '__main__':
    main()
