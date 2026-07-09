"""FastAPI-приложение: принимает webhook Restoplace, шлёт сообщение в MAX и
обслуживает админ-панель (/admin + REST API под /api/*).

Поток вебхука: разбор события → запись клиента/брони/уведомления в БД (для панели)
→ отправка сообщения клиенту в MAX через шлюз kapuchino-api (если включено в настройках).
"""

from __future__ import annotations

import json
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, auth, dashboard_api, db, gateway, messages, notify, state, web
from .config import settings
from .messages import (
    build_message,
    changed_client_fields,
    client_snapshot,
    log_summary,
    logical_event,
)
from .models import RestoplaceWebhook
from .phone import normalize_phone, to_chat_id

# На Windows консоль по умолчанию не UTF-8 — иначе кириллица/эмодзи в логах
# превращаются в «кракозябры». Переключаем потоки вывода на UTF-8.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("limemaxbot")


class _SkipSuccessAccess(logging.Filter):
    """Прячем из консоли успешные запросы (2xx/3xx) — оставляем только ошибки (4xx/5xx).

    Поллинг панели бьёт в /api/dashboard/* каждые ~2с и засоряет лог сотнями
    «200 OK». uvicorn.access кладёт код ответа в record.args[4]."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            return int(record.args[4]) >= 400
        except (TypeError, IndexError, ValueError):
            return True


logging.getLogger("uvicorn.access").addFilter(_SkipSuccessAccess())


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Старт: инициализируем БД (таблицы + владелец) и папку вложений."""
    db.init_db()
    Path(settings.media_dir).mkdir(parents=True, exist_ok=True)
    log.info("БД готова (%s). Панель: /admin", settings.db_path)
    yield


app = FastAPI(title="Lime MAX Bot", version=__version__, lifespan=lifespan)

# Вложения, отправленные менеджером из панели, отдаём как /media/*.
Path(settings.media_dir).mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=settings.media_dir), name="media")

# Страница панели + REST API.
app.include_router(web.router)
app.include_router(auth.router)
app.include_router(dashboard_api.router)


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/admin")


async def _parse_body(request: Request) -> dict:
    """Разбирает тело запроса как JSON или form-urlencoded."""
    raw = await request.body()
    if not raw:
        return {}
    text = raw.decode("utf-8", errors="ignore").strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
        return {"_raw": parsed}
    except json.JSONDecodeError:
        pass
    # form-urlencoded fallback
    form = parse_qs(text)
    return {k: (v[0] if len(v) == 1 else v) for k, v in form.items()}


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "version": __version__,
        "configured": settings.is_configured,
        "dry_run": settings.dry_run,
        "enabled_notifications": sorted(settings.notification_set),
    }


@app.post("/api/webhook")
async def gateway_incoming(request: Request) -> JSONResponse:
    """Заглушка для входящих уведомлений шлюза (incomingMessage webhook).

    На этапе 1 мы их не обрабатываем — просто отвечаем 200, чтобы шлюз
    не повторял доставку и не засорял лог 404-ми.
    """
    log.debug("Входящее уведомление шлюза получено и проигнорировано.")
    return JSONResponse({"status": "ok"})


def _clean_name(raw: str | None) -> str:
    name = (raw or "").strip()
    return "" if name in ("-", "—") else name


def _deposit_amount(payload: RestoplaceWebhook) -> float:
    """Сумма депозита из payload (если Restoplace её прислал), иначе 0."""
    data = payload.model_dump()
    for key in ("depositPrice", "deposit_price", "deposit", "depositSum"):
        value = data.get(key)
        if value:
            try:
                return float(value)
            except (ValueError, TypeError):
                pass
    return 0.0


def _record_booking(payload: RestoplaceWebhook, kind: str) -> int | None:
    """Сохраняет/обновляет клиента и бронь в БД (для отображения в панели).

    Возвращает id клиента (или None, если телефон не распознан).
    """
    place = messages.place_str(payload)
    date_str, time_str = messages.datetime_parts(payload.time_from, payload.time_to)
    end_dt = messages.parse_dt(payload.time_to)
    end_at = end_dt.isoformat() if end_dt else None
    status = "canceled" if kind == "cancelled" else "booked"

    phone = normalize_phone(payload.phone)
    client_id = db.upsert_client(phone, _clean_name(payload.name) or None) if phone else None

    db.upsert_booking(
        payload.reserve_id,
        client_id,
        status,
        place,
        date_str,
        time_str,
        payload.count,
        _deposit_amount(payload),
        end_at,
    )
    return client_id


def _notify_photo(kind: str) -> tuple[str, bytes] | None:
    """Фото к уведомлению для типа kind (created/updated/cancelled): (имя, байты)
    или None. Имя хранит meta['notify_photo_<kind>'] (для created — с откатом на
    старый единый ключ 'notify_photo'). Файл лежит в media и прикрепляется прямо
    к сообщению о брони (как подпись-текст), одним сообщением — см. обработчик.
    """
    fn = db.get_meta(f"notify_photo_{kind}")
    if not fn and kind == "created":
        fn = db.get_meta("notify_photo")  # совместимость со старым единым фото
    if not fn:
        return None
    path = Path(settings.media_dir) / fn
    if not path.exists():
        log.warning("⚠  Фото уведомления %s не найдено на диске — шлём без фото", fn)
        return None
    return fn, path.read_bytes()


@app.post(settings.webhook_path)
async def restoplace_webhook(
    request: Request,
    token: str | None = Query(default=None),
) -> JSONResponse:
    # Защита эндпоинта необязательным секретом (?token=...).
    if settings.webhook_secret and token != settings.webhook_secret:
        raise HTTPException(status_code=403, detail="invalid token")

    body = await _parse_body(request)
    payload = RestoplaceWebhook.model_validate(body)
    db.set_meta("last_webhook_at", db.now_iso())  # для индикатора «Состояние систем»

    # Опрятная сводка в консоль; полный JSON — только на уровне DEBUG.
    log.info("\n%s", log_summary(payload))
    log.debug("Сырой payload: %s", body)

    kind = logical_event(payload)

    if not kind:
        log.info("⏭  Неизвестное событие %r — пропуск", payload.event)
        return JSONResponse({"status": "ignored", "reason": "unknown event"})

    # Клиент и бронь попадают в панель ВСЕГДА (даже если уведомление отключено
    # или отфильтровано) — чтобы оператор видел все события.
    client_id = _record_booking(payload, kind)

    # --- Учёт состояния брони и определение значимых для клиента изменений ---
    rid = payload.reserve_id
    changes: list[str] = []
    is_waitlist = (payload.item_type or "") == "waitlist"
    became_booking = False  # переход «лист ожидания → появилось место»

    if kind == "created":
        state.set_snapshot(rid, client_snapshot(payload))
    elif kind == "updated":
        new_snap = client_snapshot(payload)
        prev_snap = state.get_snapshot(rid)
        state.set_snapshot(rid, new_snap)  # держим базу актуальной
        if prev_snap is None:
            log.info("ℹ  Нет базового снимка брони #%s — снимок сохранён, не шлём", rid)
            return JSONResponse({"status": "ignored", "kind": kind, "reason": "no baseline"})
        # Гость стоял в листе ожидания, а теперь получил место → это уже бронь,
        # шлём как подтверждение (а не как «изменение»), без списка изменений.
        if prev_snap.get("item_type") == "waitlist" and not is_waitlist:
            became_booking = True
        else:
            changes = changed_client_fields(prev_snap, new_snap)
            if not changes:
                log.info("⏭  Правка брони #%s не касается клиента — пропуск", rid)
                return JSONResponse(
                    {"status": "ignored", "kind": kind, "reason": "no client-relevant change"}
                )
    elif kind == "cancelled":
        state.delete_snapshot(rid)

    # Лист ожидания клиенту не отправляем: место ещё не подтверждено. Снимок выше
    # уже сохранён, поэтому момент «освободилось место» мы поймаем (became_booking).
    if is_waitlist:
        log.info("⏭  Лист ожидания (бронь #%s) — клиенту не отправляем", rid)
        return JSONResponse({"status": "ignored", "kind": kind, "reason": "waitlist"})

    if became_booking:
        kind = "created"
        log.info("🎉 Бронь #%s вышла из листа ожидания — шлём как подтверждение", rid)

    # Настройки отправки берём из панели (БД), а не только из .env.
    bot = db.get_bot_settings()
    dry = settings.dry_run or bot["dry_run"]

    if not bot["enabled"]:
        log.info("⏭  Отправка уведомлений выключена в панели — пропуск")
        return JSONResponse({"status": "ignored", "kind": kind, "reason": "notifications off"})

    if kind not in set(bot["notify_types"]):
        log.info("⏭  Уведомление '%s' отключено — пропуск", kind)
        return JSONResponse({"status": "ignored", "kind": kind})

    if changes:
        log.info("✏  Изменения для клиента (бронь #%s): %s", rid, ", ".join(changes))

    phone = normalize_phone(payload.phone)
    if not phone:
        log.warning("⚠  Не удалось определить телефон гостя: %r — пропуск", payload.phone)
        await notify.record_alert("Webhook Restoplace", f"Бронь #{rid}: телефон не распознан ({payload.phone!r})")
        return JSONResponse(
            {"status": "skipped", "kind": kind, "reason": "no valid phone"}
        )

    chat_id = to_chat_id(phone) or f"+{phone}"

    # Если задан тестовый получатель — все уведомления уходят в один чат
    # (для проверки без рассылки гостям). Очистите, чтобы слать гостю по номеру.
    if settings.test_chat_id:
        chat_id = settings.test_chat_id.strip()

    # Фото к уведомлению — своё для каждого типа (если задано во вкладке «Бот»).
    photo = _notify_photo(kind)

    try:
        # Текст берём из шаблона оператора (вкладка «Бот»), а не из жёсткого формата.
        template = db.get_templates().get(kind)
        message = build_message(payload, template)
        if photo:
            # Одно сообщение: картинка + текст-подпись (шлюз шлёт файл с caption).
            fn, raw = photo
            result = await gateway.send_file_by_upload(chat_id, raw, fn, caption=message, dry_run=dry)
        else:
            result = await gateway.send_message(chat_id, message, dry_run=dry)
    except gateway.GatewayError as exc:
        # Отдельно ловим «у номера нет аккаунта MAX (или скрыт приватностью)»:
        # это не сбой бота, а ожидаемая ситуация. Не пишем в журнал сбоёв и не
        # шлём владельцу — просто помечаем клиента (пометка видна в его карточке).
        # Отдаём 200, чтобы Restoplace не ретраил доставку, которая всё равно не пройдёт.
        if "нет аккаунта MAX" in str(exc):
            log.info("ℹ  Бронь #%s: у клиента нет аккаунта MAX — отправка пропущена", rid)
            if client_id is not None:
                db.set_client_no_max(client_id, True)
            return JSONResponse(
                {"status": "skipped", "kind": kind, "reason": "no MAX account"}
            )
        log.error("✖  Ошибка отправки через шлюз: %s", exc)
        await notify.record_alert("Отправка в MAX", f"Бронь #{rid}: {exc}")
        # 502 -> Restoplace может повторить попытку доставки.
        return JSONResponse(
            {"status": "error", "kind": kind, "detail": str(exc)},
            status_code=502,
        )

    delivered = bool(result.get("idMessage")) and not result.get("skipped")
    if client_id is not None:
        gw_msg_id = str(result["idMessage"]) if result.get("idMessage") else None
        # В панели показываем то же, что ушло гостю: текст + прикреплённое фото.
        media = [{"url": f"/media/{photo[0]}", "kind": "image", "name": photo[0]}] if photo else None
        db.add_message(
            client_id, "bot", message, kind=kind, delivered=delivered, media=media,
            channel=settings.channel, gateway_chat_id=chat_id, gateway_msg_id=gw_msg_id,
        )
        # Сообщение реально ушло — значит аккаунт MAX есть, снимаем пометку.
        if delivered:
            db.set_client_no_max(client_id, False)

    if result.get("skipped"):
        log.info("✓  Сообщение собрано (%s), отправка пропущена: %s",
                 chat_id, result.get("reason"))
    else:
        log.info("✓  Отправлено гостю %s (idMessage=%s)",
                 chat_id, result.get("idMessage"))
        # Важное событие ушло гостю по-настоящему — дублируем владельцу (не в DRY_RUN/тесте).
        await notify.notify_owner(log_summary(payload))

    return JSONResponse(
        {
            "status": "sent",
            "kind": kind,
            "chatId": chat_id,
            "gateway": result,
        }
    )
