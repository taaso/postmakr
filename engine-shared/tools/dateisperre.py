"""Dateisperren, die unter macOS, Linux und Windows gleich funktionieren.

Unter macOS und Linux nutzt das Modul flock, unter Windows msvcrt.locking auf
dem ersten Byte der Sperrdatei. Windows kennt keine geteilten Sperren, dort ist
jede Sperre exklusiv. Dashboard, Render-Runner und Renderer laden das Modul
ueber engine-shared/tools im Suchpfad.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path

WINDOWS = os.name == "nt"
if WINDOWS:
    import msvcrt
else:
    import fcntl

WARTE_SCHRITT_S = 0.05


def sperren(datei, *, geteilt: bool = False, warten: bool = True) -> None:
    """Sperrt eine geoeffnete Datei. Ohne ``warten`` wirft die Funktion
    BlockingIOError, wenn ein anderer die Sperre haelt."""
    if WINDOWS:
        while True:
            datei.seek(0)
            try:
                msvcrt.locking(datei.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError as exc:
                if not warten:
                    raise BlockingIOError(str(exc)) from exc
                time.sleep(WARTE_SCHRITT_S)
    art = fcntl.LOCK_SH if geteilt else fcntl.LOCK_EX
    fcntl.flock(datei.fileno(), art if warten else art | fcntl.LOCK_NB)


def freigeben(datei) -> None:
    """Gibt die Sperre frei. Das Schliessen der Datei tut das ebenfalls."""
    if WINDOWS:
        datei.seek(0)
        try:
            msvcrt.locking(datei.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    fcntl.flock(datei.fileno(), fcntl.LOCK_UN)


def schliessen(datei) -> None:
    """Gibt die Sperre frei und schliesst die Datei. Unter Windows faellt eine
    Sperre beim blossen Schliessen erst mit Verzoegerung weg."""
    try:
        freigeben(datei)
    except OSError:
        pass
    datei.close()


@contextmanager
def sperrdatei(pfad: Path):
    """Oeffnet ``pfad`` als Sperrdatei und gibt eine gehaltene Sperre am Ende frei."""
    datei = open(pfad, "a+", encoding="utf-8")
    try:
        yield datei
    finally:
        schliessen(datei)


@contextmanager
def gesperrt(pfad: Path, *, geteilt: bool = False, warten: bool = True):
    """Oeffnet ``pfad`` als Sperrdatei und haelt die Sperre fuer den Block."""
    Path(pfad).parent.mkdir(parents=True, exist_ok=True)
    with sperrdatei(pfad) as datei:
        sperren(datei, geteilt=geteilt, warten=warten)
        yield datei
