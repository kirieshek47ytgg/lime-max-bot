"""Сборка текста сообщения для клиента и опрятной строки для лога.

Работает с реальным events-форматом Restoplace (см. models.py).
"""

from __future__ import annotations

import re
from datetime import datetime

from .config import settings
from .items import item_name
from .models import RestoplaceWebhook

# Шаблоны сообщений по умолчанию (тот же текст, что в редакторе вкладки «Бот»).
# Реальный текст оператор правит в панели; эти значения — фолбэк и «вставить пример».
# Плейсхолдеры: {имя} {объект} {дата} {время} {гостей} {номер}. Строка, в которой
# все плейсхолдеры оказались пустыми, при рендере выкидывается (без «висящих» меток).
DEFAULT_TEMPLATES = {
    "created": (
        "✅ Бронирование принято!\n"
        "\n"
        "👤 {имя}\n"
        "🏡 {объект}\n"
        "📅 {дата}\n"
        "🕐 {время}\n"
        "👥 Гостей: {гостей}\n"
        "🔖 Бронь №{номер}\n"
        "\n"
        "✨ Во время отдыха на базе вы можете:\n"
        "• попариться в бане на дровах\n"
        "• арендовать беседку с мангалом\n"
        "• порыбачить на пруду и взять лодку напрокат\n"
        "\n"
        "Будем рады помочь — просто напишите нам!"
    ),
    "updated": (
        "✏️ Бронирование изменено\n"
        "\n"
        "👤 {имя}\n"
        "🏡 {объект}\n"
        "📅 {дата}\n"
        "🕐 {время}\n"
        "👥 Гостей: {гостей}\n"
        "🔖 Бронь №{номер}"
    ),
    "cancelled": (
        "❌ Бронирование отменено\n"
        "\n"
        "👤 {имя}\n"
        "🏡 {объект}\n"
        "📅 {дата}\n"
        "🕐 {время}\n"
        "🔖 Бронь №{номер}\n"
        "\n"
        "Если это ошибка — напишите нам, поможем восстановить."
    ),
}

# Статусы Restoplace, означающие отменённую бронь.
CANCEL_STATUSES = {6}

# Поля брони, изменение которых ВАЖНО клиенту (для алертов на reserve.updated).
# Ключ — поле payload, значение — человекочитаемая метка изменения.
CLIENT_FIELDS = {
    "time_from": "дата/время",
    "time_to": "дата/время",
    "item_id": "беседка",
    "item_type": "тип объекта",
    "floor_name": "зона",
    "count": "число гостей",
}

# Человекочитаемые названия типов объектов.
ITEM_TYPE_LABELS = {
    "alcove": "Беседка",
    "table": "Столик",
    "hall": "Зал",
    "banquet": "Банкет",
    "waitlist": "Лист ожидания",
}

_DT_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M")


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def parse_dt(value: str | None) -> datetime | None:
    """Публичная обёртка над _parse_dt (разбор времени брони Restoplace)."""
    return _parse_dt(value)


def datetime_parts(time_from: str | None, time_to: str | None) -> tuple[str | None, str | None]:
    """Возвращает (дата, время) в человекочитаемом виде.

    Примеры:
      ("27.06.2026", "13:00–14:00")           — в пределах одного дня
      ("27.06.2026 13:00 – 28.06.2026 02:00", None) — через сутки
    """
    start = _parse_dt(time_from)
    end = _parse_dt(time_to)
    if not start:
        return (None, None)

    date_str = start.strftime("%d.%m.%Y")
    if end and end.date() == start.date():
        return (date_str, f"{start:%H:%M}–{end:%H:%M}")
    if end:
        return (f"{start:%d.%m.%Y %H:%M} – {end:%d.%m.%Y %H:%M}", None)
    return (date_str, f"{start:%H:%M}")


def _item_label(item_type: str | None) -> str:
    if not item_type:
        return "Бронь"
    return ITEM_TYPE_LABELS.get(item_type, item_type)


def _clean(value: object) -> str:
    """Строка без лишних пробелов; плейсхолдеры '-'/'—' считаются пустыми."""
    text = str(value).strip() if value is not None else ""
    return "" if text in ("", "-", "—") else text


def is_cancelled(payload: RestoplaceWebhook) -> bool:
    """Отмена: status входит в CANCEL_STATUSES, либо заполнены поля отмены."""
    try:
        if payload.status is not None and int(payload.status) in CANCEL_STATUSES:
            return True
    except (ValueError, TypeError):
        pass
    return bool(_clean(payload.cancel_reason) or _clean(payload.time_cancel))


def logical_event(payload: RestoplaceWebhook) -> str | None:
    """Смысловой тип уведомления: created / updated / cancelled / None.

    Отмена приходит как event=reserve.updated со status=6 — поэтому не полагаемся
    на одно лишь имя события.
    """
    event = (payload.event or "").strip()
    if event in ("reserve.deleted", "reserve.cancelled"):
        return "cancelled"
    if event == "reserve.created":
        return "cancelled" if is_cancelled(payload) else "created"
    if event == "reserve.updated":
        return "cancelled" if is_cancelled(payload) else "updated"
    return None


def client_snapshot(payload: RestoplaceWebhook) -> dict:
    """Снимок клиентских полей брони для последующего сравнения."""
    return {
        "time_from": _clean(payload.time_from),
        "time_to": _clean(payload.time_to),
        "item_id": str(payload.item_id or ""),
        "item_type": _clean(payload.item_type),
        "floor_name": _clean(payload.floor_name),
        "count": str(payload.count or ""),
    }


def changed_client_fields(old: dict, new: dict) -> list[str]:
    """Список человекочитаемых меток клиентских полей, которые изменились."""
    labels: list[str] = []
    for key, label in CLIENT_FIELDS.items():
        if old.get(key, "") != new.get(key, "") and label not in labels:
            labels.append(label)
    return labels


def _place_str(payload: RestoplaceWebhook) -> str:
    """Название объекта: из справочника (Беседка №2) либо тип+зона."""
    if (payload.item_type or "") == "waitlist":
        return "Лист ожидания"

    base = item_name(payload.item_id) or _item_label(payload.item_type)
    floor = _clean(payload.floor_name)
    if floor and floor not in base:
        return f"{base}, {floor}"
    return base


def place_str(payload: RestoplaceWebhook) -> str:
    """Публичная обёртка над _place_str (название объекта брони)."""
    return _place_str(payload)


def _placeholder_values(payload: RestoplaceWebhook) -> dict[str, str]:
    """Значения для подстановки в шаблон. Пустые поля → пустая строка."""
    date, time = datetime_parts(payload.time_from, payload.time_to)
    return {
        "{имя}": _clean(payload.name),
        "{объект}": _place_str(payload),
        "{дата}": date or "",
        "{время}": time or "",
        "{гостей}": str(payload.count) if payload.count not in (None, "") else "",
        "{номер}": str(payload.reserve_id) if payload.reserve_id not in (None, "", 0, "0") else "",
    }


_PLACEHOLDER_RE = re.compile(r"\{[^}]+\}")


def render_template(template: str, payload: RestoplaceWebhook) -> str:
    """Подставляет поля брони в шаблон.

    Строку, где есть плейсхолдеры и ВСЕ они оказались пустыми, выкидываем целиком
    (например «👥 Гостей: {гостей}» без числа или «🔖 Бронь №{номер}» без номера —
    чтобы не было висящих меток). Лишние пустые строки схлопываются.
    """
    values = _placeholder_values(payload)
    out: list[str] = []
    for line in template.split("\n"):
        found = _PLACEHOLDER_RE.findall(line)
        if found:
            if all(not values.get(ph, "") for ph in found):
                continue  # вся строка держалась на пустых полях — убираем
            line = _PLACEHOLDER_RE.sub(lambda m: values.get(m.group(0), ""), line)
        out.append(line)
    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()  # не больше одной пустой строки подряд
    if settings.signature:
        text = f"{text}\n\n{settings.signature}"
    return text


def build_message(payload: RestoplaceWebhook, template: str | None = None) -> str:
    """Текст сообщения клиенту в MAX по шаблону смыслового типа события.

    template — готовый текст шаблона (из настроек панели). Если не передан,
    берём встроенный пример DEFAULT_TEMPLATES по типу (created/updated/cancelled).
    Лист ожидания обрабатывается как обычная бронь (объект = «Лист ожидания»).
    """
    logical = logical_event(payload) or "updated"
    if template is None:
        template = DEFAULT_TEMPLATES.get(logical, DEFAULT_TEMPLATES["updated"])
    return render_template(template, payload)


_LOGICAL_TAGS = {"created": "новая", "updated": "правка", "cancelled": "отмена"}


def log_summary(payload: RestoplaceWebhook) -> str:
    """Компактная опрятная строка для консоли."""
    logical = logical_event(payload)
    tag = _LOGICAL_TAGS.get(logical, "?")
    event = payload.event or "—"
    rid = payload.reserve_id if payload.reserve_id not in (None, "", 0) else "—"
    name = _clean(payload.name) or "—"
    phone = _clean(payload.phone) or "—"

    date, time = datetime_parts(payload.time_from, payload.time_to)
    when = " ".join(x for x in (date, time) if x) or "—"

    obj = _place_str(payload)

    count = payload.count if payload.count not in (None, "") else "—"

    lines = [
        f"📩 {event} → {tag}  (бронь #{rid})",
        f"   Гость:  {name}, {phone}",
        f"   Когда:  {when}",
        f"   Объект: {obj}",
        f"   Гостей: {count}",
    ]
    reason = _clean(payload.cancel_reason)
    if reason:
        lines.append(f"   Отмена: {reason}")
    return "\n".join(lines)
