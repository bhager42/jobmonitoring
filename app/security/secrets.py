"""Encrypt small secrets (notification URLs) with a key kept outside the database.

The key lives in its own file (default `/data/notify.key`), not in the database, so a copy of
the database alone, such as a downloaded backup, does not reveal the URLs and their tokens.
"""

import logging
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger(__name__)


class SecretUnavailableError(Exception):
    """The value was encrypted with a key this install no longer has."""


class SecretBox:
    def __init__(self, key_path: Path) -> None:
        self.key_path = key_path
        self._fernet: Fernet | None = None

    def _load(self) -> Fernet:
        if self._fernet is None:
            if not self.key_path.exists():
                self.key_path.parent.mkdir(parents=True, exist_ok=True)
                # O_EXCL: never overwrite a key written by another process in the meantime.
                try:
                    fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                except FileExistsError:
                    pass
                else:
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(Fernet.generate_key())
                    log.info("Created encryption key %s", self.key_path)
            self._fernet = Fernet(self.key_path.read_bytes().strip())
        return self._fernet

    def encrypt(self, plaintext: str) -> str:
        return self._load().encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._load().decrypt(token.encode()).decode()
        except InvalidToken as exc:
            raise SecretUnavailableError(
                f"cannot decrypt with {self.key_path}; it may come from another install"
            ) from exc
