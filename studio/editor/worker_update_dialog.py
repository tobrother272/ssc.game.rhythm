"""Dialog that downloads rhythm_worker.exe from the update server."""
from __future__ import annotations

import sys
from pathlib import Path

import requests
from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

_WORKER_URL = "https://toolmgt.mksoft.io/simple.rhythm/rhythm_worker.exe"
_WORKER_NAME = "rhythm_worker.exe"
_CHUNK = 65_536  # 64 KB per read


def _worker_dest() -> Path:
    """Return the path where rhythm_worker.exe should be saved.

    - Frozen build : next to SSCStudio.exe (same folder as sys.executable)
    - Dev mode     : <repo>/dist/rhythm_worker/rhythm_worker.exe  (mirroring
                     where PyInstaller puts it so the dev build layout stays
                     consistent with prod)
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / _WORKER_NAME
    # Development: project root / dist / rhythm_worker / rhythm_worker.exe
    repo_root = Path(__file__).resolve().parent.parent.parent
    dest_dir = repo_root / "dist" / "rhythm_worker"
    dest_dir.mkdir(parents=True, exist_ok=True)
    return dest_dir / _WORKER_NAME


# ---------------------------------------------------------------------------
# Background download worker (runs in a QThread)
# ---------------------------------------------------------------------------
class _DownloadWorker(QObject):
    progress = Signal(int, int, str)   # downloaded_bytes, total_bytes, message
    finished = Signal(str)             # dest path on success
    failed = Signal(str)               # error message

    def __init__(self, url: str, dest: Path) -> None:
        super().__init__()
        self._url = url
        self._dest = dest
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        tmp = self._dest.with_suffix(".part")
        try:
            self.progress.emit(0, 0, f"Connecting to server…")
            resp = requests.get(self._url, stream=True, timeout=30)
            resp.raise_for_status()

            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            self.progress.emit(0, total, "Download started…")

            with open(tmp, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=_CHUNK):
                    if self._cancelled:
                        fh.close()
                        tmp.unlink(missing_ok=True)
                        self.failed.emit("Download cancelled.")
                        return
                    if chunk:
                        fh.write(chunk)
                        downloaded += len(chunk)
                        pct = int(downloaded * 100 / total) if total else 0
                        mb_done = downloaded / 1_048_576
                        mb_total = total / 1_048_576 if total else 0
                        msg = (
                            f"Downloading… {mb_done:.1f} MB"
                            + (f" / {mb_total:.1f} MB ({pct}%)" if total else "")
                        )
                        self.progress.emit(downloaded, total, msg)

            # Atomic replace
            self.progress.emit(downloaded, total, "Saving file…")
            if self._dest.exists():
                self._dest.unlink()
            tmp.rename(self._dest)
            self.finished.emit(str(self._dest))

        except Exception as exc:  # noqa: BLE001
            tmp.unlink(missing_ok=True)
            self.failed.emit(str(exc))


# ---------------------------------------------------------------------------
# Dialog
# ---------------------------------------------------------------------------
class WorkerUpdateDialog(QDialog):
    """Download rhythm_worker.exe with a progress bar.

    Usage::
        dlg = WorkerUpdateDialog(parent=self)
        dlg.exec()   # or dlg.show() for non-modal
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Update Worker")
        self.setMinimumWidth(420)
        self.setModal(True)

        self._dest = _worker_dest()
        self._thread: QThread | None = None
        self._worker: _DownloadWorker | None = None

        # ---- Layout ----
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        self._dest_label = QLabel(f"<small>Saving to: {self._dest}</small>")
        self._dest_label.setWordWrap(True)
        layout.addWidget(self._dest_label)

        self._msg_label = QLabel("Connecting…")
        self._msg_label.setWordWrap(True)
        layout.addWidget(self._msg_label)

        self._progress = QProgressBar()
        self._progress.setRange(0, 0)   # indeterminate until size known
        self._progress.setTextVisible(True)
        layout.addWidget(self._progress)

        self._buttons = QDialogButtonBox()
        self._cancel_btn = self._buttons.addButton(
            "Cancel", QDialogButtonBox.ButtonRole.RejectRole
        )
        self._cancel_btn.clicked.connect(self._on_cancel)
        layout.addWidget(self._buttons)

        # Start immediately — no extra "Download" click needed.
        self._start_download()

    # ------------------------------------------------------------------
    def _start_download(self) -> None:
        worker = _DownloadWorker(url=_WORKER_URL, dest=self._dest)
        thread = QThread(self)
        worker.moveToThread(thread)

        worker.progress.connect(self._on_progress)
        worker.finished.connect(self._on_finished)
        worker.failed.connect(self._on_failed)
        thread.started.connect(worker.run)

        self._worker = worker
        self._thread = thread
        thread.start()

    def _on_cancel(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
        self.reject()

    def _on_progress(self, done: int, total: int, msg: str) -> None:
        if total > 0:
            self._progress.setRange(0, 100)
            self._progress.setValue(int(done * 100 / total))
        else:
            self._progress.setRange(0, 0)  # indeterminate
        self._set_msg(msg)

    def _on_finished(self, dest: str) -> None:
        self._cleanup_thread()
        self._progress.setRange(0, 100)
        self._progress.setValue(100)
        self._set_msg(f"✓ Download complete!\nSaved to: {dest}")
        self._cancel_btn.setText("Close")

    def _on_failed(self, error: str) -> None:
        self._cleanup_thread()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._set_msg(f"✗ Error: {error}")
        self._cancel_btn.setText("Close")

    def _set_msg(self, text: str) -> None:
        self._msg_label.setText(text)

    def _cleanup_thread(self) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(3000)
            self._thread = None
        self._worker = None

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self._worker is not None:
            self._worker.cancel()
        self._cleanup_thread()
        super().closeEvent(event)
