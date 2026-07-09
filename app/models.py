"""Pydantic-модель входящего webhook от Restoplace.

ВАЖНО: реальный формат webhook Restoplace отличается от того, что описано
в их справке. По факту приходит (events-формат):

    event       — тип события: reserve.created / reserve.updated / reserve.deleted
    reserve_id  — номер брони
    name        — имя гостя
    phone       — телефон гостя
    count       — количество гостей
    time_from   — начало брони  "YYYY-MM-DD HH:MM:SS"
    time_to     — конец брони
    item_type   — тип объекта: alcove (беседка), table (столик), waitlist (лист ожидания)
    item_id     — внутренний id объекта (НЕ человекочитаемый номер беседки)
    floor_name  — название зала/зоны
    status, success, source, email, ...

Модель разрешает любые дополнительные поля (extra="allow").
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class RestoplaceWebhook(BaseModel):
    model_config = ConfigDict(extra="allow")

    event: str | None = None
    reserve_id: int | str | None = None
    name: str | None = None
    phone: str | None = None
    count: int | str | None = None
    time_from: str | None = None
    time_to: str | None = None
    item_type: str | None = None
    item_id: int | str | None = None
    floor_name: str | None = None
    status: int | str | None = None
    success: int | str | None = None
    source: str | None = None

    # Поля отмены: отмена приходит как event=reserve.updated со status=6
    # и заполненными cancel_reason / time_cancel / userid_cancel.
    cancel_reason: str | None = None
    time_cancel: str | None = None
    userid_cancel: int | str | None = None

    def as_dict(self) -> dict:
        """Полный словарь полей, включая нестандартные (extra)."""
        return self.model_dump()
