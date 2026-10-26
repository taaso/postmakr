"""Trial Reels: Testvideos in drei Versionen (Opt-in-Modul ``trial_reels``).

Ein Trial Reel ist ein Ordner unter ``trials/<slug>/`` mit einer meta.yaml
(Titel, Laenge, Status, Caption, O-Ton, Skript, je Version Hook und
Aufnahmeplan) und drei Unterordnern ``v1``, ``v2``, ``v3`` fuer das
Rohmaterial, das du selbst aufnimmst und hochlaedst. Die Ordner sagen, welcher
Clip zu welcher Version gehoert, mehr Zuordnung braucht es nicht.

Geschrieben wird die meta.yaml von Claude (MCP oder Terminal); das Dashboard
laedt Clips hoch, loescht sie, setzt den Status und speichert die Texte.
"""
from __future__ import annotations

import os
import re
import secrets
from datetime import date
from pathlib import Path

import yaml
from flask import abort, g, jsonify, render_template, request, send_file

from .workspace import bp, read_meta

VERSIONEN = (1, 2, 3)
LAENGEN = (5, 15, 30, 60)
# Reihenfolge = Reihenfolge der Pillen auf der Uebersicht.
STATUS = {
    "entwurf": "In Arbeit",
    "bereit": "Bereit",
    "im_test": "Im Test",
    "gewinner": "Gewinner",
    "raus": "Raus",
}
TEXT_FELDER = ("caption", "oton", "skript", "aufnahmeplan", "idee", "kategorie")
# Jede Version ist ein eigenes Video und hat deshalb eigene Texte.
# Die gleichnamigen Felder oben auf Reel-Ebene sind nur noch
# Rueckfall fuer aeltere meta.yaml.
VERSION_FELDER = ("hook", "caption", "oton", "skript")
CLIP_ENDUNGEN = {".mp4", ".mov", ".m4v", ".webm", ".jpg", ".jpeg", ".png", ".heic", ".webp"}
VIDEO_ENDUNGEN = {".mp4", ".mov", ".m4v", ".webm"}
# Reels werden in der Uebersicht nach Themen gruppiert (z. B. Produkt,
# 5 Sekunden ...). Ohne Feld landen sie in dieser Gruppe, ganz unten.
OHNE_KATEGORIE = "Ohne Kategorie"
# Ein Handyclip in 4K kommt schnell auf ein paar hundert MB.
CLIP_MAX_BYTES = 4 * 1024 * 1024 * 1024
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,80}$")


def _modul_pruefen() -> None:
    g.ws.require_module("trial_reels")


def _ordner(slug: str) -> Path:
    if not _SLUG_RE.match(slug or ""):
        abort(404)
    ordner = g.ws.safe_path("trials", slug)
    if not (ordner / "meta.yaml").is_file():
        abort(404)
    return ordner


def _version(nr) -> int:
    try:
        nr = int(nr)
    except (TypeError, ValueError):
        abort(404)
    if nr not in VERSIONEN:
        abort(404)
    return nr


def _clips(ordner: Path, nr: int) -> list[dict]:
    vordner = ordner / f"v{nr}"
    if not vordner.is_dir():
        return []
    clips = []
    for datei in sorted(vordner.iterdir(), key=lambda p: p.name):
        if datei.name.startswith(".") or not datei.is_file():
            continue
        if datei.suffix.lower() not in CLIP_ENDUNGEN:
            continue
        clips.append({
            "name": datei.name,
            "video": datei.suffix.lower() in VIDEO_ENDUNGEN,
            "mb": round(datei.stat().st_size / (1024 * 1024), 1),
            "stand": int(datei.stat().st_mtime),
        })
    return clips


def _text(wert) -> str:
    return wert.strip() if isinstance(wert, str) else ""


def _laden(ordner: Path) -> dict:
    meta = read_meta(ordner / "meta.yaml")
    try:
        laenge = int(meta.get("laenge") or 0)
    except (TypeError, ValueError):
        laenge = 0
    status = meta.get("status") if meta.get("status") in STATUS else "entwurf"
    roh_versionen = meta.get("versionen") if isinstance(meta.get("versionen"), list) else []
    versionen = []
    for nr in VERSIONEN:
        roh = roh_versionen[nr - 1] if len(roh_versionen) >= nr and isinstance(roh_versionen[nr - 1], dict) else {}
        aufnahmen = []
        for a in roh.get("aufnahmen") or []:
            if isinstance(a, dict) and (_text(a.get("titel")) or _text(a.get("anweisung"))):
                aufnahmen.append({"titel": _text(a.get("titel")), "anweisung": _text(a.get("anweisung"))})
            elif isinstance(a, str) and a.strip():
                aufnahmen.append({"titel": "", "anweisung": a.strip()})
        versionen.append({
            "nr": nr,
            "name": _text(roh.get("name")) or f"Version {nr}",
            "hook": _text(roh.get("hook")),
            **{feld: _text(roh.get(feld)) or _text(meta.get(feld)) for feld in VERSION_FELDER if feld != "hook"},
            "notiz": _text(roh.get("notiz")),
            "aufnahmen": aufnahmen,
            "clips": _clips(ordner, nr),
        })
    return {
        "slug": ordner.name,
        "titel": _text(meta.get("titel")) or ordner.name,
        "laenge": laenge,
        "status": status,
        "status_label": STATUS[status],
        "datum": str(meta.get("datum") or ""),
        "format": _text(meta.get("format")),
        **{feld: _text(meta.get(feld)) for feld in TEXT_FELDER},
        "kategorie": _text(meta.get("kategorie")) or OHNE_KATEGORIE,
        "versionen": versionen,
        "mit_material": sum(1 for v in versionen if v["clips"]),
    }


def _meta_aktualisieren(ordner: Path, felder: dict, version_felder: dict | None = None) -> None:
    pfad = ordner / "meta.yaml"
    meta = read_meta(pfad)
    meta.update(felder)
    if version_felder:
        versionen = meta.get("versionen") if isinstance(meta.get("versionen"), list) else []
        while len(versionen) < len(VERSIONEN):
            versionen.append({})
        for (nr, feld), wert in version_felder.items():
            if not isinstance(versionen[nr - 1], dict):
                versionen[nr - 1] = {}
            versionen[nr - 1][feld] = wert
        meta["versionen"] = versionen
    tmp = pfad.with_name(f".meta.{secrets.token_hex(4)}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        yaml.safe_dump(meta, fh, allow_unicode=True, sort_keys=False, width=100)
    os.replace(tmp, pfad)


def nach_kategorie(reels: list[dict]) -> list[dict]:
    """[{name, reels}] alphabetisch, Ohne Kategorie zuletzt."""
    gruppen: dict[str, list] = {}
    for r in reels:
        gruppen.setdefault(r["kategorie"], []).append(r)
    namen = sorted(gruppen, key=lambda n: (n == OHNE_KATEGORIE, n.lower()))
    return [{"name": n, "reels": gruppen[n]} for n in namen]


def alle_trial_reels() -> list[dict]:
    wurzel = g.ws.trials_dir
    if not wurzel.is_dir():
        return []
    reels = [
        _laden(ordner)
        for ordner in wurzel.iterdir()
        if ordner.is_dir() and _SLUG_RE.match(ordner.name) and (ordner / "meta.yaml").is_file()
    ]
    # Neueste zuerst, gleiche Tage nach Slug.
    reels.sort(key=lambda r: (r["datum"], r["slug"]), reverse=True)
    return reels


# ---------------------------------------------------------------------------
# Seiten
# ---------------------------------------------------------------------------
@bp.route("/trial-reels")
def trial_reels():
    _modul_pruefen()
    reels = alle_trial_reels()
    tab = request.args.get("tab") or "alle"
    gruppen = {"alle": reels}
    for key in STATUS:
        gruppen[key] = [r for r in reels if r["status"] == key]
    if tab not in gruppen:
        tab = "alle"
    return render_template(
        "trial_reels.html",
        reels=gruppen[tab],
        kategorien=nach_kategorie(gruppen[tab]),
        anzahl={k: len(v) for k, v in gruppen.items()},
        status_namen=STATUS,
        tab=tab,
        title="Trial Reels",
    )


@bp.route("/trial-reels/<slug>")
def trial_reel(slug):
    _modul_pruefen()
    reel = _laden(_ordner(slug))
    return render_template(
        "trial_reel.html",
        reel=reel,
        status_namen=STATUS,
        laengen=LAENGEN,
        alle_kategorien=[k["name"] for k in nach_kategorie(alle_trial_reels()) if k["name"] != OHNE_KATEGORIE],
        title=reel["titel"],
    )


# ---------------------------------------------------------------------------
# Rohmaterial
# ---------------------------------------------------------------------------
def _sicherer_name(name: str, vordner: Path) -> str:
    stamm = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).stem).strip("._-")[:60] or "clip"
    endung = Path(name).suffix.lower()
    kandidat = f"{stamm}{endung}"
    zaehler = 2
    while (vordner / kandidat).exists():
        kandidat = f"{stamm}_{zaehler}{endung}"
        zaehler += 1
    return kandidat


@bp.route("/trial-reels/<slug>/v<nr>/hochladen", methods=["POST"])
def trial_reel_hochladen(slug, nr):
    _modul_pruefen()
    ordner = _ordner(slug)
    nr = _version(nr)
    dateien = [d for d in request.files.getlist("dateien") if d and d.filename]
    if not dateien:
        return jsonify({"ok": False, "error": "Bitte mindestens eine Datei wählen."}), 400
    for d in dateien:
        if Path(d.filename).suffix.lower() not in CLIP_ENDUNGEN:
            return jsonify({"ok": False, "error": f"{Path(d.filename).name}: erlaubt sind Videos (MP4, MOV, WebM) und Fotos."}), 400
    vordner = ordner / f"v{nr}"
    vordner.mkdir(parents=True, exist_ok=True)
    try:
        for d in dateien:
            ziel = vordner / _sicherer_name(d.filename, vordner)
            tmp = vordner / f".{ziel.name}.{secrets.token_hex(4)}.tmp"
            geschrieben = 0
            with open(tmp, "wb") as fh:
                while True:
                    stueck = d.stream.read(1024 * 1024)
                    if not stueck:
                        break
                    geschrieben += len(stueck)
                    if geschrieben > CLIP_MAX_BYTES:
                        fh.close()
                        tmp.unlink(missing_ok=True)
                        return jsonify({"ok": False, "error": f"{Path(d.filename).name} ist größer als 4 GB."}), 413
                    fh.write(stueck)
            os.replace(tmp, ziel)
    except OSError as exc:
        return jsonify({"ok": False, "error": f"Schreibfehler: {exc}"}), 500
    return jsonify({"ok": True, "clips": _clips(ordner, nr)})


@bp.route("/trial-reels/<slug>/v<nr>/loeschen", methods=["POST"])
def trial_reel_clip_loeschen(slug, nr):
    _modul_pruefen()
    ordner = _ordner(slug)
    nr = _version(nr)
    name = (request.form.get("name") or "").strip()
    if not name or "/" in name or name.startswith("."):
        abort(404)
    ziel = g.ws.safe_path("trials", slug, f"v{nr}", name)
    if ziel.is_file():
        ziel.unlink()
    return jsonify({"ok": True, "clips": _clips(ordner, nr)})


@bp.route("/trial-reels/<slug>/v<nr>/datei/<name>")
def trial_reel_datei(slug, nr, name):
    _modul_pruefen()
    _ordner(slug)
    nr = _version(nr)
    if name.startswith(".") or Path(name).suffix.lower() not in CLIP_ENDUNGEN:
        abort(404)
    ziel = g.ws.safe_path("trials", slug, f"v{nr}", name)
    if not ziel.is_file():
        abort(404)
    # Mit ?stand=<mtime> in der URL ist die Datei unveraenderlich: der Browser
    # darf sie behalten, statt bei jedem Seitenwechsel 30 MB neu zu laden.
    if request.args.get("stand"):
        antwort = send_file(ziel, conditional=True, max_age=31536000)
        antwort.headers["Cache-Control"] = "private, max-age=31536000, immutable"
        return antwort
    return send_file(ziel, conditional=True)


# ---------------------------------------------------------------------------
# Texte und Status
# ---------------------------------------------------------------------------
@bp.route("/trial-reels/<slug>/speichern", methods=["POST"])
def trial_reel_speichern(slug):
    _modul_pruefen()
    ordner = _ordner(slug)
    felder = {}
    for feld in TEXT_FELDER:
        if feld in request.form:
            felder[feld] = request.form.get(feld, "")[:20000]
    version_felder = {}
    for schluessel, wert in request.form.items():
        treffer = re.fullmatch(r"v([1-3])\.(\w+)", schluessel)
        if treffer and treffer.group(2) in VERSION_FELDER:
            version_felder[(int(treffer.group(1)), treffer.group(2))] = wert[:20000]
    if "status" in request.form:
        status = request.form.get("status")
        if status not in STATUS:
            return jsonify({"ok": False, "error": "Unbekannter Status."}), 400
        felder["status"] = status
        if status == "im_test":
            felder.setdefault("gepostet_am", date.today().isoformat())
    if "laenge" in request.form:
        try:
            laenge = int(request.form.get("laenge") or 0)
        except ValueError:
            laenge = 0
        if laenge not in LAENGEN:
            return jsonify({"ok": False, "error": "Länge muss 5, 15, 30 oder 60 Sekunden sein."}), 400
        felder["laenge"] = laenge
    if not felder and not version_felder:
        return jsonify({"ok": False, "error": "Nichts zu speichern."}), 400
    try:
        _meta_aktualisieren(ordner, felder, version_felder)
    except OSError as exc:
        return jsonify({"ok": False, "error": f"Schreibfehler: {exc}"}), 500
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Handy-Ansicht: Liste und ein Reel mit Aufnahme direkt im Browser
# (Teleprompter mit dem O-Ton der Version)
# ---------------------------------------------------------------------------
@bp.route("/m/trial")
@bp.route("/m/trial/<slug>")
def mobil_trial(slug=None):
    _modul_pruefen()
    from .workspace import load_posts, ohne_struktur_templates

    anzahl_entwuerfe = len(ohne_struktur_templates(load_posts(g.ws.drafts_dir, "drafts")))
    reel = _laden(_ordner(slug)) if slug else None
    return render_template(
        "mobil_trial.html",
        reels=[] if reel else alle_trial_reels(),
        kategorien=[] if reel else nach_kategorie(alle_trial_reels()),
        reel=reel,
        status_namen=STATUS,
        anzahl_entwuerfe=anzahl_entwuerfe,
        version_start=_version(request.args.get("v") or 1) if reel else 1,
    )
