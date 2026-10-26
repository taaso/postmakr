"""Hörcheck-Tool: hört ein fertig gemischtes Reel an und meldet Soundeffekte, die zu spät
kommen, zu leise sind oder mit der Stimme kollidieren.

Die Messung steckt in ``engine-shared/tools/hoercheck.py`` und rechnet im eigenen Prozess
(``vektor_client.hoeren``). Ohne earshot und faster-whisper misst sie, was ohne Modelle
geht, und sagt im Bericht, was nicht geprüft wurde.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from .. import lokal, modelle
from ..workspace import Workspace, WorkspaceError
from .sounds import _bestaende

REPO_ROOT = Path(__file__).resolve().parents[2]
_WERKZEUGE = str(REPO_ROOT / "engine-shared" / "tools")
if _WERKZEUGE not in sys.path:
    sys.path.insert(0, _WERKZEUGE)
import hoercheck  # noqa: E402
import vektor_client  # noqa: E402

ENDUNGEN = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac"}
MAX_SOUNDS = 80


def _schluessel(text: str) -> str:
    """Vergleichsform für Namen: klein, ohne Nummern-Vorsatz und Endung"""
    t = Path(str(text)).stem.lower().strip()
    return re.sub(r"[^a-z0-9äöüß]+", "", re.sub(r"^\d+[_\- ]+", "", t))


def _sound_finden(eintrag: dict, ws: Workspace, bestaende) -> tuple[Path | None, dict]:
    """Soll-Eintrag -> (Datei, Angaben für den Bericht). nr, name oder datei (relativ zum Workspace)."""
    nr, name, datei = eintrag.get("nr"), eintrag.get("name"), eintrag.get("datei")
    try:
        nr = int(nr) if nr not in (None, "") else None
    except (TypeError, ValueError):
        return None, {"name": str(nr)}
    if datei:
        pfad = ws.safe_path(str(datei))
        return (pfad if pfad.is_file() else None), {"name": name or pfad.stem}
    for ordner, liste in bestaende:
        for e in liste:
            treffer = (nr and str(e.get("nr")) == str(nr)) or (
                not nr and name and _schluessel(name) in (_schluessel(e.get("name", "")), _schluessel(e.get("datei", ""))))
            if treffer:
                return ordner / str(e["datei"]), {"nr": int(e["nr"]), "name": e.get("name") or name}
    return None, {"nr": nr, "name": name}


def register(mcp, fest: Workspace | None = None) -> None:

    @mcp.tool
    def check_reel_sound(datei: str, sounds: list[dict] | str | None = None, workspace: str = "") -> str:
        """Hörcheck für ein fertig gemischtes Reel (Video oder Audio im Workspace). Ruf ihn
        nach dem Mischen und bevor du den Beitrag zur Freigabe ablegst, und nach jeder
        Korrektur noch einmal.
        Du kannst den Ton nicht selbst hören, dieses Tool hört für dich.

        Meldet, wo ein Soundeffekt zu spät oder zu früh kommt, unter der Stimme verschwindet,
        mitten in ein gesprochenes Wort knallt oder vom Reelende abgeschnitten wird, dazu
        Bildschnitte ohne Ton-Akzent und die Gesamtlautheit (Ziel etwa -14 LUFS, True Peak
        unter -1 dBTP). Jeder Befund kommt mit konkreter Korrektur.

        Am genauesten mit Soll-Liste: gib alle Sounds an, die du in den Schnitt gelegt hast,
        mit der Sekunde, an der die Sound-Datei beginnt. Dann wird jeder Sound im Mix gesucht
        und auf die Millisekunde vermessen. Ohne Liste prüft das Tool nur allgemein
        (Effekt-Erkennung per Klang-Etikett, ungenauer).

        Args:
            datei: Pfad relativ zum Workspace, z. B. "drafts/2026-10-26_reel/reel_final.mp4".
            sounds: Soll-Liste, je Eintrag "zeit" (Sekunde, an der die Datei beginnt) und
                "nr" (Sound-Nummer aus find_sounds) oder "name" oder "datei" (relativ zum
                Workspace). Optional "spitze_db": geplanter Spitzenpegel im Mix (dBFS).
                Beispiel: [{"nr": 1042, "zeit": 3.2}, {"name": "Trackpad-Klick", "zeit": 7.9}]
            workspace: Name des Workspace (nur nötig, wenn es mehrere gibt).
        """
        try:
            ws = lokal.resolve(workspace, fest)
            pfad = ws.safe_path(datei)
            if not pfad.is_file():
                return f"Fehler: Datei nicht gefunden: {datei}"
            if pfad.suffix.lower() not in ENDUNGEN:
                return f"Fehler: {pfad.name} ist keine Video- oder Audiodatei ({', '.join(sorted(ENDUNGEN))})."
            if isinstance(sounds, str):
                try:
                    sounds = json.loads(sounds) if sounds.strip() else []
                except json.JSONDecodeError:
                    return "Fehler: sounds ist kein gültiges JSON. Erwartet wird eine Liste wie [{\"nr\": 1042, \"zeit\": 3.2}]."
            sounds = sounds or []
            if not isinstance(sounds, list) or len(sounds) > MAX_SOUNDS:
                return f"Fehler: sounds muss eine Liste mit höchstens {MAX_SOUNDS} Einträgen sein."
            soll, fehlt = [], []
            bestaende = _bestaende(ws) if any(isinstance(e, dict) and not e.get("datei") for e in sounds) else []
            for e in sounds:
                if not isinstance(e, dict) or e.get("zeit") is None:
                    fehlt.append(f"{e!r} (ohne zeit)")
                    continue
                try:
                    zeit = float(e["zeit"])
                except (TypeError, ValueError):
                    fehlt.append(f"{e!r} (zeit ist keine Zahl)")
                    continue
                datei_sound, angaben = _sound_finden(e, ws, bestaende)
                if datei_sound is None:
                    fehlt.append(str(e.get("nr") or e.get("name") or e.get("datei")))
                    continue
                eintrag = dict(angaben, zeit=zeit, datei=str(datei_sound))
                if e.get("spitze_db") is not None:
                    eintrag["spitze_db"] = float(e["spitze_db"])
                soll.append(eintrag)
            if fehlt:
                return ("Fehler: Diese Sounds aus der Soll-Liste kenne ich nicht: " + ", ".join(fehlt)
                        + ". Nimm die Nummer aus find_sounds oder gib datei relativ zum Workspace an.")
            erg = vektor_client.hoeren(pfad, soll)
            hinweis = modelle.hinweis_hoeren()
            return (hoercheck.bericht(erg) + f"\n\nGehört in {hoercheck.zahl(erg.get('sekunden') or 0, 1)} s."
                    + (f"\n\n{hinweis}" if hinweis else ""))
        except WorkspaceError as e:
            return f"Fehler: {e}"
        except hoercheck.HoerFehler as e:
            zusatz = (" Installiere ffmpeg über die Paketverwaltung deines Systems, auf dem Mac etwa mit "
                      "`brew install ffmpeg`.") if "ffmpeg" in str(e) else ""
            return f"Fehler beim Hörcheck: {e}.{zusatz}"
        except Exception as e:  # noqa: BLE001 - kein Traceback an den Client
            return f"Der Hörcheck ist unerwartet gescheitert ({str(e)[:200]}). Versuch es noch einmal."
