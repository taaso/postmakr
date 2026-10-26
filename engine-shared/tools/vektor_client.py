"""Zugang zu den Bedeutungs-Vektoren fuer App und MCP.

Standard: die Modelle laufen im eigenen Prozess auf diesem Rechner, wenn sie
installiert sind (requirements-extras.txt). Wer die Modelle in einem eigenen
Dienst halten will, setzt ``VEKTOR_URL`` auf einen Dienst mit derselben
Schnittstelle (POST /einbetten, POST /hoeren); mitgeliefert wird keiner. Fehlt beides (oder ist ``VEKTOR_AUS`` gesetzt, etwa in
Tests), wirft jede Funktion ``VektorFehlt`` und die Aufrufer fallen auf die
reine Stichwortsuche zurueck.

``hoeren`` (Hoercheck fuer fertige Reels) rechnet ebenfalls im eigenen Prozess, mit VEKTOR_AUS nur den Teil ohne Modelle
(Soll-Liste per Korrelation, Lautheit per ffmpeg).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np


class VektorFehlt(RuntimeError):
    pass


def _url() -> str:
    return os.environ.get("VEKTOR_URL", "").rstrip("/")


def _anfrage(art: str, *, texte=None, pfade=None, frage=False, timeout=300) -> np.ndarray:
    if os.environ.get("VEKTOR_AUS"):
        raise VektorFehlt("Vektoren abgeschaltet (VEKTOR_AUS)")
    url = _url()
    if url:
        daten = json.dumps({"art": art, "texte": texte, "pfade": [str(p) for p in pfade or []], "frage": frage}).encode()
        req = urllib.request.Request(url + "/einbetten", data=daten, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as antwort:
                return np.asarray(json.loads(antwort.read())["vektoren"], dtype=np.float32)
        except Exception as exc:  # noqa: BLE001 - Dienst weg heisst: ohne Vektoren weitersuchen
            raise VektorFehlt(f"Vektor-Dienst nicht erreichbar: {exc}") from exc
    try:
        import vektor  # noqa: PLC0415
    except ImportError as exc:
        raise VektorFehlt("Weder VEKTOR_URL noch lokale Modelle vorhanden") from exc
    try:
        if art == "text":
            return vektor.text(texte, frage=frage)
        if art in ("bild", "klang"):
            return vektor.ARTEN[art](pfade)
        return vektor.ARTEN[art](texte)
    except ImportError as exc:
        raise VektorFehlt(f"Modell-Bibliothek fehlt: {exc}") from exc


def verfuegbar() -> bool:
    if os.environ.get("VEKTOR_AUS"):
        return False
    if _url():
        return True
    try:
        import sentence_transformers  # noqa: F401, PLC0415
        import transformers  # noqa: F401, PLC0415
        return True
    except ImportError:
        return False


def text(texte: list[str], frage: bool = False) -> np.ndarray:
    return _anfrage("text", texte=texte, frage=frage)


def bild(pfade: list[str | Path]) -> np.ndarray:
    return _anfrage("bild", pfade=pfade)


def bild_text(texte: list[str]) -> np.ndarray:
    return _anfrage("bild_text", texte=texte)


def klang(pfade: list[str | Path]) -> np.ndarray:
    return _anfrage("klang", pfade=pfade)


def klang_text(texte: list[str]) -> np.ndarray:
    return _anfrage("klang_text", texte=texte)


def hoeren(pfad: str | Path, soll: list[dict] | None = None, woerter: list[dict] | None = None,
           timeout: int = 900) -> dict:
    """Hoercheck eines fertigen Schnitts (siehe hoercheck.hoeren). Wirft hoercheck.HoerFehler."""
    import hoercheck  # noqa: PLC0415
    if os.environ.get("VEKTOR_AUS"):
        return hoercheck.hoeren(pfad, soll, woerter, modelle=False)
    url = _url()
    if not url:
        return hoercheck.hoeren(pfad, soll, woerter)
    daten = json.dumps({"pfad": str(pfad), "soll": soll or [], "woerter": woerter}).encode()
    req = urllib.request.Request(url + "/hoeren", data=daten, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as antwort:
            return json.loads(antwort.read())
    except urllib.error.HTTPError as exc:
        try:
            grund = json.loads(exc.read()).get("fehler") or str(exc)
        except Exception:  # noqa: BLE001
            grund = str(exc)
        raise hoercheck.HoerFehler(grund) from exc
    except Exception as exc:  # noqa: BLE001 - Dienst weg: ohne Modelle weiterhoeren
        erg = hoercheck.hoeren(pfad, soll, woerter, modelle=False)
        erg["luecken"].insert(0, f"alles, was Modelle braucht, weil der Hör-Dienst nicht erreichbar war ({str(exc)[:80]})")
        return erg
