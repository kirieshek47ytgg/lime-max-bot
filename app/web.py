"""Отдача самой страницы админ-панели (единый HTML-файл) по адресу /admin."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse

router = APIRouter()

PANEL_FILE = Path(__file__).parent / "static" / "panel.html"


@router.get("/admin", response_class=HTMLResponse, include_in_schema=False)
def admin_panel() -> FileResponse:
    """Главная страница панели. Авторизация — уже внутри (через /api/auth)."""
    return FileResponse(PANEL_FILE, media_type="text/html")
