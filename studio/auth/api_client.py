"""HTTP client wrapper for authentication endpoints."""

from __future__ import annotations

import os
from typing import Any

import httpx


class AuthError(Exception):
    """Raised when credentials or token are invalid."""


class NetworkError(Exception):
    """Raised when server is unreachable or unhealthy."""


class AuthApiClient:
    """Synchronous wrapper over remote authentication APIs."""

    DEFAULT_BASE_URL = "https://fmmonitor.sscapi.co"
    CONNECT_TIMEOUT = 10.0
    READ_TIMEOUT = 30.0

    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (
            base_url
            or os.environ.get("HT_STUDIO_API_BASE_URL")
            or self.DEFAULT_BASE_URL
        ).rstrip("/")
        timeout = httpx.Timeout(
            connect=self.CONNECT_TIMEOUT,
            read=self.READ_TIMEOUT,
            write=10.0,
            pool=5.0,
        )
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            headers={"User-Agent": "HumanTetrisStudio/0.1"},
        )

    def login(self, username: str, password: str) -> str:
        """Authenticate credentials and return an access token."""
        try:
            response = self._client.post(
                "/api/v1/auth/login",
                data={"username": username, "password": password},
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise NetworkError(f"Khong ket noi duoc server: {exc}") from exc
        except httpx.TimeoutException as exc:
            raise NetworkError("Server khong phan hoi") from exc

        if response.status_code == 401:
            raise AuthError("Sai username hoac password")
        if response.status_code == 422:
            raise AuthError("Du lieu nhap khong hop le")
        if response.status_code >= 500:
            raise NetworkError(f"Loi server ({response.status_code})")
        if response.status_code != 200:
            message = response.text[:200]
            raise AuthError(
                f"Dang nhap that bai ({response.status_code}): {message}"
            )

        try:
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            raise NetworkError("Server tra ve du lieu khong hop le") from exc

        token = payload.get("access_token")
        if not token:
            raise AuthError("Server khong tra ve access_token")
        return token

    def fetch_me(self, token: str) -> dict[str, Any]:
        """Fetch current user using bearer token."""
        try:
            response = self._client.get(
                "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {token}"},
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise NetworkError(f"Khong ket noi duoc server: {exc}") from exc
        except httpx.TimeoutException as exc:
            raise NetworkError("Server khong phan hoi") from exc

        if response.status_code == 401:
            raise AuthError("Phien dang nhap da het han")
        if response.status_code >= 500:
            raise NetworkError(f"Loi server ({response.status_code})")
        if response.status_code != 200:
            raise AuthError(
                f"Khong lay duoc thong tin user ({response.status_code})"
            )

        try:
            return response.json()
        except Exception as exc:  # noqa: BLE001
            raise NetworkError("Server tra ve du lieu khong hop le") from exc

    def close(self) -> None:
        """Close underlying HTTP client."""
        self._client.close()

