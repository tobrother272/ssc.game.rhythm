"""Application entrypoint for Human Tetris Studio."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication, QDialog

from studio.auth.auth_service import RemoteAuthService
from studio.auth.login_window import LoginWindow
from studio.editor.main_window import MainWindow


def apply_stylesheet(app: QApplication) -> None:
    """Load QSS stylesheet if available."""
    stylesheet_path = Path(__file__).parent / "resources" / "styles.qss"
    if not stylesheet_path.exists():
        return
    app.setStyleSheet(stylesheet_path.read_text(encoding="utf-8"))


def main() -> int:
    """Run login flow then open the editor window."""
    app = QApplication(sys.argv)
    apply_stylesheet(app)

    auth = RemoteAuthService()
    while True:
        login = LoginWindow(auth)
        if login.exec() != QDialog.DialogCode.Accepted:
            auth.close()
            return 0
        user = login.authenticated_user
        if user is None:
            auth.close()
            return 0

        main_window = MainWindow(user=user, auth_service=auth)
        main_window.show()
        app.exec()
        if main_window.was_signed_out:
            continue
        break
    auth.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

