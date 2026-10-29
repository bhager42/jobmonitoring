"""First-boot setup: secret key, initial password, and the one-time setup token."""

import logging
import secrets
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.passwords import hash_password, validate_new_password
from app.config import Settings
from app.services.app_settings import PASSWORD_HASH, SECRET_KEY, get_setting, set_setting

log = logging.getLogger(__name__)


class BootstrapError(RuntimeError):
    pass


@dataclass
class AuthState:
    """Process-wide auth state, kept on `app.state.auth`."""

    secret_key: str
    password_set: bool
    setup_token: str | None = field(default=None, repr=False)

    def consume_setup_token(self) -> None:
        self.setup_token = None
        self.password_set = True


def initial_password_from_env(settings: Settings) -> str | None:
    if settings.app_password is not None and settings.app_password_file is not None:
        raise BootstrapError("Set only one of APP_PASSWORD and APP_PASSWORD_FILE.")
    if settings.app_password_file is not None:
        try:
            return settings.app_password_file.read_text().rstrip("\r\n")
        except OSError as exc:
            raise BootstrapError(
                f"Cannot read APP_PASSWORD_FILE {settings.app_password_file}: {exc.strerror}"
            ) from exc
    if settings.app_password is not None:
        return settings.app_password.get_secret_value()
    return None


async def bootstrap_auth(db: AsyncSession, settings: Settings) -> AuthState:
    secret_key = await get_setting(db, SECRET_KEY)
    if secret_key is None:
        secret_key = secrets.token_urlsafe(32)
        await set_setting(db, SECRET_KEY, secret_key)

    password_set = await get_setting(db, PASSWORD_HASH) is not None
    env_password = initial_password_from_env(settings)
    if not password_set and env_password is not None:
        try:
            validate_new_password(env_password)
        except ValueError as exc:
            raise BootstrapError(f"APP_PASSWORD is not acceptable: {exc}") from exc
        await set_setting(db, PASSWORD_HASH, hash_password(env_password))
        password_set = True
        log.warning("Password set from the environment.")
    elif password_set and env_password is not None:
        log.info(
            "Ignoring APP_PASSWORD: a password is already set. "
            "Change it in Settings or with `jobmonitor reset-password`."
        )
    await db.commit()

    state = AuthState(secret_key=secret_key, password_set=password_set)
    if not password_set:
        state.setup_token = secrets.token_urlsafe(16)
        log.warning(
            "No password is set. Setup token: %s - open http://<host>:<port>/setup to choose one.",
            state.setup_token,
        )
    return state
