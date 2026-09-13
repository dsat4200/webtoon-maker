"""Network managers with the native Windows TLS backend selected early."""
from __future__ import annotations

import sys

from PySide6.QtNetwork import QNetworkAccessManager, QSslSocket


def create_network_manager(parent=None) -> QNetworkAccessManager:
    # Qt's OpenSSL plugin can load incompatible DLLs from Python or PATH and
    # crash the process as soon as a clipboard/drag URL starts downloading.
    # Select Windows' certificate-verified TLS backend before creating sockets.
    if (sys.platform == "win32"
            and "schannel" in QSslSocket.availableBackends()
            and QSslSocket.activeBackend() != "schannel"):
        QSslSocket.setActiveBackend("schannel")
    return QNetworkAccessManager(parent)
