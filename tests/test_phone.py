from app.phone import normalize_phone, to_chat_id


def test_normalize_various_formats():
    assert normalize_phone("+7 (999) 123-45-67") == "79991234567"
    assert normalize_phone("89991234567") == "79991234567"
    assert normalize_phone("79991234567") == "79991234567"
    assert normalize_phone("9991234567") == "79991234567"


def test_normalize_invalid():
    assert normalize_phone("") is None
    assert normalize_phone(None) is None
    assert normalize_phone("abc") is None
    assert normalize_phone("123") is None


def test_to_chat_id():
    assert to_chat_id("+7 (999) 123-45-67") == "+79991234567"
    assert to_chat_id("89991234567") == "+79991234567"
    assert to_chat_id("bad") is None
