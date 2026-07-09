"""Хранилище снимков броней для определения, что изменилось при reserve.updated.

Webhook присылает полный снимок брони, а не дельту. Чтобы понять, изменилось ли
что-то значимое для клиента, мы храним предыдущий снимок клиентских полей каждой
брони (по reserve_id) в JSON-файле и сравниваем с новым.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .config import settings

log = logging.getLogger("limemaxbot.state")


def _path() -> Path:
    return Path(settings.state_file)


def _load_all() -> dict:
    path = _path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Не удалось прочитать состояние %s: %s", path, exc)
        return {}


def _save_all(data: dict) -> None:
    path = _path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        log.warning("Не удалось сохранить состояние %s: %s", path, exc)


def get_snapshot(reserve_id: object) -> dict | None:
    if reserve_id in (None, "", 0, "0"):
        return None
    return _load_all().get(str(reserve_id))


def set_snapshot(reserve_id: object, snapshot: dict) -> None:
    if reserve_id in (None, "", 0, "0"):
        return
    data = _load_all()
    data[str(reserve_id)] = snapshot
    _save_all(data)


def delete_snapshot(reserve_id: object) -> None:
    if reserve_id in (None, "", 0, "0"):
        return
    data = _load_all()
    if data.pop(str(reserve_id), None) is not None:
        _save_all(data)
