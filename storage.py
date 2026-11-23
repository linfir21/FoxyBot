"""Хранилище FoxBot: SQLite (пользователи, статистика, истории, настроения)
и разделяемое состояние в памяти (игры, рассылки, rate limiting).

БД открывается коротким соединением на каждую операцию — безопасно для тредов.
Пути и константы читаются из config в момент использования.
"""
import json
import sqlite3
import time

import config
from content import MOOD_NAMES
from core import logger


# ==== Разделяемое состояние (кэш поверх SQLite и служебные регистры) ====

# история диалогов: chat_id -> список сообщений (кэш поверх SQLite)
histories: dict[int, list] = {}
# время последнего сообщения: для авто-забывания истории (TTL)
last_ai_activity: dict[int, float] = {}
# chat_id -> метки времени последних ИИ-запросов (rate limiting)
ai_requests: dict[int, list] = {}
# мини-игры: chat_id -> состояние игры
games: dict[int, dict] = {}
# фидбек: message_id сообщения админу -> chat_id автора (для ответа реплаем)
feedback_relay: dict[int, int] = {}
# ожидающая подтверждения рассылка: admin_id -> текст ИЛИ
# кортеж ('copy', from_chat_id, message_id) для рассылки реплаем любого контента
pending_broadcasts: dict[int, object] = {}


def _users_db() -> sqlite3.Connection:
    "Открывает БД пользователей (короткое соединение на операцию — безопасно для тредов)."
    conn = sqlite3.connect(config.USERS_DB)
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
    import os
    if not os.path.exists(config.USERS_FILE):
        return
    try:
        with open(config.USERS_FILE, encoding='utf-8') as f:
            old_users = set(json.load(f))
    except (json.JSONDecodeError, OSError) as error:
        logger.warning(f'Не удалось прочитать {config.USERS_FILE}: {error}')
        return
    with _users_db() as conn:
        conn.executemany(
            'INSERT OR IGNORE INTO users (chat_id) VALUES (?)',
            [(chat_id,) for chat_id in old_users],
        )
    os.replace(config.USERS_FILE, config.USERS_FILE + '.bak')
    logger.info(f'Мигрировано {len(old_users)} пользователей из {config.USERS_FILE} в {config.USERS_DB}')


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
    if mood in config.MOODS:
        return f'{config.FOX_SYSTEM_PROMPT} {config.MOODS[mood]}'
    return config.FOX_SYSTEM_PROMPT


def _apply_mood(chat_id: int, mood: str | None) -> str:
    """Ставит или сбрасывает настроение. Системный промпт обновляется на лету,
    контекст диалога не теряется. Возвращает название настроения для показа."""
    if mood is None:
        with _users_db() as conn:
            conn.execute('DELETE FROM moods WHERE chat_id = ?', (chat_id,))
    else:
        _set_mood(chat_id, mood)
    if chat_id in histories and histories[chat_id]:
        histories[chat_id][0] = {'role': 'system', 'content': _system_prompt(chat_id)}
        _save_history(chat_id)
    return MOOD_NAMES.get(mood, 'обычное')
