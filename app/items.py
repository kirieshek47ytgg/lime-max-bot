"""Справочник объектов: внутренний item_id -> человекочитаемое название.

В webhook Restoplace человекочитаемого номера беседки нет — только внутренний
item_id. Этот справочник позволяет показывать гостю «Беседка №2» вместо id.
Заполняется в файле items.json (см. items.example.json).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .config import settings

log = logging.getLogger("limemaxbot.items")


def load_items() -> dict[str, str]:
    path = Path(settings.items_file)
    if not path.exists():
        log.info("Справочник %s не найден — выводим тип объекта и зону.", path)
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        items = {str(k): str(v) for k, v in data.items()}
        log.info("Загружен справочник объектов: %d записей.", len(items))
        return items
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        log.warning("Не удалось прочитать справочник %s: %s", path, exc)
        return {}


ITEMS: dict[str, str] = load_items()


def item_name(item_id: object) -> str | None:
    """Название объекта по id, либо None если не найдено."""
    if item_id in (None, "", 0, "0"):
        return None
    return ITEMS.get(str(item_id))
