import os
from pathlib import Path

import pytest

from app.cli import ensure_data_dir_writable


def test_creates_missing_data_dir(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "data"
    ensure_data_dir_writable(target)
    assert target.is_dir()
    assert list(target.iterdir()) == []


@pytest.mark.skipif(os.getuid() == 0, reason="root ignores directory permissions")
def test_exits_with_message_when_not_writable(tmp_path: Path) -> None:
    tmp_path.chmod(0o500)
    try:
        with pytest.raises(SystemExit, match="not writable"):
            ensure_data_dir_writable(tmp_path)
    finally:
        tmp_path.chmod(0o700)


def test_serve_runs_single_worker_uvicorn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    from app.cli import main
    from app.config import get_settings

    calls: list[dict[str, object]] = []
    monkeypatch.setattr(uvicorn, "run", lambda *_a, **kw: calls.append(kw))
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PORT", "9000")
    get_settings.cache_clear()
    try:
        main(["serve"])
    finally:
        get_settings.cache_clear()

    assert len(calls) == 1
    assert calls[0]["port"] == 9000
    assert calls[0]["workers"] == 1
    assert calls[0]["proxy_headers"] is False
    assert calls[0]["forwarded_allow_ips"] is None
