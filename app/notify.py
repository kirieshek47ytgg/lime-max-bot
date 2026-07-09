"""
Дублирование важных событий владельцу в Telegram (Надёжность).

Владелец указывает в панели свой Telegram @username (`owner_telegram` в настройках
бота). Туда уходят: подтверждённые уведомления гостю (бронь создана/изменена/
отменена) и сбои отправки. Канал всегда "telegram" — независимо от того, что
основной канал бота (settings.channel) — MAX: у kapuchino-api Telegram работает
через личный аккаунт (GramJS-userbot), поэтому умеет слать на @username напрямую,
без необходимости, чтобы владелец сначала написал боту.

Глобальный DRY_RUN (.env) по-прежнему глушит и эти уведомления (см. gateway.send_message)
— это единственный переключатель, который их отключает; локальный dry_run из панели
(«не слать гостям на время теста») на них не влияет.
"""
from __future__ import annotations

import logging

from . import db, gateway

log = logging.getLogger("limemaxbot.notify")


async def notify_owner(text: str) -> None:
    """Шлёт сообщение владельцу в Telegram, если он указал свой @username. Тихо игнорирует, если нет."""
    chat = (db.get_bot_settings().get("owner_telegram") or "").strip()
    if not chat:
        return
    try:
        await gateway.send_message(chat, text, channel="telegram")
    except gateway.GatewayError as exc:  # уведомление владельцу не должно ронять основную логику
        log.warning("Не удалось уведомить владельца в Telegram: %s", exc)


async def record_alert(source: str, message: str) -> None:
    """Записывает сбой в журнал И дублирует его владельцу в Telegram."""
    db.add_alert(source, message)
    await notify_owner(f"⚠ Сбой ({source}): {message}")
