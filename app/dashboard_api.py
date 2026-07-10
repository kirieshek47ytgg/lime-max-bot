"""REST API админ-панели: диалоги, брони, аналитика, статусы, настройки, админы.

Все эндпоинты под /api/dashboard/* закрыты авторизацией (current_admin); разделы
«только владелец» дополнительно требуют роль owner. Логика данных — поверх sqlite
(см. db.py); тяжёлой бизнес-логики тут нет, это «витрина» для панели.
"""

from __future__ import annotations

import csv
import io
import logging
import secrets
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from . import db, gateway, notify
from .auth import current_admin, require_owner
from .config import settings
from .phone import to_chat_id

log = logging.getLogger("limemaxbot.dashboard")

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

# Человекочитаемые статусы брони для панели.
STATUS_RU = {"booked": "забронирован", "completed": "завершён", "canceled": "отменён"}
VALID_STATUSES = set(STATUS_RU)


# --------------------------------------------------------------------------- #
#  Вспомогательные функции
# --------------------------------------------------------------------------- #
def _effective_status(row) -> str:
    """Бронь со статусом «забронирован», у которой время уже прошло, → «завершён».

    Это автодополнение для удобства; ручной статус из панели всегда сохраняется
    как есть и имеет приоритет (если менеджер выставил completed/canceled).
    """
    status = row["status"]
    if status == "booked" and row["end_at"]:
        try:
            if datetime.fromisoformat(row["end_at"]) < datetime.now():
                return "completed"
        except ValueError:
            pass
    return status


def _booking_description(row) -> str:
    """Краткий состав брони строкой: «Беседка №2 · 27.06.2026 · 13:00–14:00 · 3 гостя»."""
    parts = [row["place"] or "Бронь"]
    if row["date_str"]:
        parts.append(row["date_str"])
    if row["time_str"]:
        parts.append(row["time_str"])
    if row["guests"]:
        parts.append(f"{row['guests']} гостей")
    return " · ".join(parts)


def _local_date(iso: str) -> str:
    """Локальная дата (YYYY-MM-DD) из ISO-времени с таймзоной."""
    try:
        return datetime.fromisoformat(iso).astimezone().date().isoformat()
    except (ValueError, TypeError):
        return ""


# --------------------------------------------------------------------------- #
#  Кто я (роль для показа вкладки «Управление»)
# --------------------------------------------------------------------------- #
@router.get("/me")
def me(admin: dict = Depends(current_admin)) -> dict:
    return {"username": admin["username"], "role": admin["role"]}


# --------------------------------------------------------------------------- #
#  Диалоги
# --------------------------------------------------------------------------- #
@router.get("/clients")
def clients(admin: dict = Depends(current_admin)) -> dict:
    db.clear_expired_pauses()
    now = datetime.now().astimezone()
    out = []
    with db.connect() as c:
        rows = c.execute(
            """
            SELECT c.*,
                   m.text        AS last_text,
                   m.sender_type AS last_sender,
                   m.created_at  AS last_time
            FROM clients c
            LEFT JOIN messages m
              ON m.id = (SELECT id FROM messages WHERE client_id = c.id ORDER BY id DESC LIMIT 1)
            WHERE COALESCE(c.no_max, 0) = 0
            ORDER BY (m.created_at IS NULL), m.created_at DESC, c.id DESC
            """
        ).fetchall()
    for r in rows:
        # Клиент без единого сообщения (отправка провалилась / все сообщения удалили)
        # в диалогах не показываем — иначе висит «имя без переписки». Бронь при этом
        # остаётся: заказы отдаёт отдельный эндпоинт /orders (таблица bookings).
        if r["last_time"] is None:
            continue
        resume_in = None
        if r["is_bot_paused"] and r["paused_until"]:
            try:
                left = (datetime.fromisoformat(r["paused_until"]) - now).total_seconds()
                resume_in = max(0, int(left))
            except (ValueError, TypeError):
                resume_in = None
        out.append(
            {
                "id": r["id"],
                "name": r["name"],
                "last_time": r["last_time"],
                "last_text": r["last_text"] or "",
                "last_sender": r["last_sender"] or "",
                "last_messenger": r["messenger"] or "max",
                "is_bot_paused": bool(r["is_bot_paused"]),
                "resume_in": resume_in,
            }
        )
    return {"clients": out}


@router.get("/clients/{client_id}/messages")
def client_messages(client_id: int, admin: dict = Depends(current_admin)) -> dict:
    import json as _json

    with db.connect() as c:
        rows = c.execute(
            "SELECT * FROM messages WHERE client_id=? ORDER BY id", (client_id,)
        ).fetchall()
    msgs = []
    for r in rows:
        media = []
        if r["media"]:
            try:
                media = _json.loads(r["media"])
            except ValueError:
                media = []
        msgs.append(
            {
                "id": r["id"],
                "sender_type": r["sender_type"],
                "text": r["text"] or "",
                "time": r["created_at"],
                "media": media,
                "reactions": [],
            }
        )
    return {"messages": msgs}


@router.get("/clients/{client_id}/card")
def client_card(client_id: int, admin: dict = Depends(current_admin)) -> dict:
    db.complete_past_bookings()
    with db.connect() as c:
        cl = c.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
        if not cl:
            raise HTTPException(status_code=404, detail="client not found")
        brows = c.execute(
            "SELECT * FROM bookings WHERE client_id=? ORDER BY id DESC", (client_id,)
        ).fetchall()
    orders = [
        {
            "id": b["id"],
            "status_ru": STATUS_RU.get(_effective_status(b), _effective_status(b)),
            "description": _booking_description(b),
            "amount": b["amount"] or 0,
            "created_at": b["created_at"],
        }
        for b in brows
    ]
    return {
        "id": cl["id"],
        "name": cl["name"],
        "phone": cl["phone"],
        "no_max": bool(cl["no_max"]),
        "created_at": cl["created_at"],
        "notes": cl["notes"] or "",
        "orders_count": len(orders),
        "orders": orders,
    }


class NotesBody(BaseModel):
    notes: str = ""


@router.put("/clients/{client_id}/notes")
def save_notes(client_id: int, body: NotesBody, admin: dict = Depends(current_admin)) -> dict:
    with db.connect() as c:
        c.execute("UPDATE clients SET notes=? WHERE id=?", (body.notes, client_id))
    return {"status": "ok"}


class SendBody(BaseModel):
    text: str


@router.post("/clients/{client_id}/send")
async def send_message(client_id: int, body: SendBody, admin: dict = Depends(current_admin)) -> dict:
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty text")
    with db.connect() as c:
        cl = c.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
    if not cl:
        raise HTTPException(status_code=404, detail="client not found")

    bs = db.get_bot_settings()
    dry = settings.dry_run or bs["dry_run"]
    chat_id = to_chat_id(cl["phone"]) or f"+{cl['phone']}"
    delivered = False
    gw_msg_id = None
    try:
        result = await gateway.send_message(chat_id, text, dry_run=dry)
        delivered = bool(result.get("idMessage")) and not result.get("skipped")
        if result.get("idMessage"):
            gw_msg_id = str(result["idMessage"])
    except gateway.GatewayError as exc:
        # «Нет аккаунта MAX» — не сбой: не пишем в журнал, только помечаем клиента.
        if "нет аккаунта MAX" in str(exc):
            db.set_client_no_max(client_id, True)
            log.info("Ручная отправка пропущена: у клиента нет аккаунта MAX")
        else:
            await notify.record_alert("Отправка в MAX", f"Не удалось отправить ответ менеджера: {exc}")
            log.error("Ручная отправка не удалась: %s", exc)

    if delivered:
        db.set_client_no_max(client_id, False)
        db.set_client_max_chat(client_id, result.get("chatId"))  # для матчинга входящих

    db.add_message(
        client_id, "manager", text, kind="manager", delivered=delivered,
        channel=settings.channel, gateway_chat_id=chat_id, gateway_msg_id=gw_msg_id,
    )
    db.pause_bot(client_id)
    return {"status": "ok", "delivered": delivered}


@router.post("/clients/{client_id}/send-files")
async def send_files(
    client_id: int,
    files: list[UploadFile] = File(default=[]),
    caption: str = Form(default=""),
    admin: dict = Depends(current_admin),
) -> dict:
    with db.connect() as c:
        cl = c.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
    if not cl:
        raise HTTPException(status_code=404, detail="client not found")

    bs = db.get_bot_settings()
    dry = settings.dry_run or bs["dry_run"]
    chat_id = to_chat_id(cl["phone"]) or f"+{cl['phone']}"
    media_dir = Path(settings.media_dir)
    media_dir.mkdir(parents=True, exist_ok=True)

    media = []
    delivered_any = False
    gw_ids: list[str] = []
    for up in files:
        raw = await up.read()
        safe = f"{secrets.token_hex(8)}_{Path(up.filename or 'file').name}"
        (media_dir / safe).write_bytes(raw)
        ct = up.content_type or ""
        kind = "image" if ct.startswith("image/") else "video" if ct.startswith("video/") else "file"
        media.append({"url": f"/media/{safe}", "kind": kind, "name": up.filename or safe})
        try:
            res = await gateway.send_file_by_upload(
                chat_id, raw, up.filename or safe, caption=caption, dry_run=dry
            )
            if res.get("idMessage") and not res.get("skipped"):
                delivered_any = True
                gw_ids.append(str(res["idMessage"]))
                db.set_client_max_chat(client_id, res.get("chatId"))  # для матчинга входящих
        except gateway.GatewayError as exc:
            await notify.record_alert("Отправка файла в MAX", str(exc))
            log.error("Отправка файла не удалась: %s", exc)

    _PLACEHOLDER = {"image": "📷 Фото", "video": "🎬 Видео"}
    text = caption or (_PLACEHOLDER.get(media[0]["kind"], "📎 Файл") if media else "📎 Файл")
    # Несколько вложений уходят отдельными сообщениями — их id храним через запятую,
    # чтобы удалить все разом при удалении группового пузыря в панели.
    db.add_message(
        client_id, "manager", text, kind="manager", delivered=delivered_any, media=media,
        channel=settings.channel, gateway_chat_id=chat_id,
        gateway_msg_id=",".join(gw_ids) or None,
    )
    db.pause_bot(client_id)
    return {"status": "ok"}


class MsgEditBody(BaseModel):
    text: str


def _msg_chat_id(msg: dict) -> str | None:
    """chatId для правки/удаления: тот, в который реально отправляли, иначе из телефона."""
    if msg.get("gateway_chat_id"):
        return msg["gateway_chat_id"]
    phone = msg.get("phone")
    if not phone:
        return None
    return to_chat_id(phone) or f"+{phone}"


@router.put("/messages/{message_id}")
async def edit_message(
    message_id: int, body: MsgEditBody, admin: dict = Depends(current_admin)
) -> dict:
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty text")
    msg = db.get_message(message_id)
    if not msg:
        raise HTTPException(status_code=404, detail="message not found")

    # Если знаем id сообщения в мессенджере — правим и там через шлюз.
    # synced: True — поправили в мессенджере, False — попытались и не вышло,
    # None — правка только в панели (входящее/не отправлялось/DRY_RUN).
    synced = None
    gw_id = (msg.get("gateway_msg_id") or "").split(",")[0]
    if gw_id:
        bs = db.get_bot_settings()
        dry = settings.dry_run or bs["dry_run"]
        chat_id = _msg_chat_id(msg)
        try:
            res = await gateway.edit_message(
                chat_id, gw_id, text, channel=msg.get("channel"), dry_run=dry
            )
            synced = not res.get("skipped")
        except gateway.GatewayError as exc:
            synced = False
            await notify.record_alert("Правка сообщения в мессенджере", str(exc))
            log.error("Не удалось отредактировать сообщение в мессенджере: %s", exc)

    with db.connect() as c:
        c.execute("UPDATE messages SET text=? WHERE id=?", (text, message_id))
    return {"status": "ok", "synced": synced}


@router.delete("/messages/{message_id}")
async def delete_message(message_id: int, admin: dict = Depends(current_admin)) -> dict:
    msg = db.get_message(message_id)
    if not msg:
        return {"status": "ok"}  # уже удалено — считаем успехом

    # Наши исходящие удаляем и у клиента; входящие (без gateway_msg_id) — только в панели.
    synced = None
    gw_ids = [g for g in (msg.get("gateway_msg_id") or "").split(",") if g]
    if gw_ids:
        bs = db.get_bot_settings()
        dry = settings.dry_run or bs["dry_run"]
        chat_id = _msg_chat_id(msg)
        for one in gw_ids:
            try:
                res = await gateway.delete_message(
                    chat_id, one, channel=msg.get("channel"), dry_run=dry
                )
                # Достаточно одной удачи, чтобы не сбрасывать synced в False.
                if synced is None or synced is True:
                    synced = not res.get("skipped")
            except gateway.GatewayError as exc:
                synced = False
                await notify.record_alert("Удаление сообщения в мессенджере", str(exc))
                log.error("Не удалось удалить сообщение в мессенджере: %s", exc)

    with db.connect() as c:
        c.execute("DELETE FROM messages WHERE id=?", (message_id,))
    return {"status": "ok", "synced": synced}


# --------------------------------------------------------------------------- #
#  Заказы (брони)
# --------------------------------------------------------------------------- #
@router.get("/orders")
def orders(admin: dict = Depends(current_admin)) -> dict:
    db.complete_past_bookings()
    with db.connect() as c:
        rows = c.execute(
            """
            SELECT b.*, c.name AS client_name
            FROM bookings b LEFT JOIN clients c ON c.id = b.client_id
            ORDER BY b.id DESC
            """
        ).fetchall()
    out = []
    for b in rows:
        st = _effective_status(b)
        out.append(
            {
                "id": b["id"],
                "client_id": b["client_id"],
                "client_name": b["client_name"] or "Гость",
                "status": st,
                "status_ru": STATUS_RU.get(st, st),
                "description": _booking_description(b),
                # Отдельные поля брони — панель раскладывает их по строкам в карточке заказа.
                "place": b["place"] or "",
                "date": b["date_str"] or "",
                "time": b["time_str"] or "",
                "guests": b["guests"] or "",
                "amount": b["amount"] or 0,
                "created_at": b["created_at"],
            }
        )
    return {"orders": out}


class StatusBody(BaseModel):
    status: str


@router.put("/orders/{order_id}/status")
def set_order_status(order_id: int, body: StatusBody, admin: dict = Depends(current_admin)) -> dict:
    if body.status not in VALID_STATUSES:
        raise HTTPException(status_code=400, detail="invalid status")
    with db.connect() as c:
        c.execute(
            "UPDATE bookings SET status=?, updated_at=? WHERE id=?",
            (body.status, db.now_iso(), order_id),
        )
    return {"status": "ok"}


@router.delete("/orders/{order_id}")
def delete_order(order_id: int, admin: dict = Depends(current_admin)) -> dict:
    with db.connect() as c:
        c.execute("DELETE FROM bookings WHERE id=?", (order_id,))
    return {"status": "ok"}


@router.get("/orders/export")
def export_orders(admin: dict = Depends(current_admin)) -> Response:
    db.complete_past_bookings()
    with db.connect() as c:
        rows = c.execute(
            """
            SELECT b.*, c.name AS client_name, c.phone AS phone
            FROM bookings b LEFT JOIN clients c ON c.id = b.client_id
            ORDER BY b.id
            """
        ).fetchall()
    buf = io.StringIO()
    # Разделитель «;»: русский Excel по локали ждёт именно его — с запятой все
    # колонки слипаются в одну ячейку. Импорт ниже определяет разделитель сам.
    w = csv.writer(buf, delimiter=";")
    w.writerow(["id", "reserve_id", "client", "phone", "status", "place", "date", "time", "guests", "amount", "created_at"])
    for b in rows:
        w.writerow(
            [
                b["id"], b["reserve_id"] or "", b["client_name"] or "", b["phone"] or "",
                _effective_status(b), b["place"] or "", b["date_str"] or "", b["time_str"] or "",
                b["guests"] or "", b["amount"] or 0, b["created_at"],
            ]
        )
    # UTF-8 BOM (﻿): без него Excel открывает CSV как Windows-1251 и кириллица
    # превращается в кракозябры. С BOM Excel определяет UTF-8 сам. Импорт читает
    # через utf-8-sig, поэтому BOM ему не мешает.
    return Response(
        content=("﻿" + buf.getvalue()).encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=bookings.csv"},
    )


@router.post("/orders/import")
async def import_orders(file: UploadFile = File(...), admin: dict = Depends(current_admin)) -> dict:
    raw = (await file.read()).decode("utf-8-sig", errors="ignore")
    # Разделитель определяем по заголовку: наш экспорт и рус. Excel дают «;»,
    # старые/англ. файлы — «,». Берём тот, которого в первой строке больше.
    first_line = raw.split("\n", 1)[0]
    delim = ";" if first_line.count(";") >= first_line.count(",") else ","
    reader = csv.DictReader(io.StringIO(raw), delimiter=delim)
    updated = skipped = 0
    with db.connect() as c:
        for r in reader:
            oid = (r.get("id") or "").strip()
            status = (r.get("status") or "").strip()
            if not oid or status not in VALID_STATUSES:
                skipped += 1
                continue
            cur = c.execute(
                "UPDATE bookings SET status=?, updated_at=? WHERE id=?",
                (status, db.now_iso(), oid),
            )
            updated += cur.rowcount
            if not cur.rowcount:
                skipped += 1
    return {"added": 0, "updated": updated, "skipped": skipped}


# --------------------------------------------------------------------------- #
#  Аналитика
# --------------------------------------------------------------------------- #
@router.get("/analytics")
def analytics(admin: dict = Depends(current_admin)) -> dict:
    db.complete_past_bookings()
    with db.connect() as c:
        bookings = c.execute("SELECT created_at, place, status, end_at FROM bookings").fetchall()
        clients_rows = c.execute("SELECT created_at FROM clients").fetchall()
        msgs = c.execute(
            "SELECT sender_type, kind, delivered, created_at FROM messages"
        ).fetchall()
        top = c.execute(
            "SELECT place, COUNT(*) AS n FROM bookings WHERE place IS NOT NULL AND place<>'' "
            "GROUP BY place ORDER BY n DESC LIMIT 5"
        ).fetchall()

    notifications = sum(1 for m in msgs if m["sender_type"] == "bot")
    delivered = sum(1 for m in msgs if m["sender_type"] == "bot" and m["delivered"])
    confirmations = sum(1 for m in msgs if m["kind"] == "created")
    updates = sum(1 for m in msgs if m["kind"] == "updated")
    cancellations = sum(1 for m in msgs if m["kind"] == "cancelled")

    totals = {
        "bookings": len(bookings),
        "clients": len(clients_rows),
        "confirmations": confirmations,
        "updates": updates,
        "cancellations": cancellations,
        "notifications": notifications,
        "delivered_pct": round(delivered / notifications * 100) if notifications else 0,
    }

    # Гистограмма за 14 дней
    today = datetime.now().astimezone().date()
    days = [today - timedelta(days=13 - i) for i in range(14)]
    by_day = []
    for d in days:
        iso = d.isoformat()
        by_day.append(
            {
                "day": d.strftime("%d.%m"),
                "bookings": sum(1 for b in bookings if _local_date(b["created_at"]) == iso),
                "clients": sum(1 for cl in clients_rows if _local_date(cl["created_at"]) == iso),
                "notifications": sum(
                    1 for m in msgs if m["sender_type"] == "bot" and _local_date(m["created_at"]) == iso
                ),
                "cancellations": sum(
                    1 for m in msgs if m["kind"] == "cancelled" and _local_date(m["created_at"]) == iso
                ),
            }
        )

    top_products = [{"name": t["place"], "count": t["n"]} for t in top]
    # Разбивка броней по исходу: сколько всего, сколько отменено, сколько завершено.
    eff_statuses = [_effective_status(b) for b in bookings]
    completed = sum(1 for s in eff_statuses if s == "completed")
    canceled = sum(1 for s in eff_statuses if s == "canceled")
    funnel = [
        {"stage": "Брони", "count": len(bookings)},
        {"stage": "Отменены", "count": canceled},
        {"stage": "Завершены", "count": completed},
    ]
    return {"totals": totals, "by_day": by_day, "top_products": top_products, "funnel": funnel}


# --------------------------------------------------------------------------- #
#  Состояние систем (живая проверка)
# --------------------------------------------------------------------------- #
@router.get("/status")
async def status(admin: dict = Depends(current_admin)) -> dict:
    items = []

    # Restoplace: активно «пингануть» нельзя — судим по факту прихода вебхуков.
    last_wh = db.get_meta("last_webhook_at")
    items.append(
        {
            "name": "Restoplace (webhook)",
            "ok": bool(last_wh),
            "note": "получает события" if last_wh else "ожидание событий",
        }
    )

    # Шлюз kapuchino-api + доставка в MAX зависят от состояния канала.
    try:
        st = await gateway.get_state_instance()
        if not st.get("configured"):
            gw_ok, note = False, "не настроен (.env)"
        else:
            state = st.get("stateInstance")
            gw_ok = state == "authorized"
            note = "подключён" if gw_ok else f"состояние: {state or 'недоступен'}"
    except gateway.GatewayError as exc:
        gw_ok, note = False, f"ошибка: {exc}"

    items.append({"name": "Шлюз kapuchino-api", "ok": gw_ok, "note": note})
    items.append(
        {
            "name": f"{settings.channel.upper()} (доставка)",
            "ok": gw_ok,
            "note": "готова к отправке" if gw_ok else "недоступна",
        }
    )
    return {"items": items}


# --------------------------------------------------------------------------- #
#  Настройки отправки уведомлений
# --------------------------------------------------------------------------- #
@router.get("/bot-settings")
def get_settings(admin: dict = Depends(current_admin)) -> dict:
    return db.get_bot_settings()


class BotSettingsBody(BaseModel):
    enabled: bool = True
    notify_types: list[str] = []
    dry_run: bool = False
    owner_telegram: str = ""


@router.put("/bot-settings")
def put_settings(body: BotSettingsBody, admin: dict = Depends(require_owner)) -> dict:
    db.save_bot_settings(body.enabled, body.notify_types, body.dry_run, body.owner_telegram.strip())
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
#  Оформление (брендинг): цветовая гамма, название и иконка панели.
#  Хранится глобально в meta и общо для всех админов — меняет только владелец.
#  Светлая/тёмная тема НЕ здесь: она остаётся личной (localStorage у каждого).
# --------------------------------------------------------------------------- #
@router.get("/branding")
def get_branding(admin: dict = Depends(current_admin)) -> dict:
    return {
        "accent": db.get_meta("brand_accent") or "default",
        "app_name": db.get_meta("brand_name") or "",
        "app_icon": db.get_meta("brand_icon") or "💬",
    }


class BrandingBody(BaseModel):
    accent: str | None = None
    app_name: str | None = None
    app_icon: str | None = None


@router.put("/branding")
def put_branding(body: BrandingBody, admin: dict = Depends(require_owner)) -> dict:
    if body.accent is not None:
        db.set_meta("brand_accent", body.accent.strip()[:40])
    if body.app_name is not None:
        db.set_meta("brand_name", body.app_name.strip()[:40])
    if body.app_icon is not None:
        db.set_meta("brand_icon", body.app_icon.strip()[:16])
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
#  Шаблоны сообщений клиенту (вкладка «Бот»)
# --------------------------------------------------------------------------- #
@router.get("/templates")
def get_templates(admin: dict = Depends(current_admin)) -> dict:
    # templates — эффективные (с дефолтами), saved — только явно сохранённые
    # (панель применяет с сервера лишь их, чтобы не затирать локальный черновик).
    return {"templates": db.get_templates(), "saved": db.get_saved_templates()}


class TemplatesBody(BaseModel):
    templates: dict[str, str]


@router.put("/templates")
def put_templates(body: TemplatesBody, admin: dict = Depends(current_admin)) -> dict:
    db.save_templates(body.templates)
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
#  Фото к уведомлению — своё для каждого типа (подтверждение/изменение/отмена).
#  Бот прикладывает его к сообщению о брони. Имя файла храним в meta по ключу
#  notify_photo_<kind>, сам файл — в папке media.
# --------------------------------------------------------------------------- #
PHOTO_KINDS = ("created", "updated", "cancelled")
LEGACY_PHOTO_META = "notify_photo"  # старый единый ключ (одно фото на всё)


def _photo_meta_key(kind: str) -> str:
    return f"notify_photo_{kind}"


def _check_photo_kind(kind: str) -> None:
    if kind not in PHOTO_KINDS:
        raise HTTPException(status_code=400, detail="Неизвестный тип уведомления")


def _notify_photo_name(kind: str) -> str | None:
    """Имя файла фото для типа уведомления. Для created подхватываем и старый
    единый ключ notify_photo — чтобы ранее загруженное фото не потерялось."""
    fn = db.get_meta(_photo_meta_key(kind))
    if not fn and kind == "created":
        fn = db.get_meta(LEGACY_PHOTO_META)
    return fn or None


@router.get("/notify-photo")
def get_notify_photo(kind: str = "created", admin: dict = Depends(current_admin)) -> dict:
    _check_photo_kind(kind)
    fn = _notify_photo_name(kind)
    return {"url": f"/media/{fn}" if fn else None}


@router.post("/notify-photo")
async def set_notify_photo(
    kind: str = "created",
    file: UploadFile = File(...),
    admin: dict = Depends(current_admin),
) -> dict:
    _check_photo_kind(kind)
    if not (file.content_type or "").startswith("image/"):
        raise HTTPException(status_code=400, detail="Нужен файл-изображение")
    raw = await file.read()
    media_dir = Path(settings.media_dir)
    media_dir.mkdir(parents=True, exist_ok=True)
    safe = f"notify_{kind}_{secrets.token_hex(6)}_{Path(file.filename or 'photo').name}"
    (media_dir / safe).write_bytes(raw)
    # Старый файл этого типа больше не нужен — удаляем, чтобы не копить мусор.
    old = _notify_photo_name(kind)
    if old and old != safe:
        (media_dir / old).unlink(missing_ok=True)
    db.set_meta(_photo_meta_key(kind), safe)
    if kind == "created":
        db.set_meta(LEGACY_PHOTO_META, "")  # переехали на ключ по типу
    return {"url": f"/media/{safe}"}


@router.delete("/notify-photo")
def delete_notify_photo(kind: str = "created", admin: dict = Depends(current_admin)) -> dict:
    _check_photo_kind(kind)
    old = _notify_photo_name(kind)
    if old:
        (Path(settings.media_dir) / old).unlink(missing_ok=True)
    db.set_meta(_photo_meta_key(kind), "")
    if kind == "created":
        db.set_meta(LEGACY_PHOTO_META, "")
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
#  Журнал сбоёв
# --------------------------------------------------------------------------- #
@router.get("/alerts")
def alerts(admin: dict = Depends(current_admin)) -> dict:
    with db.connect() as c:
        rows = c.execute(
            "SELECT * FROM alerts ORDER BY id DESC LIMIT 100"
        ).fetchall()
        unseen = c.execute("SELECT COUNT(*) AS n FROM alerts WHERE seen=0").fetchone()["n"]
    return {
        "unseen": unseen,
        "alerts": [
            {"source": a["source"], "message": a["message"], "created_at": a["created_at"]}
            for a in rows
        ],
    }


@router.post("/alerts/seen")
def alerts_seen(admin: dict = Depends(current_admin)) -> dict:
    with db.connect() as c:
        c.execute("UPDATE alerts SET seen=1 WHERE seen=0")
    return {"status": "ok"}


@router.post("/alerts/clear")
def alerts_clear(admin: dict = Depends(current_admin)) -> dict:
    with db.connect() as c:
        c.execute("DELETE FROM alerts")
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
#  Администраторы и журнал входов (только владелец)
# --------------------------------------------------------------------------- #
ROLE_OK = {"owner", "manager"}


@router.get("/admins")
def list_admins(admin: dict = Depends(require_owner)) -> dict:
    with db.connect() as c:
        rows = c.execute("SELECT id, username, role FROM admins ORDER BY id").fetchall()
    return {"admins": [dict(r) for r in rows]}


class AdminCreate(BaseModel):
    username: str
    password: str
    role: str = "manager"


@router.post("/admins")
def add_admin(body: AdminCreate, admin: dict = Depends(require_owner)) -> dict:
    if not body.username.strip() or len(body.password) < 6 or body.role not in ROLE_OK:
        raise HTTPException(status_code=400, detail="Проверьте логин, пароль (мин. 6) и роль")
    try:
        db.create_admin(body.username.strip(), body.password, body.role)
    except Exception:  # noqa: BLE001 — уникальность логина и пр.
        raise HTTPException(status_code=400, detail="Логин уже занят")
    return {"status": "ok"}


class AdminUpdate(BaseModel):
    username: str
    role: str = "manager"
    password: str | None = None


@router.put("/admins/{admin_id}")
def update_admin(admin_id: int, body: AdminUpdate, admin: dict = Depends(require_owner)) -> dict:
    if not body.username.strip() or body.role not in ROLE_OK:
        raise HTTPException(status_code=400, detail="Проверьте логин и роль")
    if body.password is not None and body.password and len(body.password) < 6:
        raise HTTPException(status_code=400, detail="Пароль — минимум 6 символов")
    with db.connect() as c:
        c.execute(
            "UPDATE admins SET username=?, role=? WHERE id=?",
            (body.username.strip(), body.role, admin_id),
        )
        if body.password:
            c.execute(
                "UPDATE admins SET password_hash=? WHERE id=?",
                (db.hash_password(body.password), admin_id),
            )
    return {"status": "ok"}


@router.delete("/admins/{admin_id}")
def delete_admin(admin_id: int, admin: dict = Depends(require_owner)) -> dict:
    if admin_id == admin["id"]:
        raise HTTPException(status_code=400, detail="Нельзя удалить самого себя")
    with db.connect() as c:
        n = c.execute("SELECT COUNT(*) AS n FROM admins WHERE role='owner'").fetchone()["n"]
        target = c.execute("SELECT role FROM admins WHERE id=?", (admin_id,)).fetchone()
        if target and target["role"] == "owner" and n <= 1:
            raise HTTPException(status_code=400, detail="Нельзя удалить последнего владельца")
        c.execute("DELETE FROM admins WHERE id=?", (admin_id,))
        c.execute("DELETE FROM sessions WHERE admin_id=?", (admin_id,))
    return {"status": "ok"}


@router.get("/login-log")
def login_log(admin: dict = Depends(require_owner)) -> dict:
    with db.connect() as c:
        rows = c.execute(
            "SELECT username, ip, ok, created_at FROM login_log ORDER BY id DESC LIMIT 50"
        ).fetchall()
    return {
        "logs": [
            {"username": r["username"], "ip": r["ip"], "ok": bool(r["ok"]), "created_at": r["created_at"]}
            for r in rows
        ]
    }


@router.get("/backup")
def backup(admin: dict = Depends(require_owner)) -> Response:
    data = Path(settings.db_path).read_bytes()
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": "attachment; filename=bot_database.db"},
    )
