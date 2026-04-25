"""Login dialog for Human Tetris Studio."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QThreadPool, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .auth_service import AuthService, AuthUser
from .login_worker import LoginWorker, RestoreWorker
from .token_store import (
    clear_credentials,
    clear_token,
    is_keyring_available,
    load_credentials,
    load_token,
    save_credentials,
    save_token,
)


class LoginWindow(QDialog):
    """Modal login dialog with token restore support."""

    def __init__(self, service: AuthService, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._service = service
        self._authenticated_user: AuthUser | None = None
        self._pool = QThreadPool.globalInstance()
        self._active_workers: list[object] = []
        self._build_ui()
        self._prefill_saved_credentials()
        self._try_restore_token()

    @property
    def authenticated_user(self) -> AuthUser | None:
        """Get authenticated user once login succeeds."""
        return self._authenticated_user

    def _build_ui(self) -> None:
        self.setWindowTitle("Sign in - Human Tetris Studio")
        self.setModal(True)
        self.setMinimumWidth(420)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(12)

        title = QLabel("Human Tetris Studio")
        title.setObjectName("loginTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(title)

        subtitle = QLabel("Sign in to continue")
        subtitle.setObjectName("loginSubtitle")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(subtitle)

        form_layout = QFormLayout()
        form_layout.setContentsMargins(0, 8, 0, 0)
        form_layout.setHorizontalSpacing(12)
        form_layout.setVerticalSpacing(10)

        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("Username")
        self.username_input.returnPressed.connect(self._on_sign_in_clicked)
        form_layout.addRow("Username", self.username_input)

        self.password_input = QLineEdit()
        self.password_input.setPlaceholderText("Password")
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_input.returnPressed.connect(self._on_sign_in_clicked)
        form_layout.addRow("Password", self.password_input)
        root.addLayout(form_layout)

        self.remember_checkbox = QCheckBox("Remember me")
        self.remember_checkbox.setChecked(True)
        if not is_keyring_available():
            self.remember_checkbox.setChecked(False)
            self.remember_checkbox.setEnabled(False)
            self.remember_checkbox.setToolTip(
                "Remember me requires keyring package/backend"
            )
        root.addWidget(self.remember_checkbox)

        self.error_label = QLabel("")
        self.error_label.setObjectName("loginErrorLabel")
        self.error_label.setWordWrap(True)
        self.error_label.setVisible(False)
        root.addWidget(self.error_label)

        button_row = QHBoxLayout()
        button_row.addStretch()
        self.sign_in_button = QPushButton("Sign in")
        self.sign_in_button.setObjectName("primaryButton")
        self.sign_in_button.clicked.connect(self._on_sign_in_clicked)
        self.sign_in_button.setDefault(True)
        button_row.addWidget(self.sign_in_button)
        root.addLayout(button_row)

    def _on_sign_in_clicked(self) -> None:
        username = self.username_input.text().strip()
        password = self.password_input.text()
        if not username or not password:
            self._show_error("Vui long nhap username va password")
            return

        self._start_login_worker(username, password)

    def _start_login_worker(self, username: str, password: str) -> None:
        """Start background login worker with current credentials."""
        self._set_loading(True, "Signing in...")
        worker = LoginWorker(self._service, username, password)
        self._track_worker(worker)
        worker.signals.succeeded.connect(
            lambda user, w=worker: self._handle_worker_success(w, user)
        )
        worker.signals.failed.connect(
            lambda kind, msg, w=worker: self._handle_worker_failure(w, kind, msg)
        )
        self._pool.start(worker)

    def _on_login_succeeded(self, user: AuthUser) -> None:
        if self.remember_checkbox.isChecked():
            save_token(user.token)
            save_credentials(self.username_input.text().strip(), self.password_input.text())
        else:
            clear_token()
            clear_credentials()

        self._authenticated_user = user
        self._set_loading(False)
        self.accept()

    def _on_login_failed(self, kind: str, message: str) -> None:
        self._set_loading(False)
        self._show_error(message)
        if kind == "auth":
            self.password_input.clear()
            self.password_input.setFocus()

    def _try_restore_token(self) -> None:
        token = load_token()
        if token:
            self._set_loading(True, "Dang khoi phuc phien...")
            worker = RestoreWorker(self._service, token)
            self._track_worker(worker)
            worker.signals.succeeded.connect(
                lambda user, w=worker: self._handle_worker_success(w, user)
            )
            worker.signals.failed.connect(
                lambda kind, msg, w=worker: self._handle_restore_failure(w, kind, msg)
            )
            self._pool.start(worker)
            return

        username, password = load_credentials()
        if username and password:
            self.username_input.setText(username)
            self.password_input.setText(password)
            self._start_login_worker(username, password)

    def _on_restore_failed(self, _kind: str, _message: str) -> None:
        clear_token()
        self._set_loading(False)
        username, password = load_credentials()
        if username and password:
            self.username_input.setText(username)
            self.password_input.setText(password)
            self._start_login_worker(username, password)

    def _prefill_saved_credentials(self) -> None:
        """Prefill username/password from keychain if available."""
        username, password = load_credentials()
        if username:
            self.username_input.setText(username)
            self.remember_checkbox.setChecked(True)
        if password:
            self.password_input.setText(password)

    def _set_loading(self, loading: bool, message: str = "Signing in...") -> None:
        self.username_input.setEnabled(not loading)
        self.password_input.setEnabled(not loading)
        self.remember_checkbox.setEnabled(not loading)
        self.sign_in_button.setEnabled(not loading)
        self.sign_in_button.setText(message if loading else "Sign in")
        if loading:
            self.error_label.setVisible(False)

    def _show_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.setVisible(True)

    def _track_worker(self, worker: object) -> None:
        """Hold worker references to avoid premature garbage collection."""
        self._active_workers.append(worker)

    def _release_worker(self, worker: object) -> None:
        """Release worker once finished to avoid growing references."""
        if worker in self._active_workers:
            self._active_workers.remove(worker)

    def _handle_worker_success(self, worker: object, user: AuthUser) -> None:
        self._release_worker(worker)
        self._on_login_succeeded(user)

    def _handle_worker_failure(self, worker: object, kind: str, message: str) -> None:
        self._release_worker(worker)
        self._on_login_failed(kind, message)

    def _handle_restore_failure(
        self, worker: object, kind: str, message: str
    ) -> None:
        self._release_worker(worker)
        self._on_restore_failed(kind, message)

