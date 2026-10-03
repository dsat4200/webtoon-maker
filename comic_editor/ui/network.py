"""Network managers with the native Windows TLS backend selected early."""
from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QLibraryInfo, QPluginLoader
from PySide6.QtNetwork import QNetworkAccessManager, QSslSocket


_native_tls_loader = None


@lru_cache(maxsize=1)
def _select_native_tls() -> bool:
    # Qt's OpenSSL plugin can load incompatible DLLs from Python or PATH and
    # crash the process as soon as a clipboard/drag URL starts downloading.
    # Select Windows' certificate-verified TLS backend before creating sockets.
    global _native_tls_loader
    # Qt's general discovery instantiates every TLS plugin, including OpenSSL.
    # Load only the trusted backend bundled with this Qt runtime first. Keep
    # its loader alive; no process PATH search or certificate policy changes.
    plugin = Path(QLibraryInfo.path(QLibraryInfo.PluginsPath)) / "tls" / "qschannelbackend.dll"
    if plugin.is_file():
        loader = QPluginLoader(str(plugin))
        if loader.instance() is not None:
            _native_tls_loader = loader
            return QSslSocket.setActiveBackend("schannel")
    # Unusual Qt distributions can expose the backend statically instead.
    if "schannel" in QSslSocket.availableBackends():
        return QSslSocket.setActiveBackend("schannel")
    return False


def create_network_manager(parent=None) -> QNetworkAccessManager:
    if sys.platform == "win32":
        _select_native_tls()
    return QNetworkAccessManager(parent)
