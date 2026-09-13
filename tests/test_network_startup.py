"""Exercise TLS initialization in a fresh process, before any Qt sockets exist."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="Windows TLS startup regression")
def test_startup_with_https_clipboard_uses_native_tls(tmp_path):
    # The offscreen child's clipboard and settings are isolated from the user.
    # A loopback server closes the TLS handshake so no external service is used.
    script = textwrap.dedent("""
        import hashlib
        import sys
        from pathlib import Path
        from PySide6.QtCore import QMimeData, QTimer, QUrl
        from PySide6.QtNetwork import QHostAddress, QSslSocket, QTcpServer
        from PySide6.QtWidgets import QApplication

        app = QApplication([])
        if 'schannel' not in QSslSocket.availableBackends():
            sys.exit(77)
        from comic_editor.core import settings
        settings.settings_path = lambda: Path(sys.argv[1])
        server = QTcpServer()
        assert server.listen(QHostAddress.LocalHost, 0)
        accepted = []
        def reject_connection():
            socket = server.nextPendingConnection()
            accepted.append(socket)
            socket.disconnectFromHost()
        server.newConnection.connect(reject_connection)
        mime = QMimeData()
        mime.setUrls([QUrl(f'https://127.0.0.1:{server.serverPort()}/image.png')])
        app.clipboard().setMimeData(mime)

        from comic_editor.ui.main_window import MainWindow
        window = MainWindow()
        assert QSslSocket.activeBackend() == 'schannel'
        assert window._external_clipboard_reader.pending
        window.show()
        visible_at_completion = []
        def finish():
            visible_at_completion.append(window.isVisible())
            app.quit()
        QTimer.singleShot(1500, finish)
        app.exec()
        assert visible_at_completion == [True]
        assert accepted, 'The clipboard request must actually initialize TLS'
        assert not window._external_clipboard_reader.pending
        assert not window._clipboard_image_sources()
        window.hide()
        app.clipboard().clear()
        print('Startup and HTTPS failure handling survived')
    """)
    result = subprocess.run(
        [sys.executable, "-X", "faulthandler", "-c", script, str(tmp_path / "settings.json")],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode == 77:
        pytest.skip("Qt does not ship the Schannel backend")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Startup and HTTPS failure handling survived" in result.stdout
    assert "Cannot set backend" not in result.stderr
