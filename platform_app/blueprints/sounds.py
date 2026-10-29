"""Sound-Bibliothek (Modul ``sounds``).

In der Oberflaeche ist es EINE Bibliothek. Technisch kommen die Sounds aus
zwei Ordnern, damit die Nummern stabil bleiben:

- ``WORKSPACES_DIR/.platform/sounds/`` (Nr. 1 bis 999), fuer alle Workspaces.
  Wird nicht mitgeliefert: fehlt der Ordner, startet die Bibliothek leer.
- ``sounds/`` im Workspace (Nr. ab 1001), hochgeladen oder von der KI abgelegt.

Je Ordner fuehrt ``sounds.yaml`` die Sounds mit fester Nummer, Name, Art,
Thema, Stimmung, "passt zu", Schlagworten und gemessenem Klang; der Such-Index
liegt daneben unter ``.index/`` (siehe engine-shared/tools/sinnsuche.py). Im
Schnitt wird ein Sound ueber seine Nummer angesprochen.
"""
from __future__ import annotations

import os
import secrets
import subprocess
import sys
import threading
from pathlib import Path

import yaml
from flask import abort, g, jsonify, render_template, request, send_file

from .. import config
from .workspace import REPO_ROOT, bp

_WERKZEUGE = str(REPO_ROOT / "engine-shared" / "tools")
if _WERKZEUGE not in sys.path:
    sys.path.insert(0, _WERKZEUGE)
import sinnsuche  # noqa: E402
from dateisperre import gesperrt  # noqa: E402

SOUND_ENDUNGEN = {".mp3", ".wav", ".m4a", ".ogg"}
EIGENE_AB = 1001
MAX_BYTES = 25 * 1024 * 1024
MAX_SEKUNDEN = 120
# Reihenfolge der Themen (Reiter), unbekannte kommen dahinter.
THEMEN = ("Technik & Handy", "Übergänge & Bewegung", "Spannung & Drama", "Wucht & Treffer", "Geld & Erfolg",
          "Comedy & Fail", "Magie & Glanz", "Alltag & Natur", "Menschen & Publikum", "Gaming & Anime",
          "Internet-Memes", "Musik")
OHNE_THEMA = "Ohne Thema"
# ffmpeg darf fremde Uploads nur als lokale Datei lesen, nie Netz- oder
# Playlist-Verweise darin aufloesen.
NUR_DATEIEN = ("-protocol_whitelist", "file")
_index_sperre = threading.Lock()


def _modul_pruefen() -> None:
    g.ws.require_module("sounds")


def postmakr_ordner() -> Path:
    return config.WORKSPACES_DIR / ".platform" / "sounds"


def _eigene_ordner() -> Path:
    return g.ws.root / "sounds"


def _yaml_liste(pfad: Path) -> list:
    try:
        daten = yaml.safe_load(pfad.read_text(encoding="utf-8")) or []
    except (OSError, yaml.YAMLError):
        return []
    return daten if isinstance(daten, list) else []


def _katalog_sperre(ordner: Path):
    """Eine Sperre je Sound-Ordner fuer alle Aenderungen an sounds.yaml."""
    return gesperrt(ordner / ".sounds.lock")


def _katalog_schreiben(ordner: Path, liste: list) -> None:
    """Schreibt sounds.yaml atomar: erst eine Nebendatei, dann umbenennen."""
    ziel = ordner / "sounds.yaml"
    tmp = ordner / f".sounds.{secrets.token_hex(4)}.yaml"
    try:
        tmp.write_text(yaml.safe_dump(liste, allow_unicode=True, sort_keys=False), encoding="utf-8")
        os.replace(tmp, ziel)
    finally:
        tmp.unlink(missing_ok=True)


def _bestand(ordner: Path, quelle: str) -> list[dict]:
    sounds = []
    for e in _yaml_liste(ordner / "sounds.yaml"):
        if not isinstance(e, dict):
            continue
        datei = str(e.get("datei") or "")
        pfad = ordner / datei
        if not datei or "/" in datei or datei.startswith(".") or pfad.suffix.lower() not in SOUND_ENDUNGEN or not pfad.is_file():
            continue
        worte = e.get("schlagworte") or []
        sounds.append({
            "nr": int(e.get("nr") or 0),
            "name": str(e.get("name") or pfad.stem),
            "kategorie": str(e.get("kategorie") or "Geräusche"),
            "beschreibung": str(e.get("beschreibung") or ""),
            "thema": str(e.get("thema") or OHNE_THEMA),
            "stimmung": str(e.get("stimmung") or ""),
            "passt_zu": str(e.get("passt_zu") or ""),
            "schlagworte": [str(w) for w in worte] if isinstance(worte, list) else [],
            "klang": str(e.get("klang") or ""),
            "dauer": float(e.get("dauer") or 0),
            "datei": datei,
            "quelle": quelle,
            "stand": int(pfad.stat().st_mtime),
        })
    return sorted(sounds, key=lambda s: s["nr"])


def alle_sounds() -> list[dict]:
    """Alle Sounds beider Ordner, nach Nummer."""
    return sorted(_bestand(postmakr_ordner(), "postmakr") + _bestand(_eigene_ordner(), "eigen"),
                  key=lambda s: s["nr"])


def _ordner_von(sound: dict) -> Path:
    return postmakr_ordner() if sound["quelle"] == "postmakr" else _eigene_ordner()


def _bestaende(liste: list[dict]) -> list[tuple[Path, list[dict]]]:
    return [(postmakr_ordner(), [s for s in liste if s["quelle"] == "postmakr"]),
            (_eigene_ordner(), [s for s in liste if s["quelle"] == "eigen"])]


@bp.route("/sounds")
def sounds():
    _modul_pruefen()
    alle = liste = alle_sounds()
    vorhanden = {s["thema"] for s in liste}
    themen = [t for t in THEMEN if t in vorhanden] + sorted(vorhanden - set(THEMEN))
    tab = request.args.get("thema") or "alle"
    if tab != "alle" and tab not in themen:
        tab = "alle"
    frage = (request.args.get("q") or "").strip()[:120]
    aehnlich = request.args.get("aehnlich", type=int)
    basis = next((s for s in alle if s["nr"] == aehnlich), None) if aehnlich else None
    gezeigt = liste if tab == "alle" else [s for s in liste if s["thema"] == tab]
    gruende = {}
    if basis:
        frage = ""
        treffer = sinnsuche.sounds_suchen(_bestaende(alle), aehnlich_wie=basis["nr"], limit=15)
        gezeigt = [t["sound"] for t in treffer]
    elif frage:
        treffer = sinnsuche.sounds_suchen(_bestaende(gezeigt), frage=frage)
        gezeigt = [t["sound"] for t in treffer]
        gruende = {t["sound"]["nr"]: t["grund"] for t in treffer}
    gruppen = []
    if not frage and not basis:
        for t in themen:
            teil = [s for s in gezeigt if s["thema"] == t]
            if teil:
                gruppen.append((t, teil))
    return render_template(
        "sounds.html",
        sounds=gezeigt,
        gruppen=gruppen,
        themen=themen,
        anzahl={t: sum(1 for s in liste if s["thema"] == t) for t in themen} | {"alle": len(liste)},
        tab=tab,
        frage=frage,
        basis=basis,
        gruende=gruende,
        alle_themen=list(THEMEN),
        title="Sounds",
    )


def _datei_senden(ordner: Path, name: str):
    if "/" in name or name.startswith(".") or Path(name).suffix.lower() not in SOUND_ENDUNGEN:
        abort(404)
    ziel = ordner / name
    if not ziel.is_file():
        abort(404)
    antwort = send_file(ziel, conditional=True, max_age=31536000)
    if request.args.get("stand"):
        antwort.headers["Cache-Control"] = "private, max-age=31536000, immutable"
    return antwort


@bp.route("/sounds/datei/<int:nr>")
def sound_datei(nr):
    _modul_pruefen()
    treffer = next((s for s in alle_sounds() if s["nr"] == nr), None)
    if treffer is None:
        abort(404)
    return _datei_senden(_ordner_von(treffer), treffer["datei"])


def _index_aktualisieren(ordner: Path) -> None:
    """Such-Index eines Ordners im Hintergrund nachziehen (Klang und Bedeutung)."""

    def lauf():
        with _index_sperre:
            try:
                liste = [e for e in _yaml_liste(ordner / "sounds.yaml") if isinstance(e, dict) and (ordner / str(e.get("datei", ""))).is_file()]
                sinnsuche.sounds_indexieren(ordner, liste, ausgabe=lambda *_: None)
            except (sinnsuche.VektorFehlt, OSError, subprocess.CalledProcessError):
                pass  # ohne Modelle bleibt die Stichwortsuche

    threading.Thread(target=lauf, daemon=True).start()


@bp.route("/sounds/hochladen", methods=["POST"])
def sound_hochladen():
    _modul_pruefen()
    d = request.files.get("datei")
    if not d or not d.filename:
        return jsonify({"ok": False, "error": "Bitte eine Sound-Datei wählen."}), 400
    if Path(d.filename).suffix.lower() not in SOUND_ENDUNGEN:
        return jsonify({"ok": False, "error": "Erlaubt sind MP3, WAV, M4A und OGG."}), 400
    name = (request.form.get("name") or Path(d.filename).stem).strip()[:80]
    thema = (request.form.get("thema") or "").strip()
    thema = thema if thema in THEMEN else OHNE_THEMA
    ordner = _eigene_ordner()
    ordner.mkdir(exist_ok=True)
    marke = secrets.token_hex(4)
    tmp = ordner / f".upload.{marke}{Path(d.filename).suffix.lower()}"
    fertig = ordner / f".upload.{marke}.mp3"
    try:
        geschrieben = 0
        with open(tmp, "wb") as fh:
            while stueck := d.stream.read(1024 * 1024):
                geschrieben += len(stueck)
                if geschrieben > MAX_BYTES:
                    raise ValueError("Die Datei ist größer als 25 MB.")
                fh.write(stueck)
        # Einheitlich als MP3 ablegen, hoechstens zwei Minuten. Die Umwandlung
        # laeuft ausserhalb der Sperre, damit parallele Uploads nicht warten.
        subprocess.run(["ffmpeg", "-v", "error", "-y", *NUR_DATEIEN, "-i", str(tmp), "-t", str(MAX_SEKUNDEN),
                        "-vn", "-ac", "2", "-ar", "44100", "-b:a", "192k", str(fertig)],
                       check=True, capture_output=True, timeout=120)
        dauer = float(subprocess.run(["ffprobe", "-v", "error", *NUR_DATEIEN, "-show_entries", "format=duration",
                                      "-of", "csv=p=0", str(fertig)],
                                     capture_output=True, text=True, timeout=30).stdout.strip() or 0)
    except ValueError as exc:
        fertig.unlink(missing_ok=True)
        return jsonify({"ok": False, "error": str(exc)}), 413
    except (OSError, subprocess.SubprocessError):
        fertig.unlink(missing_ok=True)
        return jsonify({"ok": False, "error": "Die Datei ließ sich nicht als Sound lesen."}), 400
    finally:
        tmp.unlink(missing_ok=True)
    eintrag = {"name": name, "kategorie": "Eigene", "thema": thema,
               "stimmung": (request.form.get("stimmung") or "").strip()[:60],
               "passt_zu": (request.form.get("passt_zu") or "").strip()[:160],
               "schlagworte": [w.strip() for w in (request.form.get("schlagworte") or "").split(",") if w.strip()][:12],
               "beschreibung": "Eigener Upload.", "dauer": round(dauer, 2)}
    # Nummer vergeben und Katalog schreiben in einem Zug, mit frisch gelesenem
    # Stand: Sonst ueberschreibt ein paralleler Upload den Eintrag des anderen.
    try:
        with _katalog_sperre(ordner):
            liste = _yaml_liste(ordner / "sounds.yaml")
            nr = max([EIGENE_AB - 1] + [int(e.get("nr") or 0) for e in liste if isinstance(e, dict)]) + 1
            ziel = ordner / f"{nr}_{secrets.token_hex(3)}.mp3"
            os.replace(fertig, ziel)
            _katalog_schreiben(ordner, liste + [{"nr": nr, "datei": ziel.name, **eintrag}])
    except OSError:
        fertig.unlink(missing_ok=True)
        return jsonify({"ok": False, "error": "Der Sound ließ sich nicht speichern."}), 500
    _index_aktualisieren(ordner)
    return jsonify({"ok": True, "nr": nr})


@bp.route("/sounds/<int:nr>/loeschen", methods=["POST"])
def sound_loeschen(nr):
    _modul_pruefen()
    sound = next((s for s in alle_sounds() if s["nr"] == nr), None)
    if sound is None:
        abort(404)
    ordner = _ordner_von(sound)
    with _katalog_sperre(ordner):
        liste = _yaml_liste(ordner / "sounds.yaml")
        treffer = next((e for e in liste if isinstance(e, dict) and int(e.get("nr") or 0) == nr), None)
        if treffer is None:
            abort(404)
        datei = str(treffer.get("datei") or "")
        if datei and "/" not in datei and not datei.startswith("."):
            (ordner / datei).unlink(missing_ok=True)
        _katalog_schreiben(ordner, [e for e in liste if e is not treffer])
    _index_aktualisieren(ordner)
    return jsonify({"ok": True})
