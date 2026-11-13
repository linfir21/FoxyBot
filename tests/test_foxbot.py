"""Тесты на чистую логику FoxBot (без сети и Telegram).

Запуск: venv/Scripts/python.exe -m pytest tests/ -v
"""
import json
import os
import sys
from types import SimpleNamespace

import pytest

# Токен нужен уже при импорте foxbot — подставляем фейк до импорта
os.environ.setdefault('TOKEN', '123456:test-token')

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import foxbot  # noqa: E402


@pytest.fixture()
def tmp_users_db(tmp_path, monkeypatch):
    "Перенаправляет БД пользователей во временную папку."
    db_path = str(tmp_path / 'users.db')
    monkeypatch.setattr(foxbot, 'USERS_DB', db_path)
    monkeypatch.setattr(foxbot, 'USERS_FILE', str(tmp_path / 'users.json'))
    return db_path


# ==== Пользователи (SQLite) ====

def test_load_users_empty(tmp_users_db):
    assert foxbot.load_users() == set()


def test_save_user_roundtrip(tmp_users_db):
    foxbot.save_user(111)
    foxbot.save_user(222)
    foxbot.save_user(111)  # дубликат игнорируется
    assert foxbot.load_users() == {111, 222}


def test_migrate_users_json(tmp_users_db):
    users = [111, 222, 333]
    with open(foxbot.USERS_FILE, 'w', encoding='utf-8') as f:
        json.dump(users, f)
    foxbot.migrate_users_json()
    assert foxbot.load_users() == set(users)
    # файл перемещён в .bak, повторная миграция — no-op
    assert not os.path.exists(foxbot.USERS_FILE)
    assert os.path.exists(foxbot.USERS_FILE + '.bak')
    foxbot.migrate_users_json()
    assert foxbot.load_users() == set(users)


def test_migrate_broken_json_keeps_going(tmp_users_db):
    with open(foxbot.USERS_FILE, 'w', encoding='utf-8') as f:
        f.write('{не json')
    foxbot.migrate_users_json()  # не падает
    assert foxbot.load_users() == set()


# ==== Фото лис ====

class _FakeResponse:
    def __init__(self, payload, fail=False):
        self.payload = payload
        self.fail = fail

    def raise_for_status(self):
        if self.fail:
            raise foxbot.requests.HTTPError('500')

    def json(self):
        return self.payload


def test_get_new_image_success(monkeypatch):
    monkeypatch.setattr(
        foxbot.requests, 'get',
        lambda *a, **kw: _FakeResponse({'image': 'https://example.com/fox.jpg'}),
    )
    assert foxbot.get_new_image() == 'https://example.com/fox.jpg'


def test_get_new_image_fallback_to_cat(monkeypatch):
    def fake_get(url, **kw):
        if 'randomfox' in url:
            return _FakeResponse({}, fail=True)
        return _FakeResponse([{'url': 'https://example.com/cat.jpg'}])

    monkeypatch.setattr(foxbot.requests, 'get', fake_get)
    assert foxbot.get_new_image() == 'https://example.com/cat.jpg'


def test_get_new_image_total_failure(monkeypatch):
    def broken_get(*a, **kw):
        raise foxbot.requests.ConnectionError()

    monkeypatch.setattr(foxbot.requests, 'get', broken_get)
    assert foxbot.get_new_image() is None


# ==== ИИ-история ====

def test_ask_ai_keeps_system_prompt_and_limit(tmp_users_db, monkeypatch):
    monkeypatch.setattr(foxbot, 'AI_HISTORY_LIMIT', 4)
    sent_payloads = []

    def fake_post(url, **kw):
        # копия, т.к. ask_ai потом мутирует тот же список messages
        sent_payloads.append([dict(m) for m in kw['json']['messages']])
        return _FakeResponse({'choices': [{'message': {'content': 'Фыр!'}}]})

    monkeypatch.setattr(foxbot.requests, 'post', fake_post)
    foxbot.histories.clear()
    for i in range(6):
        assert foxbot.ask_ai(1, f'сообщение {i}') == 'Фыр!'

    # Контракт: в API уходит системный промпт + ровно AI_HISTORY_LIMIT сообщений
    last_request = sent_payloads[-1]
    assert len(last_request) == 1 + foxbot.AI_HISTORY_LIMIT
    assert last_request[0]['role'] == 'system'
    assert last_request[-1] == {'role': 'user', 'content': 'сообщение 5'}

    # В памяти история ограничена: ответ ассистента добавляется после обрезки,
    # но на следующем ходу обрезается — расти бесконечно не может
    assert len(foxbot.histories[1]) <= 2 + foxbot.AI_HISTORY_LIMIT


# ==== Статистика ====

def test_track_message_and_get_stats(tmp_users_db):
    data = foxbot.get_stats()
    assert data['total_users'] == 0
    assert data['today_messages'] == 0
    assert data['today_active'] == 0
    for _ in range(3):
        foxbot.track_message(111)
    foxbot.track_message(222)  # второй активный пользователь
    foxbot.save_user(111)
    data = foxbot.get_stats()
    assert data['total_users'] == 1
    assert data['today_messages'] == 4
    assert data['week_messages'] == 4
    assert data['today_active'] == 2
    assert data['top_hours']  # хотя бы один час записан


def test_remove_user(tmp_users_db):
    foxbot.save_user(111)
    foxbot.save_user(222)
    foxbot.remove_user(111)
    assert foxbot.load_users() == {222}
    foxbot.remove_user(111)  # повторное удаление — no-op
    assert foxbot.load_users() == {222}


# ==== Служебные ключи (meta) ====

def test_meta_roundtrip(tmp_users_db):
    assert foxbot._meta_get('last_daily_fox') is None
    foxbot._meta_set('last_daily_fox', '2026-11-13')
    assert foxbot._meta_get('last_daily_fox') == '2026-11-13'
    foxbot._meta_set('last_daily_fox', '2026-11-14')  # перезапись
    assert foxbot._meta_get('last_daily_fox') == '2026-11-14'


# ==== Персистентность ИИ-истории ====

def test_history_survives_memory_loss(tmp_users_db, monkeypatch):
    monkeypatch.setattr(foxbot.requests, 'post', _fake_post_factory([]))
    foxbot.histories.clear()
    foxbot.last_ai_activity.clear()
    foxbot.ask_ai(1, 'запомни это')
    saved = list(foxbot.histories[1])

    # имитируем рестарт: память пуста, история должна подняться из SQLite
    foxbot.histories.clear()
    foxbot.last_ai_activity.clear()
    foxbot._load_history(1)
    assert foxbot.histories[1] == saved


def test_forget_removes_history_from_db(tmp_users_db, monkeypatch):
    monkeypatch.setattr(foxbot.requests, 'post', _fake_post_factory([]))
    monkeypatch.setattr(foxbot.bot, 'send_message', lambda *a, **kw: None)
    foxbot.histories.clear()
    foxbot.last_ai_activity.clear()
    foxbot.ask_ai(1, 'привет')

    foxbot.forget_history(_msg('/forget', chat_id=1))
    assert 1 not in foxbot.histories
    foxbot._load_history(1)  # в БД тоже пусто
    assert 1 not in foxbot.histories


# ==== TTL истории ====

def _fake_post_factory(sent, answer='Фыр!'):
    def fake_post(url, **kw):
        sent.append([dict(m) for m in kw['json']['messages']])
        return _FakeResponse({'choices': [{'message': {'content': answer}}]})
    return fake_post


def test_ask_ai_ttl_expires_old_history(tmp_users_db, monkeypatch):
    monkeypatch.setattr(foxbot.requests, 'post', _fake_post_factory([]))
    foxbot.histories.clear()
    foxbot.last_ai_activity.clear()
    foxbot.ask_ai(1, 'привет')
    assert len(foxbot.histories[1]) == 3  # system + user + assistant

    # свежая активность — история накапливается
    foxbot.ask_ai(1, 'ещё')
    assert len(foxbot.histories[1]) == 5

    # протухшая активность — история сброшена автоматически
    foxbot.last_ai_activity[1] = foxbot.time.time() - 100 * 3600
    foxbot.ask_ai(1, 'после долгого сна')
    assert len(foxbot.histories[1]) == 3
    assert foxbot.histories[1][-2]['content'] == 'после долгого сна'


# ==== Одноразовые ИИ-запросы (/foxfact) ====

def test_ask_ai_once_isolated_from_history(monkeypatch):
    sent = []
    monkeypatch.setattr(foxbot.requests, 'post', _fake_post_factory(sent, answer='Факт'))
    foxbot.histories.clear()
    assert foxbot.ask_ai_once('скажи факт') == 'Факт'
    assert foxbot.histories == {}          # история не тронута
    assert len(sent[0]) == 1               # без системного промпта
    assert sent[0][0]['role'] == 'user'


# ==== Рассылка с подтверждением ====

def _msg(text, user_id=42, chat_id=42, reply_to_message=None):
    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(id=user_id),
        chat=SimpleNamespace(id=chat_id),
        reply_to_message=reply_to_message,
    )


def _call(data, user_id=42):
    return SimpleNamespace(
        id='c1',
        data=data,
        from_user=SimpleNamespace(id=user_id),
        message=SimpleNamespace(chat=SimpleNamespace(id=42)),
    )


@pytest.fixture()
def admin_env(tmp_users_db, monkeypatch):
    "Админский окружение: ADMIN_ID=42, подменённые send-функции бота."
    sent = []
    monkeypatch.setattr(foxbot, 'ADMIN_ID', 42)
    monkeypatch.setattr(
        foxbot.bot, 'send_message',
        lambda chat_id, text, **kw: sent.append((chat_id, text)),
    )
    monkeypatch.setattr(
        foxbot.bot, 'answer_callback_query', lambda *a, **kw: None
    )
    foxbot.pending_broadcasts.clear()
    return sent


def test_broadcast_waits_for_confirmation(admin_env):
    foxbot.broadcast(_msg('/broadcast Привет, лисы!'))
    assert foxbot.pending_broadcasts[42] == 'Привет, лисы!'
    # отправлен только запрос подтверждения админу — рассылки ещё нет
    assert len(admin_env) == 1
    assert not any('Рассылка завершена' in text for _, text in admin_env)


def test_broadcast_cancel(admin_env):
    foxbot.broadcast(_msg('/broadcast Тест'))
    foxbot.broadcast_confirm(_call('bc_cancel'))
    assert 42 not in foxbot.pending_broadcasts
    assert any('отменена' in text for _, text in admin_env)


def test_broadcast_send_without_pending(admin_env):
    foxbot.broadcast_confirm(_call('bc_send'))
    assert any('протух' in text for _, text in admin_env)


def test_broadcast_full_flow(admin_env):
    foxbot.broadcast(_msg('/broadcast Вторая попытка'))
    foxbot.broadcast_confirm(_call('bc_send'))
    assert any('Рассылка завершена' in text for _, text in admin_env)
    assert 42 not in foxbot.pending_broadcasts


def test_broadcast_stranger_cannot_confirm(admin_env):
    foxbot.broadcast(_msg('/broadcast Секрет'))
    foxbot.broadcast_confirm(_call('bc_send', user_id=99))  # не админ
    assert 42 in foxbot.pending_broadcasts  # рассылка не подтверждена


# ==== Рассылка реплаем (любой контент) ====

def test_broadcast_reply_copy_mode(admin_env, monkeypatch):
    copied = []
    monkeypatch.setattr(
        foxbot.bot, 'copy_message',
        lambda chat_id, from_chat_id, message_id: copied.append(
            (chat_id, from_chat_id, message_id)
        ),
    )
    foxbot.save_user(7)
    photo = SimpleNamespace(
        chat=SimpleNamespace(id=42), message_id=555,
        text=None, caption='смотри какое фото', content_type='photo',
    )
    foxbot.broadcast(_msg('/broadcast', reply_to_message=photo))
    assert foxbot.pending_broadcasts[42] == ('copy', 42, 555)

    foxbot.broadcast_confirm(_call('bc_send'))
    assert copied == [(7, 42, 555)]  # копия ушла пользователю
    assert 42 not in foxbot.pending_broadcasts


# ==== Мини-игра «Угадай число» ====

@pytest.fixture()
def game_env(tmp_users_db, monkeypatch):
    sent = []
    monkeypatch.setattr(
        foxbot.bot, 'send_message',
        lambda chat_id, text, **kw: sent.append((chat_id, text)),
    )
    monkeypatch.setattr(foxbot.random, 'randint', lambda a, b: 42)
    foxbot.games.clear()
    return sent


def test_number_game_win(game_env):
    foxbot._start_number_game(1)
    assert foxbot.games[1] == {'number': 42, 'tries': 0}

    foxbot.say_hi(_msg('50', chat_id=1))
    assert 'Меньше' in game_env[-1][1]
    foxbot.say_hi(_msg('42', chat_id=1))
    assert 'Угадал за 2' in game_env[-1][1]
    assert 1 not in foxbot.games  # игра закончилась


def test_number_game_not_a_digit(game_env):
    foxbot._start_number_game(1)
    foxbot.say_hi(_msg('привет', chat_id=1))
    assert 'число от 1 до 100' in game_env[-1][1]
    assert foxbot.games[1]['tries'] == 0  # не засчитано как попытка


def test_stopgame(game_env):
    foxbot._start_number_game(1)
    foxbot.stop_game(_msg('/stopgame', chat_id=1))
    assert 1 not in foxbot.games
    assert 'Сдаёшься' in game_env[-1][1]


# ==== Настроение лисёнка (/mood) ====

def test_mood_set_get_and_prompt(tmp_users_db):
    assert foxbot._get_mood(1) is None
    assert foxbot._system_prompt(1) == foxbot.FOX_SYSTEM_PROMPT
    foxbot._set_mood(1, 'romantic')
    assert foxbot._get_mood(1) == 'romantic'
    assert foxbot.MOODS['romantic'] in foxbot._system_prompt(1)
    foxbot._set_mood(1, 'grumpy')
    assert foxbot._get_mood(1) == 'grumpy'
    assert foxbot.MOODS['grumpy'] in foxbot._system_prompt(1)


def test_mood_updates_running_dialog(tmp_users_db, monkeypatch):
    sent_payloads = []
    monkeypatch.setattr(foxbot.requests, 'post', _fake_post_factory(sent_payloads))
    monkeypatch.setattr(foxbot.bot, 'send_message', lambda *a, **kw: None)
    foxbot.histories.clear()
    foxbot.last_ai_activity.clear()
    foxbot.ask_ai(1, 'привет')
    assert sent_payloads[-1][0]['content'] == foxbot.FOX_SYSTEM_PROMPT

    foxbot.mood_cmd(_msg('/mood calm', chat_id=1))
    assert foxbot._get_mood(1) == 'calm'
    # промпт в живой истории обновлён на лету, диалог не потерян
    assert foxbot.MOODS['calm'] in foxbot.histories[1][0]['content']
    foxbot.ask_ai(1, 'как дела')
    assert foxbot.MOODS['calm'] in sent_payloads[-1][0]['content']


def test_mood_unknown_arg_shows_help(tmp_users_db, monkeypatch):
    sent = []
    monkeypatch.setattr(
        foxbot.bot, 'send_message',
        lambda chat_id, text, **kw: sent.append(text),
    )
    foxbot.mood_cmd(_msg('/mood angry', chat_id=1))
    assert '/mood playful' in sent[-1]
    assert foxbot._get_mood(1) is None  # невалидное настроение не сохранено


# ==== Rate limiting на ИИ ====

def test_rate_limit_blocks_after_limit(monkeypatch):
    monkeypatch.setattr(foxbot, 'AI_RATE_LIMIT', 3)
    monkeypatch.setattr(foxbot, 'ADMIN_ID', 0)
    foxbot.ai_requests.clear()
    assert foxbot._ai_rate_ok(1) is True
    assert foxbot._ai_rate_ok(1) is True
    assert foxbot._ai_rate_ok(1) is True
    assert foxbot._ai_rate_ok(1) is False  # лимит исчерпан
    assert foxbot._ai_rate_ok(2) is True   # другой чат не затронут


def test_rate_limit_window_expires(monkeypatch):
    monkeypatch.setattr(foxbot, 'AI_RATE_LIMIT', 1)
    monkeypatch.setattr(foxbot, 'ADMIN_ID', 0)
    foxbot.ai_requests.clear()
    assert foxbot._ai_rate_ok(1) is True
    # старая метка протухла — окно снова свободно
    foxbot.ai_requests[1] = [foxbot.time.time() - foxbot.AI_RATE_WINDOW - 1]
    assert foxbot._ai_rate_ok(1) is True


def test_rate_limit_admin_exempt(monkeypatch):
    monkeypatch.setattr(foxbot, 'AI_RATE_LIMIT', 1)
    monkeypatch.setattr(foxbot, 'ADMIN_ID', 42)
    foxbot.ai_requests.clear()
    foxbot._ai_rate_ok(42)
    assert foxbot._ai_rate_ok(42) is True  # админ без лимита
