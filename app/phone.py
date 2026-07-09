"""Нормализация телефонного номера и преобразование в chatId шлюза kapuchino-api."""

from __future__ import annotations

import re


def normalize_phone(raw: str | None) -> str | None:
    """Приводит номер к виду 7XXXXXXXXXX (только цифры, РФ).

    Поддерживает форматы: +7 (999) 123-45-67, 89991234567, 79991234567,
    9991234567. Возвращает None, если номер распознать не удалось.
    """
    if not raw:
        return None

    digits = re.sub(r"\D", "", str(raw))
    if not digits:
        return None

    # 8XXXXXXXXXX -> 7XXXXXXXXXX
    if len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    # XXXXXXXXXX (10 цифр без кода страны) -> 7XXXXXXXXXX
    elif len(digits) == 10:
        digits = "7" + digits

    # Базовая валидация российского номера
    if len(digits) != 11 or not digits.startswith("7"):
        # Не РФ-формат — возвращаем как есть (международные номера),
        # пусть шлюз сам решит. Но пустые/слишком короткие отсекаем.
        return digits if len(digits) >= 10 else None

    return digits


def to_chat_id(raw: str | None) -> str | None:
    """Преобразует номер телефона в chatId шлюза для MAX (например +79991234567).

    MAX-сайдкар kapuchino-api резолвит получателя по номеру, только если он
    начинается с «+» (иначе считает строку готовым числовым chatId).
    """
    phone = normalize_phone(raw)
    if not phone:
        return None
    return f"+{phone}"
