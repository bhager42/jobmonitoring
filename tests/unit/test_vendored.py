"""Vendored third-party files must match the hashes recorded in VENDORED.md."""

import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VENDORED = ROOT / "app/web/static/js/VENDORED.md"


def test_vendored_files_match_recorded_hashes() -> None:
    rows = re.findall(r"^\| `([^`]+)`.*\| `([0-9a-f]{64})` \|$", VENDORED.read_text(), re.M)
    assert len(rows) == 2
    for name, expected in rows:
        path = ROOT / name if "/" in name else VENDORED.parent / name
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected, name
