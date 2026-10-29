import pytest

from app.auth.csrf import csrf_token, is_cross_origin, verify_csrf_token
from app.auth.middleware import safe_next_url
from app.auth.passwords import (
    MAX_PASSWORD_LENGTH,
    PasswordPolicyError,
    hash_password,
    needs_rehash,
    validate_new_password,
    verify_password,
)
from app.auth.sessions import cookie_name

OWN = "http://nas.local:8686"


def test_password_hash_round_trip() -> None:
    hashed = hash_password("correct horse battery staple")
    assert hashed.startswith("$argon2id$")
    assert verify_password(hashed, "correct horse battery staple")
    assert not verify_password(hashed, "correct horse battery stapler")
    assert not needs_rehash(hashed)


def test_verify_handles_garbage_hash_and_huge_input() -> None:
    assert not verify_password("not-a-hash", "anything")
    assert not verify_password(hash_password("x" * 12), "x" * (MAX_PASSWORD_LENGTH + 1))


def test_old_parameters_need_rehash() -> None:
    from argon2 import PasswordHasher

    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash("x" * 12)
    assert needs_rehash(weak)


@pytest.mark.parametrize(
    ("password", "confirm", "message"),
    [
        ("short", None, "at least 12"),
        ("long enough password", "different password", "do not match"),
        ("x" * (MAX_PASSWORD_LENGTH + 1), None, "at most"),
    ],
)
def test_password_policy(password: str, confirm: str | None, message: str) -> None:
    with pytest.raises(PasswordPolicyError, match=message):
        validate_new_password(password, confirm)


def test_csrf_tokens() -> None:
    token = csrf_token("secret", "session-a")
    assert verify_csrf_token("secret", "session-a", token)
    assert not verify_csrf_token("secret", "session-b", token)
    assert not verify_csrf_token("other-secret", "session-a", token)
    assert not verify_csrf_token("secret", "session-a", None)
    assert not verify_csrf_token("secret", "session-a", "")


@pytest.mark.parametrize(
    ("headers", "cross"),
    [
        ({"sec-fetch-site": "same-origin"}, False),
        ({"sec-fetch-site": "none"}, False),
        ({"sec-fetch-site": "same-site"}, True),
        ({"sec-fetch-site": "cross-site"}, True),
        # Sec-Fetch-Site wins over Origin when both are present.
        ({"sec-fetch-site": "cross-site", "origin": OWN}, True),
        ({"origin": OWN}, False),
        ({"origin": OWN.upper()}, False),
        ({"origin": "http://nas.local:9999"}, True),
        ({"origin": "null"}, True),
        ({}, False),  # not a browser
    ],
)
def test_is_cross_origin(headers: dict[str, str], cross: bool) -> None:
    assert is_cross_origin(headers, OWN) is cross


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        (None, "/"),
        ("", "/"),
        ("/monitors?x=1", "/monitors?x=1"),
        ("//evil.example", "/"),
        ("/\\evil.example", "/"),
        ("https://evil.example", "/"),
        ("javascript:alert(1)", "/"),
        ("/ok\r\nSet-Cookie: x=1", "/"),
    ],
)
def test_safe_next_url(target: str | None, expected: str) -> None:
    assert safe_next_url(target) == expected


def test_cookie_name() -> None:
    assert cookie_name(secure=True) == "__Host-jm_session"
    assert cookie_name(secure=False) == "jm_session"
