"""Authentication domain service abstractions."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, Protocol

from .api_client import AuthApiClient


@dataclass
class AuthUser:
    """Authenticated user details for studio session."""

    username: str
    display_name: str
    token: str
    token_expires_at: Optional[datetime] = None
    raw: dict[str, Any] = field(default_factory=dict)


class AuthService(Protocol):
    """Protocol for login, restore, and logout operations."""

    def login(self, username: str, password: str) -> AuthUser:
        """Authenticate and return user session."""

    def restore(self, token: str) -> AuthUser:
        """Restore user session from an existing token."""

    def logout(self, user: AuthUser) -> None:
        """Clear/terminate user session if server supports it."""


class RemoteAuthService:
    """Concrete auth service backed by remote HTTP endpoints."""

    def __init__(self, client: AuthApiClient | None = None) -> None:
        self._client = client or AuthApiClient()

    def login(self, username: str, password: str) -> AuthUser:
        """Authenticate using username/password."""
        token = self._client.login(username, password)
        raw = self._client.fetch_me(token)
        return self._build_user(token, raw)

    def restore(self, token: str) -> AuthUser:
        """Validate an existing token and restore user data."""
        raw = self._client.fetch_me(token)
        return self._build_user(token, raw)

    def logout(self, user: AuthUser) -> None:
        """No-op for now; local token cleanup happens elsewhere."""
        _ = user

    def close(self) -> None:
        """Close underlying API client resources."""
        self._client.close()

    def _build_user(self, token: str, raw: dict[str, Any]) -> AuthUser:
        username = raw.get("username") or raw.get("sub") or "user"
        display_name = (
            raw.get("display_name")
            or raw.get("full_name")
            or raw.get("name")
            or username
        )
        return AuthUser(
            username=username,
            display_name=display_name,
            token=token,
            token_expires_at=self._extract_jwt_exp(token),
            raw=raw,
        )

    @staticmethod
    def _extract_jwt_exp(token: str) -> Optional[datetime]:
        """Best-effort parser for JWT exp claim, no signature verification."""
        try:
            parts = token.split(".")
            if len(parts) < 2:
                return None
            payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64))
            exp = payload.get("exp")
            if not exp:
                return None
            return datetime.fromtimestamp(exp)
        except Exception:  # noqa: BLE001
            return None

