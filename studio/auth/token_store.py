"""Secure token storage via OS keychain."""

from __future__ import annotations

try:
    import keyring
    from keyring.errors import KeyringError
except ImportError:  # pragma: no cover - optional dependency fallback
    keyring = None  # type: ignore[assignment]
    KeyringError = Exception  # type: ignore[misc,assignment]

SERVICE_NAME = "human_tetris_studio"
TOKEN_KEY = "auth_token"
USERNAME_KEY = "auth_username"
PASSWORD_KEY = "auth_password"


def is_keyring_available() -> bool:
    """Return True when keyring backend can persist secrets."""
    return keyring is not None


def save_token(token: str) -> bool:
    """Persist token to OS keychain, if available."""
    if keyring is None:
        return False
    try:
        keyring.set_password(SERVICE_NAME, TOKEN_KEY, token)
        return True
    except KeyringError:
        return False


def load_token() -> str | None:
    """Load persisted token from OS keychain."""
    if keyring is None:
        return None
    try:
        return keyring.get_password(SERVICE_NAME, TOKEN_KEY)
    except KeyringError:
        return None


def clear_token() -> None:
    """Delete persisted token when signing out or token invalid."""
    if keyring is None:
        return
    try:
        keyring.delete_password(SERVICE_NAME, TOKEN_KEY)
    except KeyringError:
        return


def save_credentials(username: str, password: str) -> bool:
    """Persist username/password in keychain for remember-me."""
    if keyring is None:
        return False
    try:
        keyring.set_password(SERVICE_NAME, USERNAME_KEY, username)
        keyring.set_password(SERVICE_NAME, PASSWORD_KEY, password)
        return True
    except KeyringError:
        return False


def load_credentials() -> tuple[str | None, str | None]:
    """Load saved username/password from keychain."""
    if keyring is None:
        return None, None
    try:
        username = keyring.get_password(SERVICE_NAME, USERNAME_KEY)
        password = keyring.get_password(SERVICE_NAME, PASSWORD_KEY)
        return username, password
    except KeyringError:
        return None, None


def clear_credentials() -> None:
    """Delete saved username/password from keychain."""
    if keyring is None:
        return
    for key in (USERNAME_KEY, PASSWORD_KEY):
        try:
            keyring.delete_password(SERVICE_NAME, key)
        except KeyringError:
            continue

