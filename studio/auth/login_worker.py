"""Background workers for login and token restore flow."""

from __future__ import annotations

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from .api_client import AuthError, NetworkError
from .auth_service import AuthService


class LoginWorkerSignals(QObject):
    """Signals emitted by auth workers."""

    succeeded = Signal(object)  # AuthUser
    failed = Signal(str, str)  # kind, message


class LoginWorker(QRunnable):
    """Perform login request in thread pool."""

    def __init__(self, service: AuthService, username: str, password: str) -> None:
        super().__init__()
        self.signals = LoginWorkerSignals()
        self._service = service
        self._username = username
        self._password = password

    @Slot()
    def run(self) -> None:
        """Run login call and emit completion signal."""
        try:
            user = self._service.login(self._username, self._password)
            self.signals.succeeded.emit(user)
        except AuthError as exc:
            self.signals.failed.emit("auth", str(exc))
        except NetworkError as exc:
            self.signals.failed.emit("network", str(exc))
        except Exception as exc:  # noqa: BLE001
            self.signals.failed.emit("network", f"Loi khong xac dinh: {exc}")


class RestoreWorker(QRunnable):
    """Restore login session from saved token in thread pool."""

    def __init__(self, service: AuthService, token: str) -> None:
        super().__init__()
        self.signals = LoginWorkerSignals()
        self._service = service
        self._token = token

    @Slot()
    def run(self) -> None:
        """Run token restore and emit completion signal."""
        try:
            user = self._service.restore(self._token)
            self.signals.succeeded.emit(user)
        except AuthError as exc:
            self.signals.failed.emit("auth", str(exc))
        except NetworkError as exc:
            self.signals.failed.emit("network", str(exc))
        except Exception as exc:  # noqa: BLE001
            self.signals.failed.emit("network", f"Loi khong xac dinh: {exc}")

