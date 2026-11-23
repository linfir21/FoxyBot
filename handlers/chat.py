"""Живое общение: текстовые сообщения (кнопки + ИИ-чат), голосовые, фото,
/translate, /summary, настроение /mood и доставка ответов админа на фидбек."""
import base64
import os
import shutil
import subprocess
import tempfile

import config
import content

from core import bot, logger
from handlers.apps import send_games
from handlers.fox import fox_fact, new_fox
from handlers.fun import _play_number_round, send_games_menu
from keyboards import _mood_keyboard
from kimi import _ai_rate_ok, ask_ai, ask_ai_image, ask_ai_once
from storage import (
    _apply_mood,
    _get_mood,
    feedback_relay,
    games,
    save_user,
    track_message,
)

# Распознавание голосовых — опциональная зависимость (SpeechRecognition + ffmpeg).
# Без них голосовые просто отвечают «не расслышал», бот продолжает работать.
try:
    import speech_recognition as sr
except ImportError:
    sr = None


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
            chat_id, 'Фыр... не расслышала тебя. Напиши текстом, ладно? :3'
        )
        return
    logger.info(f'Распознано голосовое от {chat_id}: {text}')
    if not config.MOONSHOT_KEY:
        bot.send_message(chat_id, f'Ты сказал: «{text}»\n\nНо без API-ключа мне нечего ответить:3')
        return
    if not _ai_rate_ok(chat_id):
        bot.send_message(chat_id, 'Фыр-фыр, не так быстро! Дай мне перевести дух минутку:3')
        return
    try:
        answer = ask_ai(chat_id, text)
    except Exception as error:
        logger.error(f'Ошибка AI-запроса (голос): {error}')
        answer = 'Фыр... я запуталась в мыслях. Попробуй ещё раз:3'
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
    if not config.MOONSHOT_KEY:
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
    # reasoning может съесть весь бюджет: content выходит пустым — не молчим
    if not answer.strip():
        answer = 'Фыр... у меня слова застряли в горлышке. Попробуй ещё раз:3'
    for i in range(0, len(answer), 4096):
        bot.send_message(chat_id, answer[i:i + 4096])


# ==== Перевод и пересказ через ИИ ====

def _command_text(message, command: str) -> str:
    """Текст после команды, а если пусто — текст сообщения-реплая."""
    text = message.text[len(command):].strip()
    if not text and message.reply_to_message and message.reply_to_message.text:
        text = message.reply_to_message.text
    return text


def _ask_once_or_apology(chat_id: int, prompt: str, max_tokens: int = 2048):
    "Отправляет одноразовый ИИ-запрос и отвечает результатом (или извинением)."
    if not config.MOONSHOT_KEY:
        bot.send_message(chat_id, 'Фыр... без API-ключа я это не умею:3')
        return
    bot.send_chat_action(chat_id, 'typing')
    try:
        result = ask_ai_once(prompt, max_tokens=max_tokens)
    except Exception as error:
        logger.error(f'Ошибка одноразового ИИ-запроса: {error}')
        result = 'Фыр... я задремала. Попробуй ещё раз:3'
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
        max_tokens=2048,
    )


# ==== Настроение лисёнка (/mood): меню с кнопками ====

@bot.message_handler(commands=['mood'])
def mood_cmd(message):
    """Настроение лисёнка: меню с кнопками или /mood playful|romantic|calm.

    Сохраняет выбор в SQLite и подмешивает суффикс к системному промпту.
    Если диалог уже идёт — обновляет промпт на лету, контекст не теряется.
    """
    chat_id = message.chat.id
    arg = message.text[len('/mood'):].strip().lower()
    if arg and arg not in config.MOODS:
        bot.send_message(chat_id, 'Фыр! Такого настроения у меня нет:3')
        arg = ''
    if not arg:
        current = _get_mood(chat_id)
        current_name = content.MOOD_NAMES.get(current, 'обычное')
        bot.send_message(
            chat_id,
            f'Сейчас у меня настроение «{current_name}». Выбери, какой мне быть:',
            reply_markup=_mood_keyboard(current),
        )
        return
    name = _apply_mood(chat_id, arg)
    bot.send_message(chat_id, f'Фыр! Теперь у меня настроение «{name}» 🦊')
    logger.info(f'Настроение чата {chat_id}: {arg}')


@bot.callback_query_handler(func=lambda call: call.data.startswith('mood:'))
def mood_callback(call):
    """Выбор настроения по inline-кнопке: переключает и обновляет меню."""
    chat_id = call.message.chat.id
    mood = call.data.split(':', 1)[1]
    mood = None if mood == 'none' else mood
    name = _apply_mood(chat_id, mood)
    bot.answer_callback_query(call.id, f'Теперь я «{name}» 🦊')
    bot.edit_message_text(
        f'Сейчас у меня настроение «{name}». Выбери, какой мне быть:',
        chat_id=chat_id,
        message_id=call.message.message_id,
        reply_markup=_mood_keyboard(mood),
    )
    logger.info(f'Настроение чата {chat_id}: {mood}')


@bot.message_handler(content_types=['text'])
def say_hi(message):
    """Обработчик текстовых сообщений: кнопки, игра и ИИ-болтовня."""
    chat = message.chat
    chat_id = chat.id
    text = message.text
    save_user(chat_id)
    track_message(chat_id)
    logger.info(f'Получено сообщение от {chat_id}: {text}')

    # Ответ админа на фидбек (реплаем на пересланное сообщение)
    relay = message.reply_to_message
    if (relay is not None and message.from_user.id == config.ADMIN_ID
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

    # Кнопки с готовыми фразами
    if text == 'Фыр ❤️':
        bot.send_message(chat_id=chat_id, text=content.tender_phrase())
        logger.info(f'Отправлена нежность в чат {chat_id}')
        return

    if text == 'Горяченькое 🔥':
        bot.send_message(chat_id=chat_id, text=content.spicy_phrase())
        logger.info(f'Отправлено горяченькое в чат {chat_id}')
        return

    if text == 'Лисёночек:3':
        new_fox(message)
        return

    if text == '🎮 Игры APK':
        send_games(message)
        return

    if text == 'Лисий факт 🦊📚':
        fox_fact(message)
        return

    if text == '🎮 Игры':
        send_games_menu(chat_id)
        return

    # Активная игра: любой текст в «Угадай число» трактуем как попытку
    if _play_number_round(chat_id, text):
        return

    # Всё остальное уходит в ИИ-чат
    if not config.MOONSHOT_KEY:
        bot.send_message(
            chat_id=chat_id,
            text=('Привет, я Твоя Лисичка! Нажми Лисёночек:3, и я покажу тебе лису. '
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
        answer = 'Фыр... я запуталась в мыслях. Попробуй ещё раз:3'
    # лимит Telegram — 4096 символов на сообщение
    for i in range(0, len(answer), 4096):
        bot.send_message(chat_id, answer[i:i + 4096])
