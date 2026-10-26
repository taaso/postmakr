#!/usr/bin/env python3
"""Duenner Einstieg fuer postmakr lokal.

Normaler Start ueber den Startbefehl im Repo-Wurzelordner:
    ./postmakr start            (nur http://127.0.0.1:4711)
    ./postmakr start --handy    (zusaetzlich HTTPS im WLAN, siehe handy.py)

Direktstart zum Entwickeln:
    WORKSPACES_DIR="/pfad/zu/workspaces" DEV_MODE=1 PORT=4711 \
        python3 platform_app/app.py

Der Datenordner enthaelt pro Workspace einen Unterordner ODER Symlink. Die
Auswahl laeuft ueber http://127.0.0.1:$PORT/, ein einzelner Workspace ueber
/w/<slug>/. Keine Build-Schritte, kein npm.
"""

import os
import sys

# Direktstart `python3 platform_app/app.py`: das Repo-Wurzelverzeichnis (Elter
# des Pakets) muss auf den Importpfad, damit `platform_app` als Paket auffindbar
# ist. Bei Import als Modul (from platform_app import app) ist das schon der Fall.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import handy  # noqa: E402
from platform_app import create_app  # noqa: E402
from platform_app import config  # noqa: E402

app = create_app()
# Handy-Zugang: Zugangscode fuer Anfragen aus dem Netz und die Seite mit dem
# CA-Zertifikat. Tut nichts, solange postmakr nicht mit --handy laeuft.
handy.einbinden(app)


if __name__ == "__main__":
    print(f"postmakr lokal  ->  http://{config.HOST}:{config.PORT}/")
    print(f"Datenordner    = {config.WORKSPACES_DIR}")
    # threaded=True: der Aktivitaets-Feed haelt eine SSE-Verbindung offen
    # (/w/<slug>/aktivitaet/stream); ohne Threads wuerde das alle anderen
    # Requests blockieren.
    app.run(host=config.HOST, port=config.PORT, debug=False, threaded=True)
