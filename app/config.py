"""Конфигурация приложения. Читается из переменных окружения и файла .env."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Шлюз kapuchino-api (личный аккаунт MAX) ---
    # Самохостинг-шлюз (аналог green-api) из соседнего проекта kapuchino-api.
    # Принимает POST /api/v1/{channel}/sendMessage с Bearer-токеном и шлёт
    # сообщение в MAX по номеру телефона (chatId вида +79991234567).
    gateway_url: str = "http://127.0.0.1:8080"
    gateway_token: str = ""
    # Канал шлюза: max (можно whatsapp/telegram, если они привязаны в kapuchino).
    gateway_channel: str = "max"
    # Тестовый получатель: если задан, ВСЕ уведомления уходят в один чат
    # (для проверки без рассылки гостям). Формат — номер с «+» (+79991234567)
    # или готовый числовой chatId MAX. Пусто — слать гостю по его номеру.
    test_chat_id: str = ""

    # --- Webhook ---
    webhook_path: str = "/webhook/restoplace"
    webhook_secret: str = ""

    # --- Логика ---
    # Типы уведомлений через запятую: created, updated, cancelled.
    # ВАЖНО: отмена приходит как event=reserve.updated со status=6 — поэтому
    # фильтруем по СМЫСЛУ события, а не по его имени.
    # По умолчанию: подтверждение брони + отмена (без шумных правок).
    enabled_notifications: str = "created,cancelled"
    # Файл-справочник внутренних id -> человекочитаемое название объекта
    # (например {"789368": "Беседка №2"}). Если файла нет — выводится тип+зона.
    items_file: str = "items.json"
    # Файл со снимками броней (для диффа значимых изменений при reserve.updated).
    state_file: str = "state/reservations.json"
    dry_run: bool = False
    signature: str = ""

    # --- Админ-панель ---
    # Файл базы данных SQLite (клиенты, брони, сообщения, админы панели и т.п.).
    db_path: str = "bot_database.db"
    # Папка для вложений, которые менеджер шлёт из панели (отдаётся как /media).
    media_dir: str = "media"
    # Имя cookie сессии и нужен ли флаг Secure (по http локально — False).
    session_cookie: str = "lime_session"
    cookie_secure: bool = False
    # Логин/пароль владельца, создаётся автоматически при первом старте, если
    # в БД ещё нет ни одного администратора. Поменяйте в .env для боевого режима.
    admin_user: str = "admin"
    admin_password: str = "admin12345"

    # --- Прочее ---
    http_timeout: float = 20.0
    log_level: str = "INFO"
    host: str = "0.0.0.0"
    port: int = 8000

    @property
    def notification_set(self) -> set[str]:
        """Набор включённых типов уведомлений (created/updated/cancelled)."""
        return {a.strip() for a in self.enabled_notifications.split(",") if a.strip()}

    @property
    def channel(self) -> str:
        """Канал шлюза для отправки (max по умолчанию)."""
        return self.gateway_channel.strip().lower() or "max"

    @property
    def is_configured(self) -> bool:
        """Готов ли шлюз kapuchino-api к реальной отправке (есть URL и токен)."""
        return bool(self.gateway_url and self.gateway_token)


settings = Settings()
