"""Welche optionalen Modell-Pakete sind da, und was sagen wir, wenn eins fehlt?

Die Bedeutungssuche (find_sounds, suggest_sounds, find_footage) und der
Hörcheck rechnen lokal im eigenen Prozess über
``engine-shared/tools/vektor_client.py``. Fehlt ein Paket, laufen die Tools
trotzdem: die Suche fällt auf Stichworte zurück, der Hörcheck misst nur, was
ohne Modelle geht. Dieses Modul liefert dazu einen freundlichen Hinweis.

Geprüft wird nur, ob ein Paket installiert ist (``importlib.util.find_spec``),
es wird nichts geladen. So bleibt der Start schnell.
"""

from __future__ import annotations

import importlib.util
import os

# Import-Name -> Paketname für pip
SUCHE = {"torch": "torch", "sentence_transformers": "sentence-transformers", "transformers": "transformers"}
HOEREN = {"earshot": "earshot", "faster_whisper": "faster-whisper"}

INSTALL = "./postmakr installieren --extras"


def _da(modul: str) -> bool:
    try:
        return importlib.util.find_spec(modul) is not None
    except (ImportError, ValueError):
        return False


def fehlend(pakete: dict[str, str]) -> list[str]:
    """pip-Namen der Pakete, die nicht installiert sind."""
    return [pip for modul, pip in pakete.items() if not _da(modul)]


def _aus() -> bool:
    return bool(os.environ.get("VEKTOR_AUS"))


def hinweis_suche() -> str:
    """Leer, wenn die Bedeutungssuche lokal rechnen kann, sonst ein Satz dazu."""
    if os.environ.get("VEKTOR_URL", "").strip():
        return ""
    if _aus():
        return ("Hinweis: Die Bedeutungssuche ist abgeschaltet (VEKTOR_AUS ist gesetzt). "
                "Ich suche deshalb nur nach Stichworten.")
    weg = fehlend(SUCHE)
    if not weg:
        return ""
    return (f"Hinweis: Für die Suche nach Bedeutung und Klang fehlt {', '.join(weg)}. "
            f"Ich suche deshalb nur nach Stichworten. Installiere die Pakete mit `{INSTALL}`; "
            "beim ersten Aufruf lädt das Modell einmalig aus dem Netz.")


def mit_rueckfall(fn, *args, **kwargs):
    """Ruft fn auf. Scheitert ein installiertes Modell (etwa beim ersten Download
    ohne Netz), läuft fn noch einmal ohne Modelle, also mit reiner Stichwortsuche.
    Liefert (Ergebnis, Hinweis); der Hinweis ist leer, wenn alles lief."""
    try:
        return fn(*args, **kwargs), ""
    except Exception as exc:  # noqa: BLE001 - lieber Stichworte als ein Absturz
        if os.environ.get("VEKTOR_AUS"):
            raise
        os.environ["VEKTOR_AUS"] = "1"
        try:
            ergebnis = fn(*args, **kwargs)
        finally:
            os.environ.pop("VEKTOR_AUS", None)
        return ergebnis, (f"Hinweis: Das Suchmodell ließ sich nicht laden ({str(exc)[:160]}). "
                          "Ich habe deshalb nur nach Stichworten gesucht. Beim ersten Mal lädt das Modell "
                          "aus dem Netz; prüf die Verbindung und versuch es noch einmal.")


def vorladen_im_hintergrund() -> bool:
    """Lädt die Suchmodelle beim Start in einem Hintergrund-Thread, damit der erste
    find_sounds- oder find_footage-Aufruf nicht minutenlang wartet und in ein
    Client-Timeout läuft. Vorgeladen wird nur, was schon im Cache liegt: Der
    Start lädt nie etwas aus dem Netz. Die Downloads übernimmt
    ``./postmakr installieren --extras`` oder der erste Aufruf.
    Abschalten mit POSTMAKR_VORLADEN=0. Liefert, ob gestartet."""
    if os.environ.get("POSTMAKR_VORLADEN", "1").strip().lower() in ("0", "nein", "aus", "false"):
        return False
    if _aus() or os.environ.get("VEKTOR_URL", "").strip() or fehlend(SUCHE):
        return False
    try:
        import vektor  # noqa: PLC0415  (engine-shared/tools liegt nach dem Tool-Import im Pfad)
        arten = [art for art in ("text", "bild", "klang") if vektor.im_cache(art)]
    except Exception:  # noqa: BLE001 - scheitert es hier, meldet es der erste Aufruf
        return False
    if not arten:
        return False
    import threading  # noqa: PLC0415

    def laden() -> None:
        try:
            for art in arten:
                vektor._laden(art)
        except Exception:  # noqa: BLE001 - scheitert es hier, meldet es der erste Aufruf
            pass

    threading.Thread(target=laden, name="modelle-vorladen", daemon=True).start()
    return True


def hinweis_hoeren() -> str:
    """Leer, wenn der Hörcheck alles messen kann, sonst ein Satz dazu."""
    if os.environ.get("VEKTOR_URL", "").strip() or _aus():
        return ""
    weg = fehlend(HOEREN)
    if not weg:
        return ""
    return (f"Hinweis: Für den vollständigen Hörcheck fehlt {', '.join(weg)}. Gemessen habe ich, was ohne "
            f"Modelle geht (Soll-Liste und Lautheit). Installiere die Pakete mit `{INSTALL}`.")
