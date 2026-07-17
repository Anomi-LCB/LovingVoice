import pytest
from fastapi import HTTPException

from app.main import normalize_room_id, normalize_room_password


def test_room_name_requires_at_least_three_normalized_characters():
    assert normalize_room_id("주일 예배") == "주일-예배"
    assert normalize_room_id("abc") == "abc"

    with pytest.raises(HTTPException) as error:
        normalize_room_id("ab")

    assert error.value.status_code == 422
    assert "3~40자" in error.value.detail


def test_room_password_is_empty_or_at_least_four_characters_without_maximum():
    assert normalize_room_password("") == ""
    assert normalize_room_password("   ") == ""
    assert normalize_room_password("1234") == "1234"
    assert normalize_room_password("x" * 1000) == "x" * 1000

    with pytest.raises(HTTPException) as error:
        normalize_room_password("123")

    assert error.value.status_code == 422
    assert "비워두거나 4자 이상" in error.value.detail
