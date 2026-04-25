"""Auth service unit tests with mocked HTTP transport."""

from __future__ import annotations

import base64
import json
from datetime import datetime

import httpx

from studio.auth.api_client import AuthApiClient, AuthError, NetworkError
from studio.auth.auth_service import RemoteAuthService


def _jwt_with_exp(exp: int) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=")
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": exp}).encode("utf-8")
    ).rstrip(b"=")
    return f"{header.decode()}.{payload.decode()}.sig"


def _client_with_transport(handler):
    client = AuthApiClient(base_url="https://example.test")
    client._client.close()  # type: ignore[attr-defined]
    client._client = httpx.Client(  # type: ignore[attr-defined]
        base_url="https://example.test",
        transport=httpx.MockTransport(handler),
    )
    return client


def test_login_success_returns_auth_user() -> None:
    token = _jwt_with_exp(2_000_000_000)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/login"):
            return httpx.Response(200, json={"access_token": token})
        if request.url.path.endswith("/me"):
            return httpx.Response(200, json={"username": "fmremoter"})
        return httpx.Response(404)

    service = RemoteAuthService(_client_with_transport(handler))
    user = service.login("u", "p")
    assert user.username == "fmremoter"
    assert user.token == token
    assert user.token_expires_at is not None


def test_login_401_raises_auth_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    service = RemoteAuthService(_client_with_transport(handler))
    try:
        service.login("u", "p")
    except AuthError:
        assert True
        return
    assert False, "Expected AuthError"


def test_login_network_error_raises_network_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    service = RemoteAuthService(_client_with_transport(handler))
    try:
        service.login("u", "p")
    except NetworkError:
        assert True
        return
    assert False, "Expected NetworkError"


def test_extract_jwt_exp_handles_invalid_token() -> None:
    parsed = RemoteAuthService._extract_jwt_exp("invalid.token")
    assert parsed is None


def test_restore_success_and_401() -> None:
    token = _jwt_with_exp(2_000_000_000)

    def ok_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/me"):
            return httpx.Response(200, json={"username": "ok"})
        return httpx.Response(404)

    service_ok = RemoteAuthService(_client_with_transport(ok_handler))
    user = service_ok.restore(token)
    assert user.username == "ok"

    def bad_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/me"):
            return httpx.Response(401)
        return httpx.Response(404)

    service_bad = RemoteAuthService(_client_with_transport(bad_handler))
    try:
        service_bad.restore(token)
    except AuthError:
        assert True
        return
    assert False, "Expected AuthError on restore"

