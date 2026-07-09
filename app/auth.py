"""Авторизация админ-панели: вход по логину/паролю, серверные сессии в cookie.

Сессия — случайный токен в HttpOnly-cookie; сопоставление токен→админ хранится в
БД (таблица sessions). Простой антиперебор: считаем неудачные попытки по IP за
последнюю минуту и притормаживаем.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from . import db
from .config import settings

log = logging.getLogger("limemaxbot.auth")

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Антиперебор: не больше LOGIN_MAX_FAILS неудачных попыток с одного IP за
# LOGIN_WINDOW_MIN минут. Дальше — временный отказ (429), пока окно не «остынет».
LOGIN_MAX_FAILS = 10
LOGIN_WINDOW_MIN = 15
# Сколько живёт cookie сессии (совпадает с db.SESSION_TTL_DAYS на сервере).
SESSION_MAX_AGE = 60 * 60 * 24 * db.SESSION_TTL_DAYS


class LoginBody(BaseModel):
    username: str
    password: str


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "?"


def current_admin(request: Request) -> dict:
    """FastAPI-зависимость: возвращает админа по cookie или отдаёт 401."""
    token = request.cookies.get(settings.session_cookie)
    admin = db.session_admin(token)
    if not admin:
        raise HTTPException(status_code=401, detail="unauthorized")
    return admin


def require_owner(admin: dict = Depends(current_admin)) -> dict:
    """Зависимость для разделов «только владелец»."""
    if admin.get("role") != "owner":
        raise HTTPException(status_code=403, detail="forbidden")
    return admin


@router.post("/login")
def login(body: LoginBody, request: Request, response: Response) -> dict:
    ip = _client_ip(request)

    # Тормозим перебор пароля: много промахов с одного IP → временный отказ.
    if db.recent_failed_logins(ip, LOGIN_WINDOW_MIN) >= LOGIN_MAX_FAILS:
        log.warning("Слишком много неудачных входов с IP %s — временная блокировка", ip)
        raise HTTPException(
            status_code=429,
            detail="Слишком много попыток входа. Подождите несколько минут.",
        )

    admin = db.find_admin(body.username.strip())
    if not admin or not db.verify_password(body.password, admin["password_hash"]):
        db.add_login_log(body.username.strip(), ip, ok=False)
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")

    token = db.create_session(admin["id"])
    db.add_login_log(admin["username"], ip, ok=True)
    response.set_cookie(
        key=settings.session_cookie,
        value=token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=SESSION_MAX_AGE,
    )
    return {"username": admin["username"], "role": admin["role"]}


@router.post("/logout")
def logout(request: Request, response: Response) -> dict:
    db.delete_session(request.cookies.get(settings.session_cookie))
    response.delete_cookie(settings.session_cookie)
    return {"status": "ok"}


@router.get("/me")
def me(admin: dict = Depends(current_admin)) -> dict:
    return {"username": admin["username"], "role": admin["role"]}
