"""ИИ-слой FoxBot: запросы к Kimi (Moonshot) и rate limiting.

kimi-k2.6 — reasoning-модель: её рассуждения считаются в тот же бюджет
max_tokens, поэтому лимиты тут с запасом — иначе content выходит пустым
или обрезанным посреди слова (проверено на живом API).
"""
import time

import config
import requests

from core import logger
from storage import (
    _load_history,
    _save_history,
    _system_prompt,
    ai_requests,
    histories,
    last_ai_activity,
)


def _ai_rate_ok(chat_id: int) -> bool:
    """Rate limiting: не больше AI_RATE_LIMIT ИИ-запросов за AI_RATE_WINDOW секунд.
    Старые метки выкидываются, новая записывается только при успехе.
    Админа не ограничиваем — он свой."""
    if chat_id == config.ADMIN_ID:
        return True
    now = time.time()
    stamps = [t for t in ai_requests.get(chat_id, []) if now - t < config.AI_RATE_WINDOW]
    if len(stamps) >= config.AI_RATE_LIMIT:
        ai_requests[chat_id] = stamps
        return False
    stamps.append(now)
    ai_requests[chat_id] = stamps
    return True


def ask_ai(chat_id: int, text: str) -> str:
    """Отправляет сообщение пользователя в Kimi API и возвращает ответ."""
    _load_history(chat_id)  # память переживает рестарт: история лежит в SQLite
    # авто-/forget: история протухает после AI_HISTORY_TTL_HOURS неактивности
    last = last_ai_activity.get(chat_id, 0)
    if time.time() - last > config.AI_HISTORY_TTL_HOURS * 3600:
        histories.pop(chat_id, None)
    last_ai_activity[chat_id] = time.time()
    history = histories.setdefault(chat_id, [
        {'role': 'system', 'content': _system_prompt(chat_id)}
    ])
    history.append({'role': 'user', 'content': text})
    # режем историю: системный промпт + последние N сообщений
    # (history[1:], а не history, иначе системный промпт продублируется)
    history[:] = history[:1] + history[1:][-config.AI_HISTORY_LIMIT:]

    response = requests.post(
        config.AI_URL,
        headers={'Authorization': f'Bearer {config.MOONSHOT_KEY}'},
        json={
            'model': config.AI_MODEL,
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
        config.AI_URL,
        headers={'Authorization': f'Bearer {config.MOONSHOT_KEY}'},
        json={
            'model': config.AI_MODEL,
            'messages': [{'role': 'user', 'content': prompt}],
            'max_tokens': max_tokens,
            # temperature не трогаем: kimi-k2.6 принимает только значение 1
        },
        timeout=60,
    )
    response.raise_for_status()
    return response.json()['choices'][0]['message']['content'].strip()


def ask_ai_image(prompt: str, image_b64: str, mime: str = 'image/jpeg') -> str:
    """Запрос к ИИ с картинкой (vision: JPEG/PNG/WebP в base64).

    Намеренно вне истории диалога: base64 картинок раздул бы её до неприличия.
    Каждая картинка тарифицируется фиксировано (~1024 токена).
    max_tokens большой не из жадности: reasoning про картинку легко съедает
    несколько тысяч токенов — с маленьким лимитом ответ выходит пустым.
    """
    response = requests.post(
        config.AI_URL,
        headers={'Authorization': f'Bearer {config.MOONSHOT_KEY}'},
        json={
            'model': config.AI_MODEL,
            'messages': [
                {'role': 'system', 'content': config.FOX_SYSTEM_PROMPT},
                {'role': 'user', 'content': [
                    {'type': 'image_url', 'image_url': {
                        'url': f'data:{mime};base64,{image_b64}',
                    }},
                    {'type': 'text', 'text': prompt},
                ]},
            ],
            'max_tokens': 8192,
        },
        timeout=90,
    )
    response.raise_for_status()
    return response.json()['choices'][0]['message']['content'].strip()
