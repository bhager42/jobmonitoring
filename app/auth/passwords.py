"""Argon2id password hashing."""

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 1024

_hasher = PasswordHasher()  # argon2-cffi defaults follow the RFC 9106 low-memory profile


class PasswordPolicyError(ValueError):
    pass


def validate_new_password(password: str, confirm: str | None = None) -> None:
    if confirm is not None and password != confirm:
        raise PasswordPolicyError("The passwords do not match.")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError(
            f"Use at least {MIN_PASSWORD_LENGTH} characters. A few random words work well."
        )
    if len(password) > MAX_PASSWORD_LENGTH:
        raise PasswordPolicyError(f"Use at most {MAX_PASSWORD_LENGTH} characters.")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    if len(password) > MAX_PASSWORD_LENGTH:
        return False
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)
