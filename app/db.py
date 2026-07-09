"""Хранилище данных админ-панели на стандартном sqlite3 (без внешних зависимостей).

Здесь живёт всё, что показывает и редактирует панель: клиенты (гости), их
переписка (отправленные уведомления и ручные ответы менеджера), брони, журнал
сбоёв, администраторы панели с сессиями и настройки отправки.

Подход намеренно простой: на каждый запрос открываем короткое соединение
(контекст-менеджер `connect()`), коммитим на выходе. SQLite в режиме WAL спокойно
тянет такую нагрузку, а отсутствие глобального соединения снимает проблемы с
потоками (FastAPI выполняет sync-эндпоинты в пуле потоков).
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterator

from .config import settings

log = logging.getLogger("limemaxbot.db")

# Сколько минут бот «молчит» в чате после ручного ответа менеджера.
HANDOFF_MINUTES = 10

# Сколько дней живёт сессия панели. Совпадает с max_age cookie (см. auth.py):
# по истечении токен считается недействительным и удаляется на сервере.
SESSION_TTL_DAYS = 30


def now_iso() -> str:
    """Текущий момент в ISO с таймзоной (UTC) — браузер сам покажет в локальной."""
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Короткоживущее соединение с БД. Коммитит при успешном выходе."""
    conn = sqlite3.connect(settings.db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS admins (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  username      TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  role          TEXT NOT NULL DEFAULT 'manager',   -- owner / manager
  created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  token      TEXT PRIMARY KEY,
  admin_id   INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS clients (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  phone         TEXT UNIQUE NOT NULL,
  name          TEXT,
  notes         TEXT DEFAULT '',
  messenger     TEXT DEFAULT 'max',
  is_bot_paused INTEGER DEFAULT 0,
  paused_until  TEXT,
  no_max        INTEGER DEFAULT 0,   -- 1, если у номера нет аккаунта MAX (или скрыт приватностью)
  created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  client_id       INTEGER NOT NULL,
  sender_type     TEXT NOT NULL,        -- bot / manager / client
  kind            TEXT,                 -- created / updated / cancelled / manager
  text            TEXT,
  media           TEXT,                 -- JSON: [{url,kind,name}]
  delivered       INTEGER DEFAULT 0,
  -- Реквизиты сообщения на стороне мессенджера — чтобы потом править/удалять
  -- его через шлюз kapuchino-api (editMessage/deleteMessage). Заполняются только
  -- для исходящих, что реально ушли (бот/менеджер); у входящих пусто.
  channel         TEXT,                 -- max / telegram / whatsapp
  gateway_chat_id TEXT,                 -- chatId, в который отправляли
  gateway_msg_id  TEXT,                 -- id сообщения в мессенджере (для файлов — через запятую)
  created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bookings (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  reserve_id TEXT,
  client_id  INTEGER,
  status     TEXT NOT NULL DEFAULT 'booked',   -- booked / completed / canceled
  place      TEXT,
  date_str   TEXT,
  time_str   TEXT,
  guests     TEXT,
  amount     REAL DEFAULT 0,
  end_at     TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_bookings_reserve ON bookings(reserve_id)
  WHERE reserve_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS alerts (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  source     TEXT,
  message    TEXT,
  seen       INTEGER DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS login_log (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  username   TEXT,
  ip         TEXT,
  ok         INTEGER,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bot_settings (
  id             INTEGER PRIMARY KEY CHECK (id = 1),
  enabled        INTEGER DEFAULT 1,
  notify_types   TEXT,
  dry_run        INTEGER DEFAULT 0,
  owner_telegram TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);
"""


def init_db() -> None:
    """Создаёт таблицы (идемпотентно) и базовые строки настроек/владельца."""
    with connect() as c:
        c.executescript(SCHEMA)
    _migrate()
    delete_expired_sessions()  # подчищаем протухшие токены при старте
    _ensure_bot_settings()
    bootstrap_owner()


def _migrate() -> None:
    """Лёгкие миграции схемы для уже существующих БД (ALTER TABLE ADD COLUMN).

    SCHEMA с `IF NOT EXISTS` не добавляет колонки в уже созданные таблицы,
    поэтому новые поля для правки/удаления сообщений в мессенджере доводим тут.
    """
    with connect() as c:
        have = {r["name"] for r in c.execute("PRAGMA table_info(messages)").fetchall()}
        for col in ("channel", "gateway_chat_id", "gateway_msg_id"):
            if col not in have:
                c.execute(f"ALTER TABLE messages ADD COLUMN {col} TEXT")
        chave = {r["name"] for r in c.execute("PRAGMA table_info(clients)").fetchall()}
        if "no_max" not in chave:
            c.execute("ALTER TABLE clients ADD COLUMN no_max INTEGER DEFAULT 0")


# --------------------------------------------------------------------------- #
#  Администраторы, пароли, сессии
# --------------------------------------------------------------------------- #
def hash_password(password: str) -> str:
    """PBKDF2-хеш вида 'pbkdf2$<salt>$<hash>'."""
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000)
    return f"pbkdf2${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _algo, salt, digest = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000)
        return secrets.compare_digest(dk.hex(), digest)
    except (ValueError, AttributeError):
        return False


def create_admin(username: str, password: str, role: str = "manager") -> int:
    with connect() as c:
        cur = c.execute(
            "INSERT INTO admins(username, password_hash, role, created_at) VALUES(?,?,?,?)",
            (username, hash_password(password), role, now_iso()),
        )
        return int(cur.lastrowid)


def bootstrap_owner() -> None:
    """Если в БД нет ни одного админа — создаём владельца из .env."""
    with connect() as c:
        n = c.execute("SELECT COUNT(*) AS n FROM admins").fetchone()["n"]
    if n:
        return
    create_admin(settings.admin_user, settings.admin_password, "owner")
    log.warning(
        "Создан владелец панели «%s» (пароль из .env ADMIN_PASSWORD). "
        "Обязательно смените пароль для боевого режима!",
        settings.admin_user,
    )


def find_admin(username: str) -> dict | None:
    with connect() as c:
        row = c.execute("SELECT * FROM admins WHERE username=?", (username,)).fetchone()
    return dict(row) if row else None


def create_session(admin_id: int) -> str:
    token = secrets.token_urlsafe(32)
    with connect() as c:
        c.execute(
            "INSERT INTO sessions(token, admin_id, created_at) VALUES(?,?,?)",
            (token, admin_id, now_iso()),
        )
    return token


def session_admin(token: str | None) -> dict | None:
    """Админ по токену сессии или None.

    Просроченные сессии (старше SESSION_TTL_DAYS) не принимаем и сразу удаляем —
    чтобы утёкший старый токен нельзя было использовать вечно.
    """
    if not token:
        return None
    cutoff = (datetime.now(timezone.utc) - timedelta(days=SESSION_TTL_DAYS)).isoformat()
    with connect() as c:
        row = c.execute(
            "SELECT a.*, s.created_at AS session_started FROM sessions s "
            "JOIN admins a ON a.id = s.admin_id WHERE s.token=?",
            (token,),
        ).fetchone()
        if not row:
            return None
        if (row["session_started"] or "") < cutoff:
            c.execute("DELETE FROM sessions WHERE token=?", (token,))
            return None
    admin = dict(row)
    admin.pop("session_started", None)
    return admin


def delete_session(token: str | None) -> None:
    if not token:
        return
    with connect() as c:
        c.execute("DELETE FROM sessions WHERE token=?", (token,))


def delete_expired_sessions() -> None:
    """Удаляет из БД сессии старше SESSION_TTL_DAYS (гигиена таблицы sessions)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=SESSION_TTL_DAYS)).isoformat()
    with connect() as c:
        c.execute("DELETE FROM sessions WHERE created_at < ?", (cutoff,))


def recent_failed_logins(ip: str, minutes: int) -> int:
    """Сколько неудачных попыток входа было с этого IP за последние `minutes` минут.

    Используется как простой антиперебор: слишком много промахов подряд → отказ
    (см. app/auth.py). Опирается на уже существующий журнал входов login_log.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    with connect() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM login_log WHERE ip=? AND ok=0 AND created_at >= ?",
            (ip, cutoff),
        ).fetchone()
    return int(row["n"])


def add_login_log(username: str, ip: str, ok: bool) -> None:
    with connect() as c:
        c.execute(
            "INSERT INTO login_log(username, ip, ok, created_at) VALUES(?,?,?,?)",
            (username, ip, 1 if ok else 0, now_iso()),
        )


# --------------------------------------------------------------------------- #
#  Настройки отправки уведомлений
# --------------------------------------------------------------------------- #
def _ensure_bot_settings() -> None:
    """Создаёт единственную строку настроек, унаследовав значения из .env."""
    with connect() as c:
        exists = c.execute("SELECT 1 FROM bot_settings WHERE id=1").fetchone()
        if exists:
            return
        c.execute(
            "INSERT INTO bot_settings(id, enabled, notify_types, dry_run, owner_telegram) "
            "VALUES(1, 1, ?, ?, '')",
            (settings.enabled_notifications, 1 if settings.dry_run else 0),
        )


def get_bot_settings() -> dict:
    with connect() as c:
        row = c.execute("SELECT * FROM bot_settings WHERE id=1").fetchone()
    types = [t.strip() for t in (row["notify_types"] or "").split(",") if t.strip()]
    return {
        "enabled": bool(row["enabled"]),
        "notify_types": types,
        "dry_run": bool(row["dry_run"]),
        "owner_telegram": row["owner_telegram"] or "",
    }


def save_bot_settings(enabled: bool, notify_types: list[str], dry_run: bool, owner_telegram: str) -> None:
    types_csv = ",".join(t for t in notify_types if t in ("created", "updated", "cancelled"))
    with connect() as c:
        c.execute(
            "UPDATE bot_settings SET enabled=?, notify_types=?, dry_run=?, owner_telegram=? WHERE id=1",
            (1 if enabled else 0, types_csv, 1 if dry_run else 0, owner_telegram),
        )


# --------------------------------------------------------------------------- #
#  Клиенты, сообщения, брони — запись из вебхука и из панели
# --------------------------------------------------------------------------- #
def upsert_client(phone: str, name: str | None) -> int:
    """Находит клиента по телефону или создаёт; обновляет имя, если появилось."""
    with connect() as c:
        row = c.execute("SELECT id, name FROM clients WHERE phone=?", (phone,)).fetchone()
        if row:
            if name and name != row["name"]:
                c.execute("UPDATE clients SET name=? WHERE id=?", (name, row["id"]))
            return int(row["id"])
        cur = c.execute(
            "INSERT INTO clients(phone, name, created_at) VALUES(?,?,?)",
            (phone, name or "Гость", now_iso()),
        )
        return int(cur.lastrowid)


def set_client_no_max(client_id: int, absent: bool) -> None:
    """Помечает, есть ли у клиента аккаунт MAX.

    Ставится (absent=True) при отправке, отклонённой шлюзом с ошибкой резолва
    номера в MAX, и сбрасывается (absent=False), как только сообщение клиенту
    снова успешно уходит (значит, аккаунт появился/стал виден).
    """
    with connect() as c:
        c.execute("UPDATE clients SET no_max=? WHERE id=?", (1 if absent else 0, client_id))


def upsert_booking(
    reserve_id: object,
    client_id: int,
    status: str,
    place: str,
    date_str: str | None,
    time_str: str | None,
    guests: object,
    amount: float,
    end_at: str | None,
) -> int:
    """Создаёт/обновляет бронь по reserve_id (если он есть)."""
    rid = None if reserve_id in (None, "", 0, "0") else str(reserve_id)
    now = now_iso()
    with connect() as c:
        row = None
        if rid is not None:
            row = c.execute("SELECT id FROM bookings WHERE reserve_id=?", (rid,)).fetchone()
        if row:
            c.execute(
                "UPDATE bookings SET client_id=?, status=?, place=?, date_str=?, time_str=?, "
                "guests=?, amount=?, end_at=?, updated_at=? WHERE id=?",
                (client_id, status, place, date_str, time_str, str(guests or ""), amount, end_at, now, row["id"]),
            )
            return int(row["id"])
        cur = c.execute(
            "INSERT INTO bookings(reserve_id, client_id, status, place, date_str, time_str, "
            "guests, amount, end_at, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (rid, client_id, status, place, date_str, time_str, str(guests or ""), amount, end_at, now, now),
        )
        return int(cur.lastrowid)


def add_message(
    client_id: int,
    sender_type: str,
    text: str,
    kind: str | None = None,
    delivered: bool = False,
    media: list | None = None,
    channel: str | None = None,
    gateway_chat_id: str | None = None,
    gateway_msg_id: str | None = None,
) -> int:
    with connect() as c:
        cur = c.execute(
            "INSERT INTO messages(client_id, sender_type, kind, text, media, delivered, "
            "channel, gateway_chat_id, gateway_msg_id, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                client_id,
                sender_type,
                kind,
                text,
                json.dumps(media, ensure_ascii=False) if media else None,
                1 if delivered else 0,
                channel,
                gateway_chat_id,
                gateway_msg_id,
                now_iso(),
            ),
        )
        return int(cur.lastrowid)


def get_message(message_id: int) -> dict | None:
    """Сообщение по id вместе с телефоном клиента (для правки/удаления в мессенджере)."""
    with connect() as c:
        row = c.execute(
            "SELECT m.*, cl.phone AS phone FROM messages m "
            "LEFT JOIN clients cl ON cl.id = m.client_id WHERE m.id=?",
            (message_id,),
        ).fetchone()
    return dict(row) if row else None


def pause_bot(client_id: int, minutes: int = HANDOFF_MINUTES) -> None:
    """Менеджер ответил вручную — помечаем чат как «ведёт менеджер» на N минут."""
    until = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
    with connect() as c:
        c.execute(
            "UPDATE clients SET is_bot_paused=1, paused_until=? WHERE id=?",
            (until, client_id),
        )


def clear_expired_pauses() -> None:
    """Снимает паузу с чатов, где время «тишины» уже истекло."""
    now = now_iso()
    with connect() as c:
        c.execute(
            "UPDATE clients SET is_bot_paused=0, paused_until=NULL "
            "WHERE paused_until IS NOT NULL AND paused_until < ?",
            (now,),
        )


def complete_past_bookings() -> int:
    """Переводит отыгравшие по времени брони в статус 'completed' прямо в БД.

    Правило: бронь со статусом 'booked' и заполненным временем окончания
    (end_at) в прошлом считается выполненной. Отменённые ('canceled') и уже
    завершённые ('completed') не трогаем, ручной статус остаётся приоритетным.

    Сравнение ведём в наивном локальном времени (datetime.now vs end_at из
    вебхука Restoplace) — той же логикой, что и dashboard_api._effective_status,
    чтобы поведение авто-статуса и записи в БД совпадало. now_iso() (UTC) тут
    использовать нельзя: end_at наивный, сравнение разъехалось бы по TZ.
    Возвращает число обновлённых броней.
    """
    now = datetime.now()
    with connect() as c:
        rows = c.execute(
            "SELECT id, end_at FROM bookings "
            "WHERE status='booked' AND end_at IS NOT NULL AND end_at <> ''"
        ).fetchall()
        due = []
        for r in rows:
            try:
                if datetime.fromisoformat(r["end_at"]) < now:
                    due.append(r["id"])
            except (ValueError, TypeError):
                continue
        if due:
            ts = now_iso()
            c.executemany(
                "UPDATE bookings SET status='completed', updated_at=? WHERE id=?",
                [(ts, i) for i in due],
            )
        return len(due)


# --------------------------------------------------------------------------- #
#  Шаблоны сообщений клиенту (created / updated / cancelled)
# --------------------------------------------------------------------------- #
TEMPLATE_KINDS = ("created", "updated", "cancelled")


def get_saved_templates() -> dict:
    """Только явно сохранённые в БД шаблоны (без подстановки дефолтов).

    Нужно панели, чтобы отличить «оператор сохранил такой текст» от «тут дефолт» и
    не затирать локальный черновик в браузере для ещё не сохранённых типов.
    """
    raw = get_meta("templates")
    if not raw:
        return {}
    try:
        saved = json.loads(raw)
    except ValueError:
        return {}
    return {k: v for k, v in saved.items() if k in TEMPLATE_KINDS and isinstance(v, str) and v.strip()}


def get_templates() -> dict:
    """Шаблоны уведомлений из БД; недостающие добиваем встроенными примерами.

    Это серверный источник истины: их рендерит бэкенд при отправке и редактирует
    панель (вкладка «Бот»). Раньше шаблоны жили только в localStorage браузера.
    """
    from .messages import DEFAULT_TEMPLATES

    saved = get_saved_templates()
    return {k: saved.get(k) or DEFAULT_TEMPLATES[k] for k in TEMPLATE_KINDS}


def save_templates(templates: dict) -> None:
    """Сохраняет шаблоны (только три известных типа) одной JSON-строкой в meta."""
    clean = {k: str(templates.get(k, "")) for k in TEMPLATE_KINDS}
    set_meta("templates", json.dumps(clean, ensure_ascii=False))


# --------------------------------------------------------------------------- #
#  Журнал сбоёв
# --------------------------------------------------------------------------- #
def add_alert(source: str, message: str) -> None:
    with connect() as c:
        c.execute(
            "INSERT INTO alerts(source, message, seen, created_at) VALUES(?,?,0,?)",
            (source, message, now_iso()),
        )


# --------------------------------------------------------------------------- #
#  Произвольные служебные значения (например, время последнего вебхука)
# --------------------------------------------------------------------------- #
def set_meta(key: str, value: str) -> None:
    with connect() as c:
        c.execute(
            "INSERT INTO meta(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def get_meta(key: str) -> str | None:
    with connect() as c:
        row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None
