"""Handy-Ansicht.

Eigene, schlanke Oberflaeche fuer das Smartphone unter /w/<slug>/m: zwei
Reiter (Uebersicht, Entwuerfe) als Raster mit zwei Beitraegen pro Reihe,
Vollbild-Ansicht zum Durchwischen wie in einer Kurzvideo-App, Speichern in
die Fotos und Status-Aktionen (freigeben + einplanen, als gepostet
markieren).

Dazu der Markensound: ein Workspace kann eine Audiodatei hinterlegen, die in
jedes stumme Video eingesetzt wird, das postmakr verlaesst (Handy-Ansicht,
Download, Render-Export). Videos mit eigenem Ton bleiben unangetastet.

Die Routen haengen am Workspace-Blueprint, damit die Slug-Aufloesung genauso
greift wie ueberall sonst.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import subprocess
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import abort, g, jsonify, render_template, request, send_file, url_for

from .. import config
from . import workspace as ws_mod
from .workspace import beitrags_caption
from .workspace import bp

VIDEO_ENDUNGEN = {".mp4", ".mov", ".webm"}
BILD_ENDUNGEN = {".png", ".jpg", ".jpeg", ".webp"}
# Hilfsdateien, die nie als Beitragsmedium gelten (QA-Bogen, Editor-Helfer).
_KEIN_MEDIUM_RE = re.compile(
    r"(?:^qa_|kontaktbogen|_(?:hintergrund|background)\.|_grafik\d+\.|_v\d+[_.]|_start\.|_mid\.)",
    re.IGNORECASE,
)
# Toleranz fuer "Rendering ist aktuell": Skripte schreiben HTML und PNG im
# selben Lauf, das PNG kann dabei ein paar Sekunden aelter wirken.
AKTUELL_TOLERANZ_S = 5

MARKENSOUND_ENDUNGEN = {".mp3", ".m4a", ".aac", ".wav"}
MARKENSOUND_MAX_BYTES = 15 * 1024 * 1024
MARKENSOUND_CACHE_MAX = 200
# Unterhalb dieser Lautstaerke (dBFS, Spitze) gilt eine Tonspur als stumm.
# Die gerenderten Slides tragen eine anullsrc-Spur (-91 dB).
STUMM_SCHWELLE_DB = -60.0
_stumm_cache: dict[tuple, bool] = {}
_mux_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Markensound: Ablage und Einstellungen
# ---------------------------------------------------------------------------
def markensound_config() -> dict:
    cfg = ws_mod.read_config()
    roh = cfg.get("markensound") if isinstance(cfg.get("markensound"), dict) else {}
    try:
        lautstaerke = float(roh.get("lautstaerke", 0.8))
    except (TypeError, ValueError):
        lautstaerke = 0.8
    return {
        "aktiv": bool(roh.get("aktiv")),
        "datei": str(roh.get("datei") or ""),
        "name": str(roh.get("name") or ""),
        # Eigener Sound fuer die animierte CTA-Slide
        "cta_datei": str(roh.get("cta_datei") or ""),
        "cta_name": str(roh.get("cta_name") or ""),
        "lautstaerke": max(0.05, min(1.0, lautstaerke)),
    }


SOUND_ARTEN = ("haupt", "cta")


def ist_cta_datei(name: str) -> bool:
    """Die animierte Abschluss-Slide eines Karussells (slide_07_cta.mp4,
    02_slide_03_cta.mp4 aus dem Export, slide_07_cta.html als Quelle)."""
    return Path(name).stem.lower().endswith("_cta")


def markensound_ordner() -> Path:
    return g.ws.root / "assets" / "sound"


def markensound_datei(art: str = "haupt") -> Path | None:
    """Die hinterlegte Sounddatei, falls vorhanden (unabhaengig von aktiv)."""
    cfg = markensound_config()
    name = cfg["cta_datei"] if art == "cta" else cfg["datei"]
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    pfad = markensound_ordner() / name
    return pfad if pfad.is_file() and not pfad.is_symlink() else None


def markensound_aktiv(art: str = "haupt") -> Path | None:
    """Pfad der Sounddatei, wenn der Markensound eingeschaltet ist."""
    return markensound_datei(art) if markensound_config()["aktiv"] else None


def sound_fuer(name: str) -> Path | None:
    """Welcher Sound unter ein Video gehoert: CTA-Slides bekommen den
    CTA-Sound, falls hinterlegt, alles andere den Markensound."""
    if ist_cta_datei(name):
        cta = markensound_aktiv("cta")
        if cta is not None:
            return cta
    return markensound_aktiv("haupt")


def _markensound_speichern(felder: dict) -> dict:
    cfg = ws_mod.read_config()
    roh = cfg.get("markensound") if isinstance(cfg.get("markensound"), dict) else {}
    roh.update(felder)
    cfg["markensound"] = roh
    ws_mod.write_config(cfg)
    return markensound_config()


def _markensound_oeffentlich() -> dict:
    cfg = markensound_config()
    datei = markensound_datei()
    cta = markensound_datei("cta")
    return {
        "aktiv": cfg["aktiv"] and (datei is not None or cta is not None),
        "vorhanden": datei is not None,
        "name": cfg["name"] or (datei.name if datei else ""),
        "lautstaerke": cfg["lautstaerke"],
        "url": url_for("workspace.markensound_anhoeren") if datei else None,
        "cta_vorhanden": cta is not None,
        "cta_name": cfg["cta_name"] or (cta.name if cta else ""),
        "cta_url": url_for("workspace.markensound_anhoeren", art="cta") if cta else None,
    }


@bp.route("/markensound")
def markensound_anhoeren():
    datei = markensound_datei("cta" if request.args.get("art") == "cta" else "haupt")
    if datei is None:
        abort(404)
    return send_file(datei, conditional=True)


@bp.route("/einstellungen/markensound", methods=["POST"])
def einstellungen_markensound():
    """Hochladen, Ein-/Ausschalten, Lautstaerke und Entfernen des
    Markensounds. Ein Formular, das Feld aktion sagt, was passieren soll."""
    aktion = (request.form.get("aktion") or "").strip()
    art = "cta" if request.form.get("art") == "cta" else "haupt"
    basis = "cta-sound" if art == "cta" else "markensound"
    feld_datei, feld_name = ("cta_datei", "cta_name") if art == "cta" else ("datei", "name")
    try:
        if aktion == "hochladen":
            datei = request.files.get("datei")
            if datei is None or not datei.filename:
                return jsonify({"ok": False, "error": "Bitte eine Audiodatei wählen."}), 400
            endung = Path(datei.filename).suffix.lower()
            if endung not in MARKENSOUND_ENDUNGEN:
                return jsonify({"ok": False, "error": "Erlaubt sind MP3, M4A, AAC und WAV."}), 400
            inhalt = datei.read(MARKENSOUND_MAX_BYTES + 1)
            if len(inhalt) > MARKENSOUND_MAX_BYTES:
                return jsonify({"ok": False, "error": "Die Datei ist größer als 15 MB."}), 413
            ordner = markensound_ordner()
            ordner.mkdir(parents=True, exist_ok=True)
            ziel = ordner / f"{basis}{endung}"
            tmp = ordner / f".{basis}{endung}.tmp"
            tmp.write_bytes(inhalt)
            if not _ist_audio(tmp):
                tmp.unlink(missing_ok=True)
                return jsonify({"ok": False, "error": "Die Datei enthält keine lesbare Tonspur."}), 400
            alt = markensound_datei(art)
            os.replace(tmp, ziel)
            if alt is not None and alt != ziel:
                alt.unlink(missing_ok=True)
            anzeigename = Path(datei.filename).name[:120]
            _markensound_speichern({feld_datei: ziel.name, feld_name: anzeigename, "aktiv": True})
        elif aktion == "aktiv":
            if request.form.get("aktiv") == "1" and markensound_datei() is None and markensound_datei("cta") is None:
                return jsonify({"ok": False, "error": "Lade zuerst einen Sound hoch."}), 400
            _markensound_speichern({"aktiv": request.form.get("aktiv") == "1"})
        elif aktion == "lautstaerke":
            try:
                wert = float(request.form.get("lautstaerke") or "")
            except ValueError:
                return jsonify({"ok": False, "error": "Ungültige Lautstärke."}), 400
            _markensound_speichern({"lautstaerke": max(0.05, min(1.0, wert))})
        elif aktion == "entfernen":
            datei = markensound_datei(art)
            if datei is not None:
                datei.unlink(missing_ok=True)
            felder = {feld_datei: "", feld_name: ""}
            andere = markensound_datei("haupt" if art == "cta" else "cta")
            if andere is None:
                felder["aktiv"] = False
            _markensound_speichern(felder)
        else:
            return jsonify({"ok": False, "error": "Unbekannte Aktion."}), 400
    except OSError as exc:
        return jsonify({"ok": False, "error": f"Schreibfehler: {exc}"}), 500
    return jsonify({"ok": True, "markensound": _markensound_oeffentlich()})


# ---------------------------------------------------------------------------
# Markensound: Einsetzen in stumme Videos
# ---------------------------------------------------------------------------
def _ffprobe(args: list[str], pfad: Path) -> str:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", *args, "-of", "default=nw=1:nk=1", str(pfad)],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _ist_audio(pfad: Path) -> bool:
    return bool(_ffprobe(["-select_streams", "a:0", "-show_entries", "stream=codec_type"], pfad))


def _dauer_s(pfad: Path) -> float:
    try:
        return float(_ffprobe(["-show_entries", "format=duration"], pfad) or 0)
    except ValueError:
        return 0.0


def video_ist_stumm(pfad: Path) -> bool:
    """True, wenn das Video keine Tonspur hat oder nur Stille traegt."""
    try:
        st = pfad.stat()
    except OSError:
        return False
    schluessel = (str(pfad), st.st_mtime_ns, st.st_size)
    if schluessel in _stumm_cache:
        return _stumm_cache[schluessel]
    if not _ist_audio(pfad):
        stumm = True
    else:
        try:
            out = subprocess.run(
                ["ffmpeg", "-hide_banner", "-nostats", "-i", str(pfad), "-vn",
                 "-af", "volumedetect", "-f", "null", "-"],
                capture_output=True, text=True, timeout=60,
            )
            m = re.search(r"max_volume:\s*(-?[\d.]+|-inf) dB", out.stderr)
            if not m or m.group(1) == "-inf":
                stumm = True
            else:
                stumm = float(m.group(1)) < STUMM_SCHWELLE_DB
        except (OSError, subprocess.SubprocessError, ValueError):
            stumm = False  # im Zweifel das Original ausliefern
    if len(_stumm_cache) > 512:
        _stumm_cache.clear()
    _stumm_cache[schluessel] = stumm
    return stumm


def _mux_cache_ordner() -> Path:
    return config.WORKSPACES_DIR / ".platform" / "markensound_cache" / g.ws.slug


# Der CTA-Sound ist so geschnitten, dass sein Anschlag hier liegt (Tippen auf
# „Folgen“ in der CTA-Slide). Fuer Reels wird er entsprechend verschoben.
CTA_SOUND_ANSCHLAG_S = 1.55


def mit_markensound(video: Path, cta_bei: float | None = None) -> Path:
    """Liefert das Video mit Markensound, wenn einer aktiv ist und das Video
    stumm ist. Sonst das Original. cta_bei (Sekunde des Tippens auf „Folgen“
    in einem Reel, aus meta.yaml cta_ton_bei) legt zusaetzlich den CTA-Sound
    an genau diese Stelle. Ergebnisse liegen in einem Cache, der Schluessel
    haengt an Video, Sounds, Lautstaerke und Zeitpunkt."""
    sound = sound_fuer(video.name)
    if sound is None or video.suffix.lower() not in VIDEO_ENDUNGEN:
        return video
    if not video_ist_stumm(video):
        return video
    lautstaerke = markensound_config()["lautstaerke"]
    cta_sound = markensound_aktiv("cta") if cta_bei is not None and not ist_cta_datei(video.name) else None
    try:
        vs, ss = video.stat(), sound.stat()
        cs = cta_sound.stat() if cta_sound is not None else None
    except OSError:
        return video
    cta_teil = f"{cta_sound}|{cs.st_mtime_ns}|{cs.st_size}|{cta_bei}" if cs is not None else ""
    schluessel = hashlib.sha256(
        f"{video}|{vs.st_mtime_ns}|{vs.st_size}|{sound}|{ss.st_mtime_ns}|{ss.st_size}|{lautstaerke}|{cta_teil}".encode()
    ).hexdigest()[:32]
    ordner = _mux_cache_ordner()
    ziel = ordner / f"{schluessel}.mp4"
    if ziel.is_file():
        return ziel
    with _mux_lock:
        if ziel.is_file():
            return ziel
        ordner.mkdir(parents=True, exist_ok=True)
        dauer = _dauer_s(video)
        if dauer <= 0:
            return video
        # CTA-Sound laeuft genau einmal (ein Ding beim Tippen auf Folgen),
        # danach Stille bis zum Ende. Der Markensound laeuft in Schleife und
        # wird zum Ende hin ausgeblendet.
        einmal = ist_cta_datei(video.name) and sound == markensound_aktiv("cta")
        if einmal:
            filter_ = f"volume={lautstaerke:.2f},apad"
            schleife = []
        else:
            ausblenden = min(1.2, dauer / 4)
            filter_ = (
                f"volume={lautstaerke:.2f},"
                f"afade=t=in:d=0.15,"
                f"afade=t=out:st={max(0.0, dauer - ausblenden):.2f}:d={ausblenden:.2f}"
            )
            schleife = ["-stream_loop", "-1"]
        tmp = ordner / f".{schluessel}.{os.getpid()}.mp4"
        if cta_sound is not None and 0 <= cta_bei < dauer:
            # Reel: Markensound in Schleife plus CTA-Sound genau beim Tippen.
            verzug = max(0, int(round((cta_bei - CTA_SOUND_ANSCHLAG_S) * 1000)))
            ton = [
                "-i", str(cta_sound),
                "-filter_complex",
                f"[1:a]{filter_}[m];[2:a]adelay={verzug}|{verzug},volume={lautstaerke:.2f},apad[c];"
                f"[m][c]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[a]",
                "-map", "0:v:0", "-map", "[a]",
            ]
        else:
            ton = ["-map", "0:v:0", "-map", "1:a:0", "-af", filter_]
        befehl = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(video),
            *schleife, "-i", str(sound),
            *ton,
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
            "-t", f"{dauer:.3f}",
            "-movflags", "+faststart", str(tmp),
        ]
        try:
            ergebnis = subprocess.run(befehl, capture_output=True, timeout=180)
        except (OSError, subprocess.SubprocessError):
            tmp.unlink(missing_ok=True)
            return video
        if ergebnis.returncode != 0 or not tmp.is_file():
            tmp.unlink(missing_ok=True)
            return video
        os.replace(tmp, ziel)
        _mux_cache_aufraeumen(ordner)
    return ziel


def _mux_cache_aufraeumen(ordner: Path) -> None:
    try:
        dateien = sorted(
            (p for p in ordner.iterdir() if p.is_file() and p.suffix == ".mp4" and not p.name.startswith(".")),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return
    for alt in dateien[MARKENSOUND_CACHE_MAX:]:
        alt.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Medien eines Beitrags fuer das Handy
# ---------------------------------------------------------------------------
def _medien_rang(name: str):
    n = name.lower()
    if n.startswith(("reel", "video")) or "final" in n:
        return (0, n)
    if n.startswith("cover"):
        return (2, n)
    return (1, n)


def _fertiger_job(kind: str, slug: str) -> tuple[Path, dict] | None:
    for job_dir, status in ws_mod._render_jobs_fuer_beitrag(kind, slug):
        if status.get("state") == "done" and isinstance(status.get("outputs"), list):
            return job_dir, status
    return None


def _datei_url(kind: str, slug: str, quelle: str, name: str) -> str:
    return url_for("workspace.mobil_datei", kind=kind, slug=slug, quelle=quelle, filename=name)


def beitrag_medien(kind: str, slug: str, post_dir: Path) -> dict:
    """Die Dateien, die man vom Handy speichert, je Slide genau eine.

    Reihenfolge der Quellen: festgelegtes Haupt-Asset, sonst je HTML-Slide das
    aktuelle Rendering neben der Quelle (Video vor Bild, animierte Slides sind
    Motion Graphics), sonst die Ausgabe des letzten fertigen Render-Auftrags.
    Beitraege ohne HTML-Quellen (Reels aus dem Videorenderer) liefern ihre
    Videos, ohne Video ihre Bilder."""
    mdir = ws_mod.media_dir(post_dir)
    meta = ws_mod.read_meta(post_dir / "meta.yaml")
    slides: list[dict] = []

    haupt = str(meta.get("haupt_asset") or "").strip()
    if haupt and "/" not in haupt and (mdir / haupt).is_file():
        typ = "video" if Path(haupt).suffix.lower() in VIDEO_ENDUNGEN else "bild"
        return {
            "slides": [{"name": haupt, "typ": typ, "url": _datei_url(kind, slug, "beitrag", haupt), "ratio": ""}],
            "fehlen": 0,
        }

    quellen = ws_mod._quell_vorschauen(post_dir)
    if quellen:
        job = _fertiger_job(kind, slug)
        job_outputs = set(job[1].get("outputs") or []) if job else set()
        job_zeit = 0.0
        if job:
            fertig = job[0] / "export.zip"
            job_zeit = (fertig if fertig.is_file() else job[0] / "status.json").stat().st_mtime
        fehlen = 0
        for index, q in enumerate(quellen, start=1):
            name = q["name"]
            stem = Path(name).stem
            try:
                quell_zeit = (mdir / name).stat().st_mtime
            except OSError:
                quell_zeit = 0
            eintrag = None
            for endung in (".mp4", ".png", ".jpg"):
                kandidat = mdir / f"{stem}{endung}"
                if kandidat.is_file() and kandidat.stat().st_mtime >= quell_zeit - AKTUELL_TOLERANZ_S:
                    eintrag = {
                        "name": kandidat.name,
                        "typ": "video" if endung == ".mp4" else "bild",
                        "url": _datei_url(kind, slug, "beitrag", kandidat.name),
                    }
                    break
            if eintrag is None and job and job_zeit >= quell_zeit:
                out_stem = f"{index:02d}_" + (re.sub(r"[^A-Za-z0-9_-]+", "-", stem).strip("-_") or "slide")[:80]
                mp4, png = f"{out_stem}.mp4", f"{out_stem}.png"
                # Animierte Slides als Video, statische als Bild: der Export
                # hat beides, das Bild ist fuer Karussells das Richtige.
                animiert = ws_mod._render_quellvariante(post_dir, name) != name or (mdir / f"{stem}.mp4").exists()
                wahl = mp4 if animiert and mp4 in job_outputs else png
                if wahl in job_outputs:
                    eintrag = {
                        "name": wahl,
                        "typ": "video" if wahl.endswith(".mp4") else "bild",
                        "url": _datei_url(kind, slug, job[0].name, wahl),
                    }
            if eintrag is None:
                fehlen += 1
                eintrag = {
                    "name": name,
                    "typ": "quelle",
                    "url": url_for("workspace.quellvorschau", kind=kind, slug=slug, filename=name),
                }
            eintrag["ratio"] = q["ratio"]
            slides.append(eintrag)
        return {"slides": slides, "fehlen": fehlen}

    if mdir.is_dir():
        dateien = [p for p in mdir.iterdir() if p.is_file() and not p.name.startswith((".", "_"))
                   and not _KEIN_MEDIUM_RE.search(p.name)]
        videos = sorted((p.name for p in dateien if p.suffix.lower() in VIDEO_ENDUNGEN), key=_medien_rang)
        bilder = sorted((p.name for p in dateien if p.suffix.lower() in BILD_ENDUNGEN), key=_medien_rang)
        # Reels: das Video plus Cover-Bilder (fuer das Titelbild im Feed).
        auswahl = videos + [b for b in bilder if b.lower().startswith("cover")] if videos else bilder
        for name in auswahl:
            slides.append({
                "name": name,
                "typ": "video" if Path(name).suffix.lower() in VIDEO_ENDUNGEN else "bild",
                "url": _datei_url(kind, slug, "beitrag", name),
                "ratio": "",
            })
    return {"slides": slides, "fehlen": 0}


@bp.route("/m/datei/<kind>/<slug>/<quelle>/<path:filename>")
def mobil_datei(kind, slug, quelle, filename):
    """Eine Beitragsdatei fuers Handy. quelle ist 'beitrag' (Medienordner)
    oder die ID eines fertigen Render-Auftrags. Videos bekommen den
    Markensound, ?download=1 liefert als Anhang mit sprechendem Namen."""
    if "/" in filename or "\\" in filename or filename.startswith("."):
        abort(404)
    base = ws_mod.base_dir_for(kind)
    post_dir = g.ws.safe_path(base.name, slug)
    if not post_dir.is_dir():
        abort(404)
    if quelle == "beitrag":
        mdir = ws_mod.media_dir(post_dir)
        pfad = mdir / filename
        if not pfad.is_file():
            pfad = post_dir / filename
    elif ws_mod.RENDER_JOB_ID_RE.fullmatch(quelle):
        treffer = [j for j in ws_mod._render_jobs_fuer_beitrag(kind, slug) if j[0].name == quelle]
        if not treffer or treffer[0][1].get("state") != "done" or filename not in (treffer[0][1].get("outputs") or []):
            abort(404)
        pfad = treffer[0][0] / "output" / filename
    else:
        abort(404)
    if not pfad.is_file() or pfad.is_symlink():
        abort(404)
    endung = pfad.suffix.lower()
    if endung not in VIDEO_ENDUNGEN | BILD_ENDUNGEN:
        abort(404)
    cta_bei = None
    if endung in VIDEO_ENDUNGEN and quelle == "beitrag":
        try:
            roh = ws_mod.read_meta(post_dir / "meta.yaml").get("cta_ton_bei")
            cta_bei = float(roh) if roh is not None else None
        except (TypeError, ValueError):
            cta_bei = None
    auslieferung = mit_markensound(pfad, cta_bei) if endung in VIDEO_ENDUNGEN else pfad
    download = request.args.get("download") == "1"
    kurz = re.sub(r"[^A-Za-z0-9_-]+", "-", slug).strip("-")[:60] or "beitrag"
    antwort = send_file(
        auslieferung,
        mimetype="video/mp4" if endung == ".mp4" else None,
        as_attachment=download,
        download_name=f"{kurz}_{Path(filename).stem}{endung}" if download else None,
        conditional=True,
    )
    antwort.headers["Cache-Control"] = "private, max-age=300"
    return antwort


# ---------------------------------------------------------------------------
# Status: Kalender-Eintraege je Beitrag
# ---------------------------------------------------------------------------
def _kalender_je_beitrag() -> dict:
    """(kind, slug) -> Liste der gespeicherten Eintraege, neueste zuerst."""
    je = {}
    for e in ws_mod.kalender_lesen()["eintraege"]:
        schluessel = (str(e.get("kind") or ""), str(e.get("slug") or ""))
        je.setdefault(schluessel, []).append(e)
    for liste in je.values():
        liste.sort(key=lambda e: (str(e.get("datum") or ""), str(e.get("uhrzeit") or "")), reverse=True)
    return je


def _status_fuer(post: dict, eintraege: list) -> dict:
    gepostet = [e for e in eintraege if e.get("status") == "gepostet"]
    geplant = [e for e in eintraege if e.get("status") == "geplant"]
    if gepostet:
        e = gepostet[0]
        return {"key": "gepostet", "text": "Gepostet", "datum": str(e.get("datum") or "")}
    if geplant:
        e = sorted(geplant, key=lambda x: (str(x.get("datum")), str(x.get("uhrzeit"))))[0]
        return {"key": "geplant", "text": "Geplant", "datum": str(e.get("datum") or ""),
                "uhrzeit": str(e.get("uhrzeit") or "")}
    if post["kind"] == "drafts":
        return {"key": "entwurf", "text": "Entwurf", "datum": ""}
    return {"key": "freigegeben", "text": "Freigegeben", "datum": ""}


def _post_karte(post: dict, eintraege: list) -> dict:
    vorschau = post.get("preview")
    vorschau_typ = ""
    vorschau_url = None
    if vorschau:
        vorschau_url = url_for("workspace.asset", kind=post["kind"], slug=post["slug"], filename=vorschau)
        vorschau_typ = "video" if Path(vorschau).suffix.lower() in VIDEO_ENDUNGEN else "bild"
    else:
        post_dir = g.ws.root / post["kind"] / post["slug"]
        quellen = ws_mod._slide_dateien(post_dir)
        if quellen:
            vorschau_url = url_for("workspace.quellvorschau", kind=post["kind"], slug=post["slug"], filename=quellen[0])
            vorschau_typ = "quelle"
    return {
        "kind": post["kind"],
        "slug": post["slug"],
        "thema": str(post.get("thema") or post["slug"]),
        "format": ws_mod.format_label(post["format"]) if post.get("format") else "",
        "format_key": str(post.get("format") or ""),
        "datum": post.get("datum") or "",
        "vorschau": vorschau_url,
        "vorschau_typ": vorschau_typ,
        "status": _status_fuer(post, eintraege),
    }


WOCHENTAGE = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")
WOCHENTAGE_KURZ = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")
# Wie lange gepostete Termine im Plan sichtbar bleiben (Rueckblick).
PLAN_RUECKBLICK_TAGE = 7
PLAN_LEISTE_MIN_TAGE = 14


def _heute() -> date:
    try:
        zone = ZoneInfo(ws_mod.allgemein_config()["zeitzone"])
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo(ws_mod.ZEITZONE_STANDARD)
    return datetime.now(zone).date()


def _tag_label(tag: date, heute: date) -> dict:
    rel = ""
    if tag == heute:
        rel = "Heute"
    elif tag == heute + timedelta(days=1):
        rel = "Morgen"
    return {
        "lang": f"{WOCHENTAGE[tag.weekday()]}, {tag:%d.%m.}",
        "kurz": WOCHENTAGE_KURZ[tag.weekday()],
        "zahl": tag.day,
        "rel": rel,
    }


def _trial_plan_karte(e: dict, heute: date, namen: dict) -> dict | None:
    """Plan-Zeile fuer einen eingeplanten Trial-Reel-Clip. Ein Tipp oeffnet die
    Trial-Ansicht der Version (Video speichern, Caption kopieren)."""
    pub = ws_mod._kalender_eintrag_public(e)
    if not pub["existiert"]:
        return None
    status = pub["status"]
    datum = pub["datum"]
    ueberfaellig = status == "geplant" and datum < heute.isoformat()
    if ueberfaellig:
        st = {"key": "ueberfaellig", "text": "Überfällig", "datum": ""}
    elif status == "geplant":
        st = {"key": "geplant", "text": "Geplant", "datum": ""}
    else:
        st = {"key": "gepostet", "text": "Gepostet", "datum": ""}
    return {
        "kind": "trials", "slug": pub["slug"], "thema": pub["titel"], "format": "Trial Reel", "format_key": "trial",
        "datum": datum, "vorschau": pub["vorschau_url"], "vorschau_typ": "video", "status": st,
        "reihe": "", "reihe_ende": False, "eintrag_id": pub["id"], "termin_datum": datum, "uhrzeit": pub["uhrzeit"],
        "kanal": pub["kanal"], "kanal_name": namen.get(pub["kanal"], ""), "notiz": pub["notiz"],
        "ueberfaellig": ueberfaellig,
    }


def plan_daten() -> dict:
    """Der Upload-Plan: gespeicherte Kalender-Eintraege mit Beitrag, offene
    (geplant) aus jeder Zeit, gepostete der letzten Tage. Ueberfaellige
    stehen oben, danach Tag fuer Tag. Jede Zeile ist eine Karte fuer das
    Vollbild, damit man direkt vom Termin aus speichern und abhaken kann."""
    heute = _heute()
    grenze = (heute - timedelta(days=PLAN_RUECKBLICK_TAGE)).isoformat()
    namen = ws_mod.kanal_namen()
    posts_cache: dict = {}
    zeilen = []
    for e in ws_mod.kalender_lesen()["eintraege"]:
        kind, slug = str(e.get("kind") or ""), str(e.get("slug") or "")
        status = str(e.get("status") or "geplant")
        datum = str(e.get("datum") or "")
        if status == "gepostet" and datum < grenze:
            continue
        if kind == ws_mod.KALENDER_TRIAL:
            karte = _trial_plan_karte(e, heute, namen)
            if karte:
                zeilen.append(karte)
            continue
        if kind not in ws_mod.KALENDER_KINDS or not slug or slug.startswith("."):
            continue
        if (kind, slug) not in posts_cache:
            base = ws_mod.base_dir_for(kind)
            posts_cache[(kind, slug)] = ws_mod.load_post(base, kind, slug) if (base / slug).is_dir() else None
        post = posts_cache[(kind, slug)]
        if post is None:
            continue
        karte = _post_karte(post, [e])
        ueberfaellig = status == "geplant" and datum < heute.isoformat()
        if ueberfaellig:
            karte["status"] = {"key": "ueberfaellig", "text": "Überfällig", "datum": ""}
        elif status == "geplant":
            karte["status"] = {"key": "geplant", "text": "Geplant", "datum": ""}
        else:
            karte["status"] = {"key": "gepostet", "text": "Gepostet", "datum": ""}
        meta = ws_mod.read_meta(g.ws.root / kind / slug / "meta.yaml")
        karte.update({
            "reihe": str(meta.get("reihe") or "").strip(),
            "reihe_ende": False,
            "eintrag_id": str(e.get("id") or ""),
            "termin_datum": datum,
            "uhrzeit": str(e.get("uhrzeit") or ""),
            "kanal": str(e.get("kanal") or ""),
            "kanal_name": namen.get(str(e.get("kanal") or ""), ""),
            "notiz": str(e.get("notiz") or ""),
            "ueberfaellig": ueberfaellig,
        })
        zeilen.append(karte)
    zeilen.sort(key=lambda z: (not z["ueberfaellig"], z["termin_datum"], z["uhrzeit"]))

    hinweise = reihen_hinweise(zeilen, heute)

    tage = []
    offen = [i for i, z in enumerate(zeilen) if z["ueberfaellig"]]
    if offen:
        tage.append({"id": "offen", "titel": "Noch offen", "rel": "", "indizes": offen})
    for i, z in enumerate(zeilen):
        if z["ueberfaellig"]:
            continue
        if not tage or tage[-1]["id"] != z["termin_datum"]:
            try:
                tag = date.fromisoformat(z["termin_datum"])
            except ValueError:
                continue
            label = _tag_label(tag, heute)
            tage.append({"id": z["termin_datum"], "titel": label["lang"], "rel": label["rel"], "indizes": []})
        tage[-1]["indizes"].append(i)

    # Tagesleiste: ab heute bis zum letzten Termin, mindestens zwei Wochen.
    anzahl = {}
    for z in zeilen:
        if not z["ueberfaellig"]:
            anzahl[z["termin_datum"]] = anzahl.get(z["termin_datum"], 0) + 1
    letzter = max([heute] + [date.fromisoformat(d) for d in anzahl if d >= heute.isoformat()])
    tage_leiste = []
    tag = heute
    while tag <= max(letzter, heute + timedelta(days=PLAN_LEISTE_MIN_TAGE - 1)):
        label = _tag_label(tag, heute)
        tage_leiste.append({
            "id": tag.isoformat(), "kurz": label["kurz"], "zahl": label["zahl"],
            "heute": tag == heute, "anzahl": anzahl.get(tag.isoformat(), 0),
            "montag": tag.weekday() == 0,
        })
        tag += timedelta(days=1)
    return {
        "karten": zeilen,
        "tage": tage,
        "leiste": tage_leiste,
        "offen": sum(1 for z in zeilen if z["status"]["key"] in ("geplant", "ueberfaellig")),
        "hinweise": hinweise,
    }


# Wie viele Tage vor und nach der letzten Folge einer Reihe der Hinweis
# im Plan steht (Meldung, wenn der letzte Beitrag einer Reihe live geht).
REIHE_HINWEIS_VORLAUF_TAGE = 7
REIHE_HINWEIS_NACHLAUF_TAGE = 7


def reihen_hinweise(zeilen: list, heute: date) -> list:
    """Markiert je Reihe die letzte eingeplante Folge (reihe_ende) und baut
    Hinweise fuer den Plan: kurz bevor sie live geht und kurz danach, wenn
    die Reihe damit leer ist. Eine Reihe gilt als leer, wenn weder ein
    spaeterer Termin noch ein ungeplanter Entwurf derselben Reihe existiert."""
    geplant_slugs = {(z["kind"], z["slug"]) for z in zeilen if z["status"]["key"] != "gepostet"}
    vorrat: dict[str, int] = {}
    for post in ws_mod.ohne_struktur_templates(ws_mod.load_posts(g.ws.drafts_dir, "drafts")):
        if ("drafts", post["slug"]) in geplant_slugs:
            continue
        reihe = str(ws_mod.read_meta(g.ws.drafts_dir / post["slug"] / "meta.yaml").get("reihe") or "").strip()
        if reihe:
            vorrat[reihe] = vorrat.get(reihe, 0) + 1

    je_reihe: dict[str, list] = {}
    for z in zeilen:
        if z["reihe"]:
            je_reihe.setdefault(z["reihe"], []).append(z)
    hinweise = []
    for reihe, liste in je_reihe.items():
        if vorrat.get(reihe):
            continue  # es liegt noch ein fertiger Entwurf bereit
        letzte = max(liste, key=lambda z: (z["termin_datum"], z["uhrzeit"]))
        letzte["reihe_ende"] = True
        try:
            tag = date.fromisoformat(letzte["termin_datum"])
        except ValueError:
            continue
        abstand = (tag - heute).days
        gepostet = letzte["status"]["key"] == "gepostet"
        if gepostet and abstand >= -REIHE_HINWEIS_NACHLAUF_TAGE:
            text = f"Die letzte Folge von „{reihe}“ ist live. Für die Reihe ist nichts mehr eingeplant."
            art = "leer"
        elif not gepostet and abstand <= REIHE_HINWEIS_VORLAUF_TAGE:
            wann = "heute" if abstand == 0 else ("morgen" if abstand == 1 else (
                "überfällig" if abstand < 0 else f"am {_tag_label(tag, heute)['lang']}"))
            text = f"Die letzte Folge von „{reihe}“ geht {wann} live. Danach ist die Reihe leer, neue Folgen einplanen."
            if abstand < 0:
                text = f"Die letzte Folge von „{reihe}“ ist überfällig. Danach ist die Reihe leer."
            art = "bald"
        else:
            continue
        hinweise.append({"reihe": reihe, "text": text, "art": art, "datum": letzte["termin_datum"],
                         "ziel": letzte["termin_datum"] if not letzte["ueberfaellig"] else "offen"})
    hinweise.sort(key=lambda h: h["datum"])
    return hinweise


@bp.route("/m")
@bp.route("/m/<reiter>")
def mobil(reiter="plan"):
    if reiter not in ("plan", "uebersicht", "entwuerfe"):
        abort(404)
    drafts = ws_mod.ohne_struktur_templates(ws_mod.load_posts(g.ws.drafts_dir, "drafts"))
    plan = {"tage": [], "leiste": [], "offen": 0, "hinweise": []}
    if reiter == "plan":
        plan = plan_daten()
        karten = plan["karten"]
    else:
        je = _kalender_je_beitrag()
        if reiter == "entwuerfe":
            posts = drafts
        else:
            archive = ws_mod.ohne_struktur_templates(ws_mod.load_posts(g.ws.archive_dir, "archive"))
            posts = sorted(drafts + archive, key=lambda p: (p["datum"], p["slug"]), reverse=True)
        karten = [_post_karte(p, je.get((p["kind"], p["slug"]), [])) for p in posts]
    kanal_liste = ws_mod.kanal_liste()
    return render_template(
        "mobil.html",
        reiter=reiter,
        karten=karten,
        plan=plan,
        anzahl_entwuerfe=len(drafts),
        kanal_liste=kanal_liste,
        zeitzone=ws_mod.allgemein_config()["zeitzone"],
        start_post=request.args.get("post", ""),
        markensound=_markensound_oeffentlich(),
        title="Handy",
    )


def _trial_eintrag(slug):
    """Der Kalender-Eintrag eines eingeplanten Trial-Reel-Clips (?eintrag=<id>)."""
    eid = str(request.args.get("eintrag") or "")
    for e in ws_mod.kalender_lesen()["eintraege"]:
        if str(e.get("id")) == eid and e.get("kind") == ws_mod.KALENDER_TRIAL and e.get("slug") == slug:
            return e
    abort(404)


def _trial_beitrag(slug):
    """Viewer-Daten fuer einen eingeplanten Trial-Reel-Clip, im selben Format
    wie ein normaler Beitrag: ein Video zum Sichern, Caption der Version."""
    e = _trial_eintrag(slug)
    pub = ws_mod._kalender_eintrag_public(e)
    if not pub["existiert"]:
        abort(404)
    meta = ws_mod.read_meta(g.ws.root / "trials" / slug / "meta.yaml")
    versionen = meta.get("versionen") if isinstance(meta.get("versionen"), list) else []
    nr = int(pub["version"])
    v = versionen[nr - 1] if len(versionen) >= nr and isinstance(versionen[nr - 1], dict) else {}
    caption = str(v.get("caption") or meta.get("caption") or "").strip()
    url = url_for("workspace.trial_reel_datei", slug=slug, nr=nr, name=pub["clip"])
    return jsonify({
        "ok": True, "kind": "trials", "slug": slug,
        "thema": pub["titel"], "format": "Trial Reel", "caption": caption,
        "slides": [{"name": pub["clip"], "typ": "video", "url": url, "ratio": ""}],
        "fehlen": 0,
        "status": _status_fuer({"kind": "trials"}, [e]),
        "eintraege": [pub],
        "render": {"state": "idle"}, "render_url": "", "render_status_url": "",
        "detail_url": pub["detail_url"],
    })


@bp.route("/api/mobil/beitrag/<kind>/<slug>")
def mobil_beitrag(kind, slug):
    if kind == ws_mod.KALENDER_TRIAL:
        return _trial_beitrag(slug)
    base = ws_mod.base_dir_for(kind)
    post_dir = g.ws.safe_path(base.name, slug)
    if not post_dir.is_dir():
        abort(404)
    post = ws_mod.load_post(base, kind, slug)
    medien = beitrag_medien(kind, slug, post_dir)
    eintraege = _kalender_je_beitrag().get((kind, slug), [])
    render = {"state": "idle"}
    jobs = ws_mod._render_jobs_fuer_beitrag(kind, slug)
    if jobs:
        render = ws_mod._render_job_oeffentlich(jobs[0][1], kind, slug)
    return jsonify({
        "ok": True,
        "kind": kind,
        "slug": slug,
        "thema": post["thema"] if post else slug,
        "format": ws_mod.format_label(post["format"]) if post and post.get("format") else "",
        "caption": beitrags_caption(post_dir),
        "slides": medien["slides"],
        "fehlen": medien["fehlen"],
        "status": _status_fuer({"kind": kind}, eintraege),
        "eintraege": [ws_mod._kalender_eintrag_public(e) for e in eintraege],
        "render": render,
        "render_url": url_for("workspace.beitrag_render_start", kind=kind, slug=slug),
        "render_status_url": url_for("workspace.beitrag_render_status", kind=kind, slug=slug),
        "detail_url": url_for("workspace.detail", kind=kind, slug=slug),
    })


def _freigeben_intern(slug: str) -> tuple[str | None, str]:
    """Ruft die Freigabe der Detailseite auf (gleiche Archivierungslogik) und
    haengt Kalender-Eintraege des Entwurfs auf den neuen Archiv-Ordner um."""
    antwort = ws_mod.beitrag_freigeben("drafts", slug)
    code = 200
    if isinstance(antwort, tuple):
        antwort, code = antwort[0], antwort[1]
    daten = antwort.get_json(silent=True) or {}
    if code >= 400 or not daten.get("ok"):
        return None, str(daten.get("fehler") or "Freigabe fehlgeschlagen.")
    neu = daten["archive_slug"]
    with ws_mod._kalender_lock:
        alle = ws_mod.kalender_lesen()
        geaendert = False
        for e in alle["eintraege"]:
            if e.get("kind") == "drafts" and e.get("slug") == slug:
                e["kind"], e["slug"] = "archive", neu
                geaendert = True
        if geaendert:
            ws_mod.kalender_schreiben(alle)
    return neu, ""


def _trial_status(slug, aktion, body):
    """Trial-Termin vom Handy: 'gepostet' hakt genau diesen Termin ab,
    'planen' verschiebt ihn. Ein Trial Reel wird dabei nicht freigegeben."""
    try:
        datum = ws_mod._kalender_datum(body.get("datum"))
        uhrzeit = ws_mod._kalender_uhrzeit(body.get("uhrzeit"))
        kanal = ws_mod._kalender_kanal(body.get("kanal"))
    except ws_mod.KalenderFehler as exc:
        return jsonify({"ok": False, "fehler": str(exc)}), 400
    eid = str(request.args.get("eintrag") or "")
    status = "gepostet" if aktion == "gepostet" else "geplant"
    with ws_mod._kalender_lock:
        alle = ws_mod.kalender_lesen()
        treffer = next((e for e in alle["eintraege"] if str(e.get("id")) == eid
                        and e.get("kind") == ws_mod.KALENDER_TRIAL and e.get("slug") == slug), None)
        if treffer is None:
            abort(404)
        treffer.update({"datum": datum, "uhrzeit": uhrzeit, "kanal": kanal, "status": status})
        ws_mod.kalender_schreiben(alle)
    ws_mod.journal_append(
        "kalender_gepostet" if status == "gepostet" else "kalender_geplant",
        {"slug": slug, "kind": "trials", "kanal": kanal, "quelle": "handy"}, True,
        f"{'Als gepostet markiert' if status == 'gepostet' else 'Geplant'}: {datum} um {uhrzeit} Uhr", 0,
    )
    return jsonify({"ok": True, "kind": "trials", "slug": slug, "freigegeben": False,
                    "eintrag": ws_mod._kalender_eintrag_public(treffer),
                    "status": _status_fuer({"kind": "trials"}, [treffer])})


@bp.route("/api/mobil/beitrag/<kind>/<slug>/status", methods=["POST"])
def mobil_status(kind, slug):
    """Eine Handy-Aktion: 'planen' (freigeben + Kalender-Eintrag geplant) oder
    'gepostet' (freigeben + Eintrag gepostet; ein vorhandener geplanter
    Eintrag desselben Kanals wird dabei umgestellt statt verdoppelt)."""
    body = request.get_json(silent=True) or {}
    aktion = str(body.get("aktion") or "")
    if aktion not in ("planen", "gepostet"):
        return jsonify({"ok": False, "fehler": "Unbekannte Aktion."}), 400
    if kind == ws_mod.KALENDER_TRIAL:
        return _trial_status(slug, aktion, body)
    if kind not in ws_mod.KALENDER_KINDS:
        abort(404)
    try:
        datum = ws_mod._kalender_datum(body.get("datum"))
        uhrzeit = ws_mod._kalender_uhrzeit(body.get("uhrzeit"))
        kanal = ws_mod._kalender_kanal(body.get("kanal"))
        ws_mod._kalender_beitrag_dir(kind, slug)
    except ws_mod.KalenderFehler as exc:
        return jsonify({"ok": False, "fehler": str(exc)}), 400
    freigegeben = False
    if kind == "drafts":
        neu, fehler = _freigeben_intern(slug)
        if neu is None:
            return jsonify({"ok": False, "fehler": fehler}), 409
        kind, slug, freigegeben = "archive", neu, True

    status = "gepostet" if aktion == "gepostet" else "geplant"
    start = time.time()
    with ws_mod._kalender_lock:
        alle = ws_mod.kalender_lesen()
        vorhanden = None
        if aktion == "gepostet":
            for e in alle["eintraege"]:
                if (e.get("kind"), e.get("slug"), e.get("kanal"), e.get("status")) == (kind, slug, kanal, "geplant"):
                    vorhanden = e
                    break
        if vorhanden is not None:
            vorhanden.update({"datum": datum, "uhrzeit": uhrzeit, "status": status})
            eintrag = vorhanden
        else:
            try:
                eintrag = ws_mod._kalender_neuer_eintrag({
                    "kind": kind, "slug": slug, "datum": datum, "uhrzeit": uhrzeit,
                    "kanal": kanal, "status": status,
                })
            except ws_mod.KalenderFehler as exc:
                return jsonify({"ok": False, "fehler": str(exc)}), 400
            vergeben = {str(e.get("id")) for e in alle["eintraege"]}
            while eintrag["id"] in vergeben:
                eintrag["id"] = secrets.token_hex(4)
            alle["eintraege"].append(eintrag)
        try:
            ws_mod.kalender_schreiben(alle)
        except OSError as exc:
            return jsonify({"ok": False, "fehler": f"Kalender konnte nicht geschrieben werden: {exc}"}), 500
    ws_mod.journal_append(
        "kalender_gepostet" if status == "gepostet" else "kalender_geplant",
        {"slug": slug, "kind": kind, "kanal": kanal, "quelle": "handy"},
        True,
        f"{'Als gepostet markiert' if status == 'gepostet' else 'Geplant'}: {datum} um {uhrzeit} Uhr",
        int((time.time() - start) * 1000),
    )
    return jsonify({
        "ok": True,
        "kind": kind,
        "slug": slug,
        "freigegeben": freigegeben,
        "eintrag": ws_mod._kalender_eintrag_public(eintrag),
    })
