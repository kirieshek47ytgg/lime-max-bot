"""Клиент шлюза kapuchino-api для отправки сообщений в мессенджер MAX.

kapuchino-api — самохостинг-аналог green-api из соседнего проекта: держит
привязанный личный аккаунт MAX и отдаёт единый REST. Мы ходим в него по HTTP
с Bearer-токеном:

    POST {gateway_url}/api/v1/{channel}/sendMessage    {chatId, message}
    POST {gateway_url}/api/v1/{channel}/sendFile       {chatId, fileBase64, fileName, caption}
    POST {gateway_url}/api/v1/{channel}/editMessage    {chatId, messageId, message}
    POST {gateway_url}/api/v1/{channel}/deleteMessage  {chatId, messageId}
    GET  {gateway_url}/api/v1/status

Получатель (chatId) для MAX — номер телефона с ведущим «+» (+79991234567):
сайдкар сам резолвит его в внутренний chatId. См. app/phone.to_chat_id.
"""

from __future__ import annotations

import base64
import logging

import httpx

from .config import settings

log = logging.getLogger("limemaxbot.gateway")


class GatewayError(Exception):
    """Ошибка обращения к шлюзу kapuchino-api."""


def _url(method: str, channel: str | None = None) -> str:
    base = settings.gateway_url.rstrip("/")
    ch = (channel or settings.channel).strip().lower()
    return f"{base}/api/v1/{ch}/{method}"


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.gateway_token}"}


async def send_message(
    chat_id: str, message: str, dry_run: bool | None = None, channel: str | None = None,
) -> dict:
    """Отправляет текстовое сообщение через шлюз.

    В режиме DRY_RUN (или при отсутствии реквизитов) ничего не отправляет —
    только пишет в лог и возвращает заглушку. Параметр ``dry_run`` позволяет
    переопределить значение из .env (например, из настроек панели).
    ``channel`` переопределяет канал по умолчанию (settings.channel = MAX) —
    нужен для дублирования владельцу в Telegram (см. app/notify.py).
    """
    payload = {"chatId": chat_id, "message": message}
    dry = settings.dry_run if dry_run is None else dry_run

    if dry or not settings.is_configured:
        reason = "DRY_RUN" if dry else "NOT_CONFIGURED"
        log.info("[%s] Пропуск отправки. chatId=%s\n%s", reason, chat_id, message)
        return {"idMessage": None, "skipped": True, "reason": reason}

    try:
        async with httpx.AsyncClient(timeout=settings.http_timeout) as client:
            resp = await client.post(_url("sendMessage", channel), json=payload, headers=_headers())
    except httpx.HTTPError as exc:
        raise GatewayError(f"Сетевая ошибка при обращении к шлюзу: {exc}") from exc

    if resp.status_code != 200:
        raise GatewayError(f"Шлюз вернул {resp.status_code}: {resp.text[:500]}")

    data = resp.json()
    if not data.get("ok", True):
        raise GatewayError(f"Шлюз отклонил отправку: {data.get('error') or data}")
    log.info("Сообщение отправлено chatId=%s idMessage=%s", chat_id, data.get("idMessage"))
    return data


# Загрузка видео может реально идти несколько минут (шлюз льёт файл в мессенджер
# синхронно и отвечает только после того, как тот его принял) — settings.http_timeout
# (по умолчанию 20с) для текста в самый раз, но для файлов слишком короткий: клиент
# отваливался по таймауту, пока шлюз ещё грузил файл, и панель показывала «отправлено»,
# хотя реальная доставка не завершилась (а то и уходила дублем при ретрае).
FILE_UPLOAD_TIMEOUT = 300.0


async def send_file_by_upload(
    chat_id: str,
    file_bytes: bytes,
    filename: str,
    caption: str = "",
    dry_run: bool | None = None,
) -> dict:
    """Отправляет файл/фото в MAX (метод sendFile, файл в base64).

    Используется, когда менеджер прикрепляет вложение в панели. В DRY_RUN или
    без реквизитов отправку пропускает.
    """
    dry = settings.dry_run if dry_run is None else dry_run
    if dry or not settings.is_configured:
        reason = "DRY_RUN" if dry else "NOT_CONFIGURED"
        log.info("[%s] Пропуск отправки файла %s в чат %s", reason, filename, chat_id)
        return {"idMessage": None, "skipped": True, "reason": reason}

    payload = {
        "chatId": chat_id,
        "fileName": filename,
        "fileBase64": base64.b64encode(file_bytes).decode("ascii"),
        "caption": caption,
    }
    try:
        async with httpx.AsyncClient(timeout=FILE_UPLOAD_TIMEOUT) as client:
            resp = await client.post(_url("sendFile"), json=payload, headers=_headers())
    except httpx.HTTPError as exc:
        raise GatewayError(f"Сетевая ошибка при отправке файла: {exc}") from exc

    if resp.status_code != 200:
        raise GatewayError(f"sendFile вернул {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    if not data.get("ok", True):
        raise GatewayError(f"Шлюз отклонил файл: {data.get('error') or data}")
    return data


async def edit_message(
    chat_id: str,
    message_id: str,
    message: str,
    channel: str | None = None,
    dry_run: bool | None = None,
) -> dict:
    """Редактирует ранее отправленное сообщение в мессенджере через шлюз.

    Канал берётся из ``channel`` (на каком отправляли), иначе из настроек.
    В DRY_RUN/без реквизитов правит только в панели — отправку пропускает.
    """
    dry = settings.dry_run if dry_run is None else dry_run
    if dry or not settings.is_configured:
        reason = "DRY_RUN" if dry else "NOT_CONFIGURED"
        log.info("[%s] Пропуск правки сообщения %s в чате %s", reason, message_id, chat_id)
        return {"idMessage": None, "skipped": True, "reason": reason}

    payload = {"chatId": chat_id, "messageId": str(message_id), "message": message}
    try:
        async with httpx.AsyncClient(timeout=settings.http_timeout) as client:
            resp = await client.post(
                _url("editMessage", channel), json=payload, headers=_headers()
            )
    except httpx.HTTPError as exc:
        raise GatewayError(f"Сетевая ошибка при правке сообщения: {exc}") from exc

    if resp.status_code != 200:
        raise GatewayError(f"editMessage вернул {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    if not data.get("ok", True):
        raise GatewayError(f"Шлюз отклонил правку: {data.get('error') or data}")
    log.info("Сообщение отредактировано chatId=%s messageId=%s", chat_id, message_id)
    return data


async def delete_message(
    chat_id: str,
    message_id: str,
    channel: str | None = None,
    dry_run: bool | None = None,
) -> dict:
    """Удаляет ранее отправленное сообщение в мессенджере через шлюз.

    В DRY_RUN/без реквизитов удаляет только из панели — отправку пропускает.
    """
    dry = settings.dry_run if dry_run is None else dry_run
    if dry or not settings.is_configured:
        reason = "DRY_RUN" if dry else "NOT_CONFIGURED"
        log.info("[%s] Пропуск удаления сообщения %s в чате %s", reason, message_id, chat_id)
        return {"skipped": True, "reason": reason}

    payload = {"chatId": chat_id, "messageId": str(message_id)}
    try:
        async with httpx.AsyncClient(timeout=settings.http_timeout) as client:
            resp = await client.post(
                _url("deleteMessage", channel), json=payload, headers=_headers()
            )
    except httpx.HTTPError as exc:
        raise GatewayError(f"Сетевая ошибка при удалении сообщения: {exc}") from exc

    if resp.status_code != 200:
        raise GatewayError(f"deleteMessage вернул {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    if not data.get("ok", True):
        raise GatewayError(f"Шлюз отклонил удаление: {data.get('error') or data}")
    log.info("Сообщение удалено chatId=%s messageId=%s", chat_id, message_id)
    return data


async def get_state_instance() -> dict:
    """Состояние канала шлюза (для индикаторов «Состояние систем»).

    Дёргает GET /api/v1/status и достаёт нужный канал. Приводим к старому
    формату {"stateInstance": ..., "configured": ...}, чтобы не менять панель.
    «authorized» означает, что аккаунт MAX привязан и готов слать.
    """
    if not settings.is_configured:
        return {"stateInstance": None, "configured": False}
    try:
        async with httpx.AsyncClient(timeout=settings.http_timeout) as client:
            resp = await client.get(
                f"{settings.gateway_url.rstrip('/')}/api/v1/status", headers=_headers()
            )
    except httpx.HTTPError as exc:
        raise GatewayError(f"status: {exc}") from exc
    if resp.status_code != 200:
        raise GatewayError(f"status вернул {resp.status_code}")
    data = resp.json()
    channels = data.get("channels") or []
    ch = next((c for c in channels if c.get("channel") == settings.channel), None)
    return {
        "stateInstance": "authorized" if ch and ch.get("state") == "authorized" else (ch or {}).get("state"),
        "account": (ch or {}).get("account"),
        "configured": True,
    }
