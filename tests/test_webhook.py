import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db, gateway
from app.config import settings
from app.main import app
from app.messages import build_message, datetime_parts, log_summary, logical_event
from app.models import RestoplaceWebhook

SAMPLES = json.loads(
    (Path(__file__).parent / "sample_payloads.json").read_text(encoding="utf-8")
)


@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch, tmp_path):
    # Ничего реально не отправляем; локальный .env не влияет на тесты.
    monkeypatch.setattr(settings, "dry_run", True)
    monkeypatch.setattr(settings, "webhook_secret", "")
    monkeypatch.setattr(settings, "enabled_notifications", "created,updated,cancelled")
    # Изолированный файл состояния на каждый тест.
    monkeypatch.setattr(settings, "state_file", str(tmp_path / "state.json"))
    # Свежая БД панели на каждый тест (вебхук пишет в неё клиента/бронь/сообщение).
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "media_dir", str(tmp_path / "media"))
    db.init_db()


@pytest.fixture()
def client():
    return TestClient(app)


def _sample(name):
    return RestoplaceWebhook.model_validate(SAMPLES[name])


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_reserve_created_sent(client):
    resp = client.post(settings.webhook_path, json=SAMPLES["reserve_created_alcove"])
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "sent"
    assert body["kind"] == "created"
    assert body["chatId"] == "+79991112222"
    assert body["gateway"]["skipped"] is True


def test_update_without_baseline_ignored(client):
    # Правка без ранее сохранённого снимка -> снимок сохраняется, но не шлём.
    resp = client.post(settings.webhook_path, json=SAMPLES["reserve_updated"])
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ignored"
    assert body["reason"] == "no baseline"


def test_update_with_client_change_sent(client):
    # 1) создаём бронь (сохраняем базовый снимок)
    client.post(settings.webhook_path, json=SAMPLES["reserve_created_alcove"])
    # 2) переносим время -> значимое для клиента изменение -> шлём
    moved = dict(SAMPLES["reserve_created_alcove"])
    moved["event"] = "reserve.updated"
    moved["time_from"] = "2026-06-27 15:00:00"
    moved["time_to"] = "2026-06-27 17:00:00"
    resp = client.post(settings.webhook_path, json=moved)
    assert resp.status_code == 200
    assert resp.json()["status"] == "sent"
    assert resp.json()["kind"] == "updated"


def test_update_internal_only_ignored(client):
    # 1) создаём бронь
    client.post(settings.webhook_path, json=SAMPLES["reserve_created_alcove"])
    # 2) меняем только внутренние поля (заметка/теги) -> клиенту не шлём
    internal = dict(SAMPLES["reserve_created_alcove"])
    internal["event"] = "reserve.updated"
    internal["text"] = "позвонить за час"
    internal["tags"] = ["VIP"]
    resp = client.post(settings.webhook_path, json=internal)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ignored"
    assert body["reason"] == "no client-relevant change"


def test_waitlist_not_sent(client):
    # Лист ожидания клиенту не отправляем — место ещё не подтверждено.
    resp = client.post(settings.webhook_path, json=SAMPLES["reserve_created_waitlist"])
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ignored"
    assert body["reason"] == "waitlist"


def test_waitlist_becomes_booking_sent_as_created(client):
    # 1) гость попал в лист ожидания (снимок сохранён, но не шлём)
    client.post(settings.webhook_path, json=SAMPLES["reserve_created_waitlist"])
    # 2) освободилось место: тот же reserve_id, item_type стал реальным объектом
    promoted = dict(SAMPLES["reserve_created_waitlist"])
    promoted["event"] = "reserve.updated"
    promoted["item_type"] = "alcove"
    promoted["item_id"] = 789368
    promoted["floor_name"] = "Основной зал"
    resp = client.post(settings.webhook_path, json=promoted)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "sent"
    # Приходит как подтверждение брони, а не как «изменение».
    assert body["kind"] == "created"


def test_cancellation_via_update_sent(client):
    # Отмена приходит как reserve.updated со status=6 -> kind=cancelled.
    resp = client.post(settings.webhook_path, json=SAMPLES["reserve_cancelled_via_update"])
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "sent"
    assert body["kind"] == "cancelled"


def test_gateway_error_returns_502(client, monkeypatch):
    # Реальная отправка (не dry_run): ошибка шлюза -> 502, чтобы Restoplace повторил.
    monkeypatch.setattr(settings, "dry_run", False)
    monkeypatch.setattr(settings, "gateway_url", "http://127.0.0.1:8080")
    monkeypatch.setattr(settings, "gateway_token", "x")

    async def boom(*_a, **_kw):
        raise gateway.GatewayError("Шлюз вернул 500")

    monkeypatch.setattr(gateway, "send_message", boom)
    resp = client.post(settings.webhook_path, json=SAMPLES["reserve_created_alcove"])
    assert resp.status_code == 502
    assert resp.json()["status"] == "error"


def test_gateway_incoming_stub(client):
    resp = client.post("/api/webhook", json={"typeWebhook": "incomingMessageReceived"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_no_event_ignored(client):
    resp = client.post(settings.webhook_path, json={"reserve_id": 1, "phone": "79991112222"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


def test_missing_phone(client):
    payload = dict(SAMPLES["reserve_created_alcove"])
    payload["phone"] = "bad"
    resp = client.post(settings.webhook_path, json=payload)
    assert resp.status_code == 200
    assert resp.json()["status"] == "skipped"


def test_webhook_secret(monkeypatch, client):
    monkeypatch.setattr(settings, "webhook_secret", "s3cret")
    resp = client.post(settings.webhook_path, json=SAMPLES["reserve_created_alcove"])
    assert resp.status_code == 403
    resp = client.post(
        settings.webhook_path + "?token=s3cret", json=SAMPLES["reserve_created_alcove"]
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "sent"


# --- логика смыслового события ---

def test_logical_event_created():
    assert logical_event(_sample("reserve_created_alcove")) == "created"


def test_logical_event_updated():
    assert logical_event(_sample("reserve_updated")) == "updated"


def test_logical_event_cancelled():
    assert logical_event(_sample("reserve_cancelled_via_update")) == "cancelled"


# --- дата/время ---

def test_datetime_parts_same_day():
    date, time = datetime_parts("2026-06-27 13:00:00", "2026-06-27 14:00:00")
    assert date == "27.06.2026"
    assert time == "13:00–14:00"


def test_datetime_parts_missing():
    assert datetime_parts(None, None) == (None, None)


# --- сборка текста ---

def test_build_message_uses_items_map():
    # item_id 789368 есть в items.json -> "Беседка №2"
    text = build_message(_sample("reserve_created_alcove"))
    assert "Бронирование принято" in text
    assert "Беседка №2" in text
    assert "Основной зал" in text
    assert "13:00–14:00" in text
    assert "Гостей: 3" in text


def test_build_message_cancelled_header():
    text = build_message(_sample("reserve_cancelled_via_update"))
    assert "отменено" in text.lower()


def test_build_message_waitlist():
    text = build_message(_sample("reserve_created_waitlist"))
    assert "лист ожидания" in text.lower()


def test_log_summary_marks_cancellation():
    s = log_summary(_sample("reserve_cancelled_via_update"))
    assert "отмена" in s
    assert "Гость отменил сам" in s
