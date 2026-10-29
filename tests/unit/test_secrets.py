import stat
from pathlib import Path

import pytest

from app.security.secrets import SecretBox, SecretUnavailableError


def test_round_trip_and_key_file(tmp_path: Path) -> None:
    key = tmp_path / "data" / "notify.key"
    box = SecretBox(key)
    token = box.encrypt("ntfy://user:hunter2@ntfy.example/jobs")
    assert "hunter2" not in token
    assert box.decrypt(token) == "ntfy://user:hunter2@ntfy.example/jobs"
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    # A new instance (e.g. after a restart) reads the same key.
    assert SecretBox(key).decrypt(token) == "ntfy://user:hunter2@ntfy.example/jobs"


def test_other_key_cannot_decrypt(tmp_path: Path) -> None:
    token = SecretBox(tmp_path / "a.key").encrypt("secret")
    with pytest.raises(SecretUnavailableError, match="another install"):
        SecretBox(tmp_path / "b.key").decrypt(token)


def test_existing_key_is_never_overwritten(tmp_path: Path) -> None:
    key = tmp_path / "notify.key"
    first = SecretBox(key)
    token = first.encrypt("x")
    before = key.read_bytes()
    SecretBox(key).encrypt("y")
    assert key.read_bytes() == before
    assert first.decrypt(token) == "x"
